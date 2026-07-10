from __future__ import annotations

import csv
import json
from decimal import Decimal
from pathlib import Path

import pytest

from open_trader.backtest import BACKTEST_METRICS_SCHEMA_VERSION, run_backtest
from open_trader.trading_plan import TRADING_PLAN_FIELDNAMES


def write_trading_plan(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=TRADING_PLAN_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def write_prices(path: Path, rows: list[dict[str, str]]) -> None:
    fieldnames = ["date", "open", "high", "low", "close", "volume"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def active_plan_row(
    *,
    run_date: str = "2026-06-16",
    symbol: str = "MSFT",
    market: str = "US",
) -> dict[str, str]:
    return {
        "run_date": run_date,
        "symbol": symbol,
        "market": market,
        "source_status": "ok",
        "fallback_reason": "",
        "fallback_from_date": "",
        "rating": "Overweight",
        "entry_zone_low": "380",
        "entry_zone_high": "400",
        "add_price": "350",
        "stop_loss": "340",
        "target_1": "450",
        "target_2": "500",
        "max_weight": "10%",
        "catalyst": "earnings",
        "time_horizon": "3-6 months",
        "plan_text": "Buy in entry zone, trim at target.",
        "agent_reason": "趋势改善",
        "agent_excerpt": "Momentum improved.",
        "status": "active",
        "error": "",
    }


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def test_run_backtest_uses_trading_plan_after_run_date_and_writes_artifacts(
    tmp_path: Path,
) -> None:
    plan_path = tmp_path / "trading_plan.csv"
    prices_path = tmp_path / "prices.csv"
    data_dir = tmp_path / "data"
    reports_dir = tmp_path / "reports"
    write_trading_plan(plan_path, [active_plan_row()])
    write_prices(
        prices_path,
        [
            {
                "date": "2026-06-16",
                "open": "390",
                "high": "410",
                "low": "380",
                "close": "395",
                "volume": "1000",
            },
            {
                "date": "2026-06-17",
                "open": "405",
                "high": "410",
                "low": "399",
                "close": "405",
                "volume": "1000",
            },
            {
                "date": "2026-06-18",
                "open": "420",
                "high": "455",
                "low": "415",
                "close": "452",
                "volume": "1000",
            },
        ],
    )

    result = run_backtest(
        plan_path=plan_path,
        prices_path=prices_path,
        data_dir=data_dir,
        reports_dir=reports_dir,
        run_date="2026-06-16",
        symbol="MSFT",
        market="US",
        initial_cash=Decimal("100000"),
        commission_bps=Decimal("10"),
        slippage_bps=Decimal("5"),
    )

    assert result.run_id == "2026-06-16-US-MSFT-trading-plan"
    assert result.adapter == "backtrader"
    assert result.trade_count == 2
    assert result.metrics_path == (
        data_dir / "backtests/2026-06-16-US-MSFT-trading-plan/metrics.json"
    )
    assert result.trades_path.is_file()
    assert result.equity_curve_path.is_file()
    assert result.report_path.is_file()

    trades = read_csv(result.trades_path)
    assert [row["side"] for row in trades] == ["BUY", "SELL"]
    assert trades[0]["date"] == "2026-06-17"
    assert trades[0]["reason"] == "entry_zone"
    assert trades[0]["price"] == "400.2000"
    assert trades[0]["quantity"] == "24"
    assert trades[1]["date"] == "2026-06-18"
    assert trades[1]["reason"] == "target_1"
    assert trades[1]["price"] == "449.7750"

    metrics = json.loads(result.metrics_path.read_text(encoding="utf-8"))
    assert metrics["schema_version"] == BACKTEST_METRICS_SCHEMA_VERSION
    assert metrics["run_id"] == result.run_id
    assert metrics["adapter"] == "backtrader"
    assert metrics["initial_cash"] == "100000.00"
    assert metrics["trade_count"] == 2
    assert metrics["round_trips"] == 1
    assert metrics["win_rate_pct"] == "100.00"
    assert Decimal(metrics["final_equity"]) > Decimal("101000")
    assert Decimal(metrics["total_return_pct"]) > Decimal("1")
    assert "max_drawdown_pct" in metrics

    equity = read_csv(result.equity_curve_path)
    assert [row["date"] for row in equity] == [
        "2026-06-16",
        "2026-06-17",
        "2026-06-18",
    ]
    assert equity[0]["position_quantity"] == "0"
    assert equity[1]["position_quantity"] == "24"
    assert equity[2]["position_quantity"] == "0"

    report = result.report_path.read_text(encoding="utf-8")
    assert "# Backtest - 2026-06-16-US-MSFT-trading-plan" in report
    assert "标的：US.MSFT" in report
    assert "执行后端：backtrader" in report
    assert "交易次数：2" in report


def test_run_backtest_fails_before_outputs_when_plan_date_does_not_match(
    tmp_path: Path,
) -> None:
    plan_path = tmp_path / "trading_plan.csv"
    prices_path = tmp_path / "prices.csv"
    data_dir = tmp_path / "data"
    write_trading_plan(plan_path, [active_plan_row(run_date="2026-06-15")])
    write_prices(
        prices_path,
        [
            {
                "date": "2026-06-16",
                "open": "390",
                "high": "410",
                "low": "380",
                "close": "395",
                "volume": "1000",
            },
        ],
    )

    with pytest.raises(ValueError, match="no active trading plan matches"):
        run_backtest(
            plan_path=plan_path,
            prices_path=prices_path,
            data_dir=data_dir,
            reports_dir=tmp_path / "reports",
            run_date="2026-06-16",
            symbol="MSFT",
            market="US",
            initial_cash=Decimal("100000"),
            commission_bps=Decimal("10"),
            slippage_bps=Decimal("5"),
        )

    assert not (data_dir / "backtests").exists()


def test_run_backtest_marks_open_position_to_market_at_end(tmp_path: Path) -> None:
    plan_path = tmp_path / "trading_plan.csv"
    prices_path = tmp_path / "prices.csv"
    write_trading_plan(plan_path, [active_plan_row()])
    write_prices(
        prices_path,
        [
            {
                "date": "2026-06-17",
                "open": "405",
                "high": "410",
                "low": "399",
                "close": "405",
                "volume": "1000",
            },
            {
                "date": "2026-06-18",
                "open": "420",
                "high": "430",
                "low": "415",
                "close": "425",
                "volume": "1000",
            },
        ],
    )

    result = run_backtest(
        plan_path=plan_path,
        prices_path=prices_path,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        run_date="2026-06-16",
        symbol="MSFT",
        market="US",
        initial_cash=Decimal("100000"),
        commission_bps=Decimal("0"),
        slippage_bps=Decimal("0"),
    )

    trades = read_csv(result.trades_path)
    assert [row["side"] for row in trades] == ["BUY", "SELL"]
    assert trades[1]["reason"] == "end_of_backtest"
    assert trades[1]["price"] == "425.0000"


def test_run_backtest_treats_blank_plan_run_date_as_selected_date(
    tmp_path: Path,
) -> None:
    plan_path = tmp_path / "trading_plan.csv"
    prices_path = tmp_path / "prices.csv"
    write_trading_plan(plan_path, [active_plan_row(run_date="")])
    write_prices(
        prices_path,
        [
            {
                "date": "2026-06-15",
                "open": "390",
                "high": "410",
                "low": "380",
                "close": "395",
                "volume": "1000",
            },
            {
                "date": "2026-06-17",
                "open": "405",
                "high": "410",
                "low": "399",
                "close": "405",
                "volume": "1000",
            },
            {
                "date": "2026-06-18",
                "open": "420",
                "high": "455",
                "low": "415",
                "close": "452",
                "volume": "1000",
            },
        ],
    )

    result = run_backtest(
        plan_path=plan_path,
        prices_path=prices_path,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        run_date="2026-06-16",
        symbol="MSFT",
        market="US",
        initial_cash=Decimal("100000"),
        commission_bps=Decimal("0"),
        slippage_bps=Decimal("0"),
    )

    trades = read_csv(result.trades_path)
    assert trades[0]["run_date"] == "2026-06-16"
    assert trades[0]["date"] == "2026-06-17"
