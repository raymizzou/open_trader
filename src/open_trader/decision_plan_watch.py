from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from .futu_watch import QuoteClientProtocol
from .notifications import Notifier
from .plan_events import PlanEvent, append_plan_event, load_plan_events


@dataclass(frozen=True)
class DecisionPlanWatchResult:
    watched_plan_count: int
    trigger_count: int
    reset_count: int
    notification_sent_count: int
    notification_failed_count: int
    events_path: Path


def evaluate_plan_snapshot(
    *,
    plan: Mapping[str, object],
    previous_truth: Mapping[str, bool],
    last_price: Decimal,
    as_of: datetime,
) -> tuple[list[PlanEvent], dict[str, bool]]:
    truth = dict(previous_truth)
    events: list[PlanEvent] = []
    for condition in plan.get("conditions", []):
        if not isinstance(condition, Mapping):
            continue
        condition_id = str(condition.get("condition_id") or "")
        operator = condition.get("operator")
        threshold = Decimal(str(condition.get("calculated_value")))
        current = last_price >= threshold if operator == ">=" else last_price <= threshold
        previous = truth.get(condition_id, False)
        truth[condition_id] = current
        if current == previous:
            continue
        events.append(PlanEvent(
            event_id=uuid4().hex,
            plan_id=str(plan["plan_id"]),
            event_type="condition_triggered" if current else "condition_reset",
            condition_id=condition_id,
            occurred_at=as_of.isoformat(timespec="seconds"),
            payload={
                "last_price": str(last_price),
                "operator": str(operator),
                "trigger_value": str(threshold),
                "suggested_action": str(condition.get("suggested_action") or ""),
                "target_quantity": str(condition.get("target_quantity") or ""),
                "target_weight": str(condition.get("target_weight") or ""),
            },
        ))
    return events, truth


def run_decision_plan_watch(
    *,
    plans: Sequence[Mapping[str, object]],
    events_path: Path,
    quote_client: QuoteClientProtocol,
    notifier: Notifier,
    poll_seconds: float,
    once: bool,
    sleep_fn: Callable[[float], None] = time.sleep,
    now_fn: Callable[[], datetime] = datetime.now,
) -> DecisionPlanWatchResult:
    existing = load_plan_events(events_path)
    expired_ids = {
        event.plan_id for event in existing if event.event_type == "plan_expired"
    }
    truth_by_plan: dict[str, dict[str, bool]] = {}
    for event in existing:
        if event.event_type in {"condition_triggered", "condition_reset"}:
            truth_by_plan.setdefault(event.plan_id, {})[event.condition_id] = (
                event.event_type == "condition_triggered"
            )

    now = now_fn()
    active: list[Mapping[str, object]] = []
    for plan in plans:
        if plan.get("mode") != "validated_plan":
            continue
        if now >= datetime.fromisoformat(str(plan["expires_at"])):
            plan_id = str(plan["plan_id"])
            if plan_id not in expired_ids:
                append_plan_event(events_path, PlanEvent(
                    event_id=uuid4().hex, plan_id=plan_id,
                    event_type="plan_expired", condition_id="",
                    occurred_at=now.isoformat(timespec="seconds"), payload={},
                ))
                expired_ids.add(plan_id)
            continue
        active.append(plan)

    symbols = {_futu_symbol(plan): plan for plan in active}
    trigger_count = reset_count = sent_count = failed_count = 0
    try:
        while symbols:
            snapshots = quote_client.get_snapshots(list(symbols))
            as_of = now_fn()
            for futu_symbol, plan in symbols.items():
                snapshot = snapshots.get(futu_symbol)
                if snapshot is None:
                    continue
                plan_id = str(plan["plan_id"])
                transition_events, truth = evaluate_plan_snapshot(
                    plan=plan,
                    previous_truth=truth_by_plan.get(plan_id, {}),
                    last_price=snapshot.last_price,
                    as_of=as_of,
                )
                truth_by_plan[plan_id] = truth
                conditions = {
                    str(item["condition_id"]): item
                    for item in plan.get("conditions", [])
                    if isinstance(item, Mapping)
                }
                for event in transition_events:
                    append_plan_event(events_path, event)
                    if event.event_type == "condition_reset":
                        reset_count += 1
                        continue
                    trigger_count += 1
                    condition = conditions[event.condition_id]
                    try:
                        notifier.notify(
                            f"交易计划触发 · {plan['market']}.{plan['symbol']}",
                            _notification_message(plan, condition, snapshot.last_price),
                        )
                    except Exception as exc:
                        failed_count += 1
                        notification_type = "notification_failed"
                        payload = {"error": str(exc) or exc.__class__.__name__}
                    else:
                        sent_count += 1
                        notification_type = "notification_sent"
                        payload = {}
                    append_plan_event(events_path, PlanEvent(
                        event_id=uuid4().hex, plan_id=plan_id,
                        event_type=notification_type,
                        condition_id=event.condition_id,
                        occurred_at=as_of.isoformat(timespec="seconds"),
                        payload=payload,
                    ))
            if once:
                break
            sleep_fn(poll_seconds)
    finally:
        quote_client.close()
    return DecisionPlanWatchResult(
        watched_plan_count=len(active), trigger_count=trigger_count,
        reset_count=reset_count, notification_sent_count=sent_count,
        notification_failed_count=failed_count, events_path=events_path,
    )


def _futu_symbol(plan: Mapping[str, object]) -> str:
    market = str(plan["market"])
    symbol = str(plan["symbol"])
    return f"{market}.{symbol.zfill(5) if market == 'HK' else symbol}"


def _notification_message(
    plan: Mapping[str, object],
    condition: Mapping[str, object],
    last_price: Decimal,
) -> str:
    return "\n".join((
        f"触发事实：最新价 {last_price} {condition['operator']} {condition['calculated_value']}",
        f"建议动作：{condition['suggested_action']}",
        f"当前总仓位：{plan['current_quantity']}",
        f"目标总仓位：{condition['target_quantity']}（权重 {condition['target_weight']}）",
        f"参数来源：{condition['formula']}；数据日 {condition['source_date']}",
        f"查看计划：http://127.0.0.1:8766/?market={plan['market']}&symbol={plan['symbol']}&decision_tab=final",
    ))
