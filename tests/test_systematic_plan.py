from datetime import datetime
from decimal import Decimal

from open_trader.systematic_plan import (
    PlanCondition,
    StrategyPlan,
    evaluate_plan,
)


def test_plan_targets_reduced_position_when_upper_price_is_reached() -> None:
    plan = StrategyPlan(
        plan_id="US.DRAM:2026-07-13:v1",
        market="US",
        symbol="DRAM",
        current_quantity=Decimal("400"),
        conditions=(
            PlanCondition(
                condition_id="trim-at-resistance",
                kind="price_at_or_above",
                target_quantity=Decimal("300"),
                trigger_price=Decimal("65"),
                reason="10 EMA resistance",
            ),
        ),
    )

    result = evaluate_plan(
        plan,
        last_price=Decimal("65"),
        as_of=datetime.fromisoformat("2026-07-13T10:00:00"),
    )

    assert result.status == "triggered"
    assert result.condition_id == "trim-at-resistance"
    assert result.target_quantity == Decimal("300")
