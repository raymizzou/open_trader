from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
import fcntl
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Callable
from zoneinfo import ZoneInfo

from .account_sync_state import (
    BrokerAccountCandidate,
    REQUIRED_BROKERS,
    accept_candidate,
    accepted_portfolio_rows,
    load_account_sync_state,
    load_latest_statement_candidate,
    record_source_failure,
    with_dashboard_projection,
    write_json_atomic,
    write_portfolio_atomic,
)
from .dashboard import DashboardConfig
from .dashboard_quotes import DashboardQuoteService, load_published_quotes
from .futu_account import FutuAccountClient, build_futu_account_candidate
from .futu_universe import build_account_quote_universe
from .fx import DEFAULT_RATES_TO_HKD
from .holding_snapshot_import import (
    SUPPORTED_BROKERS,
    load_staged_holding_snapshot,
)
from .models import AssetClass, CashBalance, Market, Position
from .statement_import import (
    load_staged_statement_candidate,
    load_staged_statement_metadata,
)
from .tiger_account import (
    TigerAccountClient,
    build_tiger_account_candidate,
    load_tiger_account_config,
)


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def default_portfolio_path(data_dir: Path) -> Path:
    return data_dir / "latest" / "portfolio.csv"


@dataclass(frozen=True)
class AccountSyncWorkerConfig:
    data_dir: Path
    reports_dir: Path
    portfolio_path: Path
    futu_host: str
    futu_port: int
    tiger_config_dir: Path
    tiger_account: str | None
    account_interval_seconds: float = 60.0
    quote_interval_seconds: float = 5.0


class AccountSyncWorker:
    def __init__(
        self,
        config: AccountSyncWorkerConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
        now_text: Callable[[], str] | None = None,
    ) -> None:
        self.config = config
        self.clock = clock
        self.now_text = now_text or _now_text
        self._last_account_attempt: float | None = None
        self._last_quote_attempt: float | None = None
        self._quote_service: DashboardQuoteService | None = None
        self.account_loop: dict[str, object] = {}
        self.quote_loop: dict[str, object] = {}
        self._started_at = self.now_text()
        self._process_metadata = {
            "pid": os.getpid(),
            "started_at": self._started_at,
            "working_directory": str(Path.cwd()),
            "git_sha": _git_sha(),
            "code_root": str(Path(__file__).resolve().parent.parent),
        }

    def account_due(self, now: float) -> bool:
        return _is_due(
            self._last_account_attempt, now, self.config.account_interval_seconds
        )

    def quote_due(self, now: float) -> bool:
        return _is_due(
            self._last_quote_attempt, now, self.config.quote_interval_seconds
        )

    def sync_accounts_once(self) -> dict[str, object]:
        now = self.clock()
        if not self.account_due(now):
            return {"status": "skipped", "brokers": {}}
        self._last_account_attempt = now
        attempted_at = self.now_text()
        state_path = self.config.data_dir / "latest" / "account_sync_state.json"
        state = load_account_sync_state(state_path)
        results: dict[str, object] = {}
        for broker in REQUIRED_BROKERS:
            try:
                (
                    candidate,
                    statement_generation,
                    holding_generation,
                    preserve_cash,
                ) = self._candidate_for(
                    broker, attempted_at
                )
                self._write_diagnostic_candidate(attempted_at, candidate)
                next_state = accept_candidate(
                    state,
                    candidate,
                    attempted_at=attempted_at,
                    statement_generation=statement_generation,
                    holding_generation=holding_generation,
                    preserve_cash=preserve_cash,
                )
                portfolio_rows = accepted_portfolio_rows(next_state)
            except Exception as exc:
                state = record_source_failure(
                    state,
                    broker,
                    attempted_at=attempted_at,
                    message=str(exc),
                    sensitive_roots=(
                        self.config.data_dir,
                        self.config.reports_dir,
                        self.config.tiger_config_dir,
                    ),
                )
                write_json_atomic(state_path, state)
                results[broker] = {"status": "failed", "message": state["brokers"][broker]["message"]}
                continue
            try:
                write_portfolio_atomic(self.config.portfolio_path, portfolio_rows)
            except Exception:
                results[broker] = {"status": "publication_failed"}
                return {
                    "status": "publication_failed",
                    "blocker": f"portfolio_publish_failed: {broker}",
                    "brokers": results,
                }
            try:
                write_json_atomic(state_path, next_state)
            except OSError:
                try:
                    write_json_atomic(state_path, next_state)
                except Exception:
                    results[broker] = {"status": "publication_failed"}
                    return {
                        "status": "publication_failed",
                        "blocker": f"account_state_publish_failed: {broker}",
                        "brokers": results,
                    }
            except Exception:
                results[broker] = {"status": "publication_failed"}
                return {
                    "status": "publication_failed",
                    "blocker": f"account_state_publish_failed: {broker}",
                    "brokers": results,
                }
            state = next_state
            results[broker] = {"status": "ok"}
        try:
            projected_state = with_dashboard_projection(
                state,
                load_published_quotes(
                    self._quotes_path(), now=datetime.now(SHANGHAI_TZ)
                ),
                generated_at=attempted_at,
            )
            write_json_atomic(state_path, projected_state)
            state = projected_state
        except OSError:
            return {
                "status": "publication_failed",
                "blocker": "dashboard_projection_publish_failed",
                "brokers": results,
            }
        except Exception:
            return {
                "status": "failed",
                "blocker": "dashboard_projection_failed",
                "brokers": results,
            }
        ok_count = sum(
            1 for result in results.values() if result["status"] == "ok"
        )
        return {
            "status": "ok" if ok_count == len(results) else "partial" if ok_count else "failed",
            "brokers": results,
        }

    def sync_quotes_once(self) -> dict[str, object]:
        now = self.clock()
        if not self.quote_due(now):
            return {"status": "skipped"}
        self._last_quote_attempt = now
        if self._quote_service is None:
            published = load_published_quotes(
                self._quotes_path(), now=datetime.now(SHANGHAI_TZ)
            )
            self._quote_service = DashboardQuoteService(
                self._dashboard_config(),
                last_success_at=str(published["last_success_at"]),
                last_quotes={
                    symbol: dict(quote)
                    for symbol, quote in dict(published["quotes"]).items()
                    if isinstance(symbol, str) and isinstance(quote, dict)
                },
            )
        try:
            state_path = self.config.data_dir / "latest" / "account_sync_state.json"
            state = load_account_sync_state(state_path)
            payload = self._quote_service.refresh(
                build_account_quote_universe(state)
            ).to_dict()
        except Exception as exc:
            payload = self._quote_failure_payload(str(exc))
        write_json_atomic(self._quotes_path(), payload)
        if payload["status"] == "failed":
            return payload
        try:
            write_json_atomic(
                state_path,
                with_dashboard_projection(
                    state, payload, generated_at=self.now_text(),
                ),
            )
        except OSError:
            return {**payload, "status": "publication_failed", "blocker": "dashboard_projection_publish_failed"}
        except Exception:
            return {**payload, "status": "failed", "blocker": "dashboard_projection_failed"}
        return payload

    def write_heartbeat(self, *, blocker: str | None = None) -> None:
        write_json_atomic(
            self.config.data_dir / "account_sync" / "controller_status.json",
            {
                "schema_version": "open_trader.account_sync.controller.v1",
                **self._process_metadata,
                "heartbeat_at": self.now_text(),
                "phase": "blocked" if blocker else "idle",
                "account_loop": dict(self.account_loop),
                "quote_loop": dict(self.quote_loop),
                "blocker": blocker,
            },
        )

    def _dashboard_config(self) -> DashboardConfig:
        return DashboardConfig(
            portfolio_path=self.config.portfolio_path,
            data_dir=self.config.data_dir,
            reports_dir=self.config.reports_dir,
            poll_seconds=self.config.quote_interval_seconds,
            futu_host=self.config.futu_host,
            futu_port=self.config.futu_port,
        )

    def _quotes_path(self) -> Path:
        return self.config.data_dir / "latest" / "quotes.json"

    def _quote_failure_payload(self, message: str) -> dict[str, object]:
        service = self._quote_service
        assert service is not None
        return {
            "status": "failed",
            "requested_count": 0,
            "quote_count": 0,
            "missing_count": 0,
            "fetched_at": self.now_text(),
            "last_success_at": service.last_success_at,
            "stale": bool(service.last_quotes),
            "quotes": {
                symbol: {**quote, "stale": True}
                for symbol, quote in service.last_quotes.items()
            },
            "diagnostic": {"error_type": "quote_refresh_failed", "message": message},
            "fallback_count": 0,
            "us_session_status": "",
        }

    def _candidate_for(
        self, broker: str, attempted_at: str
    ) -> tuple[BrokerAccountCandidate, str | None, str | None, bool]:
        if broker in {"phillips", "eastmoney"}:
            manual = load_staged_holding_snapshot(self.config.data_dir, broker)
            staged = _staged_statement_for_worker(self.config.data_dir, broker)
            if staged is not None:
                (
                    statement_candidate,
                    statement_generation,
                    statement_staged_at,
                ) = staged
            else:
                statement_candidate = load_latest_statement_candidate(
                    self.config.data_dir, broker
                )
                statement_generation = None
                statement_staged_at = None
            manual_is_newer = False
            if manual is not None:
                manual_is_newer = statement_candidate is None or str(
                    manual["data_as_of"]
                ) > statement_candidate.data_as_of
                if (
                    not manual_is_newer
                    and statement_candidate is not None
                    and str(manual["data_as_of"]) == statement_candidate.data_as_of
                    and statement_staged_at is not None
                ):
                    manual_is_newer = _staged_at(manual) > _staged_at(
                        {"staged_at": statement_staged_at}
                    )
            if manual is not None and manual_is_newer:
                return (
                    _holding_candidate(manual),
                    None,
                    str(manual["holding_generation"]),
                    manual["cash"] == {"policy": "preserve"},
                )
            if statement_candidate is None:
                raise ValueError(f"no valid {broker} statement candidate")
            return (
                statement_candidate,
                statement_generation,
                statement_generation or "",
                False,
            )
        if broker == "futu":
            client = FutuAccountClient(
                host=self.config.futu_host,
                port=self.config.futu_port,
            )
            try:
                return build_futu_account_candidate(
                    client.fetch_snapshot(),
                    run_date=attempted_at[:10],
                    data_as_of=attempted_at,
                    fallback_fx_to_hkd=DEFAULT_RATES_TO_HKD,
                ), None, None, False
            finally:
                client.close()
        tiger_config = load_tiger_account_config(
            config_dir=self.config.tiger_config_dir,
            account=self.config.tiger_account,
            sandbox=False,
        )
        client = TigerAccountClient(config=tiger_config)
        try:
            return build_tiger_account_candidate(
                client.fetch_snapshot(),
                run_date=attempted_at[:10],
                data_as_of=attempted_at,
            ), None, None, False
        finally:
            client.close()

    def _write_diagnostic_candidate(
        self, attempted_at: str, candidate: BrokerAccountCandidate
    ) -> None:
        generation = "".join(
            character if character.isalnum() or character in "._-" else "_"
            for character in attempted_at
        )
        write_json_atomic(
            self.config.data_dir / "account_sync" / "runs" / generation / f"{candidate.broker}.json",
            _diagnostic_value(asdict(candidate)),
        )


def _holding_candidate(snapshot: dict[str, object]) -> BrokerAccountCandidate:
    broker = snapshot["broker"]
    if not isinstance(broker, str):
        raise ValueError("invalid holding snapshot broker")
    market_code, currency = SUPPORTED_BROKERS[broker]
    data_as_of = snapshot["data_as_of"]
    raw_positions = snapshot["positions"]
    raw_cash = snapshot["cash"]
    if not isinstance(data_as_of, str) or not isinstance(raw_positions, list):
        raise ValueError("invalid holding snapshot")
    positions = []
    for raw_position in raw_positions:
        if not isinstance(raw_position, dict):
            raise ValueError("invalid holding position")
        quantity = Decimal(str(raw_position["quantity"]))
        cost_price = Decimal(str(raw_position["cost_price"]))
        market_value = quantity * cost_price
        positions.append(
            Position(
                statement_id="",
                broker=broker,
                account_alias=f"{broker}_manual",
                market=Market(market_code),
                asset_class=AssetClass.STOCK,
                symbol=str(raw_position["symbol"]),
                name=str(raw_position["name"]),
                currency=currency,
                quantity=quantity,
                cost_price=cost_price,
                last_price=cost_price,
                market_value=market_value,
                cost_value=market_value,
                unrealized_pnl=Decimal("0"),
                confidence="high",
                notes="manual holding snapshot",
            )
        )
    cash: tuple[CashBalance, ...] = ()
    if raw_cash == {"policy": "replace"}:
        raise ValueError("invalid holding snapshot cash")
    if isinstance(raw_cash, dict) and raw_cash.get("policy") == "replace":
        balance = Decimal(str(raw_cash["balance"]))
        available = Decimal(str(raw_cash["available_balance"]))
        cash = (
            CashBalance(
                statement_id="",
                broker=broker,
                account_alias=f"{broker}_manual",
                currency=currency,
                cash_balance=balance,
                available_balance=available,
                confidence="high",
                notes="manual holding snapshot",
            ),
        )
    elif raw_cash != {"policy": "preserve"}:
        raise ValueError("invalid holding snapshot cash")
    return BrokerAccountCandidate(
        broker=broker,
        source_kind="manual",
        data_as_of=data_as_of,
        period=data_as_of,
        positions=tuple(positions),
        cash=cash,
        fx_rates=(),
        summary={
            "position_count": len(positions),
            "cash_count": len(cash),
            "is_real_time": False,
        },
    )


def _staged_at(value: dict[str, object]) -> datetime:
    staged_at = value.get("staged_at")
    if not isinstance(staged_at, str):
        raise ValueError("invalid holding generation")
    try:
        return datetime.fromisoformat(staged_at)
    except ValueError as error:
        raise ValueError("invalid holding generation") from error


def _staged_statement_for_worker(
    data_dir: Path, broker: str
) -> tuple[BrokerAccountCandidate, str, str | None] | None:
    staged = load_staged_statement_candidate(data_dir, broker)
    if staged is None:
        return None
    metadata = load_staged_statement_metadata(data_dir, broker)
    if metadata is None:
        candidate, generation = staged
        return candidate, generation, None
    return metadata


def _now_text() -> str:
    return datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds")


def run_account_sync_worker(
    config: AccountSyncWorkerConfig,
    *,
    once: bool = False,
    clock: Callable[[], float] = time.monotonic,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> int:
    lock_path = config.data_dir / "account_sync" / "controller.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("已有账户同步 Worker 运行", file=sys.stderr)
            return 1
        try:
            worker = AccountSyncWorker(config, clock=clock)
            while True:
                now = clock()
                if worker.account_due(now):
                    worker.account_loop = _run_loop(worker.sync_accounts_once)
                if worker.quote_due(now):
                    worker.quote_loop = _run_loop(worker.sync_quotes_once)
                worker.write_heartbeat()
                if once:
                    return 0
                sleep_fn(_next_sleep(worker, clock()))
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _run_loop(operation: Callable[[], dict[str, object]]) -> dict[str, object]:
    try:
        return operation()
    except Exception as exc:
        return {"status": "failed", "message": str(exc)}


def _is_due(last_attempt: float | None, now: float, interval: float) -> bool:
    return last_attempt is None or now - last_attempt >= interval


def _next_sleep(worker: AccountSyncWorker, now: float) -> float:
    account_due = _next_due(
        worker._last_account_attempt,
        now,
        worker.config.account_interval_seconds,
    )
    quote_due = _next_due(
        worker._last_quote_attempt,
        now,
        worker.config.quote_interval_seconds,
    )
    return max(0.0, min(account_due, quote_due) - now)


def _next_due(last_attempt: float | None, now: float, interval: float) -> float:
    return now if last_attempt is None else last_attempt + interval


def _git_sha() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            cwd=Path.cwd(),
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def _diagnostic_value(value: object) -> object:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {str(key): _diagnostic_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_diagnostic_value(item) for item in value]
    return value
