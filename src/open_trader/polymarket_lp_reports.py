"""Read-only natural-day summaries over the automatic pool's shared facts."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from .prediction_arbitrage_store import PredictionArbitrageStore


BEIJING = ZoneInfo("Asia/Shanghai")


def _stamp(value: object) -> datetime | None:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(UTC) if parsed.tzinfo else None
    except (ValueError, TypeError):
        return None


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _quantity(value: object) -> Decimal | None:
    try:
        number = Decimal(str(value))
        return number if number.is_finite() and number >= 0 else None
    except InvalidOperation:
        return None


def _bounds(day: str) -> tuple[datetime, datetime]:
    parsed = date.fromisoformat(day)
    if parsed.isoformat() != day:
        raise ValueError("lp_auto_report_date_invalid")
    start = datetime.combine(parsed, time(), BEIJING).astimezone(UTC)
    return start, start + timedelta(days=1)


def build_auto_report(
    facts: Mapping, day: str, *, now: datetime, frozen: bool = False,
) -> dict:
    start, midnight = _bounds(day)
    end = midnight if frozen else min(now, midnight)
    run_id, account_id = facts.get("auto_run_id"), facts.get("account_id")
    state = dict(facts.get("state") or {})
    enabled = _stamp(state.get("enabled_at"))
    gaps = list(facts.get("gaps") or [])
    events: dict[str, dict] = {}
    pending: list[dict] = []
    sessions = {row.get("session_id"): row for row in facts.get("sessions") or [] if isinstance(row, Mapping)}
    for raw in facts.get("events") or []:
        if not isinstance(raw, Mapping) or not run_id or raw.get("auto_run_id") != run_id:
            continue
        if raw.get("account_id", account_id) != account_id:
            continue
        row = dict(raw)
        session = sessions.get(row.get("session_id")) or {}
        row["market_title"] = session.get("market_title", session.get("question"))
        observed = _stamp(row.get("observed_at"))
        if observed is None or observed > now:
            gaps.append("event_observation_time_unknown")
            continue
        key = str(row.get("event_id") or "")
        if not key:
            pending.append({**row, "reason": "event_identity_unknown"})
            gaps.append("event_identity_unknown")
            continue
        previous = events.get(key)
        if previous is None or observed > _stamp(previous.get("observed_at")):
            events[key] = row
    known = []
    undated_orders = set()
    future_intents = {
        row.get("intent_id") for row in events.values()
        if row.get("kind") == "intent"
        and (occurred := _stamp(row.get("occurred_at"))) is not None and occurred >= end
    }
    observed_resolutions = {
        row.get("intent_id") for row in events.values()
        if row.get("kind") in {"accepted", "rejected"}
        and (occurred := _stamp(row.get("occurred_at"))) is not None and occurred < end
    }
    for row in events.values():
        if row.get("kind") == "unknown":
            if row.get("reason") == "order_receipt_unknown":
                recovered = _stamp(row.get("resolved_at"))
                if recovered is not None and recovered <= now:
                    continue
            elif row.get("intent_id") in observed_resolutions:
                continue
        occurred = _stamp(row.get("occurred_at"))
        if occurred is None:
            if row.get("intent_id") in future_intents:
                continue
            pending.append({**row, "reason": "event_time_unknown"})
            gaps.append("event_time_unknown")
            if row.get("order_id"):
                undated_orders.add(row["order_id"])
        elif occurred < end:
            known.append(row)
    # Venue timestamps may have only second precision: a terminal receipt
    # must not be replayed before its acceptance because of an opaque ID.
    action_order = {"intent": 0, "accepted": 1, "cancel_requested": 2, "fill": 3, "cancel": 4}
    known.sort(key=lambda row: (_stamp(row["occurred_at"]), action_order.get(row["kind"], 5), str(row["event_id"])))
    daily = [row for row in known if _stamp(row["occurred_at"]) >= start]
    intents = {row["intent_id"] for row in daily if row.get("kind") == "intent" and row.get("intent_id")}
    resolved = {row.get("intent_id") for row in known if row.get("kind") in {"accepted", "rejected"}}
    for row in known:
        if row.get("kind") == "intent" and row.get("intent_id") not in resolved:
            pending.append({**row, "reason": "submission_unresolved"})
    orders: dict[str, dict] = {}
    filled: dict[str, Decimal] = {}
    for row in known:
        order_id, kind = row.get("order_id"), row.get("kind")
        if not order_id:
            if kind not in {"intent", "unknown", "rejected"}:
                pending.append({**row, "reason": "order_identity_unknown"})
            continue
        if kind == "accepted":
            orders.setdefault(order_id, {**row, "state": "open", "carried_in": _stamp(row["occurred_at"]) < start})
        elif kind == "cancel_requested" and order_id in orders:
            orders[order_id]["state"] = "cancel_pending"
        elif kind == "cancel" and order_id in orders:
            orders[order_id]["state"] = "expired" if row.get("status") == "EXPIRED" else "cancelled"
        elif kind == "fill":
            quantity = _quantity(row.get("quantity"))
            if quantity is not None:
                filled[order_id] = filled.get(order_id, Decimal(0)) + quantity
            else:
                undated_orders.add(order_id)
                gaps.append("fill_quantity_unknown")
    closing = []
    for order_id, row in orders.items():
        quantity = _quantity(row.get("quantity"))
        remaining = None if quantity is None else quantity - filled.get(order_id, Decimal(0))
        if order_id in undated_orders or remaining is None or remaining < 0:
            row["state"], remaining = "unknown", None
        elif remaining == 0:
            row["state"] = "filled"
        if row["state"] not in {"filled", "cancelled", "expired"}:
            closing.append({**row, "remaining_quantity": str(remaining) if remaining is not None else None})
    fills = [row for row in daily if row.get("kind") == "fill"]
    quantities = [_quantity(row.get("quantity")) for row in fills]
    def count(kind):
        return len({row.get("order_id") or row.get("intent_id") or row["event_id"] for row in daily
                    if row.get("kind") == kind and not (kind == "cancel" and row.get("status") == "EXPIRED")})
    late_events = [row for row in known if (
        _stamp(row["observed_at"]) >= end if frozen else
        _stamp(row["occurred_at"]) < start <= _stamp(row["observed_at"])
    )]
    due = midnight + timedelta(minutes=5)
    late_generated = frozen and now > due
    if late_generated:
        gaps.append("late_generation_coverage_unverified")
    if enabled and enabled > start:
        gaps.append("enabled_after_period_start")
    rewards = {}
    for session in facts.get("sessions") or []:
        if not isinstance(session, Mapping):
            continue
        observation = session.get("reward_observation")
        if isinstance(observation, Mapping):
            key = (observation.get("condition_id"), observation.get("reward_date"))
            previous = rewards.get(key)
            if previous is None or str(observation.get("last_attempt_at") or "") > str(previous.get("last_attempt_at") or ""):
                rewards[key] = dict(observation)
    report = {
        "state": "saved" if frozen else "live", "report_date": day,
        "account_id": account_id, "auto_run_id": run_id,
        "period_start": _iso(start), "period_end": _iso(end),
        "generated_at": _iso(now), "due_at": _iso(due), "late_generated": late_generated,
        "generation_delay_seconds": max(0, (now - due).total_seconds()) if frozen else None,
        "data_updated_at": facts.get("as_of"), "runtime": state,
        "coverage": {"enabled_at": state.get("enabled_at"),
                     "actual_start": _iso(max(start, enabled)) if enabled else None,
                     "status": "gaps" if gaps else "saved_facts_only", "gaps": sorted(set(gaps))},
        "metrics": {"intent_count": len(intents), "accepted_count": count("accepted"),
                    "rejected_count": count("rejected"), "unknown_count": len(intents - resolved),
                    "filled_order_count": len({row["order_id"] for row in fills if row.get("order_id")}),
                    "filled_quantity": str(sum(quantities, Decimal(0))) if all(q is not None for q in quantities) else None,
                    "cancelled_order_count": count("cancel")},
        "metric_scope": "confirmed_event_time; intents and distinct orders, not request attempts; fill and cancel may overlap",
        "events": daily, "late_events": late_events, "pending": pending,
        "closing_orders": closing,
        "closing_orders_status": "unknown" if pending or undated_orders or facts.get("gaps") else "from_saved_events",
        "funds": {**dict(facts.get("funds") or {}), "scope": "latest_observation_not_period_end"},
        "financial_period": dict(facts.get("financial_period") or {"status": "unknown", "reason": "historical_financial_boundary_unavailable"}),
        "rewards": {"observations": list(rewards.values()), "automatic_total_usd": None, "paid": False,
                    "scope": "platform_market_day_cumulative_may_include_manual_and_other_periods",
                    "included_in_reusable_funds": False},
    }
    if "facts_read_failed" in gaps:
        report["metrics"] = {key: None for key in report["metrics"]}
        report["closing_orders_status"] = "unknown"
    return report


class AutoDailyReports:
    """Owns only report persistence; readers never reconcile, submit or notify."""

    def __init__(self, store: PredictionArbitrageStore, facts: Callable, state: Callable, *, now: Callable = lambda: datetime.now(UTC)):
        self.store, self._facts, self._state, self._now = store, facts, state, now

    def _read(self, **period) -> dict:
        try:
            return self._facts(**period)
        except (sqlite3.Error, OSError, RuntimeError, ValueError) as exc:
            state = self._state()
            return {"account_id": state.get("account_id"), "auto_run_id": state.get("auto_run_id"),
                    "state": state, "events": [], "sessions": [],
                    "gaps": ["facts_read_failed"], "error": type(exc).__name__,
                    "funds": {"status": "unknown"}, "as_of": None}

    def today(self) -> dict:
        now = self._now()
        day = now.astimezone(BEIJING).date().isoformat()
        start, _ = _bounds(day)
        return build_auto_report(self._read(period_start=_iso(start), period_end=_iso(now)), day, now=now)

    def history(self) -> dict:
        today = self.today()
        return {"today": today, "reports": self.store.lp_auto_daily_reports(
            str(today.get("account_id") or ""), str(today.get("auto_run_id") or ""), summaries=True)}

    def report(self, day: str) -> dict | None:
        _bounds(day)
        facts = self._read()
        return self.store.lp_auto_daily_report(str(facts.get("account_id") or ""), str(facts.get("auto_run_id") or ""), day)

    def generate_due(self) -> list[dict]:
        now = self._now()
        facts = self._read()
        enabled = _stamp((facts.get("state") or {}).get("enabled_at"))
        account, run = facts.get("account_id"), facts.get("auto_run_id")
        if enabled is None or not account or not run:
            return []
        local = now.astimezone(BEIJING)
        completed = local.date() - timedelta(days=1 if local.time() >= time(0, 5) else 2)
        day = enabled.astimezone(BEIJING).date()
        saved = {row["report_date"] for row in self.store.lp_auto_daily_reports(account, run, summaries=True)}
        generated = []
        while day <= completed:
            key = day.isoformat()
            if key not in saved:
                start, end = _bounds(key)
                period_facts = self._read(period_start=_iso(start), period_end=_iso(end))
                if (period_facts.get("account_id"), period_facts.get("auto_run_id")) != (account, run):
                    raise RuntimeError("lp_auto_report_identity_changed")
                report = build_auto_report(period_facts, key, now=now, frozen=True)
                generated.append(self.store.lp_save_auto_daily_report(account, run, key, report))
            day += timedelta(days=1)
        return generated
