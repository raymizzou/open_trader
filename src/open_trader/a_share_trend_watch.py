from __future__ import annotations

import json
import time as time_module
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from .a_share_trend import load_eastmoney_account, load_watch_events
from .daily_premarket import RunLock, send_notification_with_results
from .futu_quote import FutuQuoteClient, FutuQuoteError
from .futu_symbols import to_futu_symbol
from .notifications import Notifier


SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class AShareWatchResult:
    status: str
    watched_symbol_count: int
    trigger_count: int
    exception_count: int
    unknown_quote_count: int
    events_path: Path


def cn_session(now: datetime) -> str:
    local = now.astimezone(SHANGHAI).time()
    if local < time(9, 30):
        return "before"
    if local <= time(11, 30):
        return "morning"
    if local < time(13, 0):
        return "lunch"
    if local <= time(15, 0):
        return "afternoon"
    return "closed"


def append_watch_event(
    path: Path,
    *,
    symbol: str,
    trading_date: str,
    event_type: str,
    occurred_at: str,
    last_price: Decimal | None,
    active_line: Decimal | None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "event_id": uuid4().hex,
        "symbol": symbol,
        "trading_date": trading_date,
        "event_type": event_type,
        "occurred_at": occurred_at,
        "last_price": None if last_price is None else str(last_price),
        "active_line": None if active_line is None else str(active_line),
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def watch_a_share_protection(
    *,
    portfolio_path: Path,
    state_path: Path,
    events_path: Path,
    quote_client: object | None,
    notifier: Notifier,
    poll_seconds: float,
    reconnect_seconds: float,
    once: bool = False,
    quote_client_factory: Callable[[], object] | None = None,
    report_lock_path: Path | None = None,
    now_fn: Callable[[], datetime] = lambda: datetime.now(SHANGHAI),
    sleep_fn: Callable[[float], None] = time_module.sleep,
) -> AShareWatchResult:
    client = quote_client
    if quote_client_factory is None and client is not None:
        host = getattr(client, "host", None)
        port = getattr(client, "port", None)
        if isinstance(host, str) and isinstance(port, int):
            quote_client_factory = lambda: FutuQuoteClient(host=host, port=port)

    first_now = now_fn()
    trading_date = first_now.astimezone(SHANGHAI).date().isoformat()
    positions: dict[str, object] = {}
    active_lines: dict[str, Decimal | None] = {}
    alerted: set[str] = set()
    reported_lines: set[str] = set()
    reported_quotes: set[str] = set()

    trigger_count = exception_count = unknown_quote_count = 0
    calendar_checked = False
    interrupted = False
    now = first_now
    try:
        while True:
            session = cn_session(now)
            if session == "closed":
                return _result(
                    "closed",
                    positions,
                    trigger_count,
                    exception_count,
                    unknown_quote_count,
                    events_path,
                )

            if client is None:
                if quote_client_factory is None:
                    raise RuntimeError("quote client factory is required after interruption")
                try:
                    client = quote_client_factory()
                except FutuQuoteError as exc:
                    if not interrupted:
                        _record_interruption(
                            events_path, notifier, trading_date, now, str(exc)
                        )
                        interrupted = True
                    sleep_fn(reconnect_seconds)
                    now = now_fn()
                    continue

            if not calendar_checked:
                try:
                    trading_days = client.get_cn_trading_days(
                        start=trading_date, end=trading_date
                    )
                except FutuQuoteError as exc:
                    if not interrupted:
                        _record_interruption(
                            events_path, notifier, trading_date, now, str(exc)
                        )
                        interrupted = True
                    _close(client)
                    client = None
                    sleep_fn(reconnect_seconds)
                    now = now_fn()
                    continue
                calendar_checked = True
                if interrupted:
                    _record_recovery(events_path, notifier, trading_date, now)
                    interrupted = False
                if trading_date not in trading_days:
                    return _result(
                        "holiday",
                        positions,
                        trigger_count,
                        exception_count,
                        unknown_quote_count,
                        events_path,
                    )

            if session == "before":
                opening = now.astimezone(SHANGHAI).replace(
                    hour=9, minute=30, second=0, microsecond=0
                )
                sleep_fn((opening - now.astimezone(SHANGHAI)).total_seconds())
                now = now_fn()
                continue
            if session == "lunch":
                afternoon = now.astimezone(SHANGHAI).replace(
                    hour=13, minute=0, second=0, microsecond=0
                )
                sleep_fn((afternoon - now.astimezone(SHANGHAI)).total_seconds())
                now = now_fn()
                continue

            try:
                with (
                    RunLock(report_lock_path)
                    if report_lock_path is not None
                    else nullcontext()
                ):
                    account = load_eastmoney_account(
                        portfolio_path,
                        expected_date=trading_date,
                        timezone=SHANGHAI,
                    )
                    positions = {
                        item.symbol: item
                        for item in account.positions
                    }
                    active_lines = _load_active_lines(state_path)
                    prior_events = load_watch_events(events_path)
                    alerted = {
                        str(event.get("symbol", ""))
                        for event in prior_events
                        if event.get("trading_date") == trading_date
                        and event.get("event_type") == "protection_triggered"
                    }
                    reported_lines = {
                        str(event.get("symbol", ""))
                        for event in prior_events
                        if event.get("trading_date") == trading_date
                        and event.get("event_type") == "protection_line_missing"
                    }
                    reported_quotes = {
                        str(event.get("symbol", ""))
                        for event in prior_events
                        if event.get("trading_date") == trading_date
                        and event.get("event_type") == "quote_unknown"
                    }
            except RuntimeError as exc:
                if str(exc) != "daily premarket run already active":
                    raise
                sleep_fn(poll_seconds)
                now = now_fn()
                continue

            comparable: dict[str, tuple[str, Decimal]] = {}
            for symbol in sorted(positions):
                active_line = active_lines.get(symbol)
                if active_line is None:
                    if symbol not in reported_lines:
                        append_watch_event(
                            events_path,
                            symbol=symbol,
                            trading_date=trading_date,
                            event_type="protection_line_missing",
                            occurred_at=now.isoformat(timespec="seconds"),
                            last_price=None,
                            active_line=None,
                        )
                        send_notification_with_results(
                            notifier,
                            f"A股保护线缺失 · {symbol}",
                            "当前持仓缺少活动保护线，未进行价格比较；请立即人工检查。",
                            channels={"feishu", "feishu_app"},
                        )
                        reported_lines.add(symbol)
                        exception_count += 1
                    continue
                comparable[to_futu_symbol("CN", symbol)] = (symbol, active_line)

            if not comparable:
                if once:
                    return _result(
                        "completed",
                        positions,
                        trigger_count,
                        exception_count,
                        unknown_quote_count,
                        events_path,
                    )
                sleep_fn(poll_seconds)
                now = now_fn()
                continue

            try:
                snapshots = client.get_snapshots(sorted(comparable))
            except FutuQuoteError as exc:
                if not interrupted:
                    _record_interruption(
                        events_path, notifier, trading_date, now, str(exc)
                    )
                    interrupted = True
                _close(client)
                client = None
                sleep_fn(reconnect_seconds)
                now = now_fn()
                continue
            if interrupted:
                _record_recovery(events_path, notifier, trading_date, now)
                interrupted = False

            for futu_symbol, (symbol, active_line) in comparable.items():
                snapshot = snapshots.get(futu_symbol)
                if snapshot is None:
                    if symbol not in reported_quotes:
                        append_watch_event(
                            events_path,
                            symbol=symbol,
                            trading_date=trading_date,
                            event_type="quote_unknown",
                            occurred_at=now.isoformat(timespec="seconds"),
                            last_price=None,
                            active_line=active_line,
                        )
                        send_notification_with_results(
                            notifier,
                            f"A股实时价格未知 · {symbol}",
                            f"未取得有效实时价格，活动保护线 {active_line} 的状态未知；请立即人工核价。",
                            channels={"feishu", "feishu_app"},
                        )
                        reported_quotes.add(symbol)
                        unknown_quote_count += 1
                    continue
                if symbol in alerted or snapshot.last_price > active_line:
                    continue
                append_watch_event(
                    events_path,
                    symbol=symbol,
                    trading_date=trading_date,
                    event_type="protection_triggered",
                    occurred_at=now.isoformat(timespec="seconds"),
                    last_price=snapshot.last_price,
                    active_line=active_line,
                )
                send_notification_with_results(
                    notifier,
                    f"A股保护线触发 · {symbol}",
                    f"最新价 {snapshot.last_price} <= 活动保护线 {active_line}\n"
                    "建议动作：全部卖出（人工执行）",
                )
                alerted.add(symbol)
                trigger_count += 1

            if once:
                return _result(
                    "completed",
                    positions,
                    trigger_count,
                    exception_count,
                    unknown_quote_count,
                    events_path,
                )
            sleep_fn(poll_seconds)
            now = now_fn()
    finally:
        if client is not None:
            _close(client)


def _load_active_lines(path: Path) -> dict[str, Decimal | None]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("protection state is unreadable or malformed") from None
    if not isinstance(payload, Mapping) or payload.get("schema_version") != 1:
        raise ValueError("protection state has an invalid schema")
    positions = payload.get("positions")
    if not isinstance(positions, Mapping):
        raise ValueError("protection state positions must be an object")
    return {
        str(symbol): _optional_decimal(
            state.get("active_line") if isinstance(state, Mapping) else None
        )
        for symbol, state in positions.items()
    }


def _optional_decimal(value: object) -> Decimal | None:
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError):
        return None
    return result if result.is_finite() else None


def _record_interruption(
    events_path: Path,
    notifier: Notifier,
    trading_date: str,
    now: datetime,
    error: str,
) -> None:
    append_watch_event(
        events_path,
        symbol="",
        trading_date=trading_date,
        event_type="monitor_interrupted",
        occurred_at=now.isoformat(timespec="seconds"),
        last_price=None,
        active_line=None,
    )
    send_notification_with_results(
        notifier,
        "A股价格监控中断",
        f"Futu OpenD 实时价格不可用：{error}\n请立即在东方财富人工核价。",
    )


def _record_recovery(
    events_path: Path,
    notifier: Notifier,
    trading_date: str,
    now: datetime,
) -> None:
    append_watch_event(
        events_path,
        symbol="",
        trading_date=trading_date,
        event_type="monitor_recovered",
        occurred_at=now.isoformat(timespec="seconds"),
        last_price=None,
        active_line=None,
    )
    send_notification_with_results(
        notifier,
        "A股价格监控恢复",
        "Futu OpenD 实时价格已恢复，活动保护线监控继续运行。",
    )


def _result(
    status: str,
    positions: Mapping[str, object],
    trigger_count: int,
    exception_count: int,
    unknown_quote_count: int,
    events_path: Path,
) -> AShareWatchResult:
    return AShareWatchResult(
        status=status,
        watched_symbol_count=len(positions),
        trigger_count=trigger_count,
        exception_count=exception_count,
        unknown_quote_count=unknown_quote_count,
        events_path=events_path,
    )


def _close(client: object) -> None:
    close = getattr(client, "close", None)
    if callable(close):
        close()
