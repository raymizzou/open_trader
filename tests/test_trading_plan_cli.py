from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

import open_trader.cli as cli
from open_trader.cli import build_parser
from open_trader.futu_watch import QuoteSnapshot
from open_trader.trading_plan import (
    PlanQuoteStatus,
    TradingPlanBuildResult,
    TradingPlanRow,
)


def test_build_trading_plan_help_includes_expected_options(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["build-trading-plan", "--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "--advice" in output
    assert "--data-dir" in output
    assert "--date" in output
    assert "--dry-run" in output


def test_build_trading_plan_main_wires_builder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}

    def fake_build_trading_plan(**kwargs: object) -> TradingPlanBuildResult:
        captured.update(kwargs)
        data_dir = kwargs["data_dir"]
        assert isinstance(data_dir, Path)
        return TradingPlanBuildResult(
            run_date="2026-06-16",
            plan_count=1,
            plan_path=data_dir / "runs/2026-06-16/trading_plan.csv",
            latest_path=data_dir / "latest/trading_plan.csv",
        )

    monkeypatch.setattr(cli, "build_trading_plan", fake_build_trading_plan)

    result = cli.main(
        [
            "build-trading-plan",
            "--advice",
            "trading_advice.csv",
            "--data-dir",
            str(tmp_path / "data"),
            "--date",
            "2026-06-16",
            "--dry-run",
        ]
    )

    assert result == 0
    assert captured["advice_path"] == Path("trading_advice.csv")
    assert captured["data_dir"] == tmp_path / "data"
    assert captured["run_date"] == "2026-06-16"
    assert captured["update_latest"] is False
    output = capsys.readouterr().out
    assert "run_date: 2026-06-16" in output
    assert "plans: 1" in output
    assert "plan_csv:" in output


def test_check_futu_plan_main_reports_plan_statuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}
    plan = TradingPlanRow(
        run_date="2026-06-16",
        symbol="MSFT",
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
        max_weight="12%",
        catalyst="10月底财报",
        time_horizon="3-6个月",
        plan_text="plan",
        status="active",
        error="",
    )

    class FakeFutuQuoteClient:
        def __init__(self, *, host: str, port: int) -> None:
            captured["host"] = host
            captured["port"] = port

        def get_snapshots(
            self, futu_symbols: list[str]
        ) -> dict[str, QuoteSnapshot]:
            captured["symbols"] = futu_symbols
            return {
                "US.MSFT": QuoteSnapshot(
                    futu_symbol="US.MSFT",
                    last_price=Decimal("399"),
                )
            }

        def close(self) -> None:
            captured["closed"] = True

    monkeypatch.setattr(cli, "FutuQuoteClient", FakeFutuQuoteClient)
    monkeypatch.setattr(cli, "load_trading_plan_rows", lambda path: [plan])
    monkeypatch.setattr(
        cli,
        "evaluate_plan_quote",
        lambda plan, price: PlanQuoteStatus(
            symbol=plan.symbol,
            futu_symbol="US.MSFT",
            last_price=price,
            status="entry_zone",
            message="Current price is inside the planned entry zone.",
        ),
    )

    result = cli.main(
        [
            "check-futu-plan",
            "--plan",
            str(tmp_path / "trading_plan.csv"),
            "--host",
            "127.0.0.1",
            "--port",
            "11111",
        ]
    )

    assert result == 0
    assert captured["symbols"] == ["US.MSFT"]
    assert captured["closed"] is True
    output = capsys.readouterr().out
    assert "loaded 1 active trading plan(s)" in output
    assert "plan US.MSFT last_price=399 status=entry_zone" in output
    assert "Current price is inside the planned entry zone." in output
