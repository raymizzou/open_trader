from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date, datetime, time, timedelta
from pathlib import Path
import time as time_module
from zoneinfo import ZoneInfo

from .a_share_trend_watch import (
    AShareWatchResult,
    _close,
    _load_active_lines,
    _monitor_interrupted,
    _record_interruption,
    _run_review_callback,
    _notify_trend_review_deadline,
    watch_a_share_protection,
)
from .futu_quote import FutuQuoteClient, FutuQuoteError
from .market_trend import _market
from .notifications import Notifier


MARKET_TIMEZONES = {
    "HK": ZoneInfo("Asia/Hong_Kong"),
    "US": ZoneInfo("America/New_York"),
}
MARKET_LABELS = {"HK": "港股", "US": "美股"}
BROKER_LABELS = {"HK": "辉立", "US": "老虎"}


def _abnormal_result(events_path: Path) -> AShareWatchResult:
    return AShareWatchResult(
        status="abnormal",
        watched_symbol_count=0,
        trigger_count=0,
        exception_count=1,
        unknown_quote_count=0,
        events_path=events_path,
    )


def market_session(now: datetime, market: str) -> str:
    market = _market(market)
    local = now.astimezone(MARKET_TIMEZONES[market]).time()
    if local < time(9, 30):
        return "before"
    if market == "HK":
        if local <= time(12):
            return "morning"
        if local < time(13):
            return "lunch"
        if local <= time(16):
            return "afternoon"
        return "closed"
    return "open" if local <= time(16) else "closed"


def next_market_open(quote: object, *, market: str, now: datetime) -> datetime:
    market = _market(market)
    timezone = MARKET_TIMEZONES[market]
    local = now.astimezone(timezone)
    calendar = quote.get_trading_days(
        market=market,
        start=(local.date() - timedelta(days=1)).isoformat(),
        end=(local.date() + timedelta(days=14)).isoformat(),
    )
    for trading_date in sorted(date.fromisoformat(item) for item in calendar):
        if trading_date < local.date():
            continue
        opening = datetime.combine(trading_date, time(9, 30), tzinfo=timezone)
        if trading_date == local.date():
            session = market_session(local, market)
            if session not in {"closed"}:
                return opening if session == "before" else local
        else:
            return opening
    raise FutuQuoteError(f"Futu {market} calendar has no upcoming trading session")


def watch_market_protection(
    *,
    market: str,
    data_dir: Path,
    portfolio_path: Path,
    account_loader: Callable[..., object],
    state_path: Path,
    events_path: Path,
    report_lock_path: Path,
    quote_client: object | None,
    close_quote_client: bool = True,
    notifier: Notifier,
    poll_seconds: float,
    reconnect_seconds: float,
    once: bool = False,
    quote_client_factory: Callable[[], object] | None = None,
    now_fn: Callable[[], datetime] = datetime.now,
    sleep_fn: Callable[[float], None] = time_module.sleep,
    on_session_open: Callable[[str], None] | None = None,
    on_protection_trigger: Callable[[Mapping[str, object]], None] | None = None,
) -> AShareWatchResult:
    market = _market(market)
    timezone = MARKET_TIMEZONES[market]
    client = quote_client
    interrupted = _monitor_interrupted(events_path)
    now = now_fn()
    while True:
        if client is None:
            if quote_client_factory is None:
                raise RuntimeError("quote client factory is required after interruption")
            try:
                client = quote_client_factory()
            except FutuQuoteError as exc:
                if not interrupted:
                    _record_interruption(
                        events_path,
                        notifier,
                        now.astimezone(timezone).date().isoformat(),
                        now,
                        str(exc),
                        market_label=MARKET_LABELS[market],
                        broker_label=BROKER_LABELS[market],
                    )
                    interrupted = True
                if once:
                    return _abnormal_result(events_path)
                sleep_fn(reconnect_seconds)
                now = now_fn()
                continue
        try:
            local_date = now.astimezone(timezone).date().isoformat()
            if market_session(now, market) == "closed":
                current_days = client.get_trading_days(
                    market=market,
                    start=local_date,
                    end=local_date,
                )
                if local_date in current_days and on_session_open is not None:
                    _run_review_callback(
                        on_session_open,
                        local_date,
                        events_path=events_path,
                        trading_date=local_date,
                        now=now,
                        notifier=notifier,
                    )
                    _notify_trend_review_deadline(
                        data_dir=data_dir,
                        market=market,
                        trading_date=local_date,
                        now=now,
                        events_path=events_path,
                        notifier=notifier,
                    )
            opening = next_market_open(client, market=market, now=now)
        except FutuQuoteError as exc:
            if not interrupted:
                _record_interruption(
                    events_path,
                    notifier,
                    now.astimezone(timezone).date().isoformat(),
                    now,
                    str(exc),
                    market_label=MARKET_LABELS[market],
                    broker_label=BROKER_LABELS[market],
                )
                interrupted = True
            if once and not close_quote_client:
                raise
            failed_client = client
            client = None
            try:
                _close(failed_client)
            except Exception:
                if not once:
                    raise
            if once:
                return _abnormal_result(events_path)
            sleep_fn(reconnect_seconds)
            now = now_fn()
            continue
        break

    try:
        account_loader(
            portfolio_path,
            expected_date=opening.date().isoformat(),
            timezone=timezone,
        )
    except Exception:
        if not once:
            raise
        if close_quote_client:
            try:
                _close(client)
            except Exception:
                pass
        return _abnormal_result(events_path)
    local_now = now.astimezone(timezone)
    if opening > local_now:
        sleep_fn((opening - local_now).total_seconds())

    return watch_a_share_protection(
        portfolio_path=portfolio_path,
        state_path=state_path,
        events_path=events_path,
        quote_client=client,
        close_quote_client=close_quote_client,
        notifier=notifier,
        poll_seconds=poll_seconds,
        reconnect_seconds=reconnect_seconds,
        once=once,
        quote_client_factory=quote_client_factory,
        report_lock_path=report_lock_path,
        now_fn=now_fn,
        sleep_fn=sleep_fn,
        market=market,
        market_label=MARKET_LABELS[market],
        broker_label=BROKER_LABELS[market],
        session_timezone=timezone,
        session_fn=lambda value: market_session(value, market),
        account_loader=account_loader,
        on_session_open=on_session_open,
        on_protection_trigger=on_protection_trigger,
    )
