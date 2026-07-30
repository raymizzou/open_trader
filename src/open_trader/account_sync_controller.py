from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
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
    write_json_atomic,
    write_portfolio_atomic,
)
from .futu_account import FutuAccountClient, build_futu_account_candidate
from .fx import DEFAULT_RATES_TO_HKD
from .tiger_account import (
    TigerAccountClient,
    build_tiger_account_candidate,
    load_tiger_account_config,
)


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class AccountSyncControllerConfig:
    data_dir: Path
    reports_dir: Path
    portfolio_path: Path
    futu_host: str
    futu_port: int
    tiger_config_dir: Path
    tiger_account: str | None
    account_interval_seconds: float = 60.0
    quote_interval_seconds: float = 5.0


class AccountSyncController:
    def __init__(
        self,
        config: AccountSyncControllerConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
        now_text: Callable[[], str] | None = None,
    ) -> None:
        self.config = config
        self.clock = clock
        self.now_text = now_text or _now_text
        self._last_account_attempt: float | None = None

    def sync_accounts_once(self) -> dict[str, object]:
        now = self.clock()
        if (
            self._last_account_attempt is not None
            and now - self._last_account_attempt < self.config.account_interval_seconds
        ):
            return {"status": "skipped", "brokers": {}}
        self._last_account_attempt = now
        attempted_at = self.now_text()
        state_path = self.config.data_dir / "latest" / "account_sync_state.json"
        state = load_account_sync_state(state_path)
        results: dict[str, object] = {}
        for broker in REQUIRED_BROKERS:
            try:
                candidate = self._candidate_for(broker, attempted_at)
                self._write_diagnostic_candidate(attempted_at, candidate)
                next_state = accept_candidate(
                    state, candidate, attempted_at=attempted_at
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
        ok_count = sum(
            1 for result in results.values() if result["status"] == "ok"
        )
        return {
            "status": "ok" if ok_count == len(results) else "partial" if ok_count else "failed",
            "brokers": results,
        }

    def sync_quotes_once(self) -> dict[str, object]:
        return {"status": "pending"}

    def write_heartbeat(self, *, blocker: str | None = None) -> None:
        return None

    def _candidate_for(
        self, broker: str, attempted_at: str
    ) -> BrokerAccountCandidate:
        if broker in {"phillips", "eastmoney"}:
            candidate = load_latest_statement_candidate(self.config.data_dir, broker)
            if candidate is None:
                raise ValueError(f"no valid {broker} statement candidate")
            return candidate
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
                )
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
            )
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


def _now_text() -> str:
    return datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds")


def _diagnostic_value(value: object) -> object:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {str(key): _diagnostic_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_diagnostic_value(item) for item in value]
    return value
