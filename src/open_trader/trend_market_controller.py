from __future__ import annotations

import hashlib
import json
import os
import re
import socket
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from time import sleep
from zoneinfo import ZoneInfo

from .a_share_trend import (
    _process_version,
    load_futu_simulate_trend_account,
    read_delivery_receipt,
    run_a_share_trend_report,
    valid_serialized_account,
)
from .a_share_trend_watch import cn_session, watch_a_share_protection
from .daily_premarket import (
    DailyPremarketConfig,
    RunLock,
    build_notifier,
    require_trend_executor,
    require_trend_review_config,
    send_notification_with_results,
    trend_execution_mode,
)
from .futu_quote import FutuQuoteClient
from .futu_symbols import to_futu_symbol
from .kelly_order_execution import (
    ExecutorGuardedOrderClient,
    FutuSimulateOrderExecutionClient,
)
from .market_trend import market_paths, run_market_trend_report
from .market_trend_watch import (
    MARKET_TIMEZONES,
    market_session,
    watch_market_protection,
)
from .trend_review import (
    _canonical_json_bytes,
    _report_hash,
    _write_immutable,
    benchmark_fact,
    build_trend_review_projection,
    capture_trend_review_close,
    execute_trend_review_open,
    execute_trend_review_stop,
    load_trend_action_audit,
    lock_trend_execution_batch,
    record_trend_review_missed_buys,
)


STATUS_SCHEMA = "open_trader.trend_controller.status.v1"
REPORT_STEM = re.compile(r"(?P<date>\d{4}-\d{2}-\d{2})(?:-r(?P<revision>\d+))?\Z")
BUY_WINDOWS = {
    "CN": (time(9, 30), time(10, 0)),
    "HK": (time(9, 30), time(10, 0)),
    "US": (time(9, 30), time(16, 0)),
}
TIMEZONES = {"CN": ZoneInfo("Asia/Shanghai"), **MARKET_TIMEZONES}


@dataclass(frozen=True)
class ControllerCycle:
    market: str
    as_of_date: str
    execution_date: str
    report_run_date: str
    session: str
    market_open: bool
    next_check_at: datetime


@dataclass(frozen=True)
class ReportTask:
    cycle: ControllerCycle
    completes_revision_request: bool


def _market(value: str) -> str:
    market = value.strip().upper()
    if market not in BUY_WINDOWS:
        raise ValueError(f"unsupported trend market: {value}")
    return market


def _controller_root(config: DailyPremarketConfig, market: str) -> Path:
    return config.data_dir / "trend_controller" / market


def _batch_path(config: DailyPremarketConfig, market: str, execution_date: str) -> Path:
    return (
        config.data_dir
        / "trend_review"
        / "ledgers"
        / market
        / "batches"
        / f"{execution_date}.json"
    )


def _close_path(config: DailyPremarketConfig, market: str, trading_date: str) -> Path:
    return (
        config.data_dir
        / "trend_review"
        / "daily"
        / market
        / f"{trading_date}.json"
    )


def _close_completion_path(
    config: DailyPremarketConfig, market: str, trading_date: str
) -> Path:
    return (
        _controller_root(config, market)
        / "close_completions"
        / f"{trading_date}.json"
    )


def _close_completed(
    config: DailyPremarketConfig, market: str, trading_date: str
) -> bool:
    completion = _close_completion_path(config, market, trading_date)
    if not completion.exists():
        return False
    payload = _read_json(completion, "trend close completion")
    if (
        payload.get("schema_version")
        != "open_trader.trend_controller.close_completion.v1"
        or payload.get("market") != market
        or payload.get("trading_date") != trading_date
        or payload.get("fact_path")
        != str(_close_path(config, market, trading_date))
        or not _close_path(config, market, trading_date).exists()
    ):
        raise ValueError(f"invalid trend close completion: {completion}")
    return True


def _complete_close(
    config: DailyPremarketConfig,
    market: str,
    trading_date: str,
    completed_at: datetime,
) -> None:
    fact = _close_path(config, market, trading_date)
    if not fact.exists():
        raise RuntimeError("trend close capture completed without a daily fact")
    _write_immutable(
        _close_completion_path(config, market, trading_date),
        _canonical_json_bytes({
            "schema_version": "open_trader.trend_controller.close_completion.v1",
            "market": market,
            "trading_date": trading_date,
            "fact_path": str(fact),
            "completed_at": completed_at.isoformat(timespec="seconds"),
        }),
    )


def _read_json(path: Path, label: str) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid {label}: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"invalid {label}: {path}")
    return payload


def _status_payload(
    config: DailyPremarketConfig,
    market: str,
    *,
    now: datetime,
    phase: str,
    last_success: object,
    blocker: object,
    next_check_at: datetime,
    fixed_process_version: str | None = None,
) -> dict[str, object]:
    mode = trend_execution_mode(config, hostname_fn=socket.gethostname)
    return {
        "schema_version": STATUS_SCHEMA,
        "effective_mode": mode.mode,
        "executor_host": mode.executor_host,
        "local_host": mode.local_host,
        "pid": os.getpid(),
        "working_directory": str(Path.cwd().resolve()),
        "git_sha": (
            fixed_process_version
            if fixed_process_version is not None
            else _process_version(config.repo)
        ),
        "phase": phase,
        "heartbeat_at": now.isoformat(timespec="seconds"),
        "last_success": last_success,
        "blocker": blocker,
        "next_check_at": next_check_at.isoformat(timespec="seconds"),
    }


def _record_status(
    config: DailyPremarketConfig,
    market: str,
    *,
    now: datetime,
    phase: str,
    last_success: object,
    blocker: object,
    next_check_at: datetime,
    fixed_process_version: str,
) -> dict[str, object]:
    payload = _status_payload(
        config,
        market,
        now=now,
        phase=phase,
        last_success=last_success,
        blocker=blocker,
        next_check_at=next_check_at,
        fixed_process_version=fixed_process_version,
    )
    _write_status(config, market, payload)
    return payload


def _localized(now: datetime, timezone: str) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        return now.replace(tzinfo=ZoneInfo(timezone))
    return now


def _retry_at(now: datetime, failures: int) -> datetime:
    return now + timedelta(seconds=min(300, 5 * 2 ** min(failures, 6)))


def _write_status(
    config: DailyPremarketConfig,
    market: str,
    payload: Mapping[str, object],
) -> None:
    path = _controller_root(config, market) / "status.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp.write_bytes(_canonical_json_bytes(payload))
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _valid_status(payload: Mapping[str, object]) -> bool:
    required_strings = (
        "effective_mode",
        "executor_host",
        "local_host",
        "working_directory",
        "git_sha",
        "phase",
        "heartbeat_at",
        "next_check_at",
    )
    if (
        payload.get("schema_version") != STATUS_SCHEMA
        or payload.get("effective_mode") not in {"execute", "readonly"}
        or not isinstance(payload.get("pid"), int)
        or any(not isinstance(payload.get(key), str) for key in required_strings)
        or "last_success" not in payload
        or "blocker" not in payload
    ):
        return False
    try:
        heartbeat = datetime.fromisoformat(str(payload["heartbeat_at"]))
        next_check = datetime.fromisoformat(str(payload["next_check_at"]))
    except ValueError:
        return False
    return all(
        value.tzinfo is not None and value.utcoffset() is not None
        for value in (heartbeat, next_check)
    )


def load_trend_market_status(
    config: DailyPremarketConfig,
    market: str,
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    market = _market(market)
    current = now or datetime.now(TIMEZONES[market])
    mode = trend_execution_mode(config, hostname_fn=socket.gethostname)
    if mode.mode == "readonly":
        return _status_payload(
            config,
            market,
            now=current,
            phase="readonly",
            last_success=None,
            blocker=mode.reason,
            next_check_at=current,
        )
    path = _controller_root(config, market) / "status.json"
    payload = _read_json(path, "trend controller status")
    if not _valid_status(payload):
        raise ValueError(f"invalid trend controller status: {path}")
    return payload


def _derive_cycle(
    config: DailyPremarketConfig, market: str, now: datetime
) -> ControllerCycle:
    market = _market(market)
    if now.tzinfo is None or now.utcoffset() is None:
        now = now.replace(tzinfo=ZoneInfo(config.timezone))
    timezone = TIMEZONES[market]
    local = now.astimezone(timezone)
    today = local.date()
    quote = FutuQuoteClient(host=config.futu_host, port=config.futu_port)
    try:
        trading_days = sorted(
            date.fromisoformat(item)
            for item in quote.get_trading_days(
                market=market,
                start=(today - timedelta(days=35)).isoformat(),
                end=(today + timedelta(days=35)).isoformat(),
            )
        )
    finally:
        quote.close()
    if not trading_days:
        raise RuntimeError(f"Futu {market} calendar returned no trading days")
    session = cn_session(local) if market == "CN" else market_session(local, market)
    today_is_trading = today in trading_days
    completed = today_is_trading and session == "closed"
    prior = [
        item
        for item in trading_days
        if item < today or (item == today and completed)
    ]
    if not prior:
        raise RuntimeError(f"Futu {market} calendar has no completed trading session")
    as_of = prior[-1]
    future = [item for item in trading_days if item > as_of]
    if not future:
        raise RuntimeError(f"Futu {market} calendar has no next trading session")
    execution = future[0]
    if not today_is_trading:
        session = "holiday"
    market_open = today_is_trading and execution == today and session in {
        "morning",
        "afternoon",
        "open",
    }
    return ControllerCycle(
        market=market,
        as_of_date=as_of.isoformat(),
        execution_date=execution.isoformat(),
        report_run_date=(
            (as_of + timedelta(days=1)).isoformat()
            if market == "US"
            else as_of.isoformat()
        ),
        session=session,
        market_open=market_open,
        next_check_at=now + timedelta(seconds=5),
    )


def _report_dir(config: DailyPremarketConfig, market: str) -> Path:
    if market == "CN":
        return config.reports_dir / "trend_a_share"
    return market_paths(config.data_dir, config.reports_dir, market).reports


def _valid_report(
    config: DailyPremarketConfig,
    market: str,
    execution_date: str,
    path: Path,
    payload: object,
) -> bool:
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        return False
    match = REPORT_STEM.fullmatch(path.stem)
    try:
        as_of = date.fromisoformat(str(payload["as_of_date"]))
        execution = date.fromisoformat(str(payload["execution_date"]))
        generated = datetime.fromisoformat(str(payload["generated_at"]))
    except (KeyError, TypeError, ValueError):
        return False
    metadata = payload.get("metadata")
    account = payload.get("account")
    snapshot = payload.get("strategy_snapshot")
    judgments = payload.get("strategy_judgments")
    actions = judgments.get("formal_actions") if isinstance(judgments, dict) else None
    expected_broker = {"CN": "eastmoney", "US": "tiger", "HK": "phillips"}[market]
    expected_account = getattr(
        config, f"trend_review_{market.lower()}_simulate_acc_id"
    )
    if not (
        match is not None
        and match.group("date") == as_of.isoformat()
        and execution.isoformat() == execution_date
        and as_of <= execution
        and generated.tzinfo is not None
        and generated.utcoffset() is not None
        and isinstance(metadata, dict)
        and str(metadata.get("market") or "").upper() == market
        and str(metadata.get("broker") or "").lower() == expected_broker
        and isinstance(account, dict)
        and valid_serialized_account(account)
        and account.get("fresh") is True
        and account.get("source_date") == as_of.isoformat()
        and isinstance(snapshot, dict)
        and all(
            snapshot.get(key)
            for key in ("strategy_id", "strategy_version", "process_version")
        )
        and isinstance(snapshot.get("parameters"), dict)
        and isinstance(snapshot.get("parameter_rows"), list)
        and snapshot.get("parameter_rows")
        and isinstance(judgments, dict)
        and isinstance(actions, list)
        and all(
            isinstance(judgments.get(key), list)
            for key in ("holding_decisions", "top10_candidates")
        )
        and (
            expected_account <= 0
            or metadata.get("simulate_acc_id") == expected_account
        )
    ):
        return False
    for action in actions:
        if (
            not isinstance(action, dict)
            or action.get("action") not in {"BUY", "SELL_ALL"}
            or not str(action.get("symbol") or "").strip()
        ):
            return False
        if action["action"] != "BUY":
            continue
        try:
            weight = Decimal(str(action.get("target_weight")))
            quantity = Decimal(str(action.get("estimated_shares")))
            amount = Decimal(str(action.get("target_amount")))
            atr = Decimal(str(action.get("atr")))
            lot = int(action.get("lot_size") or 0)
        except (InvalidOperation, TypeError, ValueError):
            return False
        if (
            not all(
                item.is_finite() and item > 0
                for item in (weight, quantity, amount, atr)
            )
            or lot <= 0
            or quantity != quantity.to_integral_value()
            or quantity % lot
        ):
            return False
    return True


def _report_order(path: Path) -> tuple[str, int]:
    match = REPORT_STEM.fullmatch(path.stem)
    if match is None:
        return "", -1
    return match.group("date"), int(match.group("revision") or 0)


def _load_latest_valid_report(
    config: DailyPremarketConfig, market: str, execution_date: str
) -> tuple[Path, dict[str, object]] | None:
    market = _market(market)
    invalid: Path | None = None
    paths = sorted(
        _report_dir(config, market).glob("*.json"),
        key=_report_order,
        reverse=True,
    )
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if (
            not isinstance(payload, dict)
            or payload.get("execution_date") != execution_date
        ):
            continue
        if _valid_report(config, market, execution_date, path, payload):
            if invalid is not None:
                raise ValueError(
                    f"invalid frozen trend report: {invalid}; run --revision"
                )
            return path, payload
        invalid = path
    if invalid is not None:
        raise ValueError(f"invalid frozen trend report: {invalid}; run --revision")
    return None


def _load_cycle_report(
    config: DailyPremarketConfig, cycle: ControllerCycle
) -> tuple[Path, dict[str, object]] | None:
    paths = sorted(
        (
            path
            for path in _report_dir(config, cycle.market).glob(
                f"{cycle.as_of_date}*.json"
            )
            if (match := REPORT_STEM.fullmatch(path.stem)) is not None
            and match.group("date") == cycle.as_of_date
        ),
        key=_report_order,
        reverse=True,
    )
    if paths:
        path = paths[0]
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"invalid frozen trend report: {path}; run --revision"
            ) from exc
        if not _valid_report(
            config, cycle.market, cycle.execution_date, path, payload
        ):
            raise ValueError(
                f"invalid frozen trend report: {path}; run --revision"
            )
    latest = _load_latest_valid_report(
        config, cycle.market, cycle.execution_date
    )
    if latest is None or latest[1].get("as_of_date") != cycle.as_of_date:
        return None
    return latest


def _delivery_receipt_path(
    config: DailyPremarketConfig, market: str, report_path: Path
) -> Path:
    return (
        config.data_dir
        / "trend_a_share"
        / "delivery"
        / f"{report_path.stem}.json"
        if market == "CN"
        else market_paths(config.data_dir, config.reports_dir, market).root
        / "delivery"
        / f"{report_path.stem}.json"
    )


def _recovery_revision_for_report(
    config: DailyPremarketConfig,
    market: str,
    report: tuple[Path, Mapping[str, object]],
    *,
    require_receipt: bool = False,
) -> bool | None:
    path, payload = report
    receipt_path = _delivery_receipt_path(config, market, path)
    receipt = read_delivery_receipt(receipt_path, artifact_stem=path.stem)
    if receipt is None:
        if require_receipt:
            raise ValueError(
                f"selected trend report has no delivery receipt: {path}"
            )
        return None
    markdown_path = path.with_suffix(".md")
    try:
        report_json = path.read_text(encoding="utf-8")
        markdown = markdown_path.read_text(encoding="utf-8")
        receipt_report = json.loads(str(receipt["report_json"]))
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError) as exc:
        raise ValueError(
            f"delivery receipt does not match selected frozen artifacts: {receipt_path}"
        ) from exc
    if (
        not isinstance(receipt_report, Mapping)
        or report_json != receipt["report_json"]
        or markdown != receipt["markdown"]
        or _report_hash(receipt_report) != _report_hash(payload)
        or receipt["protection_state"] != receipt_report.get("protection_state")
        or receipt_report.get("protection_state") != payload.get("protection_state")
    ):
        raise ValueError(
            f"delivery receipt does not match selected frozen artifacts: {receipt_path}"
        )
    replay = payload.get("replay_evidence")
    if replay is not None:
        if not isinstance(replay, Mapping):
            raise ValueError("frozen report replay evidence is invalid")
        evidence_path = Path(str(replay.get("path") or ""))
        if not evidence_path.is_absolute():
            evidence_path = config.data_dir / evidence_path
        try:
            evidence_path.resolve().relative_to(config.data_dir.resolve())
            digest = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
        except (OSError, ValueError) as exc:
            raise ValueError("frozen report replay evidence is invalid") from exc
        if digest != replay.get("sha256"):
            raise ValueError("frozen report replay evidence hash mismatch")
    if receipt["status"] in {"prepared", "pending", "delivery_failed"}:
        return _report_order(path)[1] > 0
    return None


def _generate_report(
    config: DailyPremarketConfig, market: str, run_date: str, revision: bool
) -> None:
    require_trend_executor(config, hostname_fn=socket.gethostname)
    notifier = build_notifier(config)
    result = (
        run_a_share_trend_report(
            config=config,
            run_date=run_date,
            revision=revision,
            notifier=notifier,
        )
        if market == "CN"
        else run_market_trend_report(
            config=config,
            market=market,
            run_date=run_date,
            revision=revision,
            notifier=notifier,
        )
    )
    if result.status not in {"generated", "existing", "holiday"}:
        raise RuntimeError(f"{market} trend report generation returned {result.status}")


def _new_order_client(config: DailyPremarketConfig, market: str) -> object:
    account_id = require_trend_review_config(config, market)
    return ExecutorGuardedOrderClient(
        FutuSimulateOrderExecutionClient(
            host=config.futu_host,
            port=config.futu_port,
            simulate_acc_id=account_id,
            trd_market=market,
        ),
        lambda: require_trend_executor(config, hostname_fn=socket.gethostname),
    )


def _run_stop(
    config: DailyPremarketConfig,
    market: str,
    event: Mapping[str, object],
) -> None:
    client = _new_order_client(config, market)
    try:
        execute_trend_review_stop(
            data_dir=config.data_dir,
            market=market,
            symbol=str(event.get("symbol") or ""),
            trading_date=str(event.get("trading_date") or ""),
            event_id=str(event.get("event_id") or ""),
            client=client,
            now=str(event.get("occurred_at") or ""),
        )
    finally:
        client.close()


def _run_protection_pass(
    config: DailyPremarketConfig, market: str, trading_date: str
) -> object:
    require_trend_executor(config, hostname_fn=socket.gethostname)
    account_id = require_trend_review_config(config, market)
    notifier = build_notifier(config)

    def account_loader(
        _path: Path, *, expected_date: str, timezone: ZoneInfo
    ) -> object:
        del timezone
        return load_futu_simulate_trend_account(
            host=config.futu_host,
            port=config.futu_port,
            simulate_acc_id=account_id,
            market=market,
            expected_date=expected_date,
        )

    quote_factory = lambda: FutuQuoteClient(
        host=config.futu_host,
        port=config.futu_port,
    )
    callback = lambda event: _run_stop(config, market, event)
    if market == "CN":
        return watch_a_share_protection(
            portfolio_path=config.portfolio,
            state_path=config.data_dir / "trend_a_share/protection_state.json",
            events_path=config.data_dir / "trend_a_share/watch_events.jsonl",
            report_lock_path=config.data_dir / "runs/.trend_a_share_report.lock",
            quote_client=None,
            quote_client_factory=quote_factory,
            notifier=notifier,
            poll_seconds=5,
            reconnect_seconds=5,
            once=True,
            account_loader=account_loader,
            on_protection_trigger=callback,
        )
    paths = market_paths(config.data_dir, config.reports_dir, market)
    return watch_market_protection(
        market=market,
        data_dir=config.data_dir,
        portfolio_path=config.portfolio,
        account_loader=account_loader,
        state_path=paths.state,
        events_path=paths.events,
        report_lock_path=paths.report_lock,
        quote_client=None,
        quote_client_factory=quote_factory,
        notifier=notifier,
        poll_seconds=5,
        reconnect_seconds=5,
        once=True,
        on_protection_trigger=callback,
    )


def _protection_blocker(result: object) -> str | None:
    status = str(getattr(result, "status", "") or "")
    exceptions = getattr(result, "exception_count", None)
    unknown_quotes = getattr(result, "unknown_quote_count", None)
    if (
        status != "completed"
        or not isinstance(exceptions, int)
        or isinstance(exceptions, bool)
        or exceptions
        or not isinstance(unknown_quotes, int)
        or isinstance(unknown_quotes, bool)
        or unknown_quotes
    ):
        return (
            "protection pass abnormal: "
            f"status={status or 'missing'}, exceptions={exceptions}, "
            f"unknown_quotes={unknown_quotes}"
        )
    return None


def _execute_locked_report(
    config: DailyPremarketConfig,
    market: str,
    execution_date: str,
    report_path: Path,
    report: Mapping[str, object],
    *,
    allow_new_buys: bool = True,
) -> dict[str, object]:
    require_trend_executor(config, hostname_fn=socket.gethostname)
    now = datetime.now(TIMEZONES[market]).isoformat(timespec="seconds")
    as_of_date = str(report.get("as_of_date") or "")
    with RunLock(_revision_gate_path(config, market, execution_date)):
        request, completion = _revision_state(
            config, market, as_of_date, execution_date
        )
        if request is not None and completion is None:
            raise RuntimeError("trend report revision request is pending")
        if completion is not None:
            if completion.get("report_sha256") != _report_hash(report):
                raise RuntimeError("completed trend report revision is not selected")
        batch = lock_trend_execution_batch(
            config.data_dir,
            market=market,
            execution_date=execution_date,
            report_path=report_path,
            report=report,
            locked_at=now,
        )
    locked_path = Path(str(batch["report_path"]))
    locked_report = _read_json(locked_path, "locked trend report")
    if (
        not _valid_report(config, market, execution_date, locked_path, locked_report)
        or _report_hash(locked_report) != batch["report_sha256"]
    ):
        raise ValueError(f"invalid locked trend report: {locked_path}")
    judgments = locked_report["strategy_judgments"]
    actions = judgments["formal_actions"]
    if not actions:
        return {
            "status": "unchanged",
            "market": market,
            "date": execution_date,
            "submitted_count": 0,
            "artifact_paths": [],
        }
    missed = record_trend_review_missed_buys(
        data_dir=config.data_dir,
        report=locked_report,
        market=market,
        execution_date=execution_date,
        now=now,
    )
    sell_symbols = {
        str(action.get("symbol") or "").strip()
        for action in actions
        if action.get("action") == "SELL_ALL"
    }
    eligible_buys = sum(
        action.get("action") == "BUY"
        and str(action.get("symbol") or "").strip() not in sell_symbols
        for action in actions
    )
    if missed == eligible_buys == len(actions):
        return {
            "status": "missed_window",
            "market": market,
            "date": execution_date,
            "submitted_count": 0,
            "artifact_paths": [],
        }
    symbols = sorted(
        {
            to_futu_symbol(market, str(action["symbol"]))
            for action in actions
            if allow_new_buys and action["action"] == "BUY"
        }
    )
    quote = None
    prices: dict[str, Decimal] = {}
    client = None
    try:
        if symbols:
            try:
                quote = FutuQuoteClient(
                    host=config.futu_host, port=config.futu_port
                )
                prices = {
                    symbol: snapshot.last_price
                    for symbol, snapshot in quote.get_snapshots(symbols).items()
                }
            except Exception:
                prices = {}
        client = _new_order_client(config, market)
        return execute_trend_review_open(
            data_dir=config.data_dir,
            report=locked_report,
            client=client,
            market=market,
            execution_date=execution_date,
            now=now,
            quote_prices=prices,
        )
    finally:
        if quote is not None:
            quote.close()
        if client is not None:
            client.close()


def _capture_close(
    config: DailyPremarketConfig, market: str, trading_date: str
) -> None:
    require_trend_executor(config, hostname_fn=socket.gethostname)
    path = _close_path(config, market, trading_date)
    if path.exists():
        build_trend_review_projection(config.data_dir, market)
        return
    report_item = _load_report_for_as_of(config, market, trading_date)
    if report_item is None:
        raise FileNotFoundError(f"no {market} trend report for {trading_date}")
    _, report = report_item
    quote = None
    client = None
    try:
        quote = FutuQuoteClient(host=config.futu_host, port=config.futu_port)
        client = _new_order_client(config, market)
        capture_trend_review_close(
            data_dir=config.data_dir,
            market=market,
            trading_date=trading_date,
            report=report,
            simulate_snapshot=client.account_snapshot(),
            orders=client.list_orders(start=trading_date, end=trading_date)["orders"],
            benchmark=benchmark_fact(quote, market, trading_date),
        )
        build_trend_review_projection(config.data_dir, market)
    finally:
        if quote is not None:
            quote.close()
        if client is not None:
            client.close()


def _load_report_for_as_of(
    config: DailyPremarketConfig, market: str, as_of_date: str
) -> tuple[Path, dict[str, object]] | None:
    paths = sorted(
        _report_dir(config, market).glob(f"{as_of_date}*.json"),
        key=_report_order,
        reverse=True,
    )
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload.get("as_of_date") == as_of_date:
            execution_date = str(payload.get("execution_date") or "")
            if _valid_report(config, market, execution_date, path, payload):
                return path, payload
    return None


def _notify_once(title: str, message: str, key: object) -> bool:
    if not (
        isinstance(key, tuple)
        and len(key) == 6
        and isinstance(key[0], DailyPremarketConfig)
    ):
        raise ValueError("invalid trend controller notification key")
    config, market, execution_date, action, reason, occurred_at = key
    assert isinstance(config, DailyPremarketConfig)
    if not config.notifiers:
        return False
    identity = "|".join(map(str, (market, execution_date, action, reason)))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    path = (
        _controller_root(config, str(market))
        / "notifications"
        / str(execution_date)
        / f"{digest}.json"
    )
    if path.exists():
        return True
    try:
        attempts = send_notification_with_results(
            build_notifier(config), title, message
        )
    except Exception:
        return False
    successes = [item.channel for item in attempts if item.success]
    if not successes:
        return False
    _write_immutable(
        path,
        _canonical_json_bytes({
            "schema_version": "open_trader.trend_controller.notification.v1",
            "market": market,
            "execution_date": execution_date,
            "action": action,
            "reason": reason,
            "notified_at": occurred_at,
            "channels": successes,
        }),
    )
    return True


def _revision_paths(
    config: DailyPremarketConfig, market: str, as_of_date: str
) -> tuple[Path, Path]:
    root = _controller_root(config, market)
    return (
        root / "revision_requests" / f"{as_of_date}.json",
        root / "revision_completions" / f"{as_of_date}.json",
    )


def _revision_gate_path(
    config: DailyPremarketConfig, market: str, execution_date: str
) -> Path:
    return (
        config.data_dir
        / "runs"
        / f".trend_market_revision.{market}.{execution_date}.lock"
    )


def _report_lock_path(config: DailyPremarketConfig, market: str) -> Path:
    if market == "CN":
        return config.data_dir / "runs/.trend_a_share_report.lock"
    return market_paths(config.data_dir, config.reports_dir, market).report_lock


def _revision_baseline(
    config: DailyPremarketConfig,
    cycle: ControllerCycle,
) -> tuple[Path | None, str | None, int]:
    candidates = sorted(
        (
            path
            for path in _report_dir(config, cycle.market).glob(
                f"{cycle.as_of_date}*.json"
            )
            if _report_order(path)[0] == cycle.as_of_date
        ),
        key=_report_order,
        reverse=True,
    )
    if not candidates:
        return None, None, -1
    path = candidates[0]
    return path, hashlib.sha256(path.read_bytes()).hexdigest(), _report_order(path)[1]


def _request_revision(
    config: DailyPremarketConfig,
    cycle: ControllerCycle,
    now: datetime,
) -> Path:
    try:
        with RunLock(
            _revision_gate_path(config, cycle.market, cycle.execution_date)
        ):
            if _batch_path(config, cycle.market, cycle.execution_date).exists():
                raise ValueError(
                    "trend report revision rejected: execution has begun"
                )
            request, _ = _revision_paths(config, cycle.market, cycle.as_of_date)
            if request.exists():
                _revision_state(
                    config,
                    cycle.market,
                    cycle.as_of_date,
                    cycle.execution_date,
                )
                return request
            with RunLock(_report_lock_path(config, cycle.market), wait=True):
                baseline_path, baseline_sha, baseline_revision = _revision_baseline(
                    config, cycle
                )
                return _write_immutable(
                    request,
                    _canonical_json_bytes({
                        "schema_version": (
                            "open_trader.trend_controller.revision_request.v1"
                        ),
                        "market": cycle.market,
                        "as_of_date": cycle.as_of_date,
                        "execution_date": cycle.execution_date,
                        "baseline_report_path": (
                            str(baseline_path) if baseline_path is not None else None
                        ),
                        "baseline_report_sha256": baseline_sha,
                        "baseline_revision": baseline_revision,
                        "requested_at": now.isoformat(timespec="seconds"),
                    }),
                )
    except RuntimeError as exc:
        raise ValueError(
            "trend report revision rejected: execution has begun"
        ) from exc


def _pending_revision_report(
    config: DailyPremarketConfig,
    cycle: ControllerCycle,
    request: Mapping[str, object],
) -> tuple[Path, dict[str, object]] | None:
    try:
        latest = _load_latest_valid_report(
            config, cycle.market, cycle.execution_date
        )
    except ValueError:
        return None
    if (
        latest is None
        or _report_order(latest[0])[0] != cycle.as_of_date
        or _report_order(latest[0])[1]
        <= max(0, int(request["baseline_revision"]))
        or not _delivery_receipt_path(config, cycle.market, latest[0]).exists()
    ):
        return None
    return latest


def _revision_state(
    config: DailyPremarketConfig,
    market: str,
    as_of_date: str,
    execution_date: str,
) -> tuple[dict[str, object] | None, dict[str, object] | None]:
    request_path, completion_path = _revision_paths(config, market, as_of_date)
    if not request_path.exists():
        if completion_path.exists():
            raise ValueError(f"invalid trend report revision completion: {completion_path}")
        return None, None
    request = _read_json(request_path, "trend report revision request")
    try:
        requested_at = datetime.fromisoformat(str(request["requested_at"]))
        baseline_revision = request["baseline_revision"]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid trend report revision request: {request_path}") from exc
    baseline_path_value = request.get("baseline_report_path")
    baseline_sha = request.get("baseline_report_sha256")
    valid_baseline = (
        isinstance(baseline_revision, int)
        and not isinstance(baseline_revision, bool)
        and baseline_revision >= -1
    )
    if baseline_revision == -1:
        valid_baseline = (
            valid_baseline
            and baseline_path_value is None
            and baseline_sha is None
        )
    elif valid_baseline:
        baseline_path = Path(str(baseline_path_value or ""))
        try:
            valid_baseline = (
                isinstance(baseline_path_value, str)
                and bool(baseline_path_value)
                and _report_order(baseline_path)
                == (as_of_date, baseline_revision)
                and baseline_path.resolve().parent
                == _report_dir(config, market).resolve()
                and isinstance(baseline_sha, str)
                and hashlib.sha256(baseline_path.read_bytes()).hexdigest()
                == baseline_sha
            )
        except OSError:
            valid_baseline = False
    if (
        request.get("schema_version")
        != "open_trader.trend_controller.revision_request.v1"
        or request.get("market") != market
        or request.get("as_of_date") != as_of_date
        or request.get("execution_date") != execution_date
        or requested_at.tzinfo is None
        or requested_at.utcoffset() is None
        or not valid_baseline
    ):
        raise ValueError(f"invalid trend report revision request: {request_path}")
    if not completion_path.exists():
        return request, None
    completion = _read_json(completion_path, "trend report revision completion")
    report_path = Path(str(completion.get("report_path") or ""))
    report = _read_json(report_path, "completed trend report revision")
    try:
        completed_at = datetime.fromisoformat(str(completion["completed_at"]))
        recovery_revision = _recovery_revision_for_report(
            config, market, (report_path, report), require_receipt=True
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            f"invalid trend report revision completion: {completion_path}"
        ) from exc
    if (
        completion.get("schema_version")
        != "open_trader.trend_controller.revision_completion.v1"
        or completion.get("market") != market
        or completion.get("as_of_date") != as_of_date
        or completion.get("execution_date") != execution_date
        or completion.get("request_path") != str(request_path)
        or completion.get("request_sha256")
        != hashlib.sha256(request_path.read_bytes()).hexdigest()
        or _report_order(report_path)[0] != as_of_date
        or _report_order(report_path)[1] <= max(0, baseline_revision)
        or not _valid_report(config, market, execution_date, report_path, report)
        or completion.get("report_sha256") != _report_hash(report)
        or completed_at.tzinfo is None
        or completed_at.utcoffset() is None
        or recovery_revision is not None
    ):
        raise ValueError(f"invalid trend report revision completion: {completion_path}")
    return request, completion


def _legacy_cutover_path(
    config: DailyPremarketConfig, market: str, as_of_date: str
) -> Path:
    return (
        _controller_root(config, market)
        / "legacy_cutovers"
        / f"{as_of_date}.json"
    )


def _record_legacy_cycle_cutover(
    config: DailyPremarketConfig,
    cycle: ControllerCycle,
    *,
    actor: str,
    reason: str,
    authorized_at: datetime,
) -> Path:
    require_trend_executor(config, hostname_fn=socket.gethostname)
    path = _legacy_cutover_path(config, cycle.market, cycle.as_of_date)
    actor = actor.strip()
    reason = reason.strip()
    try:
        market = _market(cycle.market)
        as_of = date.fromisoformat(cycle.as_of_date)
        execution = date.fromisoformat(cycle.execution_date)
        authorized_at = datetime.fromisoformat(
            authorized_at.isoformat(timespec="seconds")
        )
        window_end = datetime.combine(
            execution, BUY_WINDOWS[market][1], tzinfo=TIMEZONES[market]
        )
        request_path, _ = _revision_paths(config, market, cycle.as_of_date)
        request, completion = _revision_state(
            config, market, cycle.as_of_date, cycle.execution_date
        )
        report_path, report_sha, _ = _revision_baseline(config, cycle)
        valid = (
            market == cycle.market
            and as_of.isoformat() == cycle.as_of_date
            and execution.isoformat() == cycle.execution_date
            and bool(actor)
            and bool(reason)
            and authorized_at.tzinfo is not None
            and authorized_at.utcoffset() is not None
            and authorized_at.astimezone(TIMEZONES[market]) > window_end
            and not _batch_path(config, market, cycle.execution_date).exists()
            and request is not None
            and completion is None
            and report_path is not None
            and report_sha is not None
            and report_path.resolve().parent
            == _report_dir(config, market).resolve()
            and request.get("baseline_report_path") == str(report_path)
            and request.get("baseline_report_sha256") == report_sha
        )
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid legacy trend cutover: {path}") from exc
    if not valid:
        raise ValueError(f"invalid legacy trend cutover: {path}")
    return _write_immutable(
        path,
        _canonical_json_bytes({
            "schema_version": "open_trader.trend_controller.legacy_cutover.v1",
            "market": cycle.market,
            "as_of_date": cycle.as_of_date,
            "execution_date": cycle.execution_date,
            "report_path": str(report_path),
            "report_sha256": report_sha,
            "revision_request_path": str(request_path),
            "revision_request_sha256": hashlib.sha256(
                request_path.read_bytes()
            ).hexdigest(),
            "actor": actor,
            "reason": reason,
            "authorized_at": authorized_at.isoformat(timespec="seconds"),
        }),
    )


def _legacy_cycle_cutover(
    config: DailyPremarketConfig, cycle: ControllerCycle
) -> bool:
    path = _legacy_cutover_path(config, cycle.market, cycle.as_of_date)
    if not path.exists():
        return False
    try:
        payload = _read_json(path, "legacy trend cutover")
        market = _market(cycle.market)
        as_of = date.fromisoformat(cycle.as_of_date)
        execution = date.fromisoformat(cycle.execution_date)
        authorized_at = datetime.fromisoformat(str(payload["authorized_at"]))
        window_end = datetime.combine(
            execution, BUY_WINDOWS[market][1], tzinfo=TIMEZONES[market]
        )
        request_path, _ = _revision_paths(config, market, cycle.as_of_date)
        request, completion = _revision_state(
            config, market, cycle.as_of_date, cycle.execution_date
        )
        report_path, report_sha, _ = _revision_baseline(config, cycle)
        actor = payload.get("actor")
        reason = payload.get("reason")
        valid = (
            payload.get("schema_version")
            == "open_trader.trend_controller.legacy_cutover.v1"
            and market == cycle.market
            and as_of.isoformat() == cycle.as_of_date
            and execution.isoformat() == cycle.execution_date
            and payload.get("market") == cycle.market
            and payload.get("as_of_date") == cycle.as_of_date
            and payload.get("execution_date") == cycle.execution_date
            and isinstance(actor, str)
            and bool(actor)
            and actor == actor.strip()
            and isinstance(reason, str)
            and bool(reason)
            and reason == reason.strip()
            and authorized_at.tzinfo is not None
            and authorized_at.utcoffset() is not None
            and payload.get("authorized_at")
            == authorized_at.isoformat(timespec="seconds")
            and authorized_at.astimezone(TIMEZONES[market]) > window_end
            and not _batch_path(config, market, cycle.execution_date).exists()
            and request is not None
            and completion is None
            and report_path is not None
            and report_sha is not None
            and report_path.resolve().parent
            == _report_dir(config, market).resolve()
            and request.get("baseline_report_path") == str(report_path)
            and request.get("baseline_report_sha256") == report_sha
            and payload.get("report_path") == str(report_path)
            and payload.get("report_sha256") == report_sha
            and payload.get("revision_request_path") == str(request_path)
            and payload.get("revision_request_sha256")
            == hashlib.sha256(request_path.read_bytes()).hexdigest()
        )
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid legacy trend cutover: {path}") from exc
    if not valid:
        raise ValueError(f"invalid legacy trend cutover: {path}")
    return True


def _complete_revision(
    config: DailyPremarketConfig,
    cycle: ControllerCycle,
    report: tuple[Path, dict[str, object]],
    now: datetime,
) -> None:
    request_path, completion_path = _revision_paths(
        config, cycle.market, cycle.as_of_date
    )
    request, completion = _revision_state(
        config,
        cycle.market,
        cycle.as_of_date,
        cycle.execution_date,
    )
    if request is None:
        raise RuntimeError("trend report revision request is missing")
    if completion is not None:
        return
    path, payload = report
    if (
        _report_order(path)[0] != cycle.as_of_date
        or _report_order(path)[1]
        <= max(0, int(request["baseline_revision"]))
        or not _valid_report(
            config, cycle.market, cycle.execution_date, path, payload
        )
        or _recovery_revision_for_report(
            config,
            cycle.market,
            (path, payload),
            require_receipt=True,
        )
        is not None
    ):
        raise ValueError(f"invalid completed trend report revision: {path}")
    _write_immutable(
        completion_path,
        _canonical_json_bytes({
            "schema_version": "open_trader.trend_controller.revision_completion.v1",
            "market": cycle.market,
            "as_of_date": cycle.as_of_date,
            "execution_date": cycle.execution_date,
            "request_path": str(request_path),
            "request_sha256": hashlib.sha256(request_path.read_bytes()).hexdigest(),
            "report_path": str(path),
            "report_sha256": _report_hash(payload),
            "completed_at": now.isoformat(timespec="seconds"),
        }),
    )


def _locked_report(
    config: DailyPremarketConfig,
    cycle: ControllerCycle,
    latest: tuple[Path, dict[str, object]],
    now: datetime,
) -> tuple[Path, dict[str, object]]:
    batch_path = _batch_path(config, cycle.market, cycle.execution_date)
    if not batch_path.exists():
        return latest
    batch = _read_json(batch_path, "trend execution batch")
    path = Path(str(batch.get("report_path") or ""))
    report = _read_json(path, "locked trend report")
    if (
        batch.get("schema_version") != "open_trader.trend_review.batch.v1"
        or batch.get("market") != cycle.market
        or batch.get("execution_date") != cycle.execution_date
        or not _valid_report(config, cycle.market, cycle.execution_date, path, report)
        or _report_hash(report) != batch.get("report_sha256")
    ):
        raise ValueError(f"invalid trend execution batch: {batch_path}")
    if _report_hash(latest[1]) != batch["report_sha256"]:
        _notify_once(
            f"{cycle.market} 趋势报告修订异常",
            "执行批次已锁定，后续报告不会改变当日自动操作。",
            (
                config,
                cycle.market,
                cycle.execution_date,
                "revision_after_batch_lock",
                "latest report SHA differs from locked batch",
                now.isoformat(timespec="seconds"),
            ),
        )
    return path, report


def _execution_due(cycle: ControllerCycle, now: datetime) -> bool:
    local = now.astimezone(TIMEZONES[cycle.market])
    execution_date = date.fromisoformat(cycle.execution_date)
    return local.date() > execution_date or (
        local.date() == execution_date
        and local.time().replace(tzinfo=None) >= BUY_WINDOWS[cycle.market][0]
    )


def _execution_completed(
    config: DailyPremarketConfig,
    cycle: ControllerCycle,
) -> bool:
    if _legacy_cycle_cutover(config, cycle):
        return True
    batch_path = _batch_path(config, cycle.market, cycle.execution_date)
    if not batch_path.exists():
        return False
    batch = _read_json(batch_path, "trend execution batch")
    report_path = Path(str(batch.get("report_path") or ""))
    report = _read_json(report_path, "locked trend report")
    report_sha = _report_hash(report)
    if (
        batch.get("schema_version") != "open_trader.trend_review.batch.v1"
        or batch.get("market") != cycle.market
        or batch.get("execution_date") != cycle.execution_date
        or batch.get("report_sha256") != report_sha
        or not _valid_report(
            config,
            cycle.market,
            cycle.execution_date,
            report_path,
            report,
        )
    ):
        raise ValueError(f"invalid trend execution batch: {batch_path}")

    judgments = report["strategy_judgments"]
    actions = judgments["formal_actions"]
    if not actions:
        return True

    sell_symbols = {
        str(action.get("symbol") or "").strip()
        for action in actions
        if action.get("action") == "SELL_ALL"
    }
    for action in actions:
        action_name = str(action.get("action") or "")
        symbol = str(action.get("symbol") or "").strip()
        if action_name == "BUY" and symbol in sell_symbols:
            continue
        events, resolutions = load_trend_action_audit(
            config.data_dir,
            market=cycle.market,
            execution_date=cycle.execution_date,
            symbol=symbol,
            side="buy" if action_name == "BUY" else "sell",
        )
        if any(
            item.get("resolution") in {"confirm-submitted", "abandon"}
            for item in resolutions
        ):
            continue
        if action_name == "BUY" and any(
            item.get("status") in {"filled", "missed"} for item in events
        ):
            continue
        if action_name == "SELL_ALL" and any(
            item.get("status") == "filled"
            or item.get("status") == "incomplete"
            and item.get("reason") == "position_zero_confirmed"
            for item in events
        ):
            continue
        return False
    return True


def _durable_report_cycles(
    config: DailyPremarketConfig,
    cycle: ControllerCycle,
    now: datetime,
) -> list[ControllerCycle]:
    paths = set(_report_dir(config, cycle.market).glob("*.json"))
    batch_root = (
        config.data_dir
        / "trend_review"
        / "ledgers"
        / cycle.market
        / "batches"
    )
    for batch_path in batch_root.glob("*.json"):
        batch = _read_json(batch_path, "trend execution batch")
        report_path = Path(str(batch.get("report_path") or ""))
        if report_path.exists():
            paths.add(report_path)

    cycles: dict[str, ControllerCycle] = {}
    for path in paths:
        try:
            report = _read_json(path, "trend report")
            as_of = date.fromisoformat(str(report["as_of_date"]))
            execution = date.fromisoformat(str(report["execution_date"]))
        except (KeyError, TypeError, ValueError):
            continue
        metadata = report.get("metadata")
        if (
            not isinstance(metadata, dict)
            or str(metadata.get("market") or "").upper() != cycle.market
            or as_of >= execution
            or execution.isoformat() >= cycle.execution_date
        ):
            continue
        cycles[execution.isoformat()] = ControllerCycle(
            market=cycle.market,
            as_of_date=as_of.isoformat(),
            execution_date=execution.isoformat(),
            report_run_date=(
                (as_of + timedelta(days=1)).isoformat()
                if cycle.market == "US"
                else as_of.isoformat()
            ),
            session="catchup",
            market_open=False,
            next_check_at=now + timedelta(seconds=5),
        )
    return sorted(cycles.values(), key=lambda item: item.execution_date)


def _cycle_to_reconcile(
    config: DailyPremarketConfig,
    cycle: ControllerCycle,
    now: datetime,
) -> ControllerCycle:
    durable = _durable_report_cycles(config, cycle, now)
    completion: dict[str, bool] = {}
    if durable:
        for item in durable:
            try:
                completion[item.execution_date] = _execution_completed(
                    config, item
                )
            except ValueError:
                completion[item.execution_date] = False
        unfinished = [
            item for item in durable if not completion[item.execution_date]
        ]
        if unfinished:
            oldest = unfinished[0]
            completed_before = [
                item
                for item in durable
                if item.execution_date < oldest.execution_date
                and completion[item.execution_date]
            ]
            cursor = completed_before[-1] if completed_before else oldest
        else:
            cursor = durable[-1]
    else:
        local = now.astimezone(TIMEZONES[cycle.market])
        as_of = date.fromisoformat(cycle.as_of_date)
        cursor = _derive_cycle(
            config,
            cycle.market,
            local.replace(
                year=as_of.year,
                month=as_of.month,
                day=as_of.day,
                hour=9,
                minute=31,
                second=0,
                microsecond=0,
            ),
        )

    for _ in range(10):
        if cursor.execution_date >= cycle.execution_date:
            return cycle
        if cursor.execution_date in completion:
            if not completion[cursor.execution_date]:
                return cursor
        elif not _execution_completed(config, cursor):
            return cursor
        execution = date.fromisoformat(cursor.execution_date)
        local = now.astimezone(TIMEZONES[cycle.market])
        next_cycle = _derive_cycle(
            config,
            cycle.market,
            local.replace(
                year=execution.year,
                month=execution.month,
                day=execution.day,
                hour=23,
                minute=0,
                second=0,
                microsecond=0,
            ),
        )
        if next_cycle.execution_date <= cursor.execution_date:
            raise RuntimeError("trend calendar catch-up did not advance")
        cursor = next_cycle
    return cycle


def run_trend_market_controller(
    config: DailyPremarketConfig,
    market: str,
    *,
    revision: bool = False,
    once: bool = False,
    now_fn: Callable[[], datetime] = datetime.now,
    sleep_fn: Callable[[float], None] = sleep,
) -> dict[str, object]:
    market = _market(market)
    process_version = _process_version(config.repo)
    mode = trend_execution_mode(config, hostname_fn=socket.gethostname)
    initial_now = _localized(now_fn(), config.timezone)
    if mode.mode == "readonly":
        return _status_payload(
            config,
            market,
            now=initial_now,
            phase="readonly",
            last_success=None,
            blocker=mode.reason,
            next_check_at=initial_now,
            fixed_process_version=process_version,
        )

    if revision:
        current_cycle = _derive_cycle(config, market, initial_now)
        revision_cycle = _cycle_to_reconcile(
            config, current_cycle, initial_now
        )
        _request_revision(config, revision_cycle, initial_now)

    lock_path = config.data_dir / "runs" / f".trend_market_controller.{market}.lock"
    try:
        lock = RunLock(lock_path)
        lock.__enter__()
    except RuntimeError:
        if revision:
            return _status_payload(
                config,
                market,
                now=initial_now,
                phase="revision_requested",
                last_success=None,
                blocker=None,
                next_check_at=revision_cycle.next_check_at,
                fixed_process_version=process_version,
            )
        raise

    pool = ThreadPoolExecutor(
        max_workers=1,
        thread_name_prefix=f"trend-report-{market}",
    )
    future: Future[None] | None = None
    report_target: ReportTask | None = None
    report_failures = 0
    report_retry_after: datetime | None = None
    report_blocker: str | None = None
    operation_failures = 0
    operation_retry_after: datetime | None = None
    operation_blocker: str | None = None
    cycle_failures = 0
    cycle_retry_after: datetime | None = None
    cycle_blocker: str | None = None
    last_success: object = None
    try:
        _record_status(
            config,
            market,
            now=initial_now,
            phase="starting",
            last_success=None,
            blocker=None,
            next_check_at=initial_now + timedelta(seconds=5),
            fixed_process_version=process_version,
        )
        while True:
            now = _localized(now_fn(), config.timezone)
            _record_status(
                config,
                market,
                now=now,
                phase="reconciling",
                last_success=last_success,
                blocker=cycle_blocker or report_blocker or operation_blocker,
                next_check_at=now + timedelta(seconds=5),
                fixed_process_version=process_version,
            )
            local = now.astimezone(TIMEZONES[market])
            local_session = (
                cn_session(local)
                if market == "CN"
                else market_session(local, market)
            )
            protection_error: str | None = None
            if local_session in {"morning", "afternoon", "open"}:
                try:
                    protection_error = _protection_blocker(
                        _run_protection_pass(
                            config, market, local.date().isoformat()
                        )
                    )
                except Exception as exc:
                    protection_error = f"protection pass failed: {exc}"
            if cycle_retry_after is not None and now < cycle_retry_after:
                status_payload = _record_status(
                    config,
                    market,
                    now=now,
                    phase="blocked",
                    last_success=last_success,
                    blocker=cycle_blocker,
                    next_check_at=cycle_retry_after,
                    fixed_process_version=process_version,
                )
                if once:
                    return status_payload
                sleep_fn(5)
                continue
            try:
                cycle = _derive_cycle(config, market, now)
            except Exception as exc:
                cycle_failures += 1
                cycle_retry_after = _retry_at(now, cycle_failures)
                cycle_blocker = str(exc)
                _notify_once(
                    f"{market} 趋势控制器阻塞",
                    cycle_blocker,
                    (
                        config,
                        market,
                        now.astimezone(TIMEZONES[market]).date().isoformat(),
                        "calendar",
                        cycle_blocker,
                        now.isoformat(timespec="seconds"),
                    ),
                )
                status_payload = _record_status(
                    config,
                    market,
                    now=now,
                    phase="blocked",
                    last_success=last_success,
                    blocker=cycle_blocker,
                    next_check_at=cycle_retry_after,
                    fixed_process_version=process_version,
                )
                if once:
                    return status_payload
                sleep_fn(5)
                continue
            cycle_failures = 0
            cycle_retry_after = None
            cycle_blocker = None
            phase = "monitoring" if cycle.market_open else cycle.session
            blocker = protection_error
            if blocker is not None:
                phase = "blocked"
            work_cycle = report_target.cycle if report_target else cycle
            latest: tuple[Path, dict[str, object]] | None = None
            try:
                if report_target is None:
                    work_cycle = _cycle_to_reconcile(config, cycle, now)
                request, completion = _revision_state(
                    config,
                    market,
                    work_cycle.as_of_date,
                    work_cycle.execution_date,
                )
                if report_target is not None:
                    revision_pending = report_target.completes_revision_request
                else:
                    revision_pending = request is not None and completion is None
                if revision_pending:
                    assert request is not None
                    latest = _pending_revision_report(
                        config, work_cycle, request
                    )
                else:
                    latest = _load_cycle_report(config, work_cycle)
                recovery_revision = (
                    _recovery_revision_for_report(
                        config,
                        market,
                        latest,
                        require_receipt=revision_pending,
                    )
                    if latest is not None
                    else None
                )
                if (
                    revision_pending
                    and latest is not None
                    and recovery_revision is None
                    and future is None
                ):
                    assert latest is not None
                    _complete_revision(config, work_cycle, latest, now)
                    revision_pending = False
                    report_target = None
                can_start = (
                    report_retry_after is None or now >= report_retry_after
                )
                if (
                    future is None
                    and can_start
                    and (latest is None or recovery_revision is not None)
                ):
                    if revision_pending and _batch_path(
                        config, market, work_cycle.execution_date
                    ).exists():
                        raise ValueError(
                            "trend report revision rejected: execution has begun"
                        )
                    generator_revision = (
                        recovery_revision
                        if recovery_revision is not None
                        else revision_pending
                    )
                    future = pool.submit(
                        _generate_report,
                        config,
                        market,
                        work_cycle.report_run_date,
                        generator_revision,
                    )
                    report_target = ReportTask(
                        cycle=work_cycle,
                        completes_revision_request=revision_pending,
                    )

                if future is not None and (future.done() or once):
                    report_cycle = report_target.cycle if report_target else cycle
                    try:
                        future.result(timeout=1 if once else None)
                    except TimeoutError:
                        phase = "recovering_report"
                    except Exception as exc:
                        report_failures += 1
                        report_retry_after = _retry_at(now, report_failures)
                        report_blocker = f"report generation failed: {exc}"
                        blocker = report_blocker
                        phase = "recovering_report"
                        future = None
                    else:
                        future = None
                        report_failures = 0
                        report_retry_after = None
                        report_blocker = None
                        if report_target and report_target.completes_revision_request:
                            assert request is not None
                            latest = _pending_revision_report(
                                config, report_cycle, request
                            )
                        else:
                            latest = _load_cycle_report(config, report_cycle)
                        if latest is None:
                            raise RuntimeError(
                                "report generation completed without a valid report"
                            )
                        if (
                            _recovery_revision_for_report(
                                config,
                                market,
                                latest,
                                require_receipt=(
                                    report_target.completes_revision_request
                                    if report_target
                                    else False
                                ),
                            )
                            is not None
                        ):
                            raise RuntimeError(
                                "report delivery recovery did not complete"
                            )
                        if report_target and report_target.completes_revision_request:
                            _complete_revision(config, report_cycle, latest, now)
                        work_cycle = report_cycle
                        report_target = None

                blocker = report_blocker or blocker
                operation_delayed = (
                    operation_retry_after is not None
                    and now < operation_retry_after
                )
                if operation_delayed:
                    blocker = report_blocker or operation_blocker
                    phase = "blocked"
                elif future is not None or report_target is not None:
                    phase = "recovering_report"
                elif latest is None:
                    phase = "recovering_report"
                elif _execution_due(work_cycle, now):
                    selected = _locked_report(config, work_cycle, latest, now)
                    if protection_error is not None:
                        execution = _execute_locked_report(
                            config,
                            market,
                            work_cycle.execution_date,
                            selected[0],
                            selected[1],
                            allow_new_buys=False,
                        )
                    else:
                        execution = _execute_locked_report(
                            config,
                            market,
                            work_cycle.execution_date,
                            selected[0],
                            selected[1],
                        )
                    last_success = execution
                    operation_failures = 0
                    operation_retry_after = None
                    operation_blocker = None
                    status = str(execution.get("status") or "")
                    if status in {"uncertain", "conflict"}:
                        blocker = status
                        phase = status
                        _notify_once(
                            f"{market} 趋势订单 {status}",
                            "自动提交已停止，请核对不可变账本与 Futu 订单。",
                            (
                                config,
                                market,
                                work_cycle.execution_date,
                                "execution",
                                status,
                                now.isoformat(timespec="seconds"),
                            ),
                        )
                    elif status == "missed_window":
                        phase = "missed"
                        _notify_once(
                            f"{market} 趋势买入已错过窗口",
                            "报告已保留，未完成的买入不会追单。",
                            (
                                config,
                                market,
                                work_cycle.execution_date,
                                "opening_actions",
                                "buy_window_closed",
                                now.isoformat(timespec="seconds"),
                            ),
                        )
                    else:
                        phase = (
                            "blocked"
                            if protection_error is not None
                            else "monitoring"
                            if cycle.market_open
                            else cycle.session
                        )
                    report_target = None

                close_due = (
                    cycle.session == "closed"
                    or now.astimezone(TIMEZONES[market]).date()
                    > date.fromisoformat(cycle.as_of_date)
                )
                if (
                    close_due
                    and not operation_delayed
                    and not _close_completed(config, market, cycle.as_of_date)
                    and _load_report_for_as_of(
                        config, market, cycle.as_of_date
                    ) is not None
                ):
                    _capture_close(config, market, cycle.as_of_date)
                    _complete_close(config, market, cycle.as_of_date, now)
                    if last_success is None or cycle.session == "closed":
                        last_success = {
                            "status": "close_captured",
                            "date": cycle.as_of_date,
                        }
                    operation_failures = 0
                    operation_retry_after = None
                    operation_blocker = None
                    if cycle.session == "closed":
                        phase = "closed"
            except Exception as exc:
                operation_failures += 1
                operation_retry_after = _retry_at(now, operation_failures)
                operation_blocker = str(exc)
                blocker = str(exc)
                if "invalid frozen trend report" in blocker:
                    phase = "blocked"
                elif phase != "recovering_report":
                    phase = "blocked"
                _notify_once(
                    f"{market} 趋势控制器阻塞",
                    blocker,
                    (
                        config,
                        market,
                        cycle.execution_date,
                        "controller",
                        blocker,
                        now.isoformat(timespec="seconds"),
                    ),
                )
                blocker = cycle_blocker or report_blocker or operation_blocker

            next_check = (
                operation_retry_after
                or report_retry_after
                or cycle.next_check_at
            )
            status_payload = _record_status(
                config,
                market,
                now=now,
                phase=phase,
                last_success=last_success,
                blocker=blocker,
                next_check_at=next_check,
                fixed_process_version=process_version,
            )
            if once:
                return status_payload
            sleep_fn(5)
    finally:
        pool.shutdown(wait=not once, cancel_futures=True)
        lock.__exit__(None, None, None)
