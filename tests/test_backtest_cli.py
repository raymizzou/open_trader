from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

import open_trader.cli as cli
from open_trader.backtest import BacktestResult
from open_trader.cli import build_parser


def test_run_backtest_parse_defaults_and_invalid_values() -> None:
    parser = build_parser()

    args = parser.parse_args(
        [
            "run-backtest",
            "--prices",
            "prices.csv",
            "--symbol",
            "MSFT",
            "--market",
            "US",
            "--date",
            "2026-06-16",
        ]
    )

    assert args.plan == Path("data/latest/trading_plan.csv")
    assert args.prices == Path("prices.csv")
    assert args.data_dir == Path("data")
    assert args.reports_dir == Path("reports")
    assert args.symbol == "MSFT"
    assert args.market == "US"
    assert args.date == "2026-06-16"
    assert args.initial_cash == Decimal("100000")
    assert args.commission_bps == Decimal("10")
    assert args.slippage_bps == Decimal("5")

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(
            [
                "run-backtest",
                "--prices",
                "prices.csv",
                "--symbol",
                "MSFT",
                "--market",
                "US",
                "--date",
                "2026-06-16",
                "--initial-cash",
                "0",
            ]
        )
    assert exc_info.value.code == 2


def test_run_backtest_help_includes_expected_options(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["run-backtest", "--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "--plan" in output
    assert "--prices" in output
    assert "--symbol" in output
    assert "--market" in output
    assert "--date" in output
    assert "--initial-cash" in output
    assert "--commission-bps" in output
    assert "--slippage-bps" in output


def test_run_backtest_main_wires_pipeline_and_prints_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}

    def fake_run_backtest(**kwargs: object) -> BacktestResult:
        captured.update(kwargs)
        return BacktestResult(
            run_id="2026-06-16-US-MSFT-trading-plan",
            run_date="2026-06-16",
            market="US",
            symbol="MSFT",
            trade_count=2,
            final_equity=Decimal("101184.58"),
            total_return_pct=Decimal("1.18"),
            max_drawdown_pct=Decimal("0.00"),
            metrics_path=tmp_path / "data/backtests/run/metrics.json",
            trades_path=tmp_path / "data/backtests/run/trades.csv",
            equity_curve_path=tmp_path / "data/backtests/run/equity_curve.csv",
            report_path=tmp_path / "reports/backtests/run.md",
        )

    monkeypatch.setattr(cli, "run_backtest", fake_run_backtest)

    result = cli.main(
        [
            "run-backtest",
            "--plan",
            str(tmp_path / "trading_plan.csv"),
            "--prices",
            str(tmp_path / "prices.csv"),
            "--data-dir",
            str(tmp_path / "data"),
            "--reports-dir",
            str(tmp_path / "reports"),
            "--symbol",
            "MSFT",
            "--market",
            "US",
            "--date",
            "2026-06-16",
            "--initial-cash",
            "50000",
            "--commission-bps",
            "8",
            "--slippage-bps",
            "3",
        ]
    )

    assert result == 0
    assert captured == {
        "plan_path": tmp_path / "trading_plan.csv",
        "prices_path": tmp_path / "prices.csv",
        "data_dir": tmp_path / "data",
        "reports_dir": tmp_path / "reports",
        "run_date": "2026-06-16",
        "symbol": "MSFT",
        "market": "US",
        "initial_cash": Decimal("50000"),
        "commission_bps": Decimal("8"),
        "slippage_bps": Decimal("3"),
    }

    output = capsys.readouterr().out
    assert "run_id: 2026-06-16-US-MSFT-trading-plan" in output
    assert "run_date: 2026-06-16" in output
    assert "trades: 2" in output
    assert "final_equity: 101184.58" in output
    assert "total_return_pct: 1.18" in output
    assert "max_drawdown_pct: 0.00" in output
    assert "metrics:" in output
    assert "report:" in output


def test_run_backtest_main_reports_clean_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fake_run_backtest(**kwargs: object) -> BacktestResult:
        raise ValueError("no active trading plan matches")

    monkeypatch.setattr(cli, "run_backtest", fake_run_backtest)

    with pytest.raises(SystemExit) as exc_info:
        cli.main(
            [
                "run-backtest",
                "--prices",
                "prices.csv",
                "--symbol",
                "MSFT",
                "--market",
                "US",
                "--date",
                "2026-06-16",
            ]
        )

    assert exc_info.value.code == 2
    stderr = capsys.readouterr().err
    assert "no active trading plan matches" in stderr
    assert "Traceback" not in stderr
