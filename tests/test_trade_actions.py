from __future__ import annotations

import csv
from decimal import Decimal
from pathlib import Path

from open_trader.trade_actions import (
    TRADE_ACTION_FIELDNAMES,
    PortfolioActionContext,
    load_portfolio_action_context,
)


def write_portfolio(path: Path, rows: list[dict[str, str]]) -> None:
    fieldnames = [
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
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def test_trade_action_fieldnames_are_stable() -> None:
    assert TRADE_ACTION_FIELDNAMES == [
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


def test_load_portfolio_action_context_indexes_positions_cash_and_total_value(
    tmp_path: Path,
) -> None:
    path = tmp_path / "portfolio.csv"
    write_portfolio(path, [
        {
            "sort_group": "1",
            "market": "US",
            "asset_class": "stock",
            "symbol": "MSFT",
            "name": "Microsoft",
            "currency": "USD",
            "total_quantity": "10",
            "avg_cost_price": "300",
            "last_price": "390",
            "market_value": "3900",
            "cost_value": "3000",
            "unrealized_pnl": "900",
            "unrealized_pnl_pct": "30.00%",
            "fx_source": "fixture",
            "fx_date": "2026-05-31",
            "fx_to_hkd": "7.8",
            "market_value_hkd": "30420",
            "cost_value_hkd": "23400",
            "portfolio_weight_hkd": "39.00%",
            "brokers": "futu",
            "accounts": "futu_main",
            "ai_eligible": "true",
            "analysis_symbol": "MSFT",
            "risk_flag": "normal",
            "confidence": "high",
            "notes": "",
        },
        {
            "sort_group": "5",
            "market": "CASH",
            "asset_class": "cash",
            "symbol": "USD_CASH",
            "name": "USD Cash",
            "currency": "USD",
            "total_quantity": "1",
            "avg_cost_price": "",
            "last_price": "",
            "market_value": "1000",
            "cost_value": "",
            "unrealized_pnl": "",
            "unrealized_pnl_pct": "",
            "fx_source": "fixture",
            "fx_date": "2026-05-31",
            "fx_to_hkd": "7.8",
            "market_value_hkd": "7800",
            "cost_value_hkd": "",
            "portfolio_weight_hkd": "10.00%",
            "brokers": "futu",
            "accounts": "futu_main",
            "ai_eligible": "false",
            "analysis_symbol": "",
            "risk_flag": "normal",
            "confidence": "high",
            "notes": "",
        },
    ])

    context = load_portfolio_action_context(path)

    assert context == PortfolioActionContext(
        positions={
            ("US", "MSFT"): {
                "currency": "USD",
                "quantity": Decimal("10"),
                "market_value": Decimal("3900"),
                "market_value_hkd": Decimal("30420"),
                "weight": Decimal("0.39"),
                "fx_to_hkd": Decimal("7.8"),
            }
        },
        cash_by_currency={"USD": Decimal("1000")},
        total_market_value_hkd=Decimal("38220"),
    )
