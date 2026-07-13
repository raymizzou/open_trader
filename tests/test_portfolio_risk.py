from decimal import Decimal

from open_trader.portfolio_risk import apply_single_instrument_limit


def test_risk_caps_new_target_at_ten_percent() -> None:
    result = apply_single_instrument_limit(
        current_quantity=Decimal("5"),
        proposed_quantity=Decimal("15"),
        unit_value_hkd=Decimal("100"),
        portfolio_nav_hkd=Decimal("10000"),
    )

    assert result.final_quantity == Decimal("10")
    assert result.status == "adjusted"
    assert result.reason == "single-instrument target exceeds 10%"


def test_risk_allows_existing_overweight_position_but_blocks_an_increase() -> None:
    result = apply_single_instrument_limit(
        current_quantity=Decimal("12"),
        proposed_quantity=Decimal("15"),
        unit_value_hkd=Decimal("100"),
        portfolio_nav_hkd=Decimal("10000"),
    )

    assert result.final_quantity == Decimal("12")
    assert result.status == "blocked_increase"
    assert result.reason == "现有仓位超限，禁止加仓"
