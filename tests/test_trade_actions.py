from __future__ import annotations

import csv
from dataclasses import FrozenInstanceError, replace
import pytest
from decimal import Decimal
from pathlib import Path

from open_trader.futu_watch import QuoteSnapshot
from open_trader.trading_plan import PlanQuoteStatus, TradingPlanRow
from open_trader.trading_plan import TRADING_PLAN_FIELDNAMES
from open_trader.trade_actions import (
    TRADE_ACTION_FIELDNAMES,
    PortfolioPositionSnapshot,
    PortfolioActionContext,
    TradeActionsResult,
    build_trade_action_row,
    generate_trade_actions,
    map_quote_status_to_action,
    load_portfolio_action_context,
    render_trade_actions_report,
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


def write_trading_plan(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=TRADING_PLAN_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def msft_plan_row(
    *,
    run_date: str = "2026-06-16",
    status: str = "active",
    symbol: str = "MSFT",
    agent_reason: str = "",
    agent_excerpt: str = "",
) -> dict[str, str]:
    return {
        "run_date": run_date,
        "symbol": symbol,
        "market": "US",
        "rating": "Overweight",
        "entry_zone_low": "380",
        "entry_zone_high": "400",
        "add_price": "350",
        "stop_loss": "340",
        "target_1": "450",
        "target_2": "500",
        "max_weight": "12%",
        "catalyst": "10月底财报",
        "time_horizon": "3-6个月",
        "plan_text": "操作计划：在380-400美元区间分3-4次买入目标仓位的60%，350美元附近加仓剩余40%。",
        "agent_reason": agent_reason,
        "agent_excerpt": agent_excerpt,
        "status": status,
        "error": "",
    }


@pytest.fixture
def valid_portfolio_path(tmp_path: Path) -> Path:
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
            "market_value": "96100",
            "cost_value": "",
            "unrealized_pnl": "",
            "unrealized_pnl_pct": "",
            "fx_source": "fixture",
            "fx_date": "2026-05-31",
            "fx_to_hkd": "7.8",
            "market_value_hkd": "749580",
            "cost_value_hkd": "",
            "portfolio_weight_hkd": "96.10%",
            "brokers": "futu",
            "accounts": "futu_main",
            "ai_eligible": "false",
            "analysis_symbol": "",
            "risk_flag": "normal",
            "confidence": "high",
            "notes": "",
        },
    ])
    return path


def active_plan(
    *,
    symbol: str = "MSFT",
    max_weight: str = "12%",
    plan_text: str = "操作计划：在380-400美元区间分3-4次买入目标仓位的60%，350美元附近加仓剩余40%。",
    agent_reason: str = "",
    agent_excerpt: str = "",
) -> TradingPlanRow:
    return TradingPlanRow(
        run_date="2026-06-16",
        symbol=symbol,
        market="US",
        source_status="ok",
        fallback_reason="",
        fallback_from_date="",
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
        agent_reason=agent_reason,
        agent_excerpt=agent_excerpt,
        status="active",
        error="",
    )


def portfolio_context(
    *,
    quantity: str = "10",
    avg_cost_price: str = "300",
    cash: str = "1000",
    market_value: str = "3900",
    market_value_hkd: str = "30420",
    weight: str = "0.039",
    fx_to_hkd: str = "7.8",
) -> PortfolioActionContext:
    return PortfolioActionContext(
        positions={
            ("US", "MSFT"): PortfolioPositionSnapshot(
                currency="USD",
                quantity=Decimal(quantity),
                avg_cost_price=Decimal(avg_cost_price),
                market_value=Decimal(market_value),
                market_value_hkd=Decimal(market_value_hkd),
                weight=Decimal(weight),
                fx_to_hkd=Decimal(fx_to_hkd),
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
        "avg_cost_price",
        "target_max_weight",
        "cash_available",
        "limit_price",
        "stop_price",
        "post_trade_quantity",
        "post_trade_weight",
        "post_trade_avg_cost",
        "risk_to_stop",
        "agent_reason",
        "agent_excerpt",
        "trigger_reason",
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
                avg_cost_price=Decimal("300"),
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
            avg_cost_price=Decimal("1"),
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
        ValueError, match=r"missing portfolio column\(s\): avg_cost_price, fx_to_hkd"
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
            avg_cost_price=Decimal("200"),
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
                avg_cost_price=Decimal("300"),
                market_value=Decimal("3900"),
                market_value_hkd=Decimal("30420"),
                weight=Decimal("0.39"),
                fx_to_hkd=Decimal("7.8"),
            )
        },
        cash_by_currency={},
        total_market_value_hkd=Decimal("30420"),
    )


def test_load_portfolio_action_context_tracks_invalid_position_values(
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
            avg_cost_price=Decimal("300"),
            market_value=Decimal("0"),
            market_value_hkd=Decimal("0"),
            weight=Decimal("0"),
            fx_to_hkd=Decimal("0"),
            invalid_fields=(
                "total_quantity",
                "market_value",
                "fx_to_hkd",
                "market_value_hkd",
            ),
        )
    }


@pytest.mark.parametrize("avg_cost_price", ["", "bad", "0", "-1"])
def test_load_portfolio_action_context_tracks_invalid_average_cost_values(
    tmp_path: Path,
    avg_cost_price: str,
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
            "avg_cost_price": avg_cost_price,
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
            "analysis_symbol": "AAPL",
            "risk_flag": "normal",
            "confidence": "high",
            "notes": "",
        },
    ])

    context = load_portfolio_action_context(path)

    assert context.positions[("US", "AAPL")].avg_cost_price == Decimal("0")
    assert context.positions[("US", "AAPL")].invalid_fields == ("avg_cost_price",)


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
            avg_cost_price=Decimal("300"),
            market_value=Decimal("12345678.90"),
            market_value_hkd=Decimal("10000.00"),
            weight=Decimal("0.01"),
            fx_to_hkd=Decimal("7800"),
        ),
        ("US", "BAD"): PortfolioPositionSnapshot(
            currency="USD",
            quantity=Decimal("0"),
            avg_cost_price=Decimal("300"),
            market_value=Decimal("0"),
            market_value_hkd=Decimal("10000"),
            weight=Decimal("0"),
            fx_to_hkd=Decimal("0"),
            invalid_fields=(
                "total_quantity",
                "market_value",
                "fx_to_hkd",
            ),
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
                avg_cost_price=Decimal("300"),
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
            "market,asset_class,symbol,currency,total_quantity,avg_cost_price,"
            "market_value,fx_to_hkd,market_value_hkd,portfolio_weight_hkd\n"
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
            "market,asset_class,symbol,currency,total_quantity,avg_cost_price,"
            "market_value,cost_value,unrealized_pnl,fx_to_hkd,market_value_hkd,"
            "portfolio_weight_hkd\n"
        )
        row = (
            "US,stock,AAPL,USD,10,300,3900,3000,900,7.8,30420,39.00%,"
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
    assert row["reason"] == row["error"]


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
    assert row["reason"] == row["error"]


def test_buy_action_is_review_when_last_price_is_invalid() -> None:
    row = build_trade_action_row(
        plan=active_plan(),
        quote_status=quote_status("entry_zone", price="0"),
        portfolio=portfolio_context(cash="1000"),
        source_plan="data/latest/trading_plan.csv",
    )

    assert row["action"] == "REVIEW"
    assert row["status"] == "review"
    assert "invalid last price" in row["error"]
    assert row["reason"] == row["error"]
    assert row["limit_price"] == ""
    assert row["stop_price"] == "340"


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
    assert row["limit_price"] == "350"
    assert row["stop_price"] == "340"


def test_buy_action_defaults_to_60_percent_when_plan_ratio_is_missing() -> None:
    row = build_trade_action_row(
        plan=active_plan(plan_text="操作计划：耐心等待回调，350美元附近加仓剩余40%。"),
        quote_status=quote_status("entry_zone", price="400"),
        portfolio=portfolio_context(
            quantity="0",
            cash="20000",
            market_value="0",
            market_value_hkd="0",
            weight="0",
        ),
        source_plan="data/latest/trading_plan.csv",
    )

    assert row["action"] == "BUY"
    assert row["status"] == "ready"
    assert row["suggested_quantity"] == "18"
    assert row["suggested_notional"] == "7200"


def test_buy_action_uses_remaining_entry_budget_as_binding_cap() -> None:
    row = build_trade_action_row(
        plan=active_plan(max_weight="10%"),
        quote_status=quote_status("entry_zone", price="390"),
        portfolio=portfolio_context(cash="20000"),
        source_plan="data/latest/trading_plan.csv",
    )

    assert row["action"] == "BUY"
    assert row["status"] == "ready"
    assert row["suggested_quantity"] == "5"
    assert row["suggested_notional"] == "1950"


def test_buy_action_is_review_when_no_remaining_target_budget() -> None:
    row = build_trade_action_row(
        plan=active_plan(max_weight="3%"),
        quote_status=quote_status("entry_zone", price="390"),
        portfolio=portfolio_context(cash="20000"),
        source_plan="data/latest/trading_plan.csv",
    )

    assert row["action"] == "REVIEW"
    assert row["status"] == "review"
    assert "no remaining target budget" in row["error"]
    assert row["reason"] == row["error"]


def test_buy_action_is_review_when_no_remaining_entry_budget() -> None:
    row = build_trade_action_row(
        plan=active_plan(max_weight="6%"),
        quote_status=quote_status("entry_zone", price="390"),
        portfolio=portfolio_context(cash="20000"),
        source_plan="data/latest/trading_plan.csv",
    )

    assert row["action"] == "REVIEW"
    assert row["status"] == "review"
    assert "no remaining entry budget" in row["error"]
    assert row["reason"] == row["error"]


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


@pytest.mark.parametrize("trigger_status, plan_text", [
    ("entry_zone", "操作计划：在380-400美元区间分3-4次买入目标仓位的60%，350美元附近加仓剩余40%。"),
    ("add_zone", "操作计划：350美元附近加仓。"),
])
def test_buy_side_zero_fx_is_review(
    trigger_status: str,
    plan_text: str,
) -> None:
    row = build_trade_action_row(
        plan=active_plan(plan_text=plan_text),
        quote_status=quote_status(trigger_status),
        portfolio=portfolio_context(fx_to_hkd="0"),
        source_plan="data/latest/trading_plan.csv",
    )

    assert row["action"] == "REVIEW"
    assert row["status"] == "review"
    assert "positive fx_to_hkd" in row["error"]
    assert row["reason"] == row["error"]


@pytest.mark.parametrize("trigger_status", ["entry_zone", "add_zone"])
def test_buy_side_invalid_portfolio_sizing_fields_map_to_review(
    trigger_status: str,
) -> None:
    portfolio = portfolio_context(cash="20000", market_value="0")
    broken_position = PortfolioPositionSnapshot(
        currency="USD",
        quantity=Decimal("10"),
        avg_cost_price=Decimal("300"),
        market_value=Decimal("0"),
        market_value_hkd=Decimal("30420"),
        weight=Decimal("0.039"),
        fx_to_hkd=Decimal("7.8"),
        invalid_fields=("market_value",),
    )
    portfolio = PortfolioActionContext(
        positions={("US", "MSFT"): broken_position},
        cash_by_currency=portfolio.cash_by_currency,
        total_market_value_hkd=portfolio.total_market_value_hkd,
    )

    row = build_trade_action_row(
        plan=active_plan(),
        quote_status=quote_status(trigger_status),
        portfolio=portfolio,
        source_plan="data/latest/trading_plan.csv",
    )

    assert row["action"] == "REVIEW"
    assert row["status"] == "review"
    assert "invalid portfolio sizing field(s): market_value" in row["error"]
    assert row["suggested_quantity"] == ""
    assert row["suggested_notional"] == ""


def test_ready_buy_includes_average_cost_and_post_trade_fields() -> None:
    row = build_trade_action_row(
        plan=active_plan(max_weight="10.4%"),
        quote_status=quote_status("entry_zone", price="390"),
        portfolio=portfolio_context(
            quantity="10",
            avg_cost_price="300",
            cash="20000",
            market_value="3900",
            market_value_hkd="30420",
            weight="0.039",
            fx_to_hkd="7.8",
        ),
        source_plan="data/latest/trading_plan.csv",
    )

    assert row["action"] == "BUY"
    assert row["status"] == "ready"
    assert row["avg_cost_price"] == "300"
    assert row["suggested_quantity"] == "6"
    assert row["suggested_notional"] == "2340"
    assert row["post_trade_quantity"] == "16"
    assert row["post_trade_avg_cost"] == "333.75"
    assert row["post_trade_weight"] == "6.24%"
    assert row["risk_to_stop"] == "800"


def test_invalid_average_cost_maps_buy_to_review_and_blanks_post_trade_fields() -> None:
    portfolio = portfolio_context(cash="20000")
    broken_position = PortfolioPositionSnapshot(
        currency="USD",
        quantity=Decimal("10"),
        avg_cost_price=Decimal("0"),
        market_value=Decimal("3900"),
        market_value_hkd=Decimal("30420"),
        weight=Decimal("0.039"),
        fx_to_hkd=Decimal("7.8"),
        invalid_fields=("avg_cost_price",),
    )
    portfolio = PortfolioActionContext(
        positions={("US", "MSFT"): broken_position},
        cash_by_currency=portfolio.cash_by_currency,
        total_market_value_hkd=portfolio.total_market_value_hkd,
    )

    row = build_trade_action_row(
        plan=active_plan(),
        quote_status=quote_status("entry_zone", price="390"),
        portfolio=portfolio,
        source_plan="data/latest/trading_plan.csv",
    )

    assert row["action"] == "REVIEW"
    assert row["status"] == "review"
    assert row["error"] == "invalid portfolio sizing field(s): avg_cost_price"
    assert row["post_trade_quantity"] == ""
    assert row["post_trade_weight"] == ""
    assert row["post_trade_avg_cost"] == ""
    assert row["risk_to_stop"] == ""


def test_nonpositive_average_cost_value_maps_buy_to_review() -> None:
    portfolio = portfolio_context(cash="20000")
    broken_position = PortfolioPositionSnapshot(
        currency="USD",
        quantity=Decimal("10"),
        avg_cost_price=Decimal("0"),
        market_value=Decimal("3900"),
        market_value_hkd=Decimal("30420"),
        weight=Decimal("0.039"),
        fx_to_hkd=Decimal("7.8"),
    )
    portfolio = PortfolioActionContext(
        positions={("US", "MSFT"): broken_position},
        cash_by_currency=portfolio.cash_by_currency,
        total_market_value_hkd=portfolio.total_market_value_hkd,
    )

    row = build_trade_action_row(
        plan=active_plan(),
        quote_status=quote_status("entry_zone", price="390"),
        portfolio=portfolio,
        source_plan="data/latest/trading_plan.csv",
    )

    assert row["action"] == "REVIEW"
    assert row["status"] == "review"
    assert row["error"] == "invalid portfolio sizing field(s): avg_cost_price"


@pytest.mark.parametrize(
    ("avg_cost_price", "invalid_fields"),
    [
        (Decimal("0"), ("avg_cost_price",)),
        (Decimal("0"), ()),
    ],
)
def test_buy_opening_position_allows_missing_average_cost(
    avg_cost_price: Decimal,
    invalid_fields: tuple[str, ...],
) -> None:
    portfolio = portfolio_context(
        quantity="0",
        avg_cost_price="1",
        cash="20000",
        market_value="0",
        market_value_hkd="0",
        weight="0",
        fx_to_hkd="7.8",
    )
    opening_position = PortfolioPositionSnapshot(
        currency="USD",
        quantity=Decimal("0"),
        avg_cost_price=avg_cost_price,
        market_value=Decimal("0"),
        market_value_hkd=Decimal("0"),
        weight=Decimal("0"),
        fx_to_hkd=Decimal("7.8"),
        invalid_fields=invalid_fields,
    )
    portfolio = PortfolioActionContext(
        positions={("US", "MSFT"): opening_position},
        cash_by_currency=portfolio.cash_by_currency,
        total_market_value_hkd=portfolio.total_market_value_hkd,
    )

    row = build_trade_action_row(
        plan=active_plan(plan_text="操作计划：在380-400美元区间买入目标仓位的60%。"),
        quote_status=quote_status("entry_zone", price="390"),
        portfolio=portfolio,
        source_plan="data/latest/trading_plan.csv",
    )

    assert row["action"] == "BUY"
    assert row["status"] == "ready"
    assert row["suggested_quantity"] == "18"
    assert row["suggested_notional"] == "7020"
    assert row["post_trade_quantity"] == "18"
    assert row["post_trade_avg_cost"] == "390"


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
    assert row["post_trade_quantity"] == "0"
    assert row["post_trade_weight"] == "0%"
    assert row["post_trade_avg_cost"] == ""
    assert row["risk_to_stop"] == ""


@pytest.mark.parametrize("trigger_status", ["stop_loss_hit", "target_1_hit", "target_2_hit"])
def test_sell_side_invalid_last_price_is_review(trigger_status: str) -> None:
    row = build_trade_action_row(
        plan=active_plan(),
        quote_status=quote_status(trigger_status, price="0"),
        portfolio=portfolio_context(quantity="10"),
        source_plan="data/latest/trading_plan.csv",
    )

    assert row["action"] == "REVIEW"
    assert row["status"] == "review"
    assert "invalid last price" in row["error"]
    assert row["reason"] == row["error"]
    assert row["suggested_quantity"] == ""
    assert row["suggested_notional"] == ""
    if trigger_status == "stop_loss_hit":
        assert row["priority"] == "critical"
    elif trigger_status == "target_2_hit":
        assert row["priority"] == "high"
    else:
        assert row["priority"] == "medium"


def test_stop_loss_missing_position_is_review() -> None:
    row = build_trade_action_row(
        plan=active_plan(),
        quote_status=quote_status("stop_loss_hit", price="339"),
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
    assert row["reason"] == row["error"]


def test_trim_with_quantity_below_one_share_is_review() -> None:
    row = build_trade_action_row(
        plan=active_plan(),
        quote_status=quote_status("target_1_hit", price="451"),
        portfolio=portfolio_context(quantity="1"),
        source_plan="data/latest/trading_plan.csv",
    )

    assert row["action"] == "REVIEW"
    assert row["status"] == "review"
    assert "below one share" in row["error"]
    assert row["reason"] == row["error"]


def test_target_one_trims_half_position() -> None:
    row = build_trade_action_row(
        plan=active_plan(),
        quote_status=quote_status("target_1_hit", price="451"),
        portfolio=portfolio_context(quantity="9"),
        source_plan="data/latest/trading_plan.csv",
    )

    assert row["action"] == "TRIM"
    assert row["status"] == "ready"
    assert row["limit_price"] == "451"
    assert row["stop_price"] == "340"
    assert row["suggested_quantity"] == "4"
    assert row["suggested_notional"] == "1804"
    assert row["post_trade_quantity"] == "5"
    assert row["post_trade_weight"] == "2.255%"
    assert row["post_trade_avg_cost"] == "300"
    assert row["risk_to_stop"] == "555"


def test_target_one_trim_does_not_require_average_cost_or_stop_loss() -> None:
    portfolio = portfolio_context()
    position = PortfolioPositionSnapshot(
        currency="USD",
        quantity=Decimal("200"),
        avg_cost_price=Decimal("0"),
        market_value=Decimal("4842.8"),
        market_value_hkd=Decimal("38015.98"),
        weight=Decimal("0.0305"),
        fx_to_hkd=Decimal("7.85"),
        invalid_fields=("avg_cost_price",),
    )
    portfolio = PortfolioActionContext(
        positions={("US", "MSFT"): position},
        cash_by_currency=portfolio.cash_by_currency,
        total_market_value_hkd=portfolio.total_market_value_hkd,
    )
    plan = active_plan(max_weight="", plan_text="操作计划：Reduce existing exposure by 50%.")
    plan = replace(plan, stop_loss=Decimal("0"))

    row = build_trade_action_row(
        plan=plan,
        quote_status=quote_status("target_1_hit", price="21.7"),
        portfolio=portfolio,
        source_plan="data/latest/trading_plan.csv",
    )

    assert row["action"] == "TRIM"
    assert row["status"] == "ready"
    assert row["suggested_quantity"] == "100"
    assert row["suggested_notional"] == "2170"
    assert row["post_trade_quantity"] == "100"
    assert row["post_trade_weight"] == "2.183910256410256410256410256%"
    assert row["post_trade_avg_cost"] == ""
    assert row["risk_to_stop"] == ""


def test_sell_action_handles_blank_stop_loss() -> None:
    plan = replace(active_plan(), stop_loss=None)

    row = build_trade_action_row(
        plan=plan,
        quote_status=quote_status("target_1_hit", price="451"),
        portfolio=portfolio_context(quantity="9"),
        source_plan="data/latest/trading_plan.csv",
    )

    assert row["action"] == "TRIM"
    assert row["status"] == "ready"
    assert row["stop_price"] == ""
    assert row["risk_to_stop"] == ""


@pytest.mark.parametrize(
    ("trigger_status", "expected_action"),
    [
        ("stop_loss_hit", "SELL_STOP"),
        ("target_1_hit", "TRIM"),
        ("target_2_hit", "TAKE_PROFIT"),
    ],
)
def test_nonpositive_average_cost_value_does_not_block_sell_actions(
    trigger_status: str,
    expected_action: str,
) -> None:
    portfolio = portfolio_context()
    broken_position = PortfolioPositionSnapshot(
        currency="USD",
        quantity=Decimal("10"),
        avg_cost_price=Decimal("0"),
        market_value=Decimal("3900"),
        market_value_hkd=Decimal("30420"),
        weight=Decimal("0.039"),
        fx_to_hkd=Decimal("7.8"),
    )
    portfolio = PortfolioActionContext(
        positions={("US", "MSFT"): broken_position},
        cash_by_currency=portfolio.cash_by_currency,
        total_market_value_hkd=portfolio.total_market_value_hkd,
    )

    row = build_trade_action_row(
        plan=active_plan(),
        quote_status=quote_status(trigger_status, price="451"),
        portfolio=portfolio,
        source_plan="data/latest/trading_plan.csv",
    )

    assert row["action"] == expected_action
    assert row["status"] == "ready"
    assert row["error"] == ""
    assert row["post_trade_avg_cost"] == ""


def test_underweight_trim_plan_entry_zone_maps_to_trim_without_target_weight() -> None:
    portfolio = portfolio_context(quantity="50", avg_cost_price="0")
    position = PortfolioPositionSnapshot(
        currency="USD",
        quantity=Decimal("50"),
        avg_cost_price=Decimal("0"),
        market_value=Decimal("1863"),
        market_value_hkd=Decimal("14624.55"),
        weight=Decimal("0.0117"),
        fx_to_hkd=Decimal("7.85"),
        invalid_fields=("avg_cost_price",),
    )
    portfolio = PortfolioActionContext(
        positions={("US", "MSFT"): position},
        cash_by_currency=portfolio.cash_by_currency,
        total_market_value_hkd=portfolio.total_market_value_hkd,
    )
    plan = active_plan(
        max_weight="",
        plan_text="操作计划：Trim BOTZ position by 30-40% at current levels.",
    )

    row = build_trade_action_row(
        plan=plan,
        quote_status=quote_status("entry_zone", price="38.25"),
        portfolio=portfolio,
        source_plan="data/latest/trading_plan.csv",
    )

    assert row["action"] == "TRIM"
    assert row["status"] == "ready"
    assert row["suggested_quantity"] == "25"
    assert row["suggested_notional"] == "956.25"
    assert row["target_max_weight"] == ""
    assert row["reason"] == "Plan text indicates trim at current levels."
    assert row["error"] == ""


def test_build_trade_action_row_preserves_agent_reason_and_trigger() -> None:
    row = build_trade_action_row(
        plan=active_plan(
            max_weight="",
            plan_text="操作计划：Reduce MSFT exposure at current levels.",
            agent_reason=(
                "TradingAgents建议减仓，原文依据：The bear demonstrated that "
                "normalized earnings imply a ~316x P/E."
            ),
            agent_excerpt=(
                "The bear demonstrated that normalized earnings imply a ~316x P/E."
            ),
        ),
        quote_status=quote_status("target_1_hit", price="451"),
        portfolio=portfolio_context(),
        source_plan="plan.csv",
    )

    assert row["action"] == "TRIM"
    assert row["agent_reason"].startswith("TradingAgents建议减仓")
    assert row["agent_excerpt"] == (
        "The bear demonstrated that normalized earnings imply a ~316x P/E."
    )
    assert row["trigger_reason"] == "fixture message"
    assert row["reason"] == row["agent_reason"]


def test_target_two_takes_profit_on_full_position() -> None:
    row = build_trade_action_row(
        plan=active_plan(),
        quote_status=quote_status("target_2_hit", price="501"),
        portfolio=portfolio_context(quantity="9"),
        source_plan="data/latest/trading_plan.csv",
    )

    assert row["action"] == "TAKE_PROFIT"
    assert row["status"] == "ready"
    assert row["limit_price"] == "501"
    assert row["stop_price"] == "340"
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
    assert row["limit_price"] == ""
    assert row["stop_price"] == "340"
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


def test_missing_quote_review_keeps_agent_fields_but_reason_stays_operational() -> None:
    row = build_trade_action_row(
        plan=active_plan(
            agent_reason="TradingAgents建议继续观察，但这里先保留模型叙述。",
            agent_excerpt="Model narrative excerpt.",
        ),
        quote_status=quote_status("missing_quote"),
        portfolio=portfolio_context(),
        source_plan="data/latest/trading_plan.csv",
    )

    assert row["status"] == "review"
    assert row["agent_reason"] == "TradingAgents建议继续观察，但这里先保留模型叙述。"
    assert row["trigger_reason"] == "fixture message"
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
    assert row["priority"] == "high"
    assert "missing portfolio position" in row["error"]
    assert row["reason"] == row["error"]
    assert row["limit_price"] == ""
    assert row["stop_price"] == "340"


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
    assert row["reason"] == row["error"]
    assert row["limit_price"] == ""
    assert row["stop_price"] == "340"


def test_trade_action_rows_always_match_fieldname_shape() -> None:
    buy_row = build_trade_action_row(
        plan=active_plan(),
        quote_status=quote_status("entry_zone", price="390"),
        portfolio=portfolio_context(cash="1000"),
        source_plan="data/latest/trading_plan.csv",
    )
    sell_row = build_trade_action_row(
        plan=active_plan(),
        quote_status=quote_status("stop_loss_hit", price="339"),
        portfolio=portfolio_context(quantity="10"),
        source_plan="data/latest/trading_plan.csv",
    )
    review_row = build_trade_action_row(
        plan=active_plan(),
        quote_status=quote_status("missing_quote"),
        portfolio=portfolio_context(),
        source_plan="data/latest/trading_plan.csv",
    )

    expected_keys = set(TRADE_ACTION_FIELDNAMES)
    assert set(buy_row) == expected_keys
    assert set(sell_row) == expected_keys
    assert set(review_row) == expected_keys


def test_generate_trade_actions_writes_csv_report_and_latest(
    tmp_path: Path,
    valid_portfolio_path: Path,
) -> None:
    plan_path = tmp_path / "trading_plan.csv"
    data_dir = tmp_path / "data"
    reports_dir = tmp_path / "reports"
    write_trading_plan(plan_path, [msft_plan_row()])

    result = generate_trade_actions(
        plan_path=plan_path,
        portfolio_path=valid_portfolio_path,
        data_dir=data_dir,
        reports_dir=reports_dir,
        snapshots={"US.MSFT": QuoteSnapshot(futu_symbol="US.MSFT", last_price=Decimal("390"))},
        run_date=None,
        update_latest=True,
    )

    expected_actions_path = data_dir / "runs" / "2026-06-16" / "trade_actions.csv"
    expected_latest_path = data_dir / "latest" / "trade_actions.csv"
    expected_report_path = reports_dir / "trade_actions" / "2026-06-16.md"
    assert result == TradeActionsResult(
        run_date="2026-06-16",
        action_count=1,
        ready_count=1,
        review_count=0,
        watch_count=0,
        actions_path=expected_actions_path,
        latest_path=expected_latest_path,
        report_path=expected_report_path,
    )

    with expected_actions_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) == 1
    assert rows[0]["action"] == "BUY"
    assert rows[0]["status"] == "ready"
    assert expected_latest_path.read_text(encoding="utf-8") == expected_actions_path.read_text(
        encoding="utf-8"
    )

    report = expected_report_path.read_text(encoding="utf-8")
    assert "行动：BUY" in report
    assert "标的：US.MSFT" in report
    assert "建议：买入 8 股，预算约 USD 3120" in report


def test_generate_trade_actions_writes_market_scoped_hk_paths_and_uses_hkd_cash(
    tmp_path: Path,
) -> None:
    plan_path = tmp_path / "data/runs/2026-06-19/HK/trading_plan.csv"
    hk_plan = msft_plan_row(
        run_date="2026-06-19",
        symbol="00700",
    )
    hk_plan.update(
        {
            "market": "HK",
            "entry_zone_low": "370",
            "entry_zone_high": "390",
            "max_weight": "5%",
        }
    )
    write_trading_plan(plan_path, [hk_plan])
    portfolio_path = tmp_path / "portfolio.csv"
    write_portfolio(
        portfolio_path,
        [
            {
                "market": "HK",
                "asset_class": "stock",
                "symbol": "00700",
                "currency": "HKD",
                "total_quantity": "100",
                "avg_cost_price": "350",
                "market_value": "38000",
                "fx_to_hkd": "1",
                "market_value_hkd": "38000",
                "portfolio_weight_hkd": "2.00%",
            },
            {
                "market": "CASH",
                "asset_class": "cash",
                "symbol": "HKD_CASH",
                "currency": "HKD",
                "total_quantity": "1",
                "market_value": "10000",
                "fx_to_hkd": "1",
                "market_value_hkd": "10000",
                "portfolio_weight_hkd": "0.50%",
            },
        ],
    )

    result = generate_trade_actions(
        plan_path=plan_path,
        portfolio_path=portfolio_path,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        snapshots={"HK.00700": QuoteSnapshot("HK.00700", Decimal("380"))},
        run_date="2026-06-19",
        update_latest=True,
        market="HK",
    )
    rows = list(csv.DictReader(result.actions_path.open(encoding="utf-8")))

    assert result.actions_path == tmp_path / "data/runs/2026-06-19/HK/trade_actions.csv"
    assert result.latest_path == tmp_path / "data/latest/HK/trade_actions.csv"
    assert result.report_path == tmp_path / "reports/trade_actions/2026-06-19-HK.md"
    assert rows[0]["futu_symbol"] == "HK.00700"
    assert rows[0]["notional_currency"] == "HKD"
    assert rows[0]["cash_available"] == "10000"


@pytest.mark.parametrize("market", ["JP", "../HK", ""])
def test_generate_trade_actions_rejects_invalid_market_before_writing(
    tmp_path: Path,
    valid_portfolio_path: Path,
    market: str,
) -> None:
    plan_path = tmp_path / "trading_plan.csv"
    data_dir = tmp_path / "data"
    reports_dir = tmp_path / "reports"
    write_trading_plan(plan_path, [msft_plan_row()])

    with pytest.raises(ValueError, match="market must be one of: HK, US, CN"):
        generate_trade_actions(
            plan_path=plan_path,
            portfolio_path=valid_portfolio_path,
            data_dir=data_dir,
            reports_dir=reports_dir,
            snapshots={"US.MSFT": QuoteSnapshot("US.MSFT", Decimal("390"))},
            run_date="2026-06-16",
            update_latest=True,
            market=market,
        )

    assert not (data_dir / "runs").exists()
    assert not (data_dir / "latest").exists()
    assert not (reports_dir / "trade_actions").exists()


def test_generate_trade_actions_dry_run_does_not_update_latest(
    tmp_path: Path,
    valid_portfolio_path: Path,
) -> None:
    plan_path = tmp_path / "trading_plan.csv"
    data_dir = tmp_path / "data"
    reports_dir = tmp_path / "reports"
    latest_path = data_dir / "latest" / "trade_actions.csv"
    latest_path.parent.mkdir(parents=True, exist_ok=True)
    latest_path.write_text("old latest", encoding="utf-8")
    write_trading_plan(plan_path, [msft_plan_row()])

    result = generate_trade_actions(
        plan_path=plan_path,
        portfolio_path=valid_portfolio_path,
        data_dir=data_dir,
        reports_dir=reports_dir,
        snapshots={},
        run_date="2026-06-16",
        update_latest=False,
    )

    assert result.actions_path.exists()
    assert result.report_path.exists()
    assert latest_path.read_text(encoding="utf-8") == "old latest"


def test_generate_trade_actions_marks_missing_quote_for_review(
    tmp_path: Path,
    valid_portfolio_path: Path,
) -> None:
    plan_path = tmp_path / "trading_plan.csv"
    data_dir = tmp_path / "data"
    reports_dir = tmp_path / "reports"
    write_trading_plan(plan_path, [msft_plan_row()])

    result = generate_trade_actions(
        plan_path=plan_path,
        portfolio_path=valid_portfolio_path,
        data_dir=data_dir,
        reports_dir=reports_dir,
        snapshots={},
        run_date="2026-06-16",
        update_latest=False,
    )

    with result.actions_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) == 1
    assert rows[0]["action"] == "REVIEW"
    assert rows[0]["status"] == "review"
    assert rows[0]["trigger_status"] == "missing_quote"
    assert "Futu did not return a quote." in rows[0]["reason"]
    assert "Futu did not return a quote." in rows[0]["error"]


def test_generate_trade_actions_requires_date_when_active_rows_have_no_run_date(
    tmp_path: Path,
    valid_portfolio_path: Path,
) -> None:
    plan_path = tmp_path / "trading_plan.csv"
    write_trading_plan(plan_path, [msft_plan_row(run_date="")])

    with pytest.raises(
        ValueError,
        match=r"--date is required when trading plan has no active run_date rows",
    ):
        generate_trade_actions(
            plan_path=plan_path,
            portfolio_path=valid_portfolio_path,
            data_dir=tmp_path / "data",
            reports_dir=tmp_path / "reports",
            snapshots={},
            run_date=None,
            update_latest=False,
        )


def test_generate_trade_actions_explicit_run_date_with_no_match_fails_without_writes(
    tmp_path: Path,
    valid_portfolio_path: Path,
) -> None:
    plan_path = tmp_path / "trading_plan.csv"
    data_dir = tmp_path / "data"
    reports_dir = tmp_path / "reports"
    latest_path = data_dir / "latest" / "trade_actions.csv"
    latest_path.parent.mkdir(parents=True, exist_ok=True)
    latest_path.write_text("old latest", encoding="utf-8")
    write_trading_plan(plan_path, [msft_plan_row(run_date="2026-06-15")])

    with pytest.raises(
        ValueError,
        match=r"no active trading plans match run_date 2026-06-16",
    ):
        generate_trade_actions(
            plan_path=plan_path,
            portfolio_path=valid_portfolio_path,
            data_dir=data_dir,
            reports_dir=reports_dir,
            snapshots={},
            run_date="2026-06-16",
            update_latest=True,
        )

    assert latest_path.read_text(encoding="utf-8") == "old latest"
    assert not (data_dir / "runs" / "2026-06-16" / "trade_actions.csv").exists()
    assert not (reports_dir / "trade_actions" / "2026-06-16.md").exists()


def test_generate_trade_actions_includes_blank_run_date_rows_in_selected_run(
    tmp_path: Path,
    valid_portfolio_path: Path,
) -> None:
    plan_path = tmp_path / "trading_plan.csv"
    data_dir = tmp_path / "data"
    reports_dir = tmp_path / "reports"
    write_trading_plan(plan_path, [
        msft_plan_row(run_date=""),
        msft_plan_row(run_date="2026-06-16"),
    ])

    result = generate_trade_actions(
        plan_path=plan_path,
        portfolio_path=valid_portfolio_path,
        data_dir=data_dir,
        reports_dir=reports_dir,
        snapshots={"US.MSFT": QuoteSnapshot(futu_symbol="US.MSFT", last_price=Decimal("390"))},
        run_date="2026-06-16",
        update_latest=False,
    )

    assert result.action_count == 2
    assert result.ready_count == 2
    with result.actions_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) == 2
    assert {row["run_date"] for row in rows} == {"2026-06-16"}


def test_render_trade_actions_report_orders_rows_and_formats_status_specific_suggestions() -> None:
    rows = [
        {
            "run_date": "2026-06-16",
            "symbol": "NFLX",
            "market": "US",
            "futu_symbol": "US.NFLX",
            "action": "SELL_STOP",
            "priority": "critical",
            "last_price": "700",
            "trigger_status": "stop_loss_hit",
            "suggested_quantity": "5",
            "suggested_notional": "3500",
            "notional_currency": "USD",
            "current_quantity": "5",
            "current_weight": "5%",
            "target_max_weight": "10%",
            "cash_available": "0",
            "limit_price": "",
            "stop_price": "710",
            "reason": "Current price is at or below the stop loss.",
            "source_plan": "plan.csv",
            "status": "ready",
            "error": "",
        },
        {
            "run_date": "2026-06-16",
            "symbol": "ZZZ",
            "market": "US",
            "futu_symbol": "US.ZZZ",
            "action": "BUY",
            "priority": "high",
            "last_price": "390",
            "trigger_status": "entry_zone",
            "suggested_quantity": "2",
            "suggested_notional": "780",
            "notional_currency": "USD",
            "current_quantity": "10",
            "current_weight": "39%",
            "target_max_weight": "12%",
            "cash_available": "1000",
            "limit_price": "390",
            "stop_price": "340",
            "reason": "Current price is inside the planned entry zone.",
            "source_plan": "plan.csv",
            "status": "ready",
            "error": "",
        },
        {
            "run_date": "2026-06-16",
            "symbol": "AAA",
            "market": "US",
            "futu_symbol": "US.AAA",
            "action": "BUY",
            "priority": "high",
            "last_price": "390",
            "trigger_status": "entry_zone",
            "suggested_quantity": "3",
            "suggested_notional": "1170",
            "notional_currency": "USD",
            "current_quantity": "0",
            "current_weight": "0%",
            "target_max_weight": "12%",
            "cash_available": "5000",
            "limit_price": "390",
            "stop_price": "340",
            "reason": "Current price is inside the planned entry zone.",
            "source_plan": "plan.csv",
            "status": "ready",
            "error": "",
        },
        {
            "run_date": "2026-06-16",
            "symbol": "MSFT",
            "market": "US",
            "futu_symbol": "US.MSFT",
            "action": "REVIEW",
            "priority": "medium",
            "last_price": "0",
            "trigger_status": "missing_quote",
            "suggested_quantity": "",
            "suggested_notional": "",
            "notional_currency": "USD",
            "current_quantity": "10",
            "current_weight": "39%",
            "target_max_weight": "12%",
            "cash_available": "1000",
            "limit_price": "",
            "stop_price": "340",
            "reason": "Futu did not return a quote.",
            "source_plan": "plan.csv",
            "status": "review",
            "error": "Futu did not return a quote.",
        },
        {
            "run_date": "2026-06-16",
            "symbol": "TSLA",
            "market": "US",
            "futu_symbol": "US.TSLA",
            "action": "HOLD",
            "priority": "low",
            "last_price": "300",
            "trigger_status": "watch",
            "suggested_quantity": "",
            "suggested_notional": "",
            "notional_currency": "USD",
            "current_quantity": "1",
            "current_weight": "2%",
            "target_max_weight": "8%",
            "cash_available": "1000",
            "limit_price": "",
            "stop_price": "250",
            "reason": "No plan trigger is active.",
            "source_plan": "plan.csv",
            "status": "watch",
            "error": "",
        },
    ]

    report = render_trade_actions_report("2026-06-16", rows)

    headings = [line for line in report.splitlines() if line.startswith("## ")]
    assert headings == [
        "## US.NFLX",
        "## US.AAA",
        "## US.ZZZ",
        "## US.MSFT",
        "## US.TSLA",
    ]
    assert "建议：买入 3 股，预算约 USD 1170" in report
    assert "建议：继续观察，不建议交易" in report
    assert "建议：需要人工复核：Futu did not return a quote." in report
