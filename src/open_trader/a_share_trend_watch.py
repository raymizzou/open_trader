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

from .a_share_trend import AccountSnapshot, load_eastmoney_account, load_watch_events
from .daily_premarket import RunLock, send_notification_with_results
from .futu_quote import FutuQuoteClient, FutuQuoteError
from .futu_symbols import to_futu_symbol
from .notifications import (
    CompositeNotifier,
    Notifier,
    XiaoaiSSHNotifier,
)


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
    market: str = "",
    reason: str = "",
) -> dict[str, object]:
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
    if market:
        event["market"] = market
    if reason:
        event["reason"] = reason
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    return event


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
    market: str = "CN",
    market_label: str = "A股",
    broker_label: str = "东方财富",
    session_timezone: ZoneInfo = SHANGHAI,
    session_fn: Callable[[datetime], str] = cn_session,
    account_loader: Callable[..., AccountSnapshot] = load_eastmoney_account,
    on_session_open: Callable[[str], None] | None = None,
    on_protection_trigger: Callable[[Mapping[str, object]], None] | None = None,
) -> AShareWatchResult:
    client = quote_client
    if quote_client_factory is None and client is not None:
        host = getattr(client, "host", None)
        port = getattr(client, "port", None)
        if isinstance(host, str) and isinstance(port, int):
            quote_client_factory = lambda: FutuQuoteClient(host=host, port=port)

    first_now = now_fn()
    trading_date = first_now.astimezone(session_timezone).date().isoformat()
    positions: dict[str, object] = {}
    active_lines: dict[str, Decimal | None] = {}
    trigger_events: dict[str, Mapping[str, object]] = {}
    alerted: set[str] = set()
    delivered_alerts_feishu: set[str] = set()
    delivered_alerts_macos: set[str] = set()
    reported_lines: set[str] = set()
    delivered_lines: set[str] = set()
    reported_quotes: set[str] = set()
    delivered_quotes: set[str] = set()

    trigger_count = exception_count = unknown_quote_count = 0
    calendar_checked = False
    interrupted = False
    now = first_now
    try:
        while True:
            session = session_fn(now)
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
                            events_path, notifier, trading_date, now, str(exc),
                            market_label=market_label, broker_label=broker_label,
                        )
                        interrupted = True
                    sleep_fn(reconnect_seconds)
                    now = now_fn()
                    continue

            if not calendar_checked:
                try:
                    trading_days = (
                        client.get_cn_trading_days(start=trading_date, end=trading_date)
                        if market == "CN"
                        else client.get_trading_days(
                            market=market, start=trading_date, end=trading_date
                        )
                    )
                except FutuQuoteError as exc:
                    if not interrupted:
                        _record_interruption(
                            events_path, notifier, trading_date, now, str(exc),
                            market_label=market_label, broker_label=broker_label,
                        )
                        interrupted = True
                    _close(client)
                    client = None
                    sleep_fn(reconnect_seconds)
                    now = now_fn()
                    continue
                calendar_checked = True
                if interrupted:
                    _record_recovery(
                        events_path, notifier, trading_date, now,
                        market_label=market_label,
                    )
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
                opening = now.astimezone(session_timezone).replace(
                    hour=9, minute=30, second=0, microsecond=0
                )
                sleep_fn((opening - now.astimezone(session_timezone)).total_seconds())
                now = now_fn()
                continue
            if session == "lunch":
                afternoon = now.astimezone(session_timezone).replace(
                    hour=13, minute=0, second=0, microsecond=0
                )
                sleep_fn((afternoon - now.astimezone(session_timezone)).total_seconds())
                now = now_fn()
                continue

            if on_session_open is not None:
                exception_count += _run_review_callback(
                    on_session_open,
                    trading_date,
                    events_path=events_path,
                    trading_date=trading_date,
                    now=now,
                    notifier=notifier,
                )
                _notify_trend_review_deadline(
                    data_dir=events_path.parents[1],
                    market=market,
                    trading_date=trading_date,
                    now=now,
                    events_path=events_path,
                    notifier=notifier,
                )

            try:
                with (
                    RunLock(report_lock_path)
                    if report_lock_path is not None
                    else nullcontext()
                ):
                    account = account_loader(
                        portfolio_path,
                        expected_date=trading_date,
                        timezone=session_timezone,
                    )
                    positions = {
                        item.symbol: item
                        for item in account.positions
                    }
                    active_lines = _load_active_lines(state_path)
                    prior_events = load_watch_events(events_path)
                    trigger_events = {
                        str(event.get("symbol", "")): event
                        for event in prior_events
                        if event.get("trading_date") == trading_date
                        and event.get("event_type") == "protection_triggered"
                    }
                    alerted = set(trigger_events)
                    delivered_alerts_feishu = {
                        str(event.get("symbol", ""))
                        for event in prior_events
                        if event.get("trading_date") == trading_date
                        and event.get("event_type")
                        == "protection_triggered_notification_delivered_feishu"
                    }
                    delivered_alerts_macos = {
                        str(event.get("symbol", ""))
                        for event in prior_events
                        if event.get("trading_date") == trading_date
                        and event.get("event_type")
                        == "protection_triggered_notification_delivered_macos"
                    }
                    reported_lines = {
                        str(event.get("symbol", ""))
                        for event in prior_events
                        if event.get("trading_date") == trading_date
                        and event.get("event_type") == "protection_line_missing"
                    }
                    delivered_lines = {
                        str(event.get("symbol", ""))
                        for event in prior_events
                        if event.get("trading_date") == trading_date
                        and event.get("event_type")
                        == "protection_line_missing_notification_delivered"
                    }
                    reported_quotes = {
                        str(event.get("symbol", ""))
                        for event in prior_events
                        if event.get("trading_date") == trading_date
                        and event.get("event_type") == "quote_unknown"
                    }
                    delivered_quotes = {
                        str(event.get("symbol", ""))
                        for event in prior_events
                        if event.get("trading_date") == trading_date
                        and event.get("event_type")
                        == "quote_unknown_notification_delivered"
                    }
            except RuntimeError as exc:
                if str(exc) != "daily premarket run already active":
                    raise
                sleep_fn(poll_seconds)
                now = now_fn()
                continue

            for symbol, event in sorted(trigger_events.items()):
                if symbol not in positions:
                    continue
                if on_protection_trigger is not None:
                    exception_count += _run_review_callback(
                        on_protection_trigger,
                        event,
                        events_path=events_path,
                        trading_date=trading_date,
                        now=now,
                        notifier=notifier,
                    )
                _deliver_trigger_notification(
                    events_path=events_path,
                    notifier=notifier,
                    trading_date=trading_date,
                    now=now,
                    symbol=symbol,
                    position_name=str(getattr(positions[symbol], "name", "")),
                    last_price=_optional_decimal(event.get("last_price")),
                    active_line=_optional_decimal(event.get("active_line")),
                    delivered_feishu=delivered_alerts_feishu,
                    delivered_macos=delivered_alerts_macos,
                    replay=True,
                    market_label=market_label,
                )

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
                        reported_lines.add(symbol)
                        exception_count += 1
                    if symbol not in delivered_lines:
                        attempts = send_notification_with_results(
                            notifier,
                            f"{market_label}保护线缺失 · {symbol}",
                            "当前持仓缺少活动保护线，未进行价格比较；请立即人工检查。",
                            channels={"feishu", "feishu_app"},
                        )
                        if any(attempt.success for attempt in attempts):
                            append_watch_event(
                                events_path,
                                symbol=symbol,
                                trading_date=trading_date,
                                event_type=(
                                    "protection_line_missing_notification_delivered"
                                ),
                                occurred_at=now.isoformat(timespec="seconds"),
                                last_price=None,
                                active_line=None,
                            )
                            delivered_lines.add(symbol)
                    continue
                comparable[to_futu_symbol(market, symbol)] = (symbol, active_line)

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
                        events_path, notifier, trading_date, now, str(exc),
                        market_label=market_label, broker_label=broker_label,
                    )
                    interrupted = True
                _close(client)
                client = None
                sleep_fn(reconnect_seconds)
                now = now_fn()
                continue
            if interrupted:
                _record_recovery(
                    events_path, notifier, trading_date, now,
                    market_label=market_label,
                )
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
                        reported_quotes.add(symbol)
                        unknown_quote_count += 1
                    if symbol not in delivered_quotes:
                        attempts = send_notification_with_results(
                            notifier,
                            f"{market_label}实时价格未知 · {symbol}",
                            f"未取得有效实时价格，活动保护线 {active_line} 的状态未知；请立即人工核价。",
                            channels={"feishu", "feishu_app"},
                        )
                        if any(attempt.success for attempt in attempts):
                            append_watch_event(
                                events_path,
                                symbol=symbol,
                                trading_date=trading_date,
                                event_type="quote_unknown_notification_delivered",
                                occurred_at=now.isoformat(timespec="seconds"),
                                last_price=None,
                                active_line=active_line,
                            )
                            delivered_quotes.add(symbol)
                    continue
                if snapshot.last_price > active_line:
                    continue
                if symbol not in alerted:
                    event = append_watch_event(
                        events_path,
                        symbol=symbol,
                        trading_date=trading_date,
                        event_type="protection_triggered",
                        occurred_at=now.isoformat(timespec="seconds"),
                        last_price=snapshot.last_price,
                        active_line=active_line,
                    )
                    alerted.add(symbol)
                    trigger_count += 1
                    if on_protection_trigger is not None:
                        exception_count += _run_review_callback(
                            on_protection_trigger,
                            event,
                            events_path=events_path,
                            trading_date=trading_date,
                            now=now,
                            notifier=notifier,
                        )
                    _deliver_trigger_notification(
                        events_path=events_path,
                        notifier=notifier,
                        trading_date=trading_date,
                        now=now,
                        symbol=symbol,
                        position_name=str(getattr(positions[symbol], "name", "")),
                        last_price=snapshot.last_price,
                        active_line=active_line,
                        delivered_feishu=delivered_alerts_feishu,
                        delivered_macos=delivered_alerts_macos,
                        replay=False,
                        market_label=market_label,
                    )

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


def _notify_trend_review_deadline(
    *,
    data_dir: Path,
    market: str,
    trading_date: str,
    now: datetime,
    events_path: Path,
    notifier: Notifier,
) -> None:
    deadline = time(15, 30) if market == "US" else time(10)
    timezone = ZoneInfo("America/New_York") if market == "US" else SHANGHAI
    if now.astimezone(timezone).time().replace(tzinfo=None) < deadline:
        return
    prior = load_watch_events(events_path)
    notified = {
        str(event.get("symbol") or "")
        for event in prior
        if event.get("event_type") == "trend_review_deadline_notified"
        and event.get("trading_date") == trading_date
    }
    labels = {
        "pending": "待执行",
        "submitted": "已提交",
        "partially_filled": "部分成交",
        "failed": "失败",
        "blocked": "受阻",
        "missed": "错过",
        "incomplete": "未完成",
    }
    root = (
        data_dir
        / "trend_review"
        / "ledgers"
        / market
        / "actions"
        / trading_date
    )
    for action_dir in root.glob("*"):
        paths = sorted(action_dir.glob("*.json"))
        if not paths:
            continue
        try:
            event = json.loads(paths[-1].read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        symbol = str(event.get("symbol") or "")
        status = str(event.get("status") or "")
        if not symbol or symbol in notified or status == "filled":
            continue
        attempts = send_notification_with_results(
            notifier,
            f"趋势模拟执行未完成 · {trading_date}",
            f"{symbol} · {labels.get(status, '状态未知')}",
        )
        if any(attempt.success for attempt in attempts):
            append_watch_event(
                events_path,
                symbol=symbol,
                trading_date=trading_date,
                event_type="trend_review_deadline_notified",
                occurred_at=now.isoformat(timespec="seconds"),
                last_price=None,
                active_line=None,
                market=market,
                reason=status,
            )


def _run_review_callback(
    callback: Callable[[object], None],
    value: object,
    *,
    events_path: Path,
    trading_date: str,
    now: datetime,
    notifier: Notifier,
) -> int:
    try:
        callback(value)
        return 0
    except Exception as exc:
        event = value if isinstance(value, Mapping) else {}
        append_watch_event(
            events_path,
            symbol=str(event.get("symbol") or ""),
            trading_date=trading_date,
            event_type="trend_review_callback_failed",
            occurred_at=now.isoformat(timespec="seconds"),
            last_price=_optional_decimal(event.get("last_price")),
            active_line=_optional_decimal(event.get("active_line")),
            reason=str(exc),
        )
        prior_events = load_watch_events(events_path)
        already_notified = any(
            event.get("event_type") == "trend_review_callback_failure_notified"
            and event.get("trading_date") == trading_date
            and event.get("reason") == str(exc)
            for event in prior_events
        )
        if not already_notified:
            attempts = send_notification_with_results(
                notifier,
                f"趋势模拟执行失败 · {trading_date}",
                str(exc),
            )
            if any(attempt.success for attempt in attempts):
                append_watch_event(
                    events_path,
                    symbol=str(event.get("symbol") or ""),
                    trading_date=trading_date,
                    event_type="trend_review_callback_failure_notified",
                    occurred_at=now.isoformat(timespec="seconds"),
                    last_price=_optional_decimal(event.get("last_price")),
                    active_line=_optional_decimal(event.get("active_line")),
                    reason=str(exc),
                )
        return 1


def _deliver_trigger_notification(
    *,
    events_path: Path,
    notifier: Notifier,
    trading_date: str,
    now: datetime,
    symbol: str,
    position_name: str,
    last_price: Decimal | None,
    active_line: Decimal | None,
    delivered_feishu: set[str],
    delivered_macos: set[str],
    replay: bool,
    market_label: str = "A股",
) -> None:
    if replay:
        message = (
            f"今日已触发活动保护线 {active_line if active_line is not None else '未知'}；"
            "此前提醒未完整送达，建议动作：全部卖出（人工执行）"
        )
    else:
        message = (
            f"最新价 {last_price} <= 活动保护线 {active_line}\n"
            "建议动作：全部卖出（人工执行）"
        )
    for channels, event_type, delivered in (
        (
            {"feishu", "feishu_app"},
            "protection_triggered_notification_delivered_feishu",
            delivered_feishu,
        ),
        (
            {"macos"},
            "protection_triggered_notification_delivered_macos",
            delivered_macos,
        ),
    ):
        if symbol in delivered:
            continue
        attempts = send_notification_with_results(
            notifier,
            f"{market_label}保护线触发 · {symbol}",
            message,
            channels=channels,
        )
        if any(attempt.success for attempt in attempts):
            append_watch_event(
                events_path,
                symbol=symbol,
                trading_date=trading_date,
                event_type=event_type,
                occurred_at=now.isoformat(timespec="seconds"),
                last_price=last_price,
                active_line=active_line,
            )
            delivered.add(symbol)

    if replay or not _has_xiaoai_notifier(notifier):
        return

    voice_message = "\n".join(
        [
            f"名称：{position_name}",
            f"最新价 {last_price} <= 活动保护线 {active_line}",
            "建议动作：全部卖出（人工执行）",
        ]
    )
    attempts = send_notification_with_results(
        notifier,
        f"{market_label}保护线触发 · {symbol}",
        voice_message,
        channels={"xiaoai"},
    )
    if not attempts:
        return
    attempt = attempts[0]
    if attempt.suppressed:
        append_watch_event(
            events_path,
            symbol=symbol,
            trading_date=trading_date,
            event_type=(
                "protection_triggered_notification_suppressed_quiet_hours_xiaoai"
            ),
            occurred_at=now.isoformat(timespec="seconds"),
            last_price=last_price,
            active_line=active_line,
            market=market_label,
        )
        return
    reason = "" if attempt.success else "音箱连接或播放失败"
    append_watch_event(
        events_path,
        symbol=symbol,
        trading_date=trading_date,
        event_type=(
            "protection_triggered_notification_queued_xiaoai"
            if attempt.success
            else "protection_triggered_notification_failed_xiaoai"
        ),
        occurred_at=now.isoformat(timespec="seconds"),
        last_price=last_price,
        active_line=active_line,
        market=market_label,
        reason=reason,
    )
    if attempt.success:
        return
    send_notification_with_results(
        notifier,
        "Open Trader 语音播报失败",
        "\n".join(
            [
                f"市场：{market_label}",
                f"标的：{position_name or symbol}（{symbol}）",
                "原事件：活动保护线触发",
                f"失败原因：{reason}",
                "处理：语音不重试，请按原保护线通知人工确认。",
            ]
        ),
        channels={"feishu", "feishu_app"},
    )


def _has_xiaoai_notifier(notifier: Notifier) -> bool:
    targets = (
        notifier._notifiers
        if isinstance(notifier, CompositeNotifier)
        else [notifier]
    )
    return any(isinstance(target, XiaoaiSSHNotifier) for target in targets)


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
    *,
    market_label: str = "A股",
    broker_label: str = "东方财富",
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
        f"{market_label}价格监控中断",
        f"Futu OpenD 实时价格不可用：{error}\n请立即在{broker_label}人工核价。",
    )


def _record_recovery(
    events_path: Path,
    notifier: Notifier,
    trading_date: str,
    now: datetime,
    *,
    market_label: str = "A股",
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
        f"{market_label}价格监控恢复",
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
