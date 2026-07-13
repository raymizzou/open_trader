from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal


ConditionKind = Literal["price_at_or_above", "price_at_or_below", "deadline"]
EvaluationStatus = Literal["waiting", "triggered"]


@dataclass(frozen=True)
class PlanCondition:
    condition_id: str
    kind: ConditionKind
    target_quantity: Decimal
    reason: str
    trigger_price: Decimal | None = None
    deadline: datetime | None = None


@dataclass(frozen=True)
class StrategyPlan:
    plan_id: str
    market: str
    symbol: str
    current_quantity: Decimal
    conditions: tuple[PlanCondition, ...]


@dataclass(frozen=True)
class PlanEvaluation:
    plan_id: str
    status: EvaluationStatus
    condition_id: str
    target_quantity: Decimal
    reason: str


def evaluate_plan(
    plan: StrategyPlan,
    *,
    last_price: Decimal,
    as_of: datetime,
) -> PlanEvaluation:
    del as_of
    for condition in plan.conditions:
        if (
            condition.kind == "price_at_or_above"
            and condition.trigger_price is not None
            and last_price >= condition.trigger_price
        ):
            return PlanEvaluation(
                plan_id=plan.plan_id,
                status="triggered",
                condition_id=condition.condition_id,
                target_quantity=condition.target_quantity,
                reason=condition.reason,
            )
    return PlanEvaluation(
        plan_id=plan.plan_id,
        status="waiting",
        condition_id="",
        target_quantity=plan.current_quantity,
        reason="",
    )
