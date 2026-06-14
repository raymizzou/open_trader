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


def build_portfolio_rows(
    month: str,
    positions: Iterable[Position],
    cash_balances: Iterable[CashBalance],
    fx_provider: StaticMonthEndFxProvider,
) -> list[dict[str, str]]:
    grouped: dict[tuple[Market, AssetClass, str, str], list[Position]] = defaultdict(list)
    for position in positions:
        grouped[position.identity_key()].append(position)

    raw_rows: list[dict[str, object]] = []
    for (market, asset_class, symbol, currency), group in grouped.items():
        total_quantity = sum((p.quantity for p in group), Decimal("0"))
        market_value = sum((p.market_value or Decimal("0") for p in group), Decimal("0"))
        cost_value = sum((p.cost_value or Decimal("0") for p in group), Decimal("0"))
        unrealized_pnl = market_value - cost_value if cost_value else None
        avg_cost_price = cost_value / total_quantity if total_quantity and cost_value else None
        last_price = market_value / total_quantity if total_quantity and market_value else None
        quote = fx_provider.get_rate_to_hkd(currency)
        market_value_hkd = market_value * quote.rate
        cost_value_hkd = cost_value * quote.rate if cost_value else None
        ai_eligible = any(_ai_eligible(position) for position in group)
        confidence = (
            "low"
            if any(position.confidence == "low" for position in group)
            else (
                "medium"
                if any(position.confidence == "medium" for position in group)
                else "high"
            )
        )
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
                "cost_value": cost_value if cost_value else None,
                "unrealized_pnl": unrealized_pnl,
                "unrealized_pnl_pct": (
                    (unrealized_pnl / cost_value)
                    if unrealized_pnl is not None and cost_value
                    else None
                ),
                "fx_source": quote.source,
                "fx_date": quote.fx_date,
                "fx_to_hkd": quote.rate,
                "market_value_hkd": market_value_hkd,
                "cost_value_hkd": cost_value_hkd,
                "brokers": ";".join(brokers),
                "accounts": ";".join(accounts),
                "ai_eligible": ai_eligible,
                "analysis_symbol": symbol if ai_eligible else "",
                "risk_flag": "data_check" if confidence == "low" or not market_value else "normal",
                "confidence": confidence,
                "notes": notes,
            }
        )

    for cash in cash_balances:
        quote = fx_provider.get_rate_to_hkd(cash.currency)
        market_value = cash.cash_balance
        raw_rows.append(
            {
                "sort_group": 5,
                "market": Market.CASH.value,
                "asset_class": AssetClass.CASH.value,
                "symbol": cash.symbol,
                "name": f"{cash.currency.upper()} Cash",
                "currency": cash.currency.upper(),
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
                "brokers": cash.broker,
                "accounts": cash.account_alias,
                "ai_eligible": False,
                "analysis_symbol": "",
                "risk_flag": "data_check" if cash.confidence == "low" else "normal",
                "confidence": cash.confidence,
                "notes": cash.notes,
            }
        )

    total_hkd = sum((row["market_value_hkd"] for row in raw_rows), Decimal("0"))
    output: list[dict[str, str]] = []
    for row in raw_rows:
        weight = row["market_value_hkd"] / total_hkd if total_hkd else Decimal("0")
        if (
            row["risk_flag"] != "data_check"
            and row["asset_class"]
            not in {AssetClass.CASH.value, AssetClass.MONEY_MARKET_FUND.value}
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
        key=lambda item: (int(item["sort_group"]), -Decimal(item["market_value_hkd"])),
    )
