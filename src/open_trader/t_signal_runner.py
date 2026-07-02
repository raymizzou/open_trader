from __future__ import annotations

import csv
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

from .notifications import Notifier, NullNotifier
from .t_signal import (
    TPortfolioBaseline,
    TSignal,
    TSignalInterpreter,
    TSignalTimelineEvent,
    build_t_signal_from_facts,
    to_futu_symbol,
)
from .t_signal_store import (
    index_t_signals_by_market_symbol,
    load_t_signals_cache,
    t_signals_latest_path,
    write_t_signals_artifact,
)


class TSignalMarketDataClient(Protocol):
    def get_market_facts(
        self,
        *,
        run_date: str,
        market: str,
        symbol: str,
        futu_symbol: str,
        name: str,
        session_phase: str,
        updated_at: str,
    ) -> Any:
        ...

    def close(self) -> None:
        ...


class TSignalInterpreterProtocol(Protocol):
    def interpret(self, signal: TSignal) -> TSignal:
        ...


@dataclass(frozen=True)
class TSignalWatchResult:
    run_date: str
    market: str
    signal_count: int
    notified_count: int
    run_path: Path
    latest_path: Path


def run_t_signal_watch_once(
    *,
    portfolio_path: Path,
    data_dir: Path,
    run_date: str,
    market: str,
    session_phase: str,
    market_data_client: TSignalMarketDataClient,
    interpreter: TSignalInterpreterProtocol | None = None,
    notifier: Notifier | None = None,
    now_fn: Any = datetime.now,
) -> TSignalWatchResult:
    normalized_market = market.strip().upper()
    now = now_fn()
    updated_at = now.isoformat(timespec="seconds")
    previous_by_key = index_t_signals_by_market_symbol(
        load_t_signals_cache(t_signals_latest_path(data_dir, normalized_market))
    )
    signal_interpreter = interpreter or TSignalInterpreter()
    notification_target = notifier or NullNotifier()
    signals: list[TSignal] = []
    notified_count = 0
    try:
        for row in _load_t_signal_portfolio_rows(portfolio_path, normalized_market):
            futu_symbol = to_futu_symbol(row["market"], row["symbol"])
            facts = market_data_client.get_market_facts(
                run_date=run_date,
                market=row["market"],
                symbol=row["symbol"],
                futu_symbol=futu_symbol,
                name=row["name"],
                session_phase=session_phase,
                updated_at=updated_at,
            )
            signal = build_t_signal_from_facts(
                facts=facts,
                baseline=TPortfolioBaseline(total_quantity=row["total_quantity"]),
                previous=previous_by_key.get((row["market"], row["symbol"])),
                ai_summary_zh="",
            )
            signal = signal_interpreter.interpret(signal)
            signal, sent = _apply_notification_state(
                signal,
                previous=previous_by_key.get((signal.market, signal.symbol)),
                notifier=notification_target,
                notified_at=updated_at,
            )
            notified_count += 1 if sent else 0
            signals.append(signal)
    finally:
        market_data_client.close()

    artifact = write_t_signals_artifact(
        data_dir=data_dir,
        run_date=run_date,
        market=normalized_market,
        signals=signals,
        generated_at=updated_at,
    )
    return TSignalWatchResult(
        run_date=run_date,
        market=normalized_market,
        signal_count=len(signals),
        notified_count=notified_count,
        run_path=artifact.run_path,
        latest_path=artifact.latest_path,
    )


def _load_t_signal_portfolio_rows(
    portfolio_path: Path,
    market: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with portfolio_path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            normalized_market = (row.get("market") or "").strip().upper()
            symbol = (row.get("symbol") or "").strip().upper()
            if normalized_market != market or not symbol:
                continue
            quantity = _positive_decimal(row.get("total_quantity"))
            if quantity is None:
                continue
            try:
                futu_symbol = to_futu_symbol(normalized_market, symbol)
            except ValueError:
                continue
            del futu_symbol
            rows.append(
                {
                    "market": normalized_market,
                    "symbol": symbol,
                    "name": (row.get("name") or "").strip(),
                    "total_quantity": quantity,
                }
            )
    return rows


def _positive_decimal(value: str | None) -> Decimal | None:
    try:
        decimal = Decimal(str(value or "").strip())
    except (InvalidOperation, ValueError):
        return None
    if not decimal.is_finite() or decimal <= 0:
        return None
    return decimal


def _apply_notification_state(
    signal: TSignal,
    *,
    previous: dict[str, Any] | None,
    notifier: Notifier,
    notified_at: str,
) -> tuple[TSignal, bool]:
    if not signal.notification.should_notify:
        return signal, False
    if _previous_notification_matches(signal, previous):
        return _append_notification_event(
            signal,
            event_type="notification_suppressed",
            message_zh=f"{signal.action} 信号已通知，本轮不重复发送。",
            notified=True,
            should_notify=False,
            last_notified_at=str(
                (previous or {}).get("notification", {}).get("last_notified_at") or ""
            ),
            event_at=notified_at,
        ), False

    notifier.notify(_notification_title(signal), _notification_message(signal))
    return _append_notification_event(
        signal,
        event_type="notification_sent",
        message_zh=f"已发送 {signal.action} 通知。",
        notified=True,
        should_notify=False,
        last_notified_at=notified_at,
        event_at=notified_at,
    ), True


def _previous_notification_matches(signal: TSignal, previous: dict[str, Any] | None) -> bool:
    if previous is None:
        return False
    notification = previous.get("notification")
    if not isinstance(notification, dict):
        return False
    return (
        notification.get("dedupe_key") == signal.notification.dedupe_key
        and notification.get("notified") is True
    )


def _append_notification_event(
    signal: TSignal,
    *,
    event_type: str,
    message_zh: str,
    notified: bool,
    should_notify: bool,
    last_notified_at: str,
    event_at: str,
) -> TSignal:
    return replace(
        signal,
        timeline=[
            *signal.timeline,
            TSignalTimelineEvent(
                event_at=event_at,
                event_type=event_type,
                action=signal.action,
                suggested_ratio=signal.suggested_ratio,
                message_zh=message_zh,
            ),
        ],
        notification=replace(
            signal.notification,
            should_notify=should_notify,
            notified=notified,
            last_notified_at=last_notified_at,
        ),
    )


def _notification_title(signal: TSignal) -> str:
    return f"Open Trader｜做T提醒｜{signal.market}"


def _notification_message(signal: TSignal) -> str:
    ratio = f" {signal.suggested_ratio}%" if signal.suggested_ratio else ""
    return "\n".join(
        [
            f"{signal.symbol} {signal.action}{ratio}",
            signal.signal_summary_zh.strip(),
            f"依据：{'; '.join(item.message_zh for item in signal.evidence)}",
        ]
    ).strip()
