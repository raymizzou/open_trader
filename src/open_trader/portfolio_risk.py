from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Literal


RiskStatus = Literal["allowed", "adjusted", "blocked_increase"]


@dataclass(frozen=True)
class PortfolioRiskResult:
    proposed_quantity: Decimal
    final_quantity: Decimal
    status: RiskStatus
    reason: str


def apply_single_instrument_limit(
    *,
    current_quantity: Decimal,
    proposed_quantity: Decimal,
    unit_value_hkd: Decimal,
    portfolio_nav_hkd: Decimal,
    max_weight: Decimal = Decimal("0.10"),
) -> PortfolioRiskResult:
    if unit_value_hkd <= 0 or portfolio_nav_hkd <= 0:
        raise ValueError("unit value and portfolio NAV must be positive")
    max_quantity = (
        portfolio_nav_hkd * max_weight / unit_value_hkd
    ).to_integral_value(rounding=ROUND_DOWN)
    if current_quantity > max_quantity and proposed_quantity > current_quantity:
        return PortfolioRiskResult(
            proposed_quantity=proposed_quantity,
            final_quantity=current_quantity,
            status="blocked_increase",
            reason="现有仓位超限，禁止加仓",
        )
    if proposed_quantity > max_quantity and proposed_quantity > current_quantity:
        return PortfolioRiskResult(
            proposed_quantity=proposed_quantity,
            final_quantity=max_quantity,
            status="adjusted",
            reason="single-instrument target exceeds 10%",
        )
    return PortfolioRiskResult(
        proposed_quantity=proposed_quantity,
        final_quantity=proposed_quantity,
        status="allowed",
        reason="",
    )
