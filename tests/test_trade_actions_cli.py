from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

import open_trader.cli as cli
from open_trader.cli import build_parser
from open_trader.futu_quote import FutuQuoteError
from open_trader.futu_watch import QuoteSnapshot
from open_trader.trade_actions import TradeActionsResult


def test_generate_trade_actions_parse_defaults_and_invalid_port() -> None:
    parser = build_parser()

    args = parser.parse_args(["generate-trade-actions"])

    assert args.plan == Path("data/latest/trading_plan.csv")
    assert args.portfolio == Path("data/latest/portfolio.csv")
    assert args.data_dir == Path("data")
    assert args.reports_dir == Path("reports")
    assert args.host == "127.0.0.1"
    assert args.port == 11111
    assert args.date is None
    assert args.dry_run is False

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["generate-trade-actions", "--port", "0"])

    assert exc_info.value.code == 2


def test_generate_trade_actions_help_includes_expected_options(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["generate-trade-actions", "--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "--plan" in output
    assert "--portfolio" in output
    assert "--data-dir" in output
    assert "--reports-dir" in output
    assert "--date" in output
    assert "Run date, YYYY-MM-DD." in output
    assert "Required only when active plan" in output
    assert "rows do not contain run_date." in output
    assert "--host" in output
    assert "--port" in output
    assert "--dry-run" in output


def test_generate_trade_actions_main_fetches_quotes_and_prints_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}
    selected_plan = SimpleNamespace(
        futu_symbol="US.MSFT",
        status="active",
        run_date="2026-06-16",
    )
    blank_date_plan = SimpleNamespace(
        futu_symbol="US.NVDA",
        status="active",
        run_date="",
    )
    other_date_plan = SimpleNamespace(
        futu_symbol="US.AAPL",
        status="active",
        run_date="2026-06-15",
    )
    inactive_plan = SimpleNamespace(
        futu_symbol="US.TSLA",
        status="inactive",
        run_date="2026-06-16",
    )
    duplicate_symbol_plan = SimpleNamespace(
        futu_symbol="US.MSFT",
        status="active",
        run_date="2026-06-16",
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
                    last_price=Decimal("390"),
                ),
                "US.NVDA": QuoteSnapshot(
                    futu_symbol="US.NVDA",
                    last_price=Decimal("120"),
                )
            }

        def close(self) -> None:
            captured["closed"] = True

    def fake_generate_trade_actions(**kwargs: object) -> TradeActionsResult:
        captured["generator_kwargs"] = kwargs
        return TradeActionsResult(
            run_date="2026-06-16",
            action_count=1,
            ready_count=1,
            review_count=0,
            watch_count=0,
            actions_path=tmp_path / "data/runs/2026-06-16/trade_actions.csv",
            latest_path=tmp_path / "data/latest/trade_actions.csv",
            report_path=tmp_path / "reports/trade_actions/2026-06-16.md",
        )

    monkeypatch.setattr(
        cli,
        "load_trading_plan_rows",
        lambda path: [
            selected_plan,
            blank_date_plan,
            other_date_plan,
            inactive_plan,
            duplicate_symbol_plan,
        ],
    )
    monkeypatch.setattr(cli, "FutuQuoteClient", FakeFutuQuoteClient)
    monkeypatch.setattr(cli, "generate_trade_actions", fake_generate_trade_actions)

    result = cli.main(
        [
            "generate-trade-actions",
            "--plan",
            str(tmp_path / "trading_plan.csv"),
            "--portfolio",
            str(tmp_path / "portfolio.csv"),
            "--data-dir",
            str(tmp_path / "data"),
            "--reports-dir",
            str(tmp_path / "reports"),
            "--date",
            "2026-06-16",
            "--host",
            "127.0.0.1",
            "--port",
            "11111",
            "--dry-run",
        ]
    )

    assert result == 0
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 11111
    assert captured["symbols"] == ["US.MSFT", "US.NVDA"]
    assert captured["closed"] is True
    assert captured["generator_kwargs"] == {
        "plan_path": tmp_path / "trading_plan.csv",
        "portfolio_path": tmp_path / "portfolio.csv",
        "data_dir": tmp_path / "data",
        "reports_dir": tmp_path / "reports",
        "snapshots": {
            "US.MSFT": QuoteSnapshot(
                futu_symbol="US.MSFT",
                last_price=Decimal("390"),
            ),
            "US.NVDA": QuoteSnapshot(
                futu_symbol="US.NVDA",
                last_price=Decimal("120"),
            )
        },
        "run_date": "2026-06-16",
        "update_latest": False,
    }
    output = capsys.readouterr().out
    assert "connected to Futu OpenD at 127.0.0.1:11111" in output
    assert "loaded 3 active trading plan(s)" in output
    assert "run_date: 2026-06-16" in output
    assert "actions: 1" in output
    assert "ready: 1" in output
    assert "review: 0" in output
    assert "watch: 0" in output
    assert "trade_actions_csv:" in output
    assert "report:" in output


def test_generate_trade_actions_main_reports_clean_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fake_load_trading_plan_rows(path: Path) -> list[object]:
        raise ValueError("missing trading plan column(s): symbol")

    monkeypatch.setattr(cli, "load_trading_plan_rows", fake_load_trading_plan_rows)

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["generate-trade-actions"])

    assert exc_info.value.code == 2
    stderr = capsys.readouterr().err
    assert "missing trading plan column(s): symbol" in stderr
    assert "Traceback" not in stderr


def test_generate_trade_actions_main_without_date_uses_latest_active_symbols(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}
    latest_plan = SimpleNamespace(
        futu_symbol="US.MSFT",
        status="active",
        run_date="2026-06-16",
    )
    blank_date_plan = SimpleNamespace(
        futu_symbol="US.NVDA",
        status="active",
        run_date="",
    )
    older_plan = SimpleNamespace(
        futu_symbol="US.AAPL",
        status="active",
        run_date="2026-06-15",
    )
    inactive_plan = SimpleNamespace(
        futu_symbol="US.TSLA",
        status="inactive",
        run_date="2026-06-16",
    )
    duplicate_symbol_plan = SimpleNamespace(
        futu_symbol="US.MSFT",
        status="active",
        run_date="2026-06-16",
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
                    last_price=Decimal("390"),
                ),
                "US.NVDA": QuoteSnapshot(
                    futu_symbol="US.NVDA",
                    last_price=Decimal("120"),
                ),
            }

        def close(self) -> None:
            captured["closed"] = True

    def fake_generate_trade_actions(**kwargs: object) -> TradeActionsResult:
        captured["generator_kwargs"] = kwargs
        return TradeActionsResult(
            run_date="2026-06-16",
            action_count=2,
            ready_count=1,
            review_count=1,
            watch_count=0,
            actions_path=tmp_path / "data/runs/2026-06-16/trade_actions.csv",
            latest_path=tmp_path / "data/latest/trade_actions.csv",
            report_path=tmp_path / "reports/trade_actions/2026-06-16.md",
        )

    monkeypatch.setattr(
        cli,
        "load_trading_plan_rows",
        lambda path: [
            latest_plan,
            blank_date_plan,
            older_plan,
            inactive_plan,
            duplicate_symbol_plan,
        ],
    )
    monkeypatch.setattr(cli, "FutuQuoteClient", FakeFutuQuoteClient)
    monkeypatch.setattr(cli, "generate_trade_actions", fake_generate_trade_actions)

    result = cli.main(
        [
            "generate-trade-actions",
            "--plan",
            str(tmp_path / "trading_plan.csv"),
            "--portfolio",
            str(tmp_path / "portfolio.csv"),
            "--data-dir",
            str(tmp_path / "data"),
            "--reports-dir",
            str(tmp_path / "reports"),
        ]
    )

    assert result == 0
    assert captured["symbols"] == ["US.MSFT", "US.NVDA"]
    assert captured["closed"] is True
    assert captured["generator_kwargs"]["run_date"] is None
    output = capsys.readouterr().out
    assert "loaded 3 active trading plan(s)" in output
    assert "run_date: 2026-06-16" in output


def test_generate_trade_actions_main_closes_quote_client_on_futu_quote_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}
    active_plan = SimpleNamespace(
        futu_symbol="US.MSFT",
        status="active",
        run_date="2026-06-16",
    )

    class FakeFutuQuoteClient:
        def __init__(self, *, host: str, port: int) -> None:
            captured["host"] = host
            captured["port"] = port

        def get_snapshots(
            self, futu_symbols: list[str]
        ) -> dict[str, QuoteSnapshot]:
            captured["symbols"] = futu_symbols
            raise FutuQuoteError("snapshot fetch failed")

        def close(self) -> None:
            captured["closed"] = True

    monkeypatch.setattr(cli, "load_trading_plan_rows", lambda path: [active_plan])
    monkeypatch.setattr(cli, "FutuQuoteClient", FakeFutuQuoteClient)

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["generate-trade-actions", "--date", "2026-06-16"])

    assert exc_info.value.code == 2
    assert captured["symbols"] == ["US.MSFT"]
    assert captured["closed"] is True
    stderr = capsys.readouterr().err
    assert "snapshot fetch failed" in stderr
    assert "Traceback" not in stderr


def test_generate_trade_actions_main_does_not_catch_generic_runtime_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    active_plan = SimpleNamespace(
        futu_symbol="US.MSFT",
        status="active",
        run_date="2026-06-16",
    )

    class FakeFutuQuoteClient:
        def __init__(self, *, host: str, port: int) -> None:
            pass

        def get_snapshots(
            self, futu_symbols: list[str]
        ) -> dict[str, QuoteSnapshot]:
            captured["symbols"] = futu_symbols
            return {
                "US.MSFT": QuoteSnapshot(
                    futu_symbol="US.MSFT",
                    last_price=Decimal("390"),
                )
            }

        def close(self) -> None:
            captured["closed"] = True

    def fake_generate_trade_actions(**kwargs: object) -> TradeActionsResult:
        raise RuntimeError("unexpected generator failure")

    monkeypatch.setattr(cli, "load_trading_plan_rows", lambda path: [active_plan])
    monkeypatch.setattr(cli, "FutuQuoteClient", FakeFutuQuoteClient)
    monkeypatch.setattr(cli, "generate_trade_actions", fake_generate_trade_actions)

    with pytest.raises(RuntimeError, match="unexpected generator failure"):
        cli.main(
            [
                "generate-trade-actions",
                "--plan",
                str(tmp_path / "trading_plan.csv"),
                "--portfolio",
                str(tmp_path / "portfolio.csv"),
            ]
        )

    assert captured["symbols"] == ["US.MSFT"]
    assert captured["closed"] is True
