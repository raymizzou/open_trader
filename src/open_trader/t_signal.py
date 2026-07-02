from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any


SCHEMA_VERSION = "open_trader.t_signal.v1"
SESSION_PHASES = {"pre_market", "regular", "post_market", "closed", "unknown"}
ACTIONS = {"BUY_T", "SELL_T", "HOLD", "REVIEW"}
SUGGESTED_RATIOS = {"", "6", "10", "15", "20"}
DEPTH_STATUSES = {"pass", "thin", "wide_spread", "missing"}
PRICE_POSITIONS = {
    "near_support",
    "near_resistance",
    "below_vwap_reclaim",
    "above_vwap_reject",
    "middle_range",
    "breakout",
    "breakdown",
    "unknown",
}
TREND_STATES = {
    "range_rebound",
    "range_fade",
    "uptrend",
    "downtrend",
    "choppy",
    "unknown",
}
GATE_STATUSES = {"pass", "block", "warn", "missing"}
EVIDENCE_DIRECTIONS = {"buy", "sell", "neutral", "risk"}
EVIDENCE_STRENGTHS = {"low", "medium", "high"}
TIMELINE_EVENT_TYPES = {
    "signal_created",
    "signal_changed",
    "notification_sent",
    "notification_suppressed",
    "signal_expired",
    "review_required",
}
STATUSES = {"ok", "review", "blocked", "error", "stale"}


@dataclass(frozen=True)
class TSignalPrice:
    last_price: str
    day_change_pct: str
    vwap: str
    ma_1m: str
    ma_5m: str
    day_low: str
    day_high: str


@dataclass(frozen=True)
class TSignalLiquidity:
    bid: str
    ask: str
    spread_pct: str
    bid_depth: str
    ask_depth: str
    depth_status: str


@dataclass(frozen=True)
class TSignalTechnical:
    rsi_5m: str
    volume_ratio_5m: str
    price_position: str
    trend_state: str


@dataclass(frozen=True)
class TSignalHardGate:
    name: str
    status: str
    message_zh: str


@dataclass(frozen=True)
class TSignalEvidence:
    name: str
    direction: str
    strength: str
    message_zh: str


@dataclass(frozen=True)
class TSignalTimelineEvent:
    event_at: str
    event_type: str
    action: str
    suggested_ratio: str
    message_zh: str


@dataclass(frozen=True)
class TSignalNotification:
    should_notify: bool
    notified: bool
    dedupe_key: str
    last_notified_at: str


@dataclass(frozen=True)
class TSignal:
    schema_version: str
    run_date: str
    market: str
    symbol: str
    futu_symbol: str
    name: str
    session_phase: str
    updated_at: str
    action: str
    suggested_ratio: str
    current_status: str
    signal_summary_zh: str
    price: TSignalPrice
    liquidity: TSignalLiquidity
    technical: TSignalTechnical
    hard_gates: list[TSignalHardGate]
    evidence: list[TSignalEvidence]
    timeline: list[TSignalTimelineEvent]
    notification: TSignalNotification
    status: str
    error: str

    def to_dict(self) -> dict[str, Any]:
        validate_t_signal(self)
        return asdict(self)

    def with_field(self, name: str, value: object) -> TSignal:
        return replace(self, **{name: value})


def validate_t_signal(signal: TSignal) -> None:
    _require_member("schema_version", signal.schema_version, {SCHEMA_VERSION})
    _require_member("session_phase", signal.session_phase, SESSION_PHASES)
    _require_member("action", signal.action, ACTIONS)
    _require_member("suggested_ratio", signal.suggested_ratio, SUGGESTED_RATIOS)
    _require_member("status", signal.status, STATUSES)

    _require_member("depth_status", signal.liquidity.depth_status, DEPTH_STATUSES)
    _require_member("price_position", signal.technical.price_position, PRICE_POSITIONS)
    _require_member("trend_state", signal.technical.trend_state, TREND_STATES)

    for gate in signal.hard_gates:
        _require_member("hard_gates.status", gate.status, GATE_STATUSES)

    for item in signal.evidence:
        _require_member("evidence.direction", item.direction, EVIDENCE_DIRECTIONS)
        _require_member("evidence.strength", item.strength, EVIDENCE_STRENGTHS)

    for event in signal.timeline:
        _require_member("timeline.event_type", event.event_type, TIMELINE_EVENT_TYPES)
        _require_member("timeline.action", event.action, ACTIONS)
        _require_member("timeline.suggested_ratio", event.suggested_ratio, SUGGESTED_RATIOS)
        _validate_ratio_invariant(event.action, event.suggested_ratio)

    _validate_ratio_invariant(signal.action, signal.suggested_ratio)


def _validate_ratio_invariant(action: str, suggested_ratio: str) -> None:
    if action in {"BUY_T", "SELL_T"} and not suggested_ratio:
        raise ValueError(f"{action} requires suggested_ratio")
    if action in {"HOLD", "REVIEW"} and suggested_ratio:
        raise ValueError(f"{action} requires empty suggested_ratio")


def _require_member(field_name: str, value: str, allowed: set[str]) -> None:
    if value not in allowed:
        raise ValueError(f"invalid {field_name}: {value}")
