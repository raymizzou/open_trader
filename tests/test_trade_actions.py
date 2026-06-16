from __future__ import annotations

import csv
from dataclasses import FrozenInstanceError
import pytest
from decimal import Decimal
from pathlib import Path

from open_trader.trading_plan import PlanQuoteStatus, TradingPlanRow
from open_trader.trade_actions import (
    TRADE_ACTION_FIELDNAMES,
    PortfolioPositionSnapshot,
    PortfolioActionContext,
    build_trade_action_row,
    map_quote_status_to_action,
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


def active_plan(
    *,
    symbol: str = "MSFT",
    max_weight: str = "12%",
    plan_text: str = "操作计划：在380-400美元区间分3-4次买入目标仓位的60%，350美元附近加仓剩余40%。",
) -> TradingPlanRow:
    return TradingPlanRow(
        run_date="2026-06-16",
        symbol=symbol,
        market="US",
        rating="Overweight",
        entry_zone_low=Decimal("380"),
        entry_zone_high=Decimal("400"),
        add_price=Decimal("350"),
        stop_loss=Decimal("340"),
        target_1=Decimal("450"),
        target_2=Decimal("500"),
        max_weight=max_weight,
        catalyst="10月底财报",
        time_horizon="3-6个月",
        plan_text=plan_text,
        status="active",
        error="",
    )


def portfolio_context(*, quantity: str = "10", cash: str = "1000") -> PortfolioActionContext:
    return PortfolioActionContext(
        positions={
            ("US", "MSFT"): PortfolioPositionSnapshot(
                currency="USD",
                quantity=Decimal(quantity),
                market_value=Decimal("3900"),
                market_value_hkd=Decimal("30420"),
                weight=Decimal("0.039"),
                fx_to_hkd=Decimal("7.8"),
            )
        },
        cash_by_currency={"USD": Decimal(cash)},
        total_market_value_hkd=Decimal("780000"),
    )


def quote_status(trigger_status: str, price: str = "390") -> PlanQuoteStatus:
    return PlanQuoteStatus(
        symbol="MSFT",
        futu_symbol="US.MSFT",
        last_price=Decimal(price),
        status=trigger_status,
        message="fixture message",
    )


def test_trade_action_fieldnames_are_stable() -> None:
    assert TRADE_ACTION_FIELDNAMES == (
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
    )


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

    with pytest.raises((TypeError, FrozenInstanceError)):
        context.positions[("US", "MSFT")].quantity = Decimal("20")


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


def test_load_portfolio_action_context_rejects_duplicate_positions(tmp_path: Path) -> None:
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
            "sort_group": "1",
            "market": "us",
            "asset_class": "stock",
            "symbol": "msft",
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
    ])

    with pytest.raises(ValueError, match=r"duplicate portfolio position\(s\): US\.MSFT"):
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


def test_load_portfolio_action_context_parses_grouped_numbers_and_falls_back_for_invalid_grouping(
    tmp_path: Path,
) -> None:
    path = tmp_path / "portfolio.csv"
    write_portfolio(path, [
        {
            "sort_group": "1",
            "market": "US",
            "asset_class": "stock",
            "symbol": "GOOD",
            "name": "Good",
            "currency": "USD",
            "total_quantity": "1,234",
            "avg_cost_price": "300",
            "last_price": "390",
            "market_value": "12,345,678.90",
            "cost_value": "3000",
            "unrealized_pnl": "900",
            "unrealized_pnl_pct": "30.00%",
            "fx_source": "fixture",
            "fx_date": "2026-05-31",
            "fx_to_hkd": "7,800",
            "market_value_hkd": "10,000.00",
            "cost_value_hkd": "23400",
            "portfolio_weight_hkd": "1.00%",
            "brokers": "futu",
            "accounts": "futu_main",
            "ai_eligible": "true",
            "analysis_symbol": "GOOD",
            "risk_flag": "normal",
            "confidence": "high",
            "notes": "",
        },
        {
            "sort_group": "1",
            "market": "US",
            "asset_class": "stock",
            "symbol": "BAD",
            "name": "Bad",
            "currency": "USD",
            "total_quantity": "12,34",
            "avg_cost_price": "300",
            "last_price": "390",
            "market_value": "1,2,3",
            "cost_value": "3000",
            "unrealized_pnl": "900",
            "unrealized_pnl_pct": "30.00%",
            "fx_source": "fixture",
            "fx_date": "2026-05-31",
            "fx_to_hkd": "7,8",
            "market_value_hkd": "10,000",
            "cost_value_hkd": "23400",
            "portfolio_weight_hkd": "",
            "brokers": "futu",
            "accounts": "futu_main",
            "ai_eligible": "true",
            "analysis_symbol": "BAD",
            "risk_flag": "normal",
            "confidence": "high",
            "notes": "",
        },
    ])

    context = load_portfolio_action_context(path)

    assert context.positions == {
        ("US", "GOOD"): PortfolioPositionSnapshot(
            currency="USD",
            quantity=Decimal("1234"),
            market_value=Decimal("12345678.90"),
            market_value_hkd=Decimal("10000.00"),
            weight=Decimal("0.01"),
            fx_to_hkd=Decimal("7800"),
        ),
        ("US", "BAD"): PortfolioPositionSnapshot(
            currency="USD",
            quantity=Decimal("0"),
            market_value=Decimal("0"),
            market_value_hkd=Decimal("10000"),
            weight=Decimal("0"),
            fx_to_hkd=Decimal("0"),
        ),
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


def test_map_quote_status_to_trade_action() -> None:
    assert map_quote_status_to_action("stop_loss_hit") == ("SELL_STOP", "critical")
    assert map_quote_status_to_action("target_2_hit") == ("TAKE_PROFIT", "high")
    assert map_quote_status_to_action("target_1_hit") == ("TRIM", "medium")
    assert map_quote_status_to_action("entry_zone") == ("BUY", "high")
    assert map_quote_status_to_action("add_zone") == ("ADD", "medium")
    assert map_quote_status_to_action("watch") == ("HOLD", "low")
    assert map_quote_status_to_action("missing_quote") == ("REVIEW", "medium")
    assert map_quote_status_to_action("unexpected") == ("REVIEW", "medium")


def test_buy_action_uses_plan_ratio_target_cap_and_cash_cap() -> None:
    row = build_trade_action_row(
        plan=active_plan(),
        quote_status=quote_status("entry_zone", price="390"),
        portfolio=portfolio_context(cash="1000"),
        source_plan="data/latest/trading_plan.csv",
    )

    assert row["action"] == "BUY"
    assert row["status"] == "ready"
    assert row["suggested_notional"] == "780"
    assert row["suggested_quantity"] == "2"
    assert row["cash_available"] == "1000"
    assert row["limit_price"] == "390"
    assert row["stop_price"] == "340"


def test_buy_action_is_review_when_budget_is_below_one_share() -> None:
    row = build_trade_action_row(
        plan=active_plan(),
        quote_status=quote_status("entry_zone", price="390"),
        portfolio=portfolio_context(cash="100"),
        source_plan="data/latest/trading_plan.csv",
    )

    assert row["action"] == "REVIEW"
    assert row["status"] == "review"
    assert row["suggested_quantity"] == ""
    assert "below one share" in row["error"]


def test_buy_action_is_review_when_same_currency_cash_is_zero() -> None:
    row = build_trade_action_row(
        plan=active_plan(),
        quote_status=quote_status("entry_zone", price="390"),
        portfolio=portfolio_context(cash="0"),
        source_plan="data/latest/trading_plan.csv",
    )

    assert row["action"] == "REVIEW"
    assert row["status"] == "review"
    assert "same-currency cash" in row["error"]


def test_add_action_defaults_to_40_percent_when_plan_ratio_is_missing() -> None:
    row = build_trade_action_row(
        plan=active_plan(plan_text="操作计划：350美元附近加仓。"),
        quote_status=quote_status("add_zone", price="350"),
        portfolio=portfolio_context(cash="20000"),
        source_plan="data/latest/trading_plan.csv",
    )

    assert row["action"] == "ADD"
    assert row["status"] == "ready"
    assert row["suggested_notional"] == "4550"
    assert row["suggested_quantity"] == "13"


def test_buy_action_defaults_to_60_percent_when_plan_ratio_is_missing() -> None:
    row = build_trade_action_row(
        plan=active_plan(plan_text="操作计划：耐心等待回调，350美元附近加仓剩余40%。"),
        quote_status=quote_status("entry_zone", price="400"),
        portfolio=portfolio_context(cash="20000"),
        source_plan="data/latest/trading_plan.csv",
    )

    assert row["action"] == "BUY"
    assert row["status"] == "ready"
    assert row["suggested_quantity"] == "18"
    assert row["suggested_notional"] == "7200"


def test_buy_action_uses_remaining_target_budget_as_binding_cap() -> None:
    row = build_trade_action_row(
        plan=active_plan(max_weight="5%"),
        quote_status=quote_status("entry_zone", price="390"),
        portfolio=portfolio_context(cash="20000"),
        source_plan="data/latest/trading_plan.csv",
    )

    assert row["action"] == "BUY"
    assert row["status"] == "ready"
    assert row["suggested_quantity"] == "2"
    assert row["suggested_notional"] == "780"


def test_add_action_uses_remaining_target_budget_as_binding_cap() -> None:
    row = build_trade_action_row(
        plan=active_plan(max_weight="5%", plan_text="操作计划：350美元附近加仓。"),
        quote_status=quote_status("add_zone", price="350"),
        portfolio=portfolio_context(cash="20000"),
        source_plan="data/latest/trading_plan.csv",
    )

    assert row["action"] == "ADD"
    assert row["status"] == "ready"
    assert row["suggested_quantity"] == "3"
    assert row["suggested_notional"] == "1050"


def test_stop_loss_sells_full_position() -> None:
    row = build_trade_action_row(
        plan=active_plan(),
        quote_status=quote_status("stop_loss_hit", price="339"),
        portfolio=portfolio_context(quantity="10"),
        source_plan="data/latest/trading_plan.csv",
    )

    assert row["action"] == "SELL_STOP"
    assert row["priority"] == "critical"
    assert row["status"] == "ready"
    assert row["suggested_quantity"] == "10"
    assert row["suggested_notional"] == "3390"
    assert row["limit_price"] == ""
    assert row["stop_price"] == "340"


def test_target_one_trims_half_position() -> None:
    row = build_trade_action_row(
        plan=active_plan(),
        quote_status=quote_status("target_1_hit", price="451"),
        portfolio=portfolio_context(quantity="9"),
        source_plan="data/latest/trading_plan.csv",
    )

    assert row["action"] == "TRIM"
    assert row["status"] == "ready"
    assert row["suggested_quantity"] == "4"
    assert row["suggested_notional"] == "1804"


def test_target_two_takes_profit_on_full_position() -> None:
    row = build_trade_action_row(
        plan=active_plan(),
        quote_status=quote_status("target_2_hit", price="501"),
        portfolio=portfolio_context(quantity="9"),
        source_plan="data/latest/trading_plan.csv",
    )

    assert row["action"] == "TAKE_PROFIT"
    assert row["status"] == "ready"
    assert row["suggested_quantity"] == "9"
    assert row["suggested_notional"] == "4509"


def test_watch_maps_to_hold_without_sizing() -> None:
    row = build_trade_action_row(
        plan=active_plan(),
        quote_status=quote_status("watch"),
        portfolio=portfolio_context(),
        source_plan="data/latest/trading_plan.csv",
    )

    assert row["action"] == "HOLD"
    assert row["status"] == "watch"
    assert row["suggested_quantity"] == ""
    assert row["suggested_notional"] == ""


def test_missing_quote_maps_to_review_with_quote_message() -> None:
    row = build_trade_action_row(
        plan=active_plan(),
        quote_status=quote_status("missing_quote"),
        portfolio=portfolio_context(),
        source_plan="data/latest/trading_plan.csv",
    )

    assert row["action"] == "REVIEW"
    assert row["status"] == "review"
    assert row["error"] == "fixture message"
    assert row["reason"] == "fixture message"


def test_buy_side_missing_portfolio_position_maps_to_review() -> None:
    row = build_trade_action_row(
        plan=active_plan(),
        quote_status=quote_status("entry_zone"),
        portfolio=PortfolioActionContext(
            positions={},
            cash_by_currency={"USD": Decimal("1000")},
            total_market_value_hkd=Decimal("780000"),
        ),
        source_plan="data/latest/trading_plan.csv",
    )

    assert row["action"] == "REVIEW"
    assert row["status"] == "review"
    assert "missing portfolio position" in row["error"]


def test_buy_side_unparseable_target_max_weight_maps_to_review() -> None:
    row = build_trade_action_row(
        plan=active_plan(max_weight="twelve percent"),
        quote_status=quote_status("entry_zone"),
        portfolio=portfolio_context(),
        source_plan="data/latest/trading_plan.csv",
    )

    assert row["action"] == "REVIEW"
    assert row["status"] == "review"
    assert "target max weight" in row["error"]
