from __future__ import annotations

import csv
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path


TRADE_ACTION_FIELDNAMES = [
    "run_date",
    "symbol",
    "market",
    "futu_symbol",
    "action",
    "priority",
    "last_price",
    "trigger_status",
    "suggested_quantity",
    "suggested_notional",
    "notional_currency",
    "current_quantity",
    "current_weight",
    "target_max_weight",
    "cash_available",
    "limit_price",
    "stop_price",
    "reason",
    "source_plan",
    "status",
    "error",
]

PORTFOLIO_REQUIRED_FIELDNAMES = [
    "market",
    "asset_class",
    "symbol",
    "currency",
    "total_quantity",
    "market_value",
    "fx_to_hkd",
    "market_value_hkd",
    "portfolio_weight_hkd",
]


@dataclass(frozen=True)
class PortfolioActionContext:
    positions: dict[tuple[str, str], dict[str, Decimal | str]]
    cash_by_currency: dict[str, Decimal]
    total_market_value_hkd: Decimal


def load_portfolio_action_context(portfolio_path: Path) -> PortfolioActionContext:
    with portfolio_path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        blank_columns = [name for name in fieldnames if not (name or "").strip()]
        if blank_columns:
            raise ValueError("portfolio column names must not be blank")

        duplicate_columns = sorted(
            {
                name
                for name in fieldnames
                if fieldnames.count(name) > 1
            }
        )
        if duplicate_columns:
            raise ValueError(
                f"duplicate portfolio column(s): {', '.join(duplicate_columns)}"
            )

        missing = sorted(set(PORTFOLIO_REQUIRED_FIELDNAMES) - set(fieldnames))
        if missing:
            raise ValueError(f"missing portfolio column(s): {', '.join(missing)}")
        rows = [row for row in reader]

    positions: dict[tuple[str, str], dict[str, Decimal | str]] = {}
    cash_by_currency: dict[str, Decimal] = {}
    total_market_value_hkd = Decimal("0")

    for row in rows:
        market_value_hkd = _optional_decimal(row.get("market_value_hkd", "") or "")
        if market_value_hkd is not None:
            total_market_value_hkd += market_value_hkd

        market = (row.get("market", "") or "").strip().upper()
        asset_class = (row.get("asset_class", "") or "").strip().lower()
        symbol = (row.get("symbol", "") or "").strip().upper()
        currency = (row.get("currency", "") or "").strip().upper()

        if market == "CASH" or asset_class == "cash":
            cash_value = _optional_decimal(row.get("market_value", "") or "")
            if currency and cash_value is not None:
                cash_by_currency[currency] = cash_by_currency.get(currency, Decimal("0")) + cash_value
            continue

        quantity = _optional_decimal(row.get("total_quantity", "") or "")
        market_value = _optional_decimal(row.get("market_value", "") or "")
        weight = _optional_percent(row.get("portfolio_weight_hkd", "") or "")
        fx_to_hkd = _optional_decimal(row.get("fx_to_hkd", "") or "")

        if market and symbol:
            positions[(market, symbol)] = {
                "currency": currency,
                "quantity": quantity or Decimal("0"),
                "market_value": market_value or Decimal("0"),
                "market_value_hkd": market_value_hkd or Decimal("0"),
                "weight": weight or Decimal("0"),
                "fx_to_hkd": fx_to_hkd or Decimal("0"),
            }

    return PortfolioActionContext(
        positions=positions,
        cash_by_currency=cash_by_currency,
        total_market_value_hkd=total_market_value_hkd,
    )


def _optional_decimal(value: str) -> Decimal | None:
    value = value.strip().replace(",", "")
    if not value:
        return None
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _optional_percent(value: str) -> Decimal | None:
    value = value.strip()
    if not value:
        return None
    if value.endswith("%"):
        parsed = _optional_decimal(value[:-1])
        return None if parsed is None else parsed / Decimal("100")
    parsed = _optional_decimal(value)
    return parsed
