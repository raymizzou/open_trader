from __future__ import annotations

from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable

from .fx import StaticMonthEndFxProvider
from .models import AssetClass, CashBalance, Market, Position


PORTFOLIO_FIELDNAMES = [
    "sort_group",
    "market",
    "asset_class",
    "symbol",
    "name",
    "currency",
    "total_quantity",
    "avg_cost_price",
    "last_price",
    "market_value",
    "cost_value",
    "unrealized_pnl",
    "unrealized_pnl_pct",
    "fx_source",
    "fx_date",
    "fx_to_hkd",
    "market_value_hkd",
    "cost_value_hkd",
    "portfolio_weight_hkd",
    "brokers",
    "accounts",
    "ai_eligible",
    "analysis_symbol",
    "risk_flag",
    "confidence",
    "notes",
]


def money(value: Decimal | None) -> str:
    if value is None:
        return ""
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def number(value: Decimal | None) -> str:
    if value is None:
        return ""
    normalized = value.normalize()
    return format(normalized, "f")


def pct(value: Decimal | None) -> str:
    if value is None:
        return ""
    return (
        f"{(value * Decimal('100')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}%"
    )


def _sort_group(market: Market, asset_class: AssetClass, ai_eligible: bool) -> int:
    if market == Market.US and ai_eligible:
        return 1
    if market == Market.US:
        return 2
    if market == Market.HK:
        return 3
    if market == Market.CASH:
        return 5
    return 4


def _ai_eligible(position: Position) -> bool:
    return position.market == Market.US and position.asset_class in {
        AssetClass.STOCK,
        AssetClass.ETF,
    }


def _merged_confidence(confidences: Iterable[str]) -> str:
    confidence_values = list(confidences)
    if "low" in confidence_values:
        return "low"
    if "medium" in confidence_values:
        return "medium"
    return "high"


def build_portfolio_rows(
    month: str,
    positions: Iterable[Position],
    cash_balances: Iterable[CashBalance],
    fx_provider: StaticMonthEndFxProvider,
) -> list[dict[str, str]]:
    if month != fx_provider.month:
        raise ValueError(
            f"Portfolio month {month} does not match fx_provider.month "
            f"{fx_provider.month}"
        )

    grouped: dict[tuple[Market, AssetClass, str, str], list[Position]] = defaultdict(list)
    for position in positions:
        grouped[position.identity_key()].append(position)

    raw_rows: list[dict[str, object]] = []
    for (market, asset_class, symbol, currency), group in grouped.items():
        total_quantity = sum((position.quantity for position in group), Decimal("0"))
        has_missing_required_data = any(
            position.market_value is None or position.cost_value is None
            for position in group
        )
        has_missing_market_value = any(position.market_value is None for position in group)
        has_missing_cost_value = any(position.cost_value is None for position in group)
        summed_market_value = sum(
            (
                position.market_value
                if position.market_value is not None
                else Decimal("0")
                for position in group
            ),
            Decimal("0"),
        )
        summed_cost_value = sum(
            (
                position.cost_value
                if position.cost_value is not None
                else Decimal("0")
                for position in group
            ),
            Decimal("0"),
        )
        market_value = None if has_missing_market_value else summed_market_value
        cost_value = None if has_missing_cost_value else summed_cost_value
        if all(position.unrealized_pnl is not None for position in group):
            unrealized_pnl = sum(
                (position.unrealized_pnl for position in group),
                Decimal("0"),
            )
        elif has_missing_required_data:
            unrealized_pnl = None
        else:
            unrealized_pnl = summed_market_value - summed_cost_value
        avg_cost_price = (
            None
            if has_missing_required_data
            else (summed_cost_value / total_quantity if total_quantity else None)
        )
        last_price = (
            None
            if has_missing_required_data
            else (summed_market_value / total_quantity if total_quantity else None)
        )
        quote = fx_provider.get_rate_to_hkd(currency)
        market_value_hkd = None if market_value is None else market_value * quote.rate
        cost_value_hkd = None if cost_value is None else cost_value * quote.rate
        ai_eligible = any(_ai_eligible(position) for position in group)
        confidence = _merged_confidence(position.confidence for position in group)
        brokers = sorted({position.broker for position in group})
        accounts = sorted({position.account_alias for position in group})
        name = max((position.name for position in group), key=len)
        notes = "; ".join(position.notes for position in group if position.notes)

        raw_rows.append(
            {
                "sort_group": _sort_group(market, asset_class, ai_eligible),
                "market": market.value,
                "asset_class": asset_class.value,
                "symbol": symbol,
                "name": name,
                "currency": currency,
                "total_quantity": total_quantity,
                "avg_cost_price": avg_cost_price,
                "last_price": last_price,
                "market_value": market_value,
                "cost_value": cost_value,
                "unrealized_pnl": unrealized_pnl,
                "unrealized_pnl_pct": (
                    (unrealized_pnl / cost_value)
                    if unrealized_pnl is not None
                    and cost_value is not None
                    and cost_value != Decimal("0")
                    else None
                ),
                "fx_source": quote.source,
                "fx_date": quote.fx_date,
                "fx_to_hkd": quote.rate,
                "market_value_hkd": market_value_hkd,
                "cost_value_hkd": cost_value_hkd,
                "portfolio_value_incomplete": has_missing_market_value,
                "brokers": ";".join(brokers),
                "accounts": ";".join(accounts),
                "ai_eligible": ai_eligible,
                "analysis_symbol": symbol if ai_eligible else "",
                "risk_flag": (
                    "data_check"
                    if confidence == "low" or has_missing_required_data
                    else "normal"
                ),
                "confidence": confidence,
                "notes": notes,
            }
        )

    grouped_cash: dict[tuple[Market, AssetClass, str, str], list[CashBalance]] = defaultdict(
        list
    )
    for cash in cash_balances:
        grouped_cash[
            (Market.CASH, AssetClass.CASH, cash.symbol, cash.currency.upper())
        ].append(cash)

    for (_, _, symbol, currency), group in grouped_cash.items():
        quote = fx_provider.get_rate_to_hkd(currency)
        market_value = sum((cash.cash_balance for cash in group), Decimal("0"))
        confidence = _merged_confidence(cash.confidence for cash in group)
        brokers = sorted({cash.broker for cash in group})
        accounts = sorted({cash.account_alias for cash in group})
        notes = "; ".join(cash.notes for cash in group if cash.notes)
        raw_rows.append(
            {
                "sort_group": 5,
                "market": Market.CASH.value,
                "asset_class": AssetClass.CASH.value,
                "symbol": symbol,
                "name": f"{currency} Cash",
                "currency": currency,
                "total_quantity": Decimal("1"),
                "avg_cost_price": None,
                "last_price": None,
                "market_value": market_value,
                "cost_value": None,
                "unrealized_pnl": None,
                "unrealized_pnl_pct": None,
                "fx_source": quote.source,
                "fx_date": quote.fx_date,
                "fx_to_hkd": quote.rate,
                "market_value_hkd": market_value * quote.rate,
                "cost_value_hkd": None,
                "portfolio_value_incomplete": False,
                "brokers": ";".join(brokers),
                "accounts": ";".join(accounts),
                "ai_eligible": False,
                "analysis_symbol": "",
                "risk_flag": "data_check" if confidence == "low" else "normal",
                "confidence": confidence,
                "notes": notes,
            }
        )

    portfolio_value_incomplete = any(row["portfolio_value_incomplete"] for row in raw_rows)
    total_hkd = sum(
        (
            row["market_value_hkd"]
            for row in raw_rows
            if row["market_value_hkd"] is not None
        ),
        Decimal("0"),
    )
    output: list[dict[str, str]] = []
    for row in raw_rows:
        weight = (
            None
            if portfolio_value_incomplete
            else row["market_value_hkd"] / total_hkd
            if total_hkd and row["market_value_hkd"] is not None
            else Decimal("0")
        )
        if portfolio_value_incomplete:
            row["risk_flag"] = "data_check"
        if (
            row["risk_flag"] != "data_check"
            and row["asset_class"]
            not in {AssetClass.CASH.value, AssetClass.MONEY_MARKET_FUND.value}
            and weight is not None
            and weight > Decimal("0.10")
        ):
            row["risk_flag"] = "overweight"

        output.append(
            {
                "sort_group": str(row["sort_group"]),
                "market": str(row["market"]),
                "asset_class": str(row["asset_class"]),
                "symbol": str(row["symbol"]),
                "name": str(row["name"]),
                "currency": str(row["currency"]),
                "total_quantity": number(row["total_quantity"]),
                "avg_cost_price": money(row["avg_cost_price"]),
                "last_price": money(row["last_price"]),
                "market_value": number(row["market_value"]),
                "cost_value": number(row["cost_value"]),
                "unrealized_pnl": money(row["unrealized_pnl"]),
                "unrealized_pnl_pct": pct(row["unrealized_pnl_pct"]),
                "fx_source": str(row["fx_source"]),
                "fx_date": str(row["fx_date"]),
                "fx_to_hkd": number(row["fx_to_hkd"]),
                "market_value_hkd": money(row["market_value_hkd"]),
                "cost_value_hkd": money(row["cost_value_hkd"]),
                "portfolio_weight_hkd": pct(weight),
                "brokers": str(row["brokers"]),
                "accounts": str(row["accounts"]),
                "ai_eligible": "true" if row["ai_eligible"] else "false",
                "analysis_symbol": str(row["analysis_symbol"]),
                "risk_flag": str(row["risk_flag"]),
                "confidence": str(row["confidence"]),
                "notes": str(row["notes"]),
            }
        )

    return sorted(
        output,
        key=lambda item: (
            int(item["sort_group"]),
            -Decimal(item["market_value_hkd"] or "0"),
        ),
    )
