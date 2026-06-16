from __future__ import annotations

import csv
import pytest
from decimal import Decimal
from pathlib import Path

from open_trader.trade_actions import (
    TRADE_ACTION_FIELDNAMES,
    PortfolioPositionSnapshot,
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
            ("US", "MSFT"): PortfolioPositionSnapshot(
                currency="USD",
                quantity=Decimal("10"),
                market_value=Decimal("3900"),
                market_value_hkd=Decimal("30420"),
                weight=Decimal("0.39"),
                fx_to_hkd=Decimal("7.8"),
            )
        },
        cash_by_currency={"USD": Decimal("1000")},
        total_market_value_hkd=Decimal("38220"),
    )


def test_load_portfolio_action_context_is_immutable(tmp_path: Path) -> None:
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
        }
    ])

    context = load_portfolio_action_context(path)

    with pytest.raises(TypeError):
        context.positions[("US", "MSFT")] = PortfolioPositionSnapshot(
            currency="USD",
            quantity=Decimal("1"),
            market_value=Decimal("1"),
            market_value_hkd=Decimal("1"),
            weight=Decimal("0.1"),
            fx_to_hkd=Decimal("1"),
        )

    with pytest.raises(TypeError):
        context.cash_by_currency["USD"] = Decimal("1")


def test_load_portfolio_action_context_rejects_missing_required_columns(tmp_path: Path) -> None:
    path = tmp_path / "portfolio.csv"
    fieldnames = [
        "market",
        "asset_class",
        "symbol",
        "currency",
        "total_quantity",
        "market_value",
        "market_value_hkd",
        "portfolio_weight_hkd",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows([{
            "market": "US",
            "asset_class": "stock",
            "symbol": "MSFT",
            "currency": "USD",
            "total_quantity": "10",
            "market_value": "3900",
            "market_value_hkd": "30420",
            "portfolio_weight_hkd": "39.00%",
        }])

    with pytest.raises(
        ValueError, match=r"missing portfolio column\(s\): fx_to_hkd"
    ):
        load_portfolio_action_context(path)


def test_load_portfolio_action_context_rejects_blank_portfolio_columns(tmp_path: Path) -> None:
    path = tmp_path / "portfolio.csv"
    malformed_headers = [
        "market",
        "asset_class",
        "",
        "currency",
        "symbol",
        "total_quantity",
        "market_value",
        "fx_to_hkd",
        "market_value_hkd",
        "portfolio_weight_hkd",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(",".join(malformed_headers) + "\n")

    with pytest.raises(ValueError, match=r"portfolio column names must not be blank"):
        load_portfolio_action_context(path)


def test_load_portfolio_action_context_rejects_duplicate_portfolio_columns(tmp_path: Path) -> None:
    path = tmp_path / "portfolio.csv"
    malformed_headers = [
        "market",
        "asset_class",
        "symbol",
        "symbol",
        "currency",
        "total_quantity",
        "market_value",
        "fx_to_hkd",
        "market_value_hkd",
        "portfolio_weight_hkd",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(",".join(malformed_headers) + "\n")

    with pytest.raises(ValueError, match=r"duplicate portfolio column\(s\): symbol"):
        load_portfolio_action_context(path)


def test_load_portfolio_action_context_aggregates_same_currency_cash_rows(tmp_path: Path) -> None:
    path = tmp_path / "portfolio.csv"
    write_portfolio(path, [
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
            "portfolio_weight_hkd": "0.00%",
            "brokers": "futu",
            "accounts": "futu_main",
            "ai_eligible": "false",
            "analysis_symbol": "",
            "risk_flag": "normal",
            "confidence": "high",
            "notes": "",
        },
        {
            "sort_group": "5",
            "market": "CASH",
            "asset_class": "cash",
            "symbol": "USD_CASH2",
            "name": "USD Cash2",
            "currency": "USD",
            "total_quantity": "1",
            "avg_cost_price": "",
            "last_price": "",
            "market_value": "2000",
            "cost_value": "",
            "unrealized_pnl": "",
            "unrealized_pnl_pct": "",
            "fx_source": "fixture",
            "fx_date": "2026-05-31",
            "fx_to_hkd": "7.8",
            "market_value_hkd": "15600",
            "cost_value_hkd": "",
            "portfolio_weight_hkd": "0.00%",
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

    assert context.cash_by_currency == {"USD": Decimal("3000")}


def test_load_portfolio_action_context_skips_non_cash_rows_with_missing_keys(tmp_path: Path) -> None:
    path = tmp_path / "portfolio.csv"
    write_portfolio(path, [
        {
            "sort_group": "1",
            "market": "",
            "asset_class": "stock",
            "symbol": "MISSING_MARKET",
            "name": "No Market",
            "currency": "USD",
            "total_quantity": "1",
            "avg_cost_price": "",
            "last_price": "",
            "market_value": "100",
            "cost_value": "",
            "unrealized_pnl": "",
            "unrealized_pnl_pct": "",
            "fx_source": "fixture",
            "fx_date": "2026-05-31",
            "fx_to_hkd": "7.8",
            "market_value_hkd": "780",
            "cost_value_hkd": "",
            "portfolio_weight_hkd": "0.00%",
            "brokers": "futu",
            "accounts": "futu_main",
            "ai_eligible": "true",
            "analysis_symbol": "MISSING_MARKET",
            "risk_flag": "normal",
            "confidence": "high",
            "notes": "",
        },
        {
            "sort_group": "1",
            "market": "US",
            "asset_class": "stock",
            "symbol": "",
            "name": "No Symbol",
            "currency": "USD",
            "total_quantity": "1",
            "avg_cost_price": "",
            "last_price": "",
            "market_value": "100",
            "cost_value": "",
            "unrealized_pnl": "",
            "unrealized_pnl_pct": "",
            "fx_source": "fixture",
            "fx_date": "2026-05-31",
            "fx_to_hkd": "7.8",
            "market_value_hkd": "780",
            "cost_value_hkd": "",
            "portfolio_weight_hkd": "0.00%",
            "brokers": "futu",
            "accounts": "futu_main",
            "ai_eligible": "true",
            "analysis_symbol": "",
            "risk_flag": "normal",
            "confidence": "high",
            "notes": "",
        },
        {
            "sort_group": "1",
            "market": "US",
            "asset_class": "stock",
            "symbol": "GOOD",
            "name": "Good Row",
            "currency": "USD",
            "total_quantity": "2",
            "avg_cost_price": "200",
            "last_price": "100",
            "market_value": "200",
            "cost_value": "200",
            "unrealized_pnl": "0",
            "unrealized_pnl_pct": "0.00%",
            "fx_source": "fixture",
            "fx_date": "2026-05-31",
            "fx_to_hkd": "7.8",
            "market_value_hkd": "1560",
            "cost_value_hkd": "1560",
            "portfolio_weight_hkd": "1.00%",
            "brokers": "futu",
            "accounts": "futu_main",
            "ai_eligible": "true",
            "analysis_symbol": "GOOD",
            "risk_flag": "normal",
            "confidence": "high",
            "notes": "",
        },
    ])

    context = load_portfolio_action_context(path)

    assert context.positions == {
        ("US", "GOOD"): PortfolioPositionSnapshot(
            currency="USD",
            quantity=Decimal("2"),
            market_value=Decimal("200"),
            market_value_hkd=Decimal("1560"),
            weight=Decimal("0.01"),
            fx_to_hkd=Decimal("7.8"),
        )
    }


def test_load_portfolio_action_context_accepts_utf8_sig_input(tmp_path: Path) -> None:
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
        }
    ])
    raw = path.read_bytes()
    path.write_bytes(b"\xef\xbb\xbf" + raw)

    context = load_portfolio_action_context(path)

    assert context == PortfolioActionContext(
        positions={
            ("US", "MSFT"): PortfolioPositionSnapshot(
                currency="USD",
                quantity=Decimal("10"),
                market_value=Decimal("3900"),
                market_value_hkd=Decimal("30420"),
                weight=Decimal("0.39"),
                fx_to_hkd=Decimal("7.8"),
            )
        },
        cash_by_currency={},
        total_market_value_hkd=Decimal("30420"),
    )


def test_load_portfolio_action_context_falls_back_to_zero_for_invalid_position_values(
    tmp_path: Path,
) -> None:
    path = tmp_path / "portfolio.csv"
    write_portfolio(path, [
        {
            "sort_group": "1",
            "market": "US",
            "asset_class": "stock",
            "symbol": "AAPL",
            "name": "Apple",
            "currency": "USD",
            "total_quantity": "",
            "avg_cost_price": "300",
            "last_price": "390",
            "market_value": "bad",
            "cost_value": "3000",
            "unrealized_pnl": "900",
            "unrealized_pnl_pct": "",
            "fx_source": "fixture",
            "fx_date": "2026-05-31",
            "fx_to_hkd": "bad",
            "market_value_hkd": "",
            "cost_value_hkd": "23400",
            "portfolio_weight_hkd": "",
            "brokers": "futu",
            "accounts": "futu_main",
            "ai_eligible": "true",
            "analysis_symbol": "AAPL",
            "risk_flag": "normal",
            "confidence": "high",
            "notes": "",
        },
    ])

    context = load_portfolio_action_context(path)

    assert context.positions == {
        ("US", "AAPL"): PortfolioPositionSnapshot(
            currency="USD",
            quantity=Decimal("0"),
            market_value=Decimal("0"),
            market_value_hkd=Decimal("0"),
            weight=Decimal("0"),
            fx_to_hkd=Decimal("0"),
        )
    }


def test_load_portfolio_action_context_keeps_blank_optional_string_fields(tmp_path: Path) -> None:
    path = tmp_path / "portfolio.csv"
    write_portfolio(path, [
        {
            "sort_group": "1",
            "market": "US",
            "asset_class": "stock",
            "symbol": "AAPL",
            "name": "Apple",
            "currency": "",
            "total_quantity": "10",
            "avg_cost_price": "300",
            "last_price": "390",
            "market_value": "3900",
            "cost_value": "3000",
            "unrealized_pnl": "900",
            "unrealized_pnl_pct": "",
            "fx_source": "fixture",
            "fx_date": "2026-05-31",
            "fx_to_hkd": "7.8",
            "market_value_hkd": "30420",
            "cost_value_hkd": "23400",
            "portfolio_weight_hkd": "",
            "brokers": "futu",
            "accounts": "futu_main",
            "ai_eligible": "true",
            "analysis_symbol": "AAPL",
            "risk_flag": "normal",
            "confidence": "high",
            "notes": "",
        },
    ])

    context = load_portfolio_action_context(path)

    assert context == PortfolioActionContext(
        positions={
            ("US", "AAPL"): PortfolioPositionSnapshot(
                currency="",
                quantity=Decimal("10"),
                market_value=Decimal("3900"),
                market_value_hkd=Decimal("30420"),
                weight=Decimal("0"),
                fx_to_hkd=Decimal("7.8"),
            )
        },
        cash_by_currency={},
        total_market_value_hkd=Decimal("30420"),
    )


def test_load_portfolio_action_context_skips_truncated_rows(tmp_path: Path) -> None:
    path = tmp_path / "portfolio.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(
            "market,asset_class,symbol,currency,total_quantity,market_value,"
            "fx_to_hkd,market_value_hkd,portfolio_weight_hkd\n"
        )
        handle.write("US,stock,TRUNC,USD,10\n")

    context = load_portfolio_action_context(path)

    assert context == PortfolioActionContext(
        positions={},
        cash_by_currency={},
        total_market_value_hkd=Decimal("0"),
    )


def test_load_portfolio_action_context_skips_rows_with_extra_cells(tmp_path: Path) -> None:
    path = tmp_path / "portfolio.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        header = (
            "market,asset_class,symbol,currency,total_quantity,market_value,"
            "cost_value,unrealized_pnl,fx_to_hkd,market_value_hkd,portfolio_weight_hkd\n"
        )
        row = (
            "US,stock,AAPL,USD,10,3900,3000,900,7.8,30420,39.00%,"
            "extra-cell\n"
        )
        handle.write(header)
        handle.write(row)

    context = load_portfolio_action_context(path)

    assert context == PortfolioActionContext(
        positions={},
        cash_by_currency={},
        total_market_value_hkd=Decimal("0"),
    )


def test_load_portfolio_action_context_parses_percentage_to_fractional_weight(tmp_path: Path) -> None:
    path = tmp_path / "portfolio.csv"
    write_portfolio(path, [
        {
            "sort_group": "1",
            "market": "US",
            "asset_class": "stock",
            "symbol": "AAPL",
            "name": "Apple",
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
            "portfolio_weight_hkd": "12.50%",
            "brokers": "futu",
            "accounts": "futu_main",
            "ai_eligible": "true",
            "analysis_symbol": "AAPL",
            "risk_flag": "normal",
            "confidence": "high",
            "notes": "",
        },
    ])

    context = load_portfolio_action_context(path)

    assert context.positions[("US", "AAPL")].weight == Decimal("0.125")


def test_load_portfolio_action_context_prefers_only_percentage_weight_strings(
    tmp_path: Path,
) -> None:
    path = tmp_path / "portfolio.csv"
    write_portfolio(path, [
        {
            "sort_group": "1",
            "market": "US",
            "asset_class": "stock",
            "symbol": "AAPL",
            "name": "Apple",
            "currency": "USD",
            "total_quantity": "10",
            "avg_cost_price": "300",
            "last_price": "390",
            "market_value": "3900",
            "cost_value": "3000",
            "unrealized_pnl": "900",
            "unrealized_pnl_pct": "",
            "fx_source": "fixture",
            "fx_date": "2026-05-31",
            "fx_to_hkd": "7.8",
            "market_value_hkd": "30420",
            "cost_value_hkd": "23400",
            "portfolio_weight_hkd": "12.50",
            "brokers": "futu",
            "accounts": "futu_main",
            "ai_eligible": "true",
            "analysis_symbol": "AAPL",
            "risk_flag": "normal",
            "confidence": "high",
            "notes": "",
        },
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
            "unrealized_pnl_pct": "",
            "fx_source": "fixture",
            "fx_date": "2026-05-31",
            "fx_to_hkd": "7.8",
            "market_value_hkd": "30420",
            "cost_value_hkd": "23400",
            "portfolio_weight_hkd": "12.50%",
            "brokers": "futu",
            "accounts": "futu_main",
            "ai_eligible": "true",
            "analysis_symbol": "MSFT",
            "risk_flag": "normal",
            "confidence": "high",
            "notes": "",
        },
    ])

    context = load_portfolio_action_context(path)

    assert context.positions[("US", "AAPL")].weight == Decimal("0")
    assert context.positions[("US", "MSFT")].weight == Decimal("0.125")
