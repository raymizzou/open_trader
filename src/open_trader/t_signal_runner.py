from __future__ import annotations

import csv
import json
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

from .account_sync_state import (
    CONTROLLER_STALE_SECONDS,
    effective_source_status,
    load_account_sync_state,
)
from .daily_premarket import send_notification_with_results
from .notifications import Notifier, NullNotifier
from .t_signal import (
    TMarketFacts,
    TPortfolioBaseline,
    TSignal,
    TSignalEvidence,
    TSignalHardGate,
    TSignalInterpreter,
    TSignalLiquidity,
    TSignalNotification,
    TSignalPrice,
    TSignalTechnical,
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
    blocked_count: int = 0


def run_t_signal_watch_once(
    *,
    portfolio_path: Path,
    account_state_path: Path,
    controller_status_path: Path,
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
    if now.tzinfo is None or now.utcoffset() is None:
        now = now.astimezone()
    updated_at = now.isoformat(timespec="seconds")
    signals: list[TSignal] = []
    notified_count = 0
    blocked_count = 0
    try:
        previous_by_key = index_t_signals_by_market_symbol(
            load_t_signals_cache(t_signals_latest_path(data_dir, normalized_market))
        )
        signal_interpreter = interpreter or TSignalInterpreter()
        notification_target = notifier or NullNotifier()
        source_statuses = _source_statuses(account_state_path, now)
        controller_status = _controller_status(controller_status_path, now)
        for row in _load_t_signal_portfolio_rows(portfolio_path, normalized_market):
            previous = previous_by_key.get((row["market"], row["symbol"]))
            blocked_reason = _blocked_reason(row, source_statuses, controller_status)
            if blocked_reason:
                signals.append(
                    _build_blocked_signal(
                        row=row,
                        previous=previous,
                        run_date=run_date,
                        session_phase=session_phase,
                        updated_at=updated_at,
                        error=blocked_reason,
                    )
                )
                blocked_count += 1
                continue
            try:
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
                    previous=previous,
                    ai_summary_zh="",
                )
                signal = signal_interpreter.interpret(signal)
            except Exception as exc:
                signal = _build_error_signal(
                    row=row,
                    run_date=run_date,
                    session_phase=session_phase,
                    updated_at=updated_at,
                    error=exc,
                )
            signal, sent = _apply_notification_state(
                signal,
                previous=previous,
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
        blocked_count=blocked_count,
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
                    "brokers": _canonical_brokers(row.get("brokers")),
                }
            )
    return rows


def _canonical_brokers(value: str | None) -> tuple[str, ...]:
    return tuple(dict.fromkeys(part.strip().lower() for part in (value or "").split(";") if part.strip()))


def _source_statuses(account_state_path: Path, now: datetime) -> dict[str, tuple[str, str]]:
    state = load_account_sync_state(account_state_path)
    brokers = state.get("brokers")
    if not isinstance(brokers, dict):
        return {}
    return {
        broker: (
            effective_source_status(source, now=now),
            str(source.get("last_success_at") or ""),
        )
        for broker, source in brokers.items()
        if isinstance(broker, str) and isinstance(source, dict)
    }


def _controller_status(controller_status_path: Path, now: datetime) -> str:
    try:
        payload = json.loads(controller_status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "unknown"
    if not isinstance(payload, dict) or payload.get("schema_version") != "open_trader.account_sync.controller.v1":
        return "unknown"
    try:
        heartbeat = datetime.fromisoformat(str(payload.get("heartbeat_at") or ""))
    except ValueError:
        return "unknown"
    if heartbeat.tzinfo is None or heartbeat.utcoffset() is None:
        return "unknown"
    return "stale" if (now - heartbeat).total_seconds() > CONTROLLER_STALE_SECONDS else "ok"


def _blocked_reason(
    row: dict[str, Any],
    source_statuses: dict[str, tuple[str, str]],
    controller_status: str,
) -> str:
    broker_names = row["brokers"]
    assert isinstance(broker_names, tuple)
    last_success = next(
        (source_statuses.get(broker, ("unknown", ""))[1] for broker in broker_names if source_statuses.get(broker, ("unknown", ""))[1]),
        "",
    )
    if controller_status != "ok":
        return _blocked_message(f"账户同步控制器心跳{_status_label(controller_status)}", last_success)
    for broker in broker_names:
        status, last_success = source_statuses.get(broker, ("unknown", ""))
        if status != "ok":
            return _blocked_message(f"账户数据{_status_label(status)}", last_success)
    return "" if broker_names else _blocked_message("账户数据状态未知", "")


def _status_label(status: str) -> str:
    return {"stale": "已过期", "failed": "同步失败", "unknown": "状态未知"}.get(status, "状态未知")


def _blocked_message(label: str, last_success: str) -> str:
    if last_success:
        return f"{label}，数据截至 {last_success}，仅供人工复核。"
    return f"{label}，最近成功时间未知，仅供人工复核。"


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
        return _carry_previous_notification_cycle(signal, previous), False
    previous_match = _previous_notification_match_type(signal, previous)
    if previous_match:
        was_notified = previous_match == "notified"
        return _append_notification_event(
            signal,
            event_type="notification_suppressed",
            message_zh=(
                f"{signal.action} 信号已通知，本轮不重复发送。"
                if was_notified
                else f"{signal.action} 通知已尝试发送，本轮不重复尝试。"
            ),
            notified=was_notified,
            should_notify=False,
            last_notified_at=_previous_last_notified_at(previous),
            last_notified_dedupe_key=(
                signal.notification.dedupe_key
                if was_notified
                else _previous_last_notified_dedupe_key(previous)
            ),
            last_attempted_dedupe_key=signal.notification.dedupe_key,
            event_at=notified_at,
        ), False

    if getattr(notifier, "records_delivery", True) is False:
        return signal, False

    attempts = send_notification_with_results(
        notifier,
        _notification_title(signal),
        _notification_message(signal),
        channels={"macos", "xiaoai"},
    )
    failures = [
        attempt for attempt in attempts if not attempt.success and not attempt.suppressed
    ]
    if failures:
        return _append_notification_event(
            signal,
            event_type="notification_failed",
            message_zh=f"{signal.action} 通知发送失败，信号已保留在 UI 中。",
            notified=False,
            should_notify=False,
            last_notified_at=_previous_last_notified_at(previous),
            last_notified_dedupe_key=_previous_last_notified_dedupe_key(previous),
            last_attempted_dedupe_key=signal.notification.dedupe_key,
            event_at=notified_at,
            status="review",
            error=f"notification failed: {failures[0].error or failures[0].error_type}",
        ), False
    if not attempts:
        return _append_notification_event(
            signal,
            event_type="notification_suppressed",
            message_zh=f"{signal.action} 通知因飞书策略未发送，信号已保留在 UI 中。",
            notified=False,
            should_notify=False,
            last_notified_at=_previous_last_notified_at(previous),
            last_notified_dedupe_key=_previous_last_notified_dedupe_key(previous),
            last_attempted_dedupe_key=signal.notification.dedupe_key,
            event_at=notified_at,
        ), False
    return _append_notification_event(
        signal,
        event_type="notification_sent",
        message_zh=f"已发送 {signal.action} 通知。",
        notified=True,
        should_notify=False,
        last_notified_at=notified_at,
        last_notified_dedupe_key=signal.notification.dedupe_key,
        last_attempted_dedupe_key=signal.notification.dedupe_key,
        event_at=notified_at,
    ), True


def _previous_notification_match_type(
    signal: TSignal,
    previous: dict[str, Any] | None,
) -> str:
    if signal.notification.dedupe_key == _previous_last_notified_dedupe_key(previous):
        return "notified"
    if signal.notification.dedupe_key == _previous_last_attempted_dedupe_key(previous):
        return "attempted"
    return ""


def _previous_notification(previous: dict[str, Any] | None) -> dict[str, Any]:
    if previous is None:
        return {}
    notification = previous.get("notification")
    if not isinstance(notification, dict):
        return {}
    return notification


def _previous_last_notified_at(previous: dict[str, Any] | None) -> str:
    return str(_previous_notification(previous).get("last_notified_at") or "")


def _previous_last_notified_dedupe_key(previous: dict[str, Any] | None) -> str:
    notification = _previous_notification(previous)
    explicit = str(notification.get("last_notified_dedupe_key") or "")
    if explicit:
        return explicit
    if notification.get("notified") is True:
        return str(notification.get("dedupe_key") or "")
    return ""


def _previous_last_attempted_dedupe_key(previous: dict[str, Any] | None) -> str:
    notification = _previous_notification(previous)
    explicit = str(notification.get("last_attempted_dedupe_key") or "")
    if explicit:
        return explicit
    if notification.get("notified") is True:
        return str(notification.get("dedupe_key") or "")
    return ""


def _carry_previous_notification_cycle(
    signal: TSignal,
    previous: dict[str, Any] | None,
) -> TSignal:
    previous_notified_key = _previous_last_notified_dedupe_key(previous)
    previous_attempted_key = _previous_last_attempted_dedupe_key(previous)
    previous_notified_at = _previous_last_notified_at(previous)
    if not previous_notified_key and not previous_attempted_key and not previous_notified_at:
        return signal
    return replace(
        signal,
        notification=replace(
            signal.notification,
            notified=signal.notification.dedupe_key == previous_notified_key,
            last_notified_at=previous_notified_at,
            last_notified_dedupe_key=previous_notified_key,
            last_attempted_dedupe_key=previous_attempted_key,
        ),
    )


def _append_notification_event(
    signal: TSignal,
    *,
    event_type: str,
    message_zh: str,
    notified: bool,
    should_notify: bool,
    last_notified_at: str,
    last_notified_dedupe_key: str,
    last_attempted_dedupe_key: str,
    event_at: str,
    status: str | None = None,
    error: str | None = None,
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
            last_notified_dedupe_key=last_notified_dedupe_key,
            last_attempted_dedupe_key=last_attempted_dedupe_key,
        ),
        status=status or signal.status,
        error=error if error is not None else signal.error,
    )


def _build_error_signal(
    *,
    row: dict[str, Any],
    run_date: str,
    session_phase: str,
    updated_at: str,
    error: Exception,
) -> TSignal:
    try:
        futu_symbol = to_futu_symbol(row["market"], row["symbol"])
    except ValueError:
        futu_symbol = f"{row['market']}.{row['symbol']}"
    facts = TMarketFacts(
        run_date=run_date,
        market=row["market"],
        symbol=row["symbol"],
        futu_symbol=futu_symbol,
        name=row["name"],
        session_phase=session_phase,
        updated_at=updated_at,
        last_price=None,
        day_change_pct=None,
        vwap=None,
        ma_1m=None,
        ma_5m=None,
        day_low=None,
        day_high=None,
        bid=None,
        ask=None,
        bid_depth=None,
        ask_depth=None,
        rsi_5m=None,
        volume_ratio_5m=None,
    )
    signal = build_t_signal_from_facts(
        facts=facts,
        baseline=TPortfolioBaseline(total_quantity=row["total_quantity"]),
        previous=None,
        ai_summary_zh="",
    )
    return replace(
        signal,
        current_status="做T信号生成失败，需要人工复核。",
        signal_summary_zh="做T信号生成失败，已转入人工复核。",
        status="error",
        error=str(error),
    )


def _build_blocked_signal(
    *,
    row: dict[str, Any],
    previous: dict[str, Any] | None,
    run_date: str,
    session_phase: str,
    updated_at: str,
    error: str,
) -> TSignal:
    signal = _signal_from_previous(previous) or _build_error_signal(
        row=row,
        run_date=run_date,
        session_phase=session_phase,
        updated_at=updated_at,
        error=RuntimeError(error),
    )
    return replace(
        signal,
        run_date=run_date,
        session_phase=session_phase,
        updated_at=updated_at,
        action="REVIEW",
        suggested_ratio="",
        current_status=error,
        signal_summary_zh=error,
        status="error",
        error=error,
        timeline=[
            *_previous_timeline(previous),
            TSignalTimelineEvent(
                event_at=updated_at,
                event_type="review_required",
                action="REVIEW",
                suggested_ratio="",
                message_zh=error,
            ),
        ],
        notification=replace(
            signal.notification,
            should_notify=False,
            notified=False,
            dedupe_key=f"{run_date}|{signal.futu_symbol}|REVIEW|",
            last_notified_at=_previous_last_notified_at(previous),
            last_notified_dedupe_key=_previous_last_notified_dedupe_key(previous),
            last_attempted_dedupe_key=_previous_last_attempted_dedupe_key(previous),
        ),
    )


def _signal_from_previous(previous: dict[str, Any] | None) -> TSignal | None:
    if previous is None:
        return None
    try:
        timeline = _previous_timeline(previous)
        if not isinstance(previous.get("timeline"), list) or len(timeline) != len(previous["timeline"]):
            return None
        return TSignal(
            **_previous_signal_fields(previous),
            price=TSignalPrice(**_previous_strings(previous.get("price"), TSignalPrice)),
            liquidity=TSignalLiquidity(
                **_previous_strings(previous.get("liquidity"), TSignalLiquidity)
            ),
            technical=TSignalTechnical(
                **_previous_strings(previous.get("technical"), TSignalTechnical)
            ),
            hard_gates=[
                TSignalHardGate(**_previous_strings(item, TSignalHardGate))
                for item in _previous_list(previous.get("hard_gates"))
            ],
            evidence=[
                TSignalEvidence(**_previous_strings(item, TSignalEvidence))
                for item in _previous_list(previous.get("evidence"))
            ],
            timeline=timeline,
            notification=TSignalNotification(
                should_notify=previous["notification"]["should_notify"],
                notified=previous["notification"]["notified"],
                **_previous_strings(previous["notification"], TSignalNotification, skip={"should_notify", "notified"}),
            ),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _previous_signal_fields(previous: dict[str, Any]) -> dict[str, str]:
    return _previous_strings(
        previous,
        TSignal,
        skip={"price", "liquidity", "technical", "hard_gates", "evidence", "timeline", "notification"},
    )


def _previous_strings(
    value: object,
    fields: type[Any],
    *,
    skip: set[str] = set(),
) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError
    result = {name: value[name] for name in fields.__dataclass_fields__ if name not in skip}
    if not all(isinstance(item, str) for item in result.values()):
        raise ValueError
    return result


def _previous_list(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError
    return value


def _previous_timeline(previous: dict[str, Any] | None) -> list[TSignalTimelineEvent]:
    records = previous.get("timeline") if previous else None
    if not isinstance(records, list):
        return []
    timeline: list[TSignalTimelineEvent] = []
    for record in records:
        if not isinstance(record, dict):
            return []
        action = record.get("action")
        suggested_ratio = record.get("suggested_ratio")
        event_type = record.get("event_type")
        event_at = record.get("event_at")
        message_zh = record.get("message_zh")
        if (
            action not in {"BUY_T", "SELL_T", "HOLD", "REVIEW"}
            or not isinstance(suggested_ratio, str)
            or (action in {"BUY_T", "SELL_T"}) != bool(suggested_ratio)
            or event_type not in {
                "signal_created",
                "signal_changed",
                "notification_sent",
                "notification_suppressed",
                "notification_failed",
                "signal_expired",
                "review_required",
            }
            or not isinstance(event_at, str)
            or not isinstance(message_zh, str)
        ):
            return []
        timeline.append(
            TSignalTimelineEvent(
                event_at=event_at,
                event_type=event_type,
                action=action,
                suggested_ratio=suggested_ratio,
                message_zh=message_zh,
            )
        )
    return timeline


def _notification_title(signal: TSignal) -> str:
    return f"Open Trader｜做T提醒｜{signal.futu_symbol}｜{_action_label(signal.action)}"


def _notification_message(signal: TSignal) -> str:
    evidence_lines = _notification_evidence_lines(signal)
    return "\n".join(
        [
            f"动作：{_action_label(signal.action)}",
            f"比例：{signal.suggested_ratio}%" if signal.suggested_ratio else "比例：-",
            f"状态：{_notification_status(signal)}",
            "",
            "结论：",
            _localized_action_text(signal.signal_summary_zh.strip(), signal.action),
            "",
            "依据：",
            *evidence_lines,
            "",
            f"时间：{_notification_time(signal.updated_at)}",
        ]
    ).strip()


def _action_label(action: str) -> str:
    return {
        "BUY_T": "买入做T",
        "SELL_T": "卖出做T",
    }.get(action, action)


def _notification_status(signal: TSignal) -> str:
    phase = {
        "pre_market": "盘前",
        "regular": "盘中",
        "post_market": "盘后",
        "closed": "休市",
        "unknown": "未知时段",
    }.get(signal.session_phase, "未知时段")
    if signal.status == "ok":
        return f"{phase}有效，等待执行确认"
    status = _localized_action_text(signal.current_status.strip(), signal.action)
    return status or f"{phase}需要复核"


def _notification_evidence_lines(signal: TSignal) -> list[str]:
    lines = [
        f"{index}. {item.message_zh.strip()}"
        for index, item in enumerate(signal.evidence, start=1)
        if item.message_zh.strip()
    ]
    return lines or ["-"]


def _notification_time(updated_at: str) -> str:
    value = updated_at.strip()
    if not value:
        return "-"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def _localized_action_text(text: str, action: str) -> str:
    label = _action_label(action)
    return (
        text.replace(f"触发 {action}", f"触发{label}")
        .replace(f"生成 {action} 信号", f"生成{label}信号")
        .replace(f"{action} 条件满足", f"{label}条件满足")
        .replace(action, label)
    )
