from __future__ import annotations

import hashlib
import json
import os
import re
import socket
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from contextlib import suppress
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile
from time import sleep
from zoneinfo import ZoneInfo

from .a_share_trend import (
    _process_version,
    load_futu_simulate_trend_account,
    read_delivery_receipt,
    run_a_share_trend_report,
    valid_serialized_account,
    valid_frozen_report_contract,
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
from .futu_quote import FutuQuoteClient, FutuQuoteError
from .futu_symbols import to_futu_symbol
from .kelly_order_execution import (
    ExecutorGuardedOrderClient,
    FutuOrderExecutionError,
    FutuSimulateOrderExecutionClient,
)
from .market_trend import market_paths, run_market_trend_report
from .trend_allocation import allocation_reference_for_report
from .market_trend_watch import (
    MARKET_TIMEZONES,
    market_session,
    watch_market_protection,
)
from .notification_policy import (
    BROKER_LABELS,
    MARKET_LABELS,
    brief_zh_detail,
    group_order_alerts,
    render_attention,
    render_order_alert,
)
from .opend_incident import (
    OpenDIncidentStateError,
    classify_opend_error,
    record_opend_failure,
    record_opend_health,
)
from .tiger_account import load_tiger_account_config
from .trend_api_stats import (
    STATISTICS_CYCLE_SCHEMA,
    FutuSimulateFillClient,
    TigerActualFillClient,
    _read_optional_json,
    run_trend_statistics_cycle,
    trend_statistics_cycle_path,
)
from .trend_review import (
    _canonical_json_bytes,
    _report_hash,
    _write_immutable,
    benchmark_fact,
    build_trend_review_projection,
    capture_trend_review_close,
    execute_relative_rotations,
    execute_trend_review_open,
    execute_trend_review_stop,
    long_term_benchmark_cycle_path,
    load_trend_action_audit,
    lock_trend_execution_batch,
    overheat_trim_progress,
    _preflight_open_actions,
    record_trend_review_missed_buys,
    refresh_long_term_benchmark,
    relative_rotations_completed,
    trend_action_futu_symbol,
)
from .trend_statement_consumer import consume_accepted_statement_facts


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
    allocation_reference: Mapping[str, object] | None = None


class ReportGenerationError(RuntimeError):
    def __init__(
        self, message: str, waiting_reason: str | None = None
    ) -> None:
        super().__init__(message)
        self.waiting_reason = waiting_reason


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


def _trend_review_projection_current(
    config: DailyPremarketConfig, market: str
) -> bool:
    try:
        projection = _read_json(
            config.data_dir / "latest" / f"trend_review_{market.lower()}.json",
            "trend review projection",
        )
    except ValueError:
        return False
    return projection.get("schema_version") == "open_trader.trend_review.projection.v5"


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
    waiting: object = None,
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
        "waiting": waiting,
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
    waiting: object = None,
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
        waiting=waiting,
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
    config: DailyPremarketConfig,
    market: str,
    now: datetime,
    *,
    quote_client: object | None = None,
) -> ControllerCycle:
    market = _market(market)
    if now.tzinfo is None or now.utcoffset() is None:
        now = now.replace(tzinfo=ZoneInfo(config.timezone))
    timezone = TIMEZONES[market]
    local = now.astimezone(timezone)
    today = local.date()
    owns_quote = quote_client is None
    quote = quote_client
    if quote is None:
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
        if owns_quote:
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
    if not valid_frozen_report_contract(payload):
        return False
    allocation = payload.get("allocation")
    if allocation is not None:
        assert isinstance(allocation, Mapping)
        try:
            daily_path = PurePosixPath(str(allocation["daily_path"]))
            daily = config.data_dir / daily_path.relative_to("data")
            body = daily.read_bytes()
            snapshot = json.loads(body)
        except (KeyError, OSError, ValueError, json.JSONDecodeError):
            return False
        if (
            hashlib.sha256(body).hexdigest() != allocation.get("sha256")
            or not isinstance(snapshot, Mapping)
            or any(
                snapshot.get(key) != allocation.get(key)
                for key in ("allocation_date", "generated_at", "roots", "markets")
            )
        ):
            return False
    try:
        _preflight_open_actions(payload, market)
    except ValueError:
        return False
    for action in actions:
        if (
            not isinstance(action, dict)
            or action.get("action") not in {"BUY", "SELL_ALL", "SELL_PARTIAL"}
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
    config: DailyPremarketConfig,
    market: str,
    run_date: str,
    revision: bool,
    allocation_reference: Mapping[str, object] | None = None,
) -> None:
    require_trend_executor(config, hostname_fn=socket.gethostname)
    notifier = build_notifier(config)
    result = (
        run_a_share_trend_report(
            config=config,
            run_date=run_date,
            revision=revision,
            notifier=notifier,
            allocation_reference=allocation_reference,
        )
        if market == "CN"
        else run_market_trend_report(
            config=config,
            market=market,
            run_date=run_date,
            revision=revision,
            notifier=notifier,
            allocation_reference=allocation_reference,
        )
    )
    if result.status not in {"generated", "existing", "holiday"}:
        raise ReportGenerationError(
            f"{market} trend report generation returned {result.status}"
            + (f": {result.waiting_reason}" if result.waiting_reason else ""),
            waiting_reason=result.waiting_reason,
        )


def _trend_waiting_reason(
    *,
    phase: str,
    execution_date: str,
    latest: object,
    report_waiting: str | None,
    blocker: object,
) -> object:
    if phase == "holiday":
        return (
            f"下一执行日 {execution_date} 报告已产出，今日休市无需重跑"
        )
    if phase == "recovering_report" and latest is None:
        waiting = f"下一执行日 {execution_date} 报告未产出，正在补产"
        if report_waiting:
            waiting = f"{waiting}：{report_waiting}"
        return waiting
    return blocker


def _run_cycle_statistics(
    config: DailyPremarketConfig,
    cycle: ControllerCycle,
    now: datetime,
    process_version: str,
) -> dict[str, object]:
    state_path = trend_statistics_cycle_path(
        config.data_dir, cycle.market, cycle.as_of_date
    )
    state = _read_optional_json(state_path)
    if state.get("status") == "completed":
        return {**state, "status": "already_completed"}

    futu = FutuSimulateFillClient(
        host=config.futu_host,
        port=config.futu_port,
        simulate_acc_id=require_trend_review_config(config, cycle.market),
        trd_market=cycle.market,
    )
    tiger = None
    try:
        if cycle.market == "US":
            tiger = TigerActualFillClient(
                config=load_tiger_account_config(
                    config_dir=Path("~/.tigeropen/"),
                    account=None,
                    sandbox=False,
                )
            )
        return run_trend_statistics_cycle(
            data_dir=config.data_dir,
            reports_dir=config.reports_dir,
            market=cycle.market,
            as_of_date=cycle.as_of_date,
            generated_at=now.isoformat(timespec="seconds"),
            process_git_sha=process_version,
            futu_client=futu,
            tiger_client=tiger,
        )
    finally:
        try:
            futu.close()
        finally:
            if tiger is not None:
                tiger.close()


def _run_cycle_long_term_benchmark(
    config: DailyPremarketConfig,
    cycle: ControllerCycle,
    now: datetime,
    process_version: str,
    quote_client: object,
) -> dict[str, object]:
    return refresh_long_term_benchmark(
        config.data_dir,
        cycle.market,
        quote_client,
        now=now,
        process_git_sha=process_version,
    )


def _record_long_term_benchmark_exception(
    config: DailyPremarketConfig,
    cycle: ControllerCycle,
    now: datetime,
    process_version: str,
    error: BaseException,
) -> dict[str, object]:
    month = now.astimezone(TIMEZONES[cycle.market]).strftime("%Y-%m")
    path = long_term_benchmark_cycle_path(config.data_dir, cycle.market, month)
    with RunLock(path.with_suffix(".lock"), wait=True):
        previous = _read_optional_json(path)
        if previous.get("status") == "completed":
            return {**previous, "status": "already_completed"}
        state = {
            "schema_version": "open_trader.trend_review.long_term_benchmark.attempt.v1",
            "status": "failed",
            "market": cycle.market,
            "month": month,
            "attempt_count": int(previous.get("attempt_count", 0)) + 1,
            "attempted_at": now.isoformat(timespec="seconds"),
            "process_git_sha": process_version,
            "reason": str(error),
        }
        _write_notification_state(path, state)
        return state


def _allocation_reference_for_cycle(
    config: DailyPremarketConfig,
    *,
    now: datetime,
    quote_client: object,
) -> Mapping[str, object] | None:
    """Use the single Shanghai allocation decision; reports never produce one."""
    if not config.trend_animals_api_key:
        return None
    if now.tzinfo is None or now.utcoffset() is None:
        now = now.replace(tzinfo=ZoneInfo(config.timezone))
    allocation_date = now.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
    day = date.fromisoformat(allocation_date)
    a_trading_days = quote_client.get_trading_days(
        market="CN",
        start=(day - timedelta(days=35)).isoformat(),
        end=(day + timedelta(days=1)).isoformat(),
    )
    return allocation_reference_for_report(
        config, allocation_date=allocation_date, a_trading_days=a_trading_days
    )


def _gate_futu_trade_context(
    config: DailyPremarketConfig,
    market: str,
    *,
    quote_client: object | None = None,
) -> None:
    owns_quote = quote_client is None
    quote = quote_client
    if quote is None:
        quote = FutuQuoteClient(host=config.futu_host, port=config.futu_port)
    try:
        trading_date = datetime.now(TIMEZONES[market]).date().isoformat()
        quote.get_trading_days(
            market=market,
            start=trading_date,
            end=trading_date,
            use_cache=False,
        )
    finally:
        if owns_quote:
            quote.close()


def _new_order_client(
    config: DailyPremarketConfig,
    market: str,
    quote_client: object | None = None,
) -> object:
    account_id = require_trend_review_config(config, market)
    _gate_futu_trade_context(config, market, quote_client=quote_client)
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
    *,
    quote_client: object | None = None,
) -> dict[str, object]:
    client = _new_order_client(config, market, quote_client)
    try:
        result = execute_trend_review_stop(
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
    if result.get("status") == "uncertain":
        _notify_feishu_once(
            f"{market} 保护卖出待人工确认",
            "保护触发已记录，但先前卖出订单状态不确定；请立即核对 Futu 持仓和订单。",
            (
                config,
                market,
                str(event.get("trading_date") or ""),
                "protection_sell",
                "uncertain",
                str(event.get("occurred_at") or ""),
            ),
        )
    return result


def _run_protection_pass(
    config: DailyPremarketConfig,
    market: str,
    trading_date: str,
    *,
    quote_client: object | None = None,
    account_loader: Callable[..., object] | None = None,
) -> object:
    require_trend_executor(config, hostname_fn=socket.gethostname)
    notifier = build_notifier(config)

    if account_loader is None:
        account_id = require_trend_review_config(config, market)

        def account_loader(
            _path: Path, *, expected_date: str, timezone: ZoneInfo
        ) -> object:
            del timezone
            _gate_futu_trade_context(
                config, market, quote_client=quote_client
            )
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
    callback = lambda event: _run_stop(
        config, market, event, quote_client=quote_client
    )
    if market == "CN":
        return watch_a_share_protection(
            portfolio_path=config.portfolio,
            state_path=config.data_dir / "trend_a_share/protection_state.json",
            events_path=config.data_dir / "trend_a_share/watch_events.jsonl",
            report_lock_path=config.data_dir / "runs/.trend_a_share_report.lock",
            quote_client=quote_client,
            close_quote_client=quote_client is None,
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
        quote_client=quote_client,
        close_quote_client=quote_client is None,
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
        status not in {"completed", "holiday"}
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
    quote_client: object | None = None,
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
    rotation_pairs = judgments.get("simulate_rotation_pairs", [])
    if not actions and not rotation_pairs:
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
    ) if actions else 0
    sell_symbols = {
        trend_action_futu_symbol(locked_report, action, market)
        for action in actions
        if action.get("action") in {"SELL_ALL", "SELL_PARTIAL"}
    }
    eligible_buys = sum(
        action.get("action") == "BUY"
        and trend_action_futu_symbol(locked_report, action, market)
        not in sell_symbols
        for action in actions
    )
    if actions and missed == eligible_buys == len(actions) and not rotation_pairs:
        return {
            "status": "missed_window",
            "market": market,
            "date": execution_date,
            "submitted_count": 0,
            "artifact_paths": [],
        }
    symbols = sorted(
        {
            trend_action_futu_symbol(locked_report, action, market)
            for action in actions
            if (
                allow_new_buys
                and action["action"] == "BUY"
                and trend_action_futu_symbol(locked_report, action, market)
                not in sell_symbols
            )
        } | {
            str(pair.get("buy_futu_symbol") or "")
            for pair in rotation_pairs
            if allow_new_buys
            and isinstance(pair, Mapping)
            and str(pair.get("buy_futu_symbol") or "")
        }
    )
    quote = quote_client
    owns_quote = False
    prices: dict[str, Decimal] = {}
    client = None
    try:
        if symbols:
            try:
                if quote is None:
                    quote = FutuQuoteClient(
                        host=config.futu_host, port=config.futu_port
                    )
                    owns_quote = True
                prices = {
                    symbol: snapshot.last_price
                    for symbol, snapshot in quote.get_snapshots(symbols).items()
                }
            except Exception:
                prices = {}
        client = _new_order_client(
            config, market, quote_client if quote_client is not None else quote
        )
        ordinary = execute_trend_review_open(
            data_dir=config.data_dir,
            report=locked_report,
            client=client,
            market=market,
            execution_date=execution_date,
            now=now,
            quote_prices=prices,
        )
        if allow_new_buys and ordinary.get("status") == "quote_unavailable":
            raise RuntimeError("current quote unavailable for pending trend buy")
        ordinary_complete = not actions or _execution_completed(
            config,
            ControllerCycle(
                market=market,
                as_of_date=as_of_date,
                execution_date=execution_date,
                report_run_date=str(locked_report.get("generated_at") or "")[:10],
                session="execution",
                market_open=True,
                next_check_at=datetime.fromisoformat(now),
            ),
            include_rotations=False,
        )
        rotation = (
            execute_relative_rotations(
                data_dir=config.data_dir,
                report=locked_report,
                client=client,
                market=market,
                execution_date=execution_date,
                now=now,
                quote_prices=prices,
            )
            if allow_new_buys and rotation_pairs and ordinary_complete
            else {
                "status": "unchanged", "submitted_count": 0,
                "artifact_paths": [],
            }
        )
        return {
            "status": (
                rotation.get("status")
                if rotation.get("status") != "unchanged"
                else ordinary.get("status")
            ),
            "market": market,
            "date": execution_date,
            "submitted_count": int(ordinary.get("submitted_count") or 0)
            + int(rotation.get("submitted_count") or 0),
            "artifact_paths": [
                *ordinary.get("artifact_paths", []),
                *rotation.get("artifact_paths", []),
            ],
        }
    finally:
        if quote is not None and owns_quote:
            quote.close()
        if client is not None:
            client.close()


def _capture_close(
    config: DailyPremarketConfig,
    market: str,
    trading_date: str,
    *,
    quote_client: object | None = None,
    account_client: object | None = None,
    account_client_factory: Callable[[], object] | None = None,
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
    quote = quote_client
    client = account_client
    owns_quote = quote is None
    owns_client = client is None and account_client_factory is None
    try:
        if quote is None:
            quote = FutuQuoteClient(host=config.futu_host, port=config.futu_port)
        if client is None:
            client = (
                account_client_factory()
                if account_client_factory is not None
                else _new_order_client(config, market, quote)
            )
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
        if quote is not None and owns_quote:
            quote.close()
        if client is not None and owns_client:
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


def _notification_key(
    key: object,
) -> tuple[DailyPremarketConfig, str, str, str, str, str]:
    if not (
        isinstance(key, tuple)
        and len(key) == 6
        and isinstance(key[0], DailyPremarketConfig)
        and all(isinstance(value, str) for value in key[1:])
    ):
        raise ValueError("invalid trend controller notification key")
    config, market, execution_date, action, reason, occurred_at = key
    assert isinstance(config, DailyPremarketConfig)
    assert all(
        isinstance(value, str)
        for value in (market, execution_date, action, reason, occurred_at)
    )
    return config, market, execution_date, action, reason, occurred_at


def _write_notification_state(path: Path, state: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp: Path | None = None
    try:
        with NamedTemporaryFile(
            "wb",
            delete=False,
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        ) as handle:
            temp = Path(handle.name)
            handle.write(_canonical_json_bytes(state))
        os.replace(temp, path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def _statistics_notification_path(
    config: DailyPremarketConfig,
    cycle: ControllerCycle,
    action: str,
) -> Path:
    identity = "|".join(
        (cycle.market, cycle.as_of_date, action, "statistics_cycle")
    )
    return (
        _controller_root(config, cycle.market)
        / "notifications"
        / cycle.as_of_date
        / f"{hashlib.sha256(identity.encode('utf-8')).hexdigest()}.json"
    )


def _statistics_notification_time(
    config: DailyPremarketConfig,
    cycle: ControllerCycle,
    action: str,
) -> str | None:
    path = _statistics_notification_path(config, cycle, action)
    if not path.exists():
        return None
    occurred_at = _read_json(path, "trend statistics notification").get(
        "occurred_at"
    )
    return str(occurred_at or "") or None


def _update_statistics_cycle_state(
    config: DailyPremarketConfig,
    cycle: ControllerCycle,
    updates: Mapping[str, object],
) -> dict[str, object]:
    path = trend_statistics_cycle_path(config.data_dir, cycle.market, cycle.as_of_date)
    with RunLock(path.with_suffix(".lock"), wait=True):
        state = (
            _read_json(path, "trend statistics cycle")
            if path.exists()
            else {
                "schema_version": STATISTICS_CYCLE_SCHEMA,
                "market": cycle.market,
                "as_of_date": cycle.as_of_date,
            }
        )
        state.update(updates)
        _write_notification_state(path, state)
        return state


def _record_statistics_exception(
    config: DailyPremarketConfig,
    cycle: ControllerCycle,
    now: datetime,
    process_version: str,
    error: BaseException,
) -> dict[str, object]:
    path = trend_statistics_cycle_path(config.data_dir, cycle.market, cycle.as_of_date)
    with RunLock(path.with_suffix(".lock"), wait=True):
        previous = _read_optional_json(path)
        if previous.get("status") == "completed":
            return {**previous, "status": "already_completed"}
        state = {
            "schema_version": STATISTICS_CYCLE_SCHEMA,
            "status": "failed",
            "market": cycle.market,
            "as_of_date": cycle.as_of_date,
            "attempt_count": int(previous.get("attempt_count", 0)) + 1,
            "attempted_at": now.isoformat(timespec="seconds"),
            "process_git_sha": process_version,
            "reason": str(error),
        }
        for field in ("failure_notified_at", "recovery_notified_at"):
            if field in previous:
                state[field] = previous[field]
        _write_notification_state(path, state)
        return state


def _notify_statistics_result(
    config: DailyPremarketConfig,
    cycle: ControllerCycle,
    result: Mapping[str, object],
    now: datetime,
) -> dict[str, object]:
    status = str(result.get("status") or "")
    failure_at = _statistics_notification_time(
        config, cycle, "statistics_failed"
    )
    recovery_at = _statistics_notification_time(
        config, cycle, "statistics_recovered"
    )
    occurred_at = now.isoformat(timespec="seconds")
    if status == "failed" and failure_at is None:
        _notify_once(
            f"{cycle.market} 趋势统计刷新失败",
            str(result.get("reason") or "statistics refresh failed"),
            (
                config,
                cycle.market,
                cycle.as_of_date,
                "statistics_failed",
                "statistics_cycle",
                occurred_at,
            ),
        )
        failure_at = _statistics_notification_time(
            config, cycle, "statistics_failed"
        ) or occurred_at
    elif status in {"completed", "already_completed"} and failure_at is not None:
        if recovery_at is None:
            _notify_once(
                f"{cycle.market} 趋势统计刷新已恢复",
                "statistics refresh recovered",
                (
                    config,
                    cycle.market,
                    cycle.as_of_date,
                    "statistics_recovered",
                    "statistics_cycle",
                    occurred_at,
                ),
            )
            recovery_at = _statistics_notification_time(
                config, cycle, "statistics_recovered"
            ) or occurred_at

    updates: dict[str, object] = {}
    if failure_at is not None:
        updates["failure_notified_at"] = failure_at
        updates["recovery_notified_at"] = recovery_at
    return (
        _update_statistics_cycle_state(config, cycle, updates)
        if updates
        else dict(result)
    )


def _record_statement_statistics_diagnostic(
    config: DailyPremarketConfig,
    market: str,
    broker: str,
    result: Mapping[str, object],
    now: datetime,
) -> None:
    path = _controller_root(config, market) / "statement_statistics" / f"{broker}.json"
    diagnostic = dict(result)
    diagnostic.setdefault(
        "schema_version", "open_trader.trend.statement_consumption.v1"
    )
    diagnostic.setdefault("broker", broker)
    diagnostic.setdefault("attempted_at", now.isoformat(timespec="seconds"))
    _write_notification_state(path, diagnostic)


def _notification_retry_lock(path: Path) -> RunLock:
    return RunLock(path.with_suffix(".lock"))


def _controller_feishu_payload(
    title: str,
    message: str,
    *,
    market: str,
    execution_date: str,
    action: str,
) -> tuple[str, str]:
    broker = BROKER_LABELS[market]
    market_label = MARKET_LABELS[market]
    if action in {"statistics_failed", "statistics_recovered"}:
        recovered = action == "statistics_recovered"
        return render_attention(
            broker,
            f"{market_label}趋势统计{'已恢复' if recovered else '待恢复'}",
            execution_date,
            happened=(
                "趋势统计刷新已恢复" if recovered else "趋势统计刷新失败"
            ),
            impact="报告与执行继续使用最后一次已接受的统计快照",
            action=(
                "无需补跑报告"
                if recovered
                else "检查统计周期状态并等待自动重试"
            ),
            detail=brief_zh_detail(message),
        )
    if action == "revision_after_batch_lock":
        return render_attention(
            broker,
            f"{market_label}趋势报告修订异常",
            execution_date,
            happened="执行批次锁定后报告发生修订",
            impact="当日自动操作继续使用已锁定版本",
            action="核对冻结报告与修订记录",
            detail=brief_zh_detail(message),
        )
    if "复盘" in title:
        return render_attention(
            broker,
            f"{market_label}趋势复盘待恢复",
            execution_date,
            happened="趋势复盘未完成",
            impact="复盘数据暂未更新",
            action="检查 OpenD 与复盘账本后等待自动恢复",
            detail=brief_zh_detail(message),
        )
    return render_attention(
        broker,
        f"{market_label}趋势控制器阻塞",
        execution_date,
        happened="趋势控制器已进入阻塞状态",
        impact=f"{market_label}自动趋势流程暂停",
        action="检查 Dashboard 控制器状态与最近日志",
        detail=brief_zh_detail(message),
    )


def _notify_channels_once(
    key: object,
    *,
    non_feishu_payload: tuple[str, str] | None,
    feishu_payload: tuple[str, str] | None,
) -> bool:
    config, market, execution_date, action, reason, occurred_at = (
        _notification_key(key)
    )
    identity = "|".join((market, execution_date, action, reason))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    path = (
        _controller_root(config, market)
        / "notifications"
        / execution_date
        / f"{digest}.json"
    )
    legacy_path = (
        path.with_name(
            f"{hashlib.sha256('|'.join((market, execution_date, 'controller', reason)).encode('utf-8')).hexdigest()}.json"
        )
        if action == "review"
        else None
    )
    try:
        with _notification_retry_lock(path):
            if legacy_path is not None:
                with _notification_retry_lock(legacy_path):
                    if legacy_path.exists():
                        legacy_state = _read_json(
                            legacy_path, "trend controller notification"
                        )
                        if (
                            legacy_state.get("schema_version")
                            == "open_trader.trend_controller.notification.v2"
                            and "趋势复盘待恢复"
                            in str(legacy_state.get("feishu_title") or "")
                        ):
                            if any(
                                channel in {"feishu", "feishu_app"}
                                for channel in legacy_state.get("channels", [])
                            ):
                                return True
                            path = legacy_path
                            non_feishu_payload = None
            if path.exists():
                state = _read_json(path, "trend controller notification")
                if (
                    state.get("schema_version")
                    == "open_trader.trend_controller.notification.v1"
                ):
                    return True
                if (
                    state.get("schema_version")
                    != "open_trader.trend_controller.notification.v2"
                ):
                    raise ValueError(
                        f"invalid trend controller notification: {path}"
                    )
            else:
                state = {
                    "schema_version": "open_trader.trend_controller.notification.v2",
                    "market": market,
                    "execution_date": execution_date,
                    "action": action,
                    "reason": reason,
                    "occurred_at": occurred_at,
                    "non_feishu_attempted": False,
                    "feishu_attempts": 0,
                    "feishu_title": feishu_payload[0] if feishu_payload else "",
                    "feishu_message": feishu_payload[1] if feishu_payload else "",
                    "channels": [],
                }
            channels = [
                channel
                for channel in state.get("channels", [])
                if isinstance(channel, str)
            ]
            if (
                non_feishu_payload is not None
                and not state.get("non_feishu_attempted")
            ):
                try:
                    attempts = send_notification_with_results(
                        build_notifier(config),
                        *non_feishu_payload,
                        channels={"macos", "xiaoai"},
                    )
                except Exception:
                    attempts = []
                channels.extend(
                    attempt.channel
                    for attempt in attempts
                    if attempt.success and attempt.channel not in channels
                )
                state["non_feishu_attempted"] = True

            feishu_delivered = any(
                channel in {"feishu", "feishu_app"} for channel in channels
            )
            if (
                feishu_payload is not None
                and not feishu_delivered
                and int(state.get("feishu_attempts", 0)) < 2
            ):
                try:
                    attempts = send_notification_with_results(
                        build_notifier(config),
                        str(state["feishu_title"]),
                        str(state["feishu_message"]),
                        channels={"feishu", "feishu_app"},
                    )
                except Exception:
                    attempts = []
                channels.extend(
                    attempt.channel
                    for attempt in attempts
                    if attempt.success and attempt.channel not in channels
                )
                state["feishu_attempts"] = int(
                    state.get("feishu_attempts", 0)
                ) + 1

            state["channels"] = channels
            _write_notification_state(path, state)
            requested = []
            if non_feishu_payload is not None:
                requested.append(
                    any(channel in {"macos", "xiaoai"} for channel in channels)
                )
            if feishu_payload is not None:
                requested.append(
                    any(
                        channel in {"feishu", "feishu_app"}
                        for channel in channels
                    )
                )
            return bool(requested) and all(requested)
    except RuntimeError:
        return False


def _notify_once(title: str, message: str, key: object) -> bool:
    _, market, execution_date, action, _, _ = _notification_key(key)
    return _notify_channels_once(
        key,
        non_feishu_payload=(title, message),
        feishu_payload=_controller_feishu_payload(
            title,
            message,
            market=market,
            execution_date=execution_date,
            action=action,
        ),
    )


def _notify_non_feishu_once(title: str, message: str, key: object) -> bool:
    return _notify_channels_once(
        key,
        non_feishu_payload=(title, message),
        feishu_payload=None,
    )


def _notify_feishu_once(title: str, message: str, key: object) -> bool:
    return _notify_channels_once(
        key,
        non_feishu_payload=None,
        feishu_payload=(title, message),
    )


def _latest_action_events(
    config: DailyPremarketConfig,
    market: str,
    execution_date: str,
) -> list[Mapping[str, object]]:
    root = (
        config.data_dir
        / "trend_review"
        / "ledgers"
        / market
        / "actions"
        / execution_date
    )
    events: list[Mapping[str, object]] = []
    for action_dir in sorted(root.glob("*")):
        paths = sorted(action_dir.glob("*.json"))
        if not paths:
            continue
        try:
            event = json.loads(paths[-1].read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if isinstance(event, Mapping):
            events.append(event)
    return events


def _notify_order_groups(
    config: DailyPremarketConfig,
    market: str,
    execution_date: str,
    events: list[Mapping[str, object]],
    occurred_at: str,
) -> int:
    sent = 0
    for group in group_order_alerts(market, events):
        title, message = render_order_alert(
            group,
            broker_label=BROKER_LABELS[market],
            trading_date=execution_date,
        )
        key = (
            config,
            market,
            execution_date,
            f"order_{group.side}_{group.status}",
            f"{group.side}:{group.status}",
            occurred_at,
        )
        sent += int(_notify_feishu_once(title, message, key))
    return sent


def _notify_order_groups_or_batch_failure(
    config: DailyPremarketConfig,
    market: str,
    execution_date: str,
    events: list[Mapping[str, object]],
    occurred_at: str,
    status: str,
) -> int:
    if group_order_alerts(market, events):
        return _notify_order_groups(
            config, market, execution_date, events, occurred_at
        )
    title, message = render_attention(
        BROKER_LABELS[market],
        f"{MARKET_LABELS[market]}批次执行失败",
        execution_date,
        happened="订单执行异常，但无法从动作账本确认方向",
        impact="相关订单状态需要人工核对",
        action="查看不可变动作账本与控制器日志",
    )
    return int(_notify_feishu_once(
        title,
        message,
        (
            config,
            market,
            execution_date,
            "order_batch_failed",
            status,
            occurred_at,
        ),
    ))


def _notify_controller_failure(
    config: DailyPremarketConfig,
    market: str,
    execution_date: str,
    action: str,
    error: BaseException,
    occurred_at: datetime,
) -> bool:
    title = (
        f"{market} 趋势复盘待恢复"
        if action == "review"
        else f"{market} 趋势控制器阻塞"
    )
    notification_action = action
    reason = brief_zh_detail(str(error))
    category = classify_opend_error(error)
    occurred_text = occurred_at.isoformat(timespec="seconds")
    if category is None:
        identity = str(
            getattr(error, "error_type", error.__class__.__name__)
        )
        return _notify_once(
            title,
            str(error),
            (
                config,
                market,
                execution_date,
                notification_action,
                identity,
                occurred_text,
            ),
        )

    _notify_non_feishu_once(
        title,
        str(error),
        (
            config,
            market,
            execution_date,
            notification_action,
            str(error),
            occurred_text,
        ),
    )
    if category == "connectivity":
        problem = "OpenD 连接故障"
        happened = "OpenD 连接异常"
        next_action = "检查 OpenD 登录与网络"
    else:
        problem = "OpenD 请求限频"
        happened = "OpenD 请求已触发限频"
        next_action = "暂停手工重跑，等待限频窗口恢复"
    shared_title, shared_message = render_attention(
        "系统",
        problem,
        execution_date,
        happened=happened,
        impact="CN、HK、US 行情与订单监控可能中断",
        action=next_action,
        detail=reason,
    )

    def send_shared_feishu(title: str, message: str) -> str | None:
        attempts = send_notification_with_results(
            build_notifier(config),
            title,
            message,
            channels={"feishu", "feishu_app"},
        )
        return next(
            (attempt.channel for attempt in attempts if attempt.success), None
        )

    try:
        return record_opend_failure(
            data_dir=config.data_dir,
            market=market,
            category=category,
            reason=reason,
            occurred_at=occurred_at,
            send_feishu=send_shared_feishu,
            title=shared_title,
            message=shared_message,
        )
    except OpenDIncidentStateError:
        fallback_title, fallback_message = _controller_feishu_payload(
            title,
            reason,
            market=market,
            execution_date=execution_date,
            action=notification_action,
        )
        return _notify_feishu_once(
            fallback_title,
            fallback_message,
            (
                config,
                market,
                execution_date,
                notification_action,
                category,
                occurred_text,
            ),
        )


def _retry_pending_feishu_notifications(config: DailyPremarketConfig) -> None:
    for path in sorted(
        (config.data_dir / "trend_controller").glob(
            "*/notifications/**/*.json"
        )
    ):
        try:
            with _notification_retry_lock(path):
                try:
                    state = _read_json(path, "trend controller notification")
                except ValueError:
                    continue
                channels = [
                    channel
                    for channel in state.get("channels", [])
                    if isinstance(channel, str)
                ]
                if (
                    state.get("schema_version")
                    != "open_trader.trend_controller.notification.v2"
                    or state.get("feishu_attempts") != 1
                    or any(
                        channel in {"feishu", "feishu_app"}
                        for channel in channels
                    )
                ):
                    continue
                try:
                    attempts = send_notification_with_results(
                        build_notifier(config),
                        str(state.get("feishu_title") or ""),
                        str(state.get("feishu_message") or ""),
                        channels={"feishu", "feishu_app"},
                    )
                except Exception:
                    attempts = []
                channels.extend(
                    attempt.channel
                    for attempt in attempts
                    if attempt.success and attempt.channel not in channels
                )
                state["feishu_attempts"] = 2
                state["channels"] = channels
                _write_notification_state(path, state)
        except RuntimeError:
            continue


def _notify_protection_blocker(
    config: DailyPremarketConfig,
    market: str,
    trading_date: str,
    protection_error: str,
    occurred_at: str,
) -> bool:
    title, message = render_attention(
        BROKER_LABELS[market],
        f"{MARKET_LABELS[market]}保护监控阻塞",
        trading_date,
        happened="保护检查整体异常，已禁止新买入",
        impact=f"{MARKET_LABELS[market]}活动保护线无法完整检查",
        action="查看 Dashboard 风险状态并人工核价",
        detail=protection_error,
    )
    return _notify_feishu_once(
        title,
        message,
        (
            config,
            market,
            trading_date,
            "protection_monitor_blocked",
            "protection_pass_abnormal",
            occurred_at,
        ),
    )


def _revision_paths(
    config: DailyPremarketConfig, market: str, as_of_date: str
) -> tuple[Path, Path]:
    root = _controller_root(config, market)
    return (
        root / "revision_requests" / f"{as_of_date}.json",
        root / "revision_completions" / f"{as_of_date}.json",
    )


def _revision_migration_path(
    config: DailyPremarketConfig, market: str, as_of_date: str
) -> Path:
    return (
        _controller_root(config, market)
        / "revision_migrations"
        / f"{as_of_date}.json"
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


def _load_revision_migration(
    config: DailyPremarketConfig,
    market: str,
    as_of_date: str,
    execution_date: str,
    request_path: Path,
    request: Mapping[str, object],
    completion_path: Path,
    completion: Mapping[str, object],
    baseline_revision: int,
) -> dict[str, object] | None:
    path = _revision_migration_path(config, market, as_of_date)
    if not path.exists():
        return None
    migration = _read_json(path, "trend report revision migration")
    expected_keys = {
        "schema_version",
        "market",
        "as_of_date",
        "execution_date",
        "revision_request_path",
        "revision_request_sha256",
        "source_completion_path",
        "source_completion_sha256",
        "from_report_path",
        "from_report_sha256",
        "from_revision",
        "to_report_path",
        "to_report_sha256",
        "to_revision",
        "actor",
        "reason",
        "authorized_at",
        "accepted_git_sha",
    }
    if set(migration) != expected_keys:
        raise ValueError(f"invalid trend report revision migration: {path}")
    source_report_path = Path(str(completion.get("report_path") or ""))
    target_report_path = Path(str(migration.get("to_report_path") or ""))
    try:
        authorized_at = datetime.fromisoformat(str(migration["authorized_at"]))
        from_revision = migration["from_revision"]
        to_revision = migration["to_revision"]
        if (
            not isinstance(from_revision, int)
            or isinstance(from_revision, bool)
            or not isinstance(to_revision, int)
            or isinstance(to_revision, bool)
        ):
            raise ValueError("revision number must be an integer")
        target_report = _read_json(
            target_report_path, "migrated trend report revision"
        )
        target_recovery_revision = _recovery_revision_for_report(
            config,
            market,
            (target_report_path, target_report),
            require_receipt=True,
        )
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid trend report revision migration: {path}") from exc
    try:
        request_sha = hashlib.sha256(request_path.read_bytes()).hexdigest()
        completion_sha = hashlib.sha256(completion_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ValueError(f"invalid trend report revision migration: {path}") from exc
    valid = (
        migration.get("schema_version")
        == "open_trader.trend_controller.revision_migration.v1"
        and migration.get("market") == market
        and migration.get("as_of_date") == as_of_date
        and migration.get("execution_date") == execution_date
        and migration.get("revision_request_path") == str(request_path)
        and migration.get("revision_request_sha256") == request_sha
        and migration.get("source_completion_path") == str(completion_path)
        and migration.get("source_completion_sha256") == completion_sha
        and migration.get("from_report_path") == str(source_report_path)
        and migration.get("from_report_sha256") == completion.get("report_sha256")
        and migration.get("from_revision") == from_revision
        and migration.get("to_report_path") == str(target_report_path)
        and migration.get("to_report_sha256") == _report_hash(target_report)
        and migration.get("to_revision") == to_revision
        and _report_order(source_report_path)
        == (as_of_date, from_revision)
        and from_revision > max(0, baseline_revision)
        and _report_order(target_report_path) == (as_of_date, to_revision)
        and to_revision > from_revision
        and target_report_path.resolve().parent == _report_dir(config, market).resolve()
        and _valid_report(
            config, market, execution_date, target_report_path, target_report
        )
        and target_recovery_revision is None
        and isinstance(migration.get("actor"), str)
        and bool(migration["actor"])
        and migration["actor"] == migration["actor"].strip()
        and isinstance(migration.get("reason"), str)
        and bool(migration["reason"])
        and migration["reason"] == migration["reason"].strip()
        and authorized_at.tzinfo is not None
        and authorized_at.utcoffset() is not None
        and migration.get("authorized_at")
        == authorized_at.isoformat(timespec="seconds")
        and isinstance(migration.get("accepted_git_sha"), str)
        and bool(re.fullmatch(r"[0-9a-f]{40}", migration["accepted_git_sha"]))
    )
    if not valid:
        raise ValueError(f"invalid trend report revision migration: {path}")
    return migration


def _record_revision_migration(
    config: DailyPremarketConfig,
    cycle: ControllerCycle,
    target_report: tuple[Path, Mapping[str, object]],
    *,
    actor: str,
    reason: str,
    authorized_at: datetime,
    accepted_git_sha: str,
) -> Path:
    """Append an audited revision selection without rewriting report history."""
    require_trend_executor(config, hostname_fn=socket.gethostname)
    market = _market(cycle.market)
    path = _revision_migration_path(config, market, cycle.as_of_date)
    actor = actor.strip()
    reason = reason.strip()
    target_path, target_payload = target_report
    try:
        authorized_at = datetime.fromisoformat(
            authorized_at.isoformat(timespec="seconds")
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid trend report revision migration: {path}") from exc
    if path.exists():
        request_path, completion_path = _revision_paths(
            config, market, cycle.as_of_date
        )
        request, completion = _revision_state(
            config, market, cycle.as_of_date, cycle.execution_date
        )
        if request is None or completion is None:
            raise ValueError(f"invalid trend report revision migration: {path}")
        migration = _load_revision_migration(
            config,
            market,
            cycle.as_of_date,
            cycle.execution_date,
            request_path,
            request,
            completion_path,
            _read_json(completion_path, "trend report revision completion"),
            int(request["baseline_revision"]),
        )
        if migration is None:
            raise ValueError(f"invalid trend report revision migration: {path}")
        if (
            migration.get("to_report_path") != str(target_path)
            or migration.get("to_report_sha256") != _report_hash(target_payload)
            or migration.get("actor") != actor
            or migration.get("reason") != reason
            or migration.get("authorized_at")
            != authorized_at.isoformat(timespec="seconds")
            or migration.get("accepted_git_sha") != accepted_git_sha
        ):
            raise ValueError(f"immutable trend report revision migration collision: {path}")
        return path
    try:
        request_path, completion_path = _revision_paths(
            config, market, cycle.as_of_date
        )
        request, completion = _revision_state(
            config, market, cycle.as_of_date, cycle.execution_date
        )
        stored_target = _read_json(
            target_path, "migrated trend report revision"
        )
        target_revision = _report_order(target_path)[1]
        source_path = Path(str(completion.get("report_path") or "")) if completion else Path()
        source_revision = _report_order(source_path)[1]
        valid = (
            market == cycle.market
            and request is not None
            and completion is not None
            and bool(actor)
            and bool(reason)
            and authorized_at.tzinfo is not None
            and authorized_at.utcoffset() is not None
            and isinstance(accepted_git_sha, str)
            and bool(re.fullmatch(r"[0-9a-f]{40}", accepted_git_sha))
            and not _batch_path(config, market, cycle.execution_date).exists()
            and target_revision > source_revision
            and target_path.resolve().parent == _report_dir(config, market).resolve()
            and _report_order(target_path)[0] == cycle.as_of_date
            and _valid_report(
                config, market, cycle.execution_date, target_path, stored_target
            )
            and _recovery_revision_for_report(
                config,
                market,
                (target_path, stored_target),
                require_receipt=True,
            )
            is None
            and _report_hash(stored_target) == _report_hash(target_payload)
        )
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid trend report revision migration: {path}") from exc
    if not valid:
        raise ValueError(f"invalid trend report revision migration: {path}")
    payload: dict[str, object] = {
        "schema_version": "open_trader.trend_controller.revision_migration.v1",
        "market": market,
        "as_of_date": cycle.as_of_date,
        "execution_date": cycle.execution_date,
        "revision_request_path": str(request_path),
        "revision_request_sha256": hashlib.sha256(
            request_path.read_bytes()
        ).hexdigest(),
        "source_completion_path": str(completion_path),
        "source_completion_sha256": hashlib.sha256(
            completion_path.read_bytes()
        ).hexdigest(),
        "from_report_path": str(source_path),
        "from_report_sha256": completion["report_sha256"],
        "from_revision": source_revision,
        "to_report_path": str(target_path),
        "to_report_sha256": _report_hash(stored_target),
        "to_revision": target_revision,
        "actor": actor,
        "reason": reason,
        "authorized_at": authorized_at.isoformat(timespec="seconds"),
        "accepted_git_sha": accepted_git_sha,
    }
    return _write_immutable(path, _canonical_json_bytes(payload))


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
    migration = _load_revision_migration(
        config,
        market,
        as_of_date,
        execution_date,
        request_path,
        request,
        completion_path,
        completion,
        baseline_revision,
    )
    if migration is None:
        return request, completion
    effective_completion = dict(completion)
    effective_completion["report_path"] = migration["to_report_path"]
    effective_completion["report_sha256"] = migration["to_report_sha256"]
    effective_completion["revision_migration_path"] = str(
        _revision_migration_path(config, market, as_of_date)
    )
    return request, effective_completion


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
    report_missing: bool = False,
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
        report_path, report_sha, report_revision = _revision_baseline(
            config, cycle
        )
        report_binding_valid = (
            report_missing is True
            and (report_path, report_sha, report_revision) == (None, None, -1)
            and as_of < authorized_at.astimezone(TIMEZONES[market]).date()
            and not any(
                _report_dir(config, market).glob(f"{cycle.as_of_date}*")
            )
            and request is not None
            and request.get("baseline_report_path") is None
            and request.get("baseline_report_sha256") is None
            and request.get("baseline_revision") == -1
        ) or (
            report_missing is False
            and report_path is not None
            and report_sha is not None
            and report_path.resolve().parent
            == _report_dir(config, market).resolve()
            and request is not None
            and request.get("baseline_report_path") == str(report_path)
            and request.get("baseline_report_sha256") == report_sha
        )
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
            and report_binding_valid
        )
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid legacy trend cutover: {path}") from exc
    if not valid:
        raise ValueError(f"invalid legacy trend cutover: {path}")
    payload: dict[str, object] = {
        "schema_version": "open_trader.trend_controller.legacy_cutover.v1",
        "market": cycle.market,
        "as_of_date": cycle.as_of_date,
        "execution_date": cycle.execution_date,
        "report_path": str(report_path) if report_path is not None else None,
        "report_sha256": report_sha,
        "revision_request_path": str(request_path),
        "revision_request_sha256": hashlib.sha256(
            request_path.read_bytes()
        ).hexdigest(),
        "actor": actor,
        "reason": reason,
        "authorized_at": authorized_at.isoformat(timespec="seconds"),
    }
    if report_missing:
        payload["report_missing"] = True
    return _write_immutable(path, _canonical_json_bytes(payload))


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
        report_path, report_sha, report_revision = _revision_baseline(
            config, cycle
        )
        actor = payload.get("actor")
        reason = payload.get("reason")
        report_missing = payload.get("report_missing", False)
        report_binding_valid = (
            report_missing is True
            and (report_path, report_sha, report_revision) == (None, None, -1)
            and as_of < authorized_at.astimezone(TIMEZONES[market]).date()
            and not any(
                _report_dir(config, market).glob(f"{cycle.as_of_date}*")
            )
            and request is not None
            and request.get("baseline_report_path") is None
            and request.get("baseline_report_sha256") is None
            and request.get("baseline_revision") == -1
            and payload.get("report_path") is None
            and payload.get("report_sha256") is None
        ) or (
            report_missing is False
            and report_path is not None
            and report_sha is not None
            and report_path.resolve().parent
            == _report_dir(config, market).resolve()
            and request is not None
            and request.get("baseline_report_path") == str(report_path)
            and request.get("baseline_report_sha256") == report_sha
            and payload.get("report_path") == str(report_path)
            and payload.get("report_sha256") == report_sha
        )
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
            and report_binding_valid
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


def _pending_or_data_missing_buy(action: Mapping[str, object]) -> bool:
    """BUY 待条件（executable=False）或数据缺失类（0 股/0 手/无 ATR）条目判定。

    口径与 trend_review.record_trend_review_missed_buys 的跳过条件一致：此类条目
    不真实下单、不产生终态事件，也不应阻塞轮换执行与当日批次完成。
    """
    if action.get("executable") is False:
        return True
    try:
        shares = Decimal(str(action.get("estimated_shares") or "0"))
        lot_size = Decimal(str(action.get("lot_size") or "0"))
        atr = Decimal(str(action.get("atr") or "0"))
    except (InvalidOperation, TypeError, ValueError):
        return True
    return shares <= 0 or lot_size <= 0 or atr <= 0


def _execution_completed(
    config: DailyPremarketConfig,
    cycle: ControllerCycle,
    *,
    progress: Callable[[], None] | None = None,
    include_rotations: bool = True,
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
        return not include_rotations or relative_rotations_completed(
            config.data_dir,
            report=report,
            market=cycle.market,
            execution_date=cycle.execution_date,
        )

    sell_symbols = {
        trend_action_futu_symbol(report, action, cycle.market)
        for action in actions
        if action.get("action") in {"SELL_ALL", "SELL_PARTIAL"}
    }
    for action in actions:
        action_name = str(action.get("action") or "")
        symbol = str(action.get("symbol") or "").strip()
        if (
            action_name == "BUY"
            and trend_action_futu_symbol(report, action, cycle.market)
            in sell_symbols
        ):
            continue
        if action_name == "BUY" and _pending_or_data_missing_buy(action):
            # 待条件/数据缺失买入不产生终态事件，直接视为已完成，避免轮换执行
            # 与当日批次完成被此类条目永久阻塞。
            continue
        events, resolutions = load_trend_action_audit(
            config.data_dir,
            market=cycle.market,
            execution_date=cycle.execution_date,
            symbol=symbol,
            futu_symbol=trend_action_futu_symbol(report, action, cycle.market),
            side="buy" if action_name == "BUY" else "sell",
            progress=progress,
        )
        position_zero_events = [
            item for item in events if item.get("sell_goal") == "position_zero"
        ]
        if position_zero_events:
            if any(
                item.get("status") in {"filled", "incomplete"}
                and item.get("reason") == "position_zero_confirmed"
                for item in position_zero_events
            ):
                continue
            return False
        if any(
            item.get("resolution") in {"confirm-submitted", "abandon"}
            for item in resolutions
        ):
            continue
        if action_name == "BUY" and any(
            item.get("status") in {"filled", "missed"} for item in events
        ):
            continue
        if action_name == "SELL_PARTIAL":
            if any(
                item.get("sell_goal") == "partial_30"
                and item.get("status") == "below_lot"
                for item in events
            ):
                continue
            for item in events:
                if (
                    item.get("sell_goal") != "partial_30"
                    or item.get("status") not in {"filled", "partially_filled"}
                ):
                    continue
                try:
                    filled = Decimal(str(item.get("filled_qty")))
                    target = Decimal(str(item.get("lifecycle_target_qty")))
                except (InvalidOperation, TypeError, ValueError):
                    return False
                if (
                    filled.is_finite()
                    and target.is_finite()
                    and target > 0
                ):
                    continue
                return False
            progress = overheat_trim_progress(
                config.data_dir,
                market=cycle.market,
                symbol=symbol,
                position_started_for=str(action.get("position_started_for") or ""),
            )
            try:
                filled = Decimal(str(progress["filled_qty"]))
                target = Decimal(str(progress["lifecycle_target_qty"]))
            except (InvalidOperation, KeyError, TypeError, ValueError):
                return False
            if not (
                filled.is_finite()
                and target.is_finite()
                and target > 0
                and filled >= target
            ):
                return False
            continue
        if action_name == "SELL_ALL" and any(
            item.get("status") in {"filled", "incomplete"}
            and item.get("reason") == "position_zero_confirmed"
            and item.get("sell_goal") in {None, "position_zero"}
            for item in events
        ):
            continue
        return False
    return not include_rotations or relative_rotations_completed(
        config.data_dir,
        report=report,
        market=cycle.market,
        execution_date=cycle.execution_date,
    )


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
    *,
    quote_client: object | None = None,
    completed_execution_dates: set[str] | None = None,
    progress: Callable[[], None] | None = None,
) -> ControllerCycle:
    def execution_completed(item: ControllerCycle) -> bool:
        if progress is None:
            return _execution_completed(config, item)
        return _execution_completed(config, item, progress=progress)

    durable = _durable_report_cycles(config, cycle, now)
    completion: dict[str, bool] = {}
    if durable:
        for item in durable:
            if (
                completed_execution_dates is not None
                and item.execution_date in completed_execution_dates
            ):
                completion[item.execution_date] = True
                continue
            try:
                completion[item.execution_date] = execution_completed(item)
            except ValueError:
                completion[item.execution_date] = False
            if (
                completion[item.execution_date]
                and completed_execution_dates is not None
            ):
                completed_execution_dates.add(item.execution_date)
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
            quote_client=quote_client,
        )

    for _ in range(10):
        if cursor.execution_date >= cycle.execution_date:
            return cycle
        if cursor.execution_date in completion:
            if not completion[cursor.execution_date]:
                return cursor
        elif not execution_completed(cursor):
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
            quote_client=quote_client,
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
    report_waiting: str | None = None
    review_failures = 0
    review_retry_after: datetime | None = None
    operation_failures = 0
    operation_retry_after: datetime | None = None
    operation_blocker: str | None = None
    cycle_failures = 0
    cycle_retry_after: datetime | None = None
    cycle_blocker: str | None = None
    statistics_identity: tuple[str, str] | None = None
    statistics_status: str | None = None
    statistics_failures = 0
    statistics_retry_after: datetime | None = None
    benchmark_identity: tuple[str, str] | None = None
    benchmark_status: str | None = None
    benchmark_failures = 0
    benchmark_retry_after: datetime | None = None
    last_success: object = None
    completed_execution_dates: set[str] = set()
    quote_client: object | None = None
    account_client: object | None = None

    def close_client(client: object | None) -> None:
        close = getattr(client, "close", None)
        if callable(close):
            close()

    def shared_quote() -> object:
        nonlocal quote_client
        if quote_client is None:
            quote_client = FutuQuoteClient(
                host=config.futu_host, port=config.futu_port
            )
        return quote_client

    def reset_quote() -> None:
        nonlocal quote_client
        failed_client = quote_client
        quote_client = None
        with suppress(Exception):
            close_client(failed_client)

    def reset_account() -> None:
        nonlocal account_client
        failed_client = account_client
        account_client = None
        with suppress(Exception):
            close_client(failed_client)

    def shared_account() -> object:
        nonlocal account_client
        if account_client is None:
            account_id = require_trend_review_config(config, market)
            _gate_futu_trade_context(
                config, market, quote_client=shared_quote()
            )
            account_client = FutuSimulateOrderExecutionClient(
                host=config.futu_host,
                port=config.futu_port,
                simulate_acc_id=account_id,
                trd_market=market,
            )
        return account_client

    def load_account(
        _path: Path, *, expected_date: str, timezone: ZoneInfo
    ) -> object:
        nonlocal account_client
        del timezone
        try:
            client = shared_account()
            return load_futu_simulate_trend_account(
                host=config.futu_host,
                port=config.futu_port,
                simulate_acc_id=require_trend_review_config(config, market),
                market=market,
                expected_date=expected_date,
                account_client=client,
            )
        except Exception:
            reset_account()
            raise

    last_reconciliation_heartbeat: datetime | None = None

    def reconciliation_progress() -> None:
        nonlocal last_reconciliation_heartbeat
        heartbeat_now = _localized(now_fn(), config.timezone)
        if (
            last_reconciliation_heartbeat is not None
            and timedelta(0)
            <= heartbeat_now - last_reconciliation_heartbeat
            < timedelta(seconds=5)
        ):
            return
        last_reconciliation_heartbeat = heartbeat_now
        _record_status(
            config,
            market,
            now=heartbeat_now,
            phase="reconciling",
            last_success=last_success,
            blocker=cycle_blocker or report_blocker or operation_blocker,
            next_check_at=heartbeat_now + timedelta(seconds=5),
            fixed_process_version=process_version,
        )

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
            _retry_pending_feishu_notifications(config)
            statement_broker = {
                "CN": "eastmoney",
                "HK": "phillips",
            }.get(market)
            if statement_broker is not None:
                try:
                    statement_statistics = consume_accepted_statement_facts(
                        data_dir=config.data_dir,
                        reports_dir=config.reports_dir,
                        broker=statement_broker,
                        generated_at=now.isoformat(timespec="seconds"),
                    )
                except Exception as exc:
                    statement_statistics = {
                        "status": "failed",
                        "reason": str(exc),
                    }
                with suppress(Exception):
                    _record_statement_statistics_diagnostic(
                        config,
                        market,
                        statement_broker,
                        statement_statistics,
                        now,
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
                            config,
                            market,
                            local.date().isoformat(),
                            quote_client=shared_quote(),
                            account_loader=load_account,
                        )
                    )
                except Exception as exc:
                    if isinstance(exc, FutuQuoteError):
                        reset_quote()
                    protection_error = f"protection pass failed: {exc}"
            if protection_error is not None:
                _notify_protection_blocker(
                    config,
                    market,
                    local.date().isoformat(),
                    protection_error,
                    now.isoformat(timespec="seconds"),
                )
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
                cycle = _derive_cycle(
                    config, market, now, quote_client=shared_quote()
                )
            except Exception as exc:
                if isinstance(exc, FutuQuoteError):
                    reset_quote()
                cycle_failures += 1
                cycle_retry_after = _retry_at(now, cycle_failures)
                cycle_blocker = str(exc)
                _notify_controller_failure(
                    config,
                    market,
                    now.astimezone(TIMEZONES[market]).date().isoformat(),
                    "calendar",
                    exc,
                    now,
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
            with suppress(OpenDIncidentStateError):
                record_opend_health(config.data_dir, market, now)
            cycle_failures = 0
            cycle_retry_after = None
            cycle_blocker = None
            current_statistics_identity = (cycle.market, cycle.as_of_date)
            if statistics_identity != current_statistics_identity:
                statistics_identity = current_statistics_identity
                statistics_status = None
                statistics_failures = 0
                statistics_retry_after = None
            current_benchmark_identity = (
                cycle.market,
                now.astimezone(TIMEZONES[cycle.market]).strftime("%Y-%m"),
            )
            if benchmark_identity != current_benchmark_identity:
                benchmark_identity = current_benchmark_identity
                benchmark_status = None
                benchmark_failures = 0
                benchmark_retry_after = None
            phase = "monitoring" if cycle.market_open else cycle.session
            blocker = protection_error
            if blocker is not None:
                phase = "blocked"
            work_cycle = report_target.cycle if report_target else cycle
            latest: tuple[Path, dict[str, object]] | None = None
            try:
                if report_target is None:
                    work_cycle = _cycle_to_reconcile(
                        config,
                        cycle,
                        now,
                        quote_client=shared_quote(),
                        completed_execution_dates=completed_execution_dates,
                        progress=reconciliation_progress,
                    )
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
                natural_statistics_due = (
                    not revision
                    and report_target is None
                    and future is None
                    and work_cycle == cycle
                    and not revision_pending
                    and recovery_revision is None
                    and statistics_status not in {"completed", "already_completed"}
                    and (
                        statistics_retry_after is None
                        or now >= statistics_retry_after
                    )
                )
                if natural_statistics_due:
                    try:
                        statistics_result = _run_cycle_statistics(
                            config, cycle, now, process_version
                        )
                    except Exception as exc:
                        try:
                            statistics_result = _record_statistics_exception(
                                config, cycle, now, process_version, exc
                            )
                        except Exception:
                            statistics_result = {
                                "status": "failed",
                                "reason": str(exc),
                            }
                    statistics_status = str(statistics_result.get("status") or "")
                    if statistics_status not in {
                        "failed",
                        "completed",
                        "already_completed",
                    }:
                        statistics_result = {
                            "status": "failed",
                            "reason": (
                                "statistics refresh returned invalid status: "
                                f"{statistics_status or '<empty>'}"
                            ),
                        }
                        statistics_status = "failed"
                    with suppress(Exception):
                        _notify_statistics_result(
                            config, cycle, statistics_result, now
                        )
                    if statistics_status == "failed":
                        statistics_failures += 1
                        statistics_retry_after = _retry_at(
                            now, statistics_failures
                        )
                    else:
                        statistics_failures = 0
                        statistics_retry_after = None
                natural_benchmark_due = (
                    not revision
                    and report_target is None
                    and future is None
                    and work_cycle == cycle
                    and not revision_pending
                    and recovery_revision is None
                    and benchmark_status not in {"completed", "already_completed"}
                    and (
                        benchmark_retry_after is None
                        or now >= benchmark_retry_after
                    )
                )
                if natural_benchmark_due:
                    try:
                        benchmark_result = _run_cycle_long_term_benchmark(
                            config, cycle, now, process_version, shared_quote()
                        )
                    except Exception as exc:
                        try:
                            benchmark_result = _record_long_term_benchmark_exception(
                                config, cycle, now, process_version, exc
                            )
                        except Exception:
                            benchmark_result = {"status": "failed", "reason": str(exc)}
                    benchmark_status = str(benchmark_result.get("status") or "")
                    if benchmark_status == "failed":
                        benchmark_failures += 1
                        benchmark_retry_after = _retry_at(now, benchmark_failures)
                    else:
                        benchmark_failures = 0
                        benchmark_retry_after = None
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
                    allocation_reference = _allocation_reference_for_cycle(
                        config, now=now, quote_client=shared_quote()
                    )
                    report_args: tuple[object, ...] = (
                        config,
                        market,
                        work_cycle.report_run_date,
                        generator_revision,
                    )
                    if config.trend_animals_api_key:
                        report_args += (allocation_reference,)
                    future = pool.submit(_generate_report, *report_args)
                    report_target = ReportTask(
                        cycle=work_cycle,
                        completes_revision_request=revision_pending,
                        allocation_reference=allocation_reference,
                    )

                if future is not None and (future.done() or once):
                    report_cycle = report_target.cycle if report_target else cycle
                    try:
                        future.result(timeout=1 if once else None)
                    except TimeoutError:
                        phase = "recovering_report"
                        report_waiting = None
                    except Exception as exc:
                        report_failures += 1
                        report_retry_after = _retry_at(now, report_failures)
                        report_blocker = f"report generation failed: {exc}"
                        report_waiting = getattr(exc, "waiting_reason", None)
                        blocker = report_blocker
                        phase = "recovering_report"
                        future = None
                    else:
                        future = None
                        report_failures = 0
                        report_retry_after = None
                        report_blocker = None
                        report_waiting = None
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
                review_delayed = (
                    review_retry_after is not None
                    and now < review_retry_after
                )
                if operation_delayed:
                    blocker = report_blocker or operation_blocker
                    phase = "blocked"
                elif future is not None or report_target is not None:
                    phase = "recovering_report"
                elif latest is None:
                    phase = "recovering_report"
                elif _execution_due(work_cycle, now):
                    judgments = latest[1].get("strategy_judgments")
                    formal_actions = (
                        judgments.get("formal_actions")
                        if isinstance(judgments, dict)
                        else None
                    )
                    rotation_pairs = (
                        judgments.get("simulate_rotation_pairs")
                        if isinstance(judgments, dict)
                        else None
                    )
                    if (
                        _execution_completed(
                            config,
                            work_cycle,
                            progress=reconciliation_progress,
                        )
                        and (formal_actions or rotation_pairs)
                    ):
                        execution = {
                            "status": "reconciled",
                            "market": market,
                            "date": work_cycle.execution_date,
                            "submitted_count": 0,
                            "artifact_paths": [],
                        }
                    else:
                        selected = _locked_report(config, work_cycle, latest, now)
                        if protection_error is not None:
                            execution = _execute_locked_report(
                                config,
                                market,
                                work_cycle.execution_date,
                                selected[0],
                                selected[1],
                                allow_new_buys=False,
                                quote_client=shared_quote(),
                            )
                        else:
                            execution = _execute_locked_report(
                                config,
                                market,
                                work_cycle.execution_date,
                                selected[0],
                                selected[1],
                                quote_client=shared_quote(),
                            )
                    last_success = execution
                    operation_failures = 0
                    operation_retry_after = None
                    operation_blocker = None
                    status = str(execution.get("status") or "")
                    if status == "reconciled":
                        phase = (
                            "monitoring"
                            if cycle.market_open
                            else cycle.session
                        )
                    elif status in {"uncertain", "conflict"}:
                        blocker = status
                        phase = status
                        occurred_at = now.isoformat(timespec="seconds")
                        _notify_non_feishu_once(
                            f"{market} 趋势订单 {status}",
                            "自动提交已停止，请核对不可变账本与 Futu 订单。",
                            (
                                config,
                                market,
                                work_cycle.execution_date,
                                "execution",
                                status,
                                occurred_at,
                            ),
                        )
                        _notify_order_groups_or_batch_failure(
                            config,
                            market,
                            work_cycle.execution_date,
                            [
                                event
                                for event in _latest_action_events(
                                    config,
                                    market,
                                    work_cycle.execution_date,
                                )
                                if event.get("status") == status
                            ],
                            occurred_at,
                            status,
                        )
                    elif status == "missed_window":
                        phase = "missed"
                        occurred_at = now.isoformat(timespec="seconds")
                        _notify_non_feishu_once(
                            f"{market} 趋势买入已错过窗口",
                            "报告已保留，未完成的买入不会追单。",
                            (
                                config,
                                market,
                                work_cycle.execution_date,
                                "opening_actions",
                                "buy_window_closed",
                                occurred_at,
                            ),
                        )
                        _notify_order_groups_or_batch_failure(
                            config,
                            market,
                            work_cycle.execution_date,
                            [
                                event
                                for event in _latest_action_events(
                                    config,
                                    market,
                                    work_cycle.execution_date,
                                )
                                if event.get("status")
                                in {"missed", "missed_window"}
                            ],
                            occurred_at,
                            status,
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

                if (
                    review_delayed
                    and blocker is None
                    and phase
                    not in {
                        "blocked",
                        "recovering_report",
                        "uncertain",
                        "conflict",
                        "missed",
                    }
                ):
                    phase = "recovering_review"

                close_due = (
                    cycle.session == "closed"
                    or now.astimezone(TIMEZONES[market]).date()
                    > date.fromisoformat(cycle.as_of_date)
                )
                close_completed = close_due and _close_completed(
                    config, market, cycle.as_of_date
                )
                if (
                    close_due
                    and not operation_delayed
                    and not review_delayed
                    and (
                        not close_completed
                        or not _trend_review_projection_current(config, market)
                    )
                    and _load_report_for_as_of(
                        config, market, cycle.as_of_date
                    ) is not None
                ):
                    try:
                        _capture_close(
                            config,
                            market,
                            cycle.as_of_date,
                            quote_client=shared_quote(),
                            account_client=account_client,
                            account_client_factory=shared_account,
                        )
                        if not close_completed:
                            _complete_close(config, market, cycle.as_of_date, now)
                    except FutuOrderExecutionError:
                        reset_account()
                        raise
                    except FutuQuoteError as exc:
                        reset_quote()
                        review_failures += 1
                        review_retry_after = _retry_at(now, review_failures)
                        if blocker is None and phase not in {
                            "blocked",
                            "recovering_report",
                            "uncertain",
                            "conflict",
                            "missed",
                        }:
                            phase = "recovering_review"
                        _notify_controller_failure(
                            config,
                            market,
                            cycle.execution_date,
                            "review",
                            exc,
                            now,
                        )
                    else:
                        if last_success is None or cycle.session == "closed":
                            last_success = {
                                "status": "close_captured",
                                "date": cycle.as_of_date,
                            }
                        operation_failures = 0
                        operation_retry_after = None
                        operation_blocker = None
                        review_failures = 0
                        review_retry_after = None
                        if cycle.session == "closed":
                            phase = "closed"
            except Exception as exc:
                if isinstance(exc, FutuQuoteError):
                    reset_quote()
                operation_failures += 1
                operation_retry_after = _retry_at(now, operation_failures)
                operation_blocker = str(exc)
                blocker = str(exc)
                if "invalid frozen trend report" in blocker:
                    phase = "blocked"
                elif phase != "recovering_report":
                    phase = "blocked"
                _notify_controller_failure(
                    config,
                    market,
                    cycle.execution_date,
                    "controller",
                    exc,
                    now,
                )
                blocker = cycle_blocker or report_blocker or operation_blocker

            if (
                last_success is None
                and blocker is None
                and latest is not None
                and phase
                not in {
                    "starting",
                    "reconciling",
                    "recovering_report",
                    "blocked",
                    "uncertain",
                    "conflict",
                    "missed",
                }
            ):
                last_success = {
                    "status": "reconciled",
                    "market": market,
                    "date": work_cycle.execution_date,
                    "submitted_count": 0,
                    "artifact_paths": [],
                }
            next_check = (
                operation_retry_after
                or report_retry_after
                or review_retry_after
                or cycle.next_check_at
            )
            if (
                statistics_retry_after is not None
                and statistics_retry_after < next_check
            ):
                next_check = statistics_retry_after
            status_now = max(now, last_reconciliation_heartbeat or now)
            status_payload = _record_status(
                config,
                market,
                now=status_now,
                phase=phase,
                last_success=last_success,
                blocker=blocker,
                waiting=_trend_waiting_reason(
                    phase=phase,
                    execution_date=work_cycle.execution_date,
                    latest=latest,
                    report_waiting=report_waiting,
                    blocker=blocker,
                ),
                next_check_at=next_check,
                fixed_process_version=process_version,
            )
            if once:
                return status_payload
            sleep_fn(5)
    finally:
        try:
            close_client(account_client)
        finally:
            try:
                close_client(quote_client)
            finally:
                try:
                    pool.shutdown(wait=not once, cancel_futures=True)
                finally:
                    lock.__exit__(None, None, None)
