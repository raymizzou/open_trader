from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from datetime import date, timedelta
from decimal import Decimal, ROUND_DOWN, ROUND_UP, localcontext
from pathlib import Path

import pytest

from open_trader.trend_curve_backtest import (
    run_trend_curve_backtest,
    run_trend_curve_portfolio_backtest,
)


def _write_curve_database(path: Path, temperatures: list[tuple[str, str]]) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE trend_curve_points (
                market TEXT NOT NULL,
                symbol TEXT NOT NULL,
                curve_date TEXT NOT NULL,
                price TEXT NOT NULL,
                temperature TEXT NOT NULL,
                strength TEXT NOT NULL,
                mom TEXT,
                yoy TEXT,
                bar TEXT,
                asset_id INTEGER NOT NULL,
                group_id INTEGER NOT NULL,
                tm_id INTEGER NOT NULL,
                ccy_id INTEGER NOT NULL,
                PRIMARY KEY (market, symbol, curve_date)
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO trend_curve_points
            (market, symbol, curve_date, price, temperature, strength,
             mom, yoy, bar, asset_id, group_id, tm_id, ccy_id)
            VALUES ('US', 'TEST', ?, '10', ?, '80', NULL, NULL, NULL,
                    1, 2, 3, 4)
            """,
            temperatures,
        )


def _write_ohlc(path: Path) -> None:
    rows = []
    execution_opens = {
        "2026-01-03": "10",
        "2026-01-05": "12",
        "2026-01-08": "12",
        "2026-01-10": "10.8",
        "2026-01-13": "10.8",
        "2026-01-15": "10.8",
    }
    for day in range(1, 16):
        trading_date = f"2026-01-{day:02d}"
        opening = execution_opens.get(trading_date, "99")
        rows.append(
            {
                "date": trading_date,
                "open": opening,
                "high": opening,
                "low": opening,
                "close": opening,
            }
        )
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=("date", "open", "high", "low", "close"))
        writer.writeheader()
        writer.writerows(rows)


def _write_portfolio_curve_database(
    path: Path, rows: list[tuple[str, str, str]]
) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE trend_curve_points (
                market TEXT NOT NULL,
                symbol TEXT NOT NULL,
                curve_date TEXT NOT NULL,
                price TEXT NOT NULL,
                temperature TEXT NOT NULL,
                strength TEXT NOT NULL,
                mom TEXT,
                yoy TEXT,
                bar TEXT,
                asset_id INTEGER NOT NULL,
                group_id INTEGER NOT NULL,
                tm_id INTEGER NOT NULL,
                ccy_id INTEGER NOT NULL,
                PRIMARY KEY (market, symbol, curve_date)
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO trend_curve_points
            (market, symbol, curve_date, price, temperature, strength,
             mom, yoy, bar, asset_id, group_id, tm_id, ccy_id)
            VALUES ('US', ?, ?, '10', ?, '80', NULL, NULL, NULL,
                    1, 2, 3, 4)
            """,
            rows,
        )


def _write_portfolio_ohlc(
    path: Path, symbol: str, dates: list[str]
) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=("date", "open", "high", "low", "close")
        )
        writer.writeheader()
        for trading_date in ["2024-12-31", *dates, "2026-01-02"]:
            if symbol == "B" and trading_date == "2025-01-04":
                continue
            if symbol == "A":
                values = {
                    "2024-12-31": ("100", "100"),
                    "2025-01-04": ("3", "4"),
                    "2025-01-05": ("3.4", "3.4"),
                    "2025-01-06": ("3.4", "3"),
                    "2026-01-02": ("1", "1"),
                }.get(trading_date, ("3", "3"))
            else:
                values = {
                    "2024-12-31": ("20", "20"),
                    "2026-01-02": ("5", "5"),
                }.get(trading_date, ("10", "10"))
            opening, closing = values
            writer.writerow(
                {
                    "date": trading_date,
                    "open": opening,
                    "high": max(opening, closing),
                    "low": min(opening, closing),
                    "close": closing,
                }
            )


def test_portfolio_backtest_preflights_boundaries_renormalizes_fixed_sleeves_and_reports_metrics(
    tmp_path: Path,
) -> None:
    database = tmp_path / "history.sqlite3"
    prices_dir = tmp_path / "prices"
    prices_dir.mkdir()
    portfolio = tmp_path / "portfolio.csv"
    exclusions = tmp_path / "exclusions.json"
    portfolio.write_text(
        "market,asset_class,symbol,analysis_symbol,name,market_value_hkd,ai_eligible\n"
        "US,stock,A,A,甲,60,true\n"
        "US,stock,B,B,乙,40,true\n"
        "US,stock,C,C,丙,100,true\n"
        "US,stock,D,D,丁,200,true\n",
        encoding="utf-8",
    )
    exclusions.write_text(
        json.dumps({"US.D": "configured exclusion"}), encoding="utf-8"
    )
    dates = [
        (date(2025, 1, 1) + timedelta(days=offset)).isoformat()
        for offset in range(366)
    ]
    _write_portfolio_curve_database(
        database,
        [
            ("A", "2025-01-01", "温"),
            ("A", "2025-01-03", "热"),
            ("A", "2025-01-05", "平"),
            ("A", "2026-01-01", "平"),
            ("B", "2025-01-01", "平"),
            ("B", "2026-01-01", "平"),
            ("C", "2025-01-01", "平"),
        ],
    )
    _write_portfolio_ohlc(prices_dir / "A.csv", "A", dates)
    _write_portfolio_ohlc(prices_dir / "B.csv", "B", dates)
    _write_portfolio_ohlc(prices_dir / "C.csv", "C", dates)

    result = run_trend_curve_portfolio_backtest(
        database=database,
        prices_dir=prices_dir,
        portfolio=portfolio,
        exclusions=exclusions,
        start_date="2025-01-01",
        end_date="2026-01-01",
        initial_cash=Decimal("100"),
        commission_bps=Decimal("0"),
        slippage_bps=Decimal("0"),
    )

    included = {row["symbol"]: row for row in result["preflight"]["included"]}
    excluded = {row["symbol"]: row for row in result["preflight"]["excluded"]}
    per_symbol = {row["symbol"]: row for row in result["per_symbol"]}
    strategy_metrics = result["strategy"]["metrics"]
    buy_and_hold_metrics = result["buy_and_hold"]["metrics"]
    assert {
        "included": [(symbol, included[symbol]["name_zh"]) for symbol in sorted(included)],
        "excluded": {
            symbol: (row["name_zh"], row["reason"])
            for symbol, row in sorted(excluded.items())
        },
        "weights": result["weights"],
        "allocated_cash": {
            symbol: row["allocated_cash"] for symbol, row in per_symbol.items()
        },
        "strategy_equity_at_b_gap": [
            (row["date"], row["equity"])
            for row in result["strategy"]["equity_curve"]
            if row["date"] == "2025-01-04"
        ],
        "a_trades": [
            (trade["action"], trade["date"])
            for trade in per_symbol["A"]["result"]["trades"]
        ],
        "strategy_metrics": {
            key: strategy_metrics[key]
            for key in (
                "final_equity",
                "total_return_pct",
                "annualized_return_pct",
                "max_drawdown_pct",
                "sharpe_ratio",
                "calmar_ratio",
                "completed_round_count",
                "win_rate",
                "payoff_ratio_status",
            )
        },
        "buy_and_hold_metrics": {
            key: buy_and_hold_metrics[key]
            for key in (
                "final_equity",
                "total_return_pct",
                "annualized_return_pct",
                "max_drawdown_pct",
                "sharpe_ratio",
                "calmar_ratio",
            )
        },
        "source_hashes": result["source_hashes"],
    } == {
        "included": [("A", "甲"), ("B", "乙")],
        "excluded": {
            "C": ("丙", "missing_trend_curve_end"),
            "D": ("丁", "configured exclusion"),
        },
        "weights": {"A": "0.6", "B": "0.4"},
        "allocated_cash": {"A": "60", "B": "40"},
        "strategy_equity_at_b_gap": [("2025-01-04", "120")],
        "a_trades": [("BUY", "2025-01-04"), ("EXIT", "2025-01-06")],
        "strategy_metrics": {
            "final_equity": "108",
            "total_return_pct": "8",
            "annualized_return_pct": "8",
            "max_drawdown_pct": "10",
                "sharpe_ratio": "0.3716959708375140636603190558",
            "calmar_ratio": "0.8",
            "completed_round_count": 1,
            "win_rate": "1",
            "payoff_ratio_status": "no_losses",
        },
        "buy_and_hold_metrics": {
            "final_equity": "100",
            "total_return_pct": "0",
            "annualized_return_pct": "0",
            "max_drawdown_pct": "16.66666666666666666666666667",
            "sharpe_ratio": "0.09145339284257147718054561494",
            "calmar_ratio": "0",
        },
        "source_hashes": {
            "portfolio_csv": hashlib.sha256(portfolio.read_bytes()).hexdigest(),
            "exclusions_json": hashlib.sha256(exclusions.read_bytes()).hexdigest(),
            "trend_curve_database": hashlib.sha256(database.read_bytes()).hexdigest(),
            "ohlc_csvs": {
                symbol: hashlib.sha256(
                    (prices_dir / f"{symbol}.csv").read_bytes()
                ).hexdigest()
                for symbol in ("A", "B")
            },
        },
    }


def test_portfolio_backtest_ignores_pre_start_curve_state_and_defaults_cash(
    tmp_path: Path,
) -> None:
    database = tmp_path / "history.sqlite3"
    prices_dir = tmp_path / "prices"
    prices_dir.mkdir()
    portfolio = tmp_path / "portfolio.csv"
    exclusions = tmp_path / "exclusions.json"
    portfolio.write_text(
        "market,asset_class,symbol,analysis_symbol,name,market_value_hkd,ai_eligible\n"
        "US,stock,A,A,甲,100,true\n",
        encoding="utf-8",
    )
    exclusions.write_text("{}", encoding="utf-8")
    _write_portfolio_curve_database(
        database,
        [
            ("A", "2025-12-31", "温"),
            ("A", "2026-01-01", "热"),
            ("A", "2026-01-02", "热"),
        ],
    )
    _write_portfolio_ohlc(
        prices_dir / "A.csv", "A", ["2026-01-01", "2026-01-02"]
    )

    result = run_trend_curve_portfolio_backtest(
        database=database,
        prices_dir=prices_dir,
        portfolio=portfolio,
        exclusions=exclusions,
        start_date="2026-01-01",
        end_date="2026-01-02",
        commission_bps=Decimal("0"),
        slippage_bps=Decimal("0"),
    )

    assert {
        "trades": result["strategy"]["trades"],
        "completed_rounds": result["strategy"]["completed_rounds"],
        "final_equity": result["strategy"]["metrics"]["final_equity"],
        "initial_cash": result["assumptions"]["initial_cash"],
    } == {
        "trades": [],
        "completed_rounds": [],
        "final_equity": "1000000",
        "initial_cash": "1000000",
    }


def test_portfolio_buy_and_hold_drawdown_starts_from_initial_cash(
    tmp_path: Path,
) -> None:
    database = tmp_path / "history.sqlite3"
    prices_dir = tmp_path / "prices"
    prices_dir.mkdir()
    portfolio = tmp_path / "portfolio.csv"
    exclusions = tmp_path / "exclusions.json"
    portfolio.write_text(
        "market,asset_class,symbol,analysis_symbol,name,market_value_hkd,ai_eligible\n"
        "US,stock,A,A,甲,100,true\n",
        encoding="utf-8",
    )
    exclusions.write_text("{}", encoding="utf-8")
    _write_portfolio_curve_database(
        database, [("A", "2026-01-01", "平")]
    )
    with (prices_dir / "A.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=("date", "open", "high", "low", "close")
        )
        writer.writeheader()
        writer.writerow(
            {
                "date": "2026-01-01",
                "open": "100",
                "high": "100",
                "low": "100",
                "close": "100",
            }
        )

    result = run_trend_curve_portfolio_backtest(
        database=database,
        prices_dir=prices_dir,
        portfolio=portfolio,
        exclusions=exclusions,
        start_date="2026-01-01",
        end_date="2026-01-01",
        initial_cash=Decimal("1000"),
        commission_bps=Decimal("10"),
        slippage_bps=Decimal("5"),
    )
    metrics = result["buy_and_hold"]["metrics"]

    assert {
        "final_equity": metrics["final_equity"],
        "total_return_pct": metrics["total_return_pct"],
        "max_drawdown_pct": metrics["max_drawdown_pct"],
        "calmar_ratio": metrics["calmar_ratio"],
    } == {
        "final_equity": "997.3",
        "total_return_pct": "-0.27",
        "max_drawdown_pct": "0.27",
        "calmar_ratio": "-1",
    }


def test_backtest_strict_warm_to_hot_rounds_use_next_open_and_trend_metric_semantics(
    tmp_path: Path,
) -> None:
    database = tmp_path / "history.sqlite3"
    prices = tmp_path / "prices.csv"
    _write_curve_database(
        database,
        [
            (f"2026-01-{day:02d}", temperature)
            for day, temperature in enumerate(
                ["温", "热", "热", "平", "平", "温", "热", "热", "平", "平", "温", "热", "热", "平", "平"],
                start=1,
            )
        ],
    )
    _write_ohlc(prices)

    result = run_trend_curve_backtest(
        database=database,
        ohlc_csv=prices,
        market="US",
        symbol="TEST",
        start_date="2026-01-01",
        end_date="2026-01-15",
        initial_cash=Decimal("1000"),
        commission_bps=Decimal("0"),
        slippage_bps=Decimal("0"),
    )

    assert {
        "execution_dates": [trade["date"] for trade in result["trades"]],
        "round_cash_after_exit": [
            trade["cash_after"] for trade in result["trades"] if trade["side"] == "EXIT"
        ],
        "final_equity": result["metrics"]["final_equity"],
        "total_return_pct": result["metrics"]["total_return_pct"],
        "win_rate": result["metrics"]["win_rate"],
        "payoff_ratio": result["metrics"]["payoff_ratio"],
    } == {
        "execution_dates": [
            "2026-01-03",
            "2026-01-05",
            "2026-01-08",
            "2026-01-10",
            "2026-01-13",
            "2026-01-15",
        ],
        "round_cash_after_exit": ["1200", "1080", "1080"],
        "final_equity": "1080",
        "total_return_pct": "8",
        "win_rate": "0.3333333333333333333333333333",
        "payoff_ratio": "2",
    }


def test_strict_warm_to_hot_entry_ablation_changes_only_prior_temperature(
    tmp_path: Path,
) -> None:
    prices = tmp_path / "prices.csv"
    _write_ohlc(prices)
    baseline_database = tmp_path / "baseline.sqlite3"
    variant_database = tmp_path / "variant.sqlite3"
    _write_curve_database(
        baseline_database,
        [("2026-01-01", "温"), ("2026-01-02", "热")],
    )
    _write_curve_database(
        variant_database,
        [("2026-01-01", "平"), ("2026-01-02", "热")],
    )

    def run(database: Path) -> dict[str, object]:
        return run_trend_curve_backtest(
            database=database,
            ohlc_csv=prices,
            market="US",
            symbol="TEST",
            start_date="2026-01-01",
            end_date="2026-01-03",
            initial_cash=Decimal("1000"),
            commission_bps=Decimal("0"),
            slippage_bps=Decimal("0"),
        )

    baseline = run(baseline_database)
    variant = run(variant_database)

    assert (
        [
            (decision["date"], decision["action"])
            for decision in baseline["decisions"]
            if decision["action"] == "BUY"
        ],
        [
            (decision["date"], decision["action"])
            for decision in variant["decisions"]
            if decision["action"] == "BUY"
        ],
    ) == ([ ("2026-01-02", "BUY") ], [])


def test_non_trading_day_warm_to_hot_executes_at_first_later_open(tmp_path: Path) -> None:
    database = tmp_path / "history.sqlite3"
    prices = tmp_path / "prices.csv"
    _write_curve_database(
        database,
        [
            ("2026-05-22", "温"),
            ("2026-05-25", "热"),
            ("2026-05-26", "热"),
            ("2026-05-27", "平"),
        ],
    )
    with prices.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=("date", "open", "high", "low", "close"))
        writer.writeheader()
        writer.writerows(
            [
                {"date": "2026-05-22", "open": "9", "high": "9", "low": "9", "close": "9"},
                {"date": "2026-05-26", "open": "10", "high": "10", "low": "10", "close": "10"},
                {"date": "2026-05-27", "open": "11", "high": "11", "low": "11", "close": "11"},
                {"date": "2026-05-28", "open": "12", "high": "12", "low": "12", "close": "12"},
            ]
        )

    result = run_trend_curve_backtest(
        database=database,
        ohlc_csv=prices,
        market="US",
        symbol="TEST",
        start_date="2026-05-22",
        end_date="2026-05-28",
        initial_cash=Decimal("1000"),
        commission_bps=Decimal("0"),
        slippage_bps=Decimal("0"),
    )

    assert (
        [(trade["action"], trade["date"]) for trade in result["trades"]],
        [
            (decision["date"], decision["reason"])
            for decision in result["decisions"]
            if decision["action"] == "BUY"
        ],
        result["trades"][-1]["cash_after"],
        result["metrics"]["final_equity"],
    ) == (
        [("BUY", "2026-05-26"), ("EXIT", "2026-05-28")],
        [("2026-05-25", "warm_to_hot")],
        "1200",
        "1200",
    )


def test_open_position_is_closed_at_last_close_with_end_of_data_reason(
    tmp_path: Path,
) -> None:
    database = tmp_path / "history.sqlite3"
    prices = tmp_path / "prices.csv"
    _write_curve_database(
        database,
        [
            ("2026-01-01", "温"),
            ("2026-01-02", "热"),
            ("2026-01-03", "热"),
            ("2026-01-04", "热"),
        ],
    )
    with prices.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=("date", "open", "high", "low", "close"))
        writer.writeheader()
        writer.writerows(
            [
                {"date": "2026-01-01", "open": "99", "high": "99", "low": "99", "close": "10"},
                {"date": "2026-01-02", "open": "99", "high": "99", "low": "99", "close": "10"},
                {"date": "2026-01-03", "open": "10", "high": "10", "low": "10", "close": "10"},
                {"date": "2026-01-04", "open": "99", "high": "99", "low": "99", "close": "11"},
            ]
        )

    result = run_trend_curve_backtest(
        database=database,
        ohlc_csv=prices,
        market="US",
        symbol="TEST",
        start_date="2026-01-01",
        end_date="2026-01-04",
        initial_cash=Decimal("1000"),
        commission_bps=Decimal("10"),
        slippage_bps=Decimal("5"),
    )
    exit_trade = result["trades"][-1]
    final_equity_row = result["equity_curve"][-1]

    assert (
        exit_trade["date"],
        exit_trade["price"],
        exit_trade["fees"],
        exit_trade["cash_after"],
        exit_trade["reason"],
        final_equity_row["drawdown_pct"],
        result["metrics"]["max_drawdown_pct"],
    ) == (
        "2026-01-04",
        "10.9945",
        "1.0884555",
        "1095.8815495",
        "end_of_data",
        "0.1487866896119063137119996423",
        "0.1487866896119063137119996423",
    )


def test_flat_before_execution_cancels_pending_buy(tmp_path: Path) -> None:
    database = tmp_path / "history.sqlite3"
    prices = tmp_path / "prices.csv"
    _write_curve_database(
        database,
        [
            ("2026-01-01", "温"),
            ("2026-01-02", "热"),
            ("2026-01-03", "平"),
        ],
    )
    with prices.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=("date", "open", "high", "low", "close"))
        writer.writeheader()
        writer.writerows(
            [
                {"date": "2026-01-01", "open": "10", "high": "10", "low": "10", "close": "10"},
                {"date": "2026-01-02", "open": "10", "high": "10", "low": "10", "close": "10"},
                {"date": "2026-01-05", "open": "10", "high": "10", "low": "10", "close": "10"},
            ]
        )

    result = run_trend_curve_backtest(
        database=database,
        ohlc_csv=prices,
        market="US",
        symbol="TEST",
        start_date="2026-01-01",
        end_date="2026-01-05",
        initial_cash=Decimal("1000"),
        commission_bps=Decimal("0"),
        slippage_bps=Decimal("0"),
    )

    assert (
        [
            (decision["date"], decision["action"], decision["reason"])
            for decision in result["decisions"]
            if decision["date"] == "2026-01-02"
        ],
        result["trades"],
        result["completed_rounds"],
        result["metrics"]["final_equity"],
    ) == (
        [("2026-01-02", "BUY", "warm_to_hot")],
        [],
        [],
        "1000",
    )


def test_backtest_result_is_independent_of_process_decimal_precision(tmp_path: Path) -> None:
    database = tmp_path / "history.sqlite3"
    prices = tmp_path / "prices.csv"
    _write_curve_database(
        database,
        [
            (f"2026-01-{day:02d}", temperature)
            for day, temperature in enumerate(
                ["温", "热", "热", "平", "平", "温", "热", "热", "平", "平", "温", "热", "热", "平", "平"],
                start=1,
            )
        ],
    )
    _write_ohlc(prices)
    inputs = {
        "database": database,
        "ohlc_csv": prices,
        "market": "US",
        "symbol": "TEST",
        "start_date": "2026-01-01",
        "end_date": "2026-01-15",
        "initial_cash": Decimal("1000"),
        "commission_bps": Decimal("10"),
        "slippage_bps": Decimal("5"),
    }

    with localcontext() as context:
        context.prec = 6
        context.rounding = ROUND_DOWN
        low_precision_result = run_trend_curve_backtest(**inputs)
    with localcontext() as context:
        context.prec = 28
        context.rounding = ROUND_UP
        high_precision_result = run_trend_curve_backtest(**inputs)

    assert low_precision_result == high_precision_result


def test_slippage_at_or_above_one_hundred_percent_is_rejected(tmp_path: Path) -> None:
    database = tmp_path / "history.sqlite3"
    prices = tmp_path / "prices.csv"
    _write_curve_database(
        database,
        [("2026-01-01", "温"), ("2026-01-02", "热")],
    )
    _write_ohlc(prices)

    with pytest.raises(ValueError, match="slippage_bps.*10000"):
        run_trend_curve_backtest(
            database=database,
            ohlc_csv=prices,
            market="US",
            symbol="TEST",
            start_date="2026-01-01",
            end_date="2026-01-03",
            initial_cash=Decimal("1000"),
            commission_bps=Decimal("0"),
            slippage_bps=Decimal("10000"),
        )
