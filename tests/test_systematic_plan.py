from datetime import datetime
from decimal import Decimal

from open_trader.systematic_plan import (
    PlanCondition,
    StrategyPlan,
    evaluate_plan,
    order_for_target,
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


def test_plan_targets_zero_when_protection_price_is_reached() -> None:
    plan = StrategyPlan(
        plan_id="US.DRAM:2026-07-13:v1",
        market="US",
        symbol="DRAM",
        current_quantity=Decimal("400"),
        conditions=(
            PlanCondition(
                condition_id="exit-at-protection",
                kind="price_at_or_below",
                target_quantity=Decimal("0"),
                trigger_price=Decimal("57"),
                reason="structural support invalidated",
            ),
        ),
    )

    result = evaluate_plan(
        plan,
        last_price=Decimal("57"),
        as_of=datetime.fromisoformat("2026-07-13T10:00:00"),
    )

    assert result.status == "triggered"
    assert result.condition_id == "exit-at-protection"
    assert result.target_quantity == Decimal("0")


def test_plan_targets_reduced_position_when_deadline_is_reached() -> None:
    plan = StrategyPlan(
        plan_id="US.DRAM:2026-07-13:v1",
        market="US",
        symbol="DRAM",
        current_quantity=Decimal("400"),
        conditions=(
            PlanCondition(
                condition_id="trim-at-deadline",
                kind="deadline",
                target_quantity=Decimal("300"),
                deadline=datetime.fromisoformat("2026-07-15T16:00:00"),
                reason="bounce window expired",
            ),
        ),
    )

    result = evaluate_plan(
        plan,
        last_price=Decimal("63"),
        as_of=datetime.fromisoformat("2026-07-15T16:00:00"),
    )

    assert result.status == "triggered"
    assert result.condition_id == "trim-at-deadline"
    assert result.target_quantity == Decimal("300")


def test_order_uses_only_the_remaining_difference_to_target() -> None:
    first = order_for_target(
        current_quantity=Decimal("400"),
        target_quantity=Decimal("300"),
    )
    after_partial_fill = order_for_target(
        current_quantity=Decimal("330"),
        target_quantity=Decimal("300"),
    )

    assert first is not None
    assert first.side == "sell"
    assert first.quantity == Decimal("100")
    assert after_partial_fill is not None
    assert after_partial_fill.side == "sell"
    assert after_partial_fill.quantity == Decimal("30")
