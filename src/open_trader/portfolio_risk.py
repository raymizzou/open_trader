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


@dataclass(frozen=True)
class EntryRiskResult:
    final_quantity: Decimal
    planned_stop_risk: Decimal
    normal_cost: Decimal
    cash_required: Decimal
    decisive_constraint: str
    reason: str


def size_entry_by_risk(
    *,
    entry_price: Decimal,
    protection_line: Decimal,
    fx_to_account_currency: Decimal,
    portfolio_nav: Decimal,
    nominal_weight_limit: Decimal,
    single_entry_risk_limit: Decimal,
    portfolio_remaining_risk: Decimal,
    available_cash: Decimal,
    lot_size: Decimal,
    normal_cost_rate: Decimal,
) -> EntryRiskResult:
    if any(
        not value.is_finite() or value <= 0
        for value in (
            entry_price,
            fx_to_account_currency,
            portfolio_nav,
            nominal_weight_limit,
            single_entry_risk_limit,
            lot_size,
        )
    ) or not protection_line.is_finite() or any(
        not value.is_finite() or value < 0
        for value in (portfolio_remaining_risk, available_cash, normal_cost_rate)
    ):
        raise ValueError("entry risk inputs are invalid")

    unit_notional = entry_price * fx_to_account_currency
    unit_risk = (
        max(Decimal("0"), entry_price - protection_line)
        * fx_to_account_currency
        + unit_notional * normal_cost_rate
    )
    if unit_risk <= 0:
        raise ValueError("entry risk inputs are invalid")
    cash_per_unit = unit_notional * (Decimal("1") + normal_cost_rate)
    caps = (
        ("名义仓位上限", portfolio_nav * nominal_weight_limit / unit_notional),
        ("单笔风险上限", single_entry_risk_limit / unit_risk),
        ("组合剩余风险", portfolio_remaining_risk / unit_risk),
        ("现金", available_cash / cash_per_unit),
    )
    decisive_constraint, raw_quantity = min(caps, key=lambda item: item[1])
    quantity = (
        raw_quantity / lot_size
    ).to_integral_value(rounding=ROUND_DOWN) * lot_size

    def facts(value: Decimal) -> tuple[Decimal, Decimal, Decimal]:
        normal_cost = value * unit_notional * normal_cost_rate
        return value * unit_risk, normal_cost, value * cash_per_unit

    planned_risk, normal_cost, cash_required = facts(quantity)
    if quantity > 0 and (
        quantity * unit_notional > portfolio_nav * nominal_weight_limit
        or planned_risk > single_entry_risk_limit
        or planned_risk > portfolio_remaining_risk
        or cash_required > available_cash
    ):
        quantity -= lot_size
        planned_risk, normal_cost, cash_required = facts(quantity)

    lot_text = format(lot_size, "f")
    if "." in lot_text:
        lot_text = lot_text.rstrip("0").rstrip(".")
    return EntryRiskResult(
        final_quantity=quantity,
        planned_stop_risk=planned_risk,
        normal_cost=normal_cost,
        cash_required=cash_required,
        decisive_constraint=decisive_constraint,
        reason=(
            ""
            if quantity > 0
            else f"最小交易单位 {lot_text} 股超过{decisive_constraint}"
        ),
    )


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
