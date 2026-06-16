from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

import open_trader.cli as cli
from open_trader.cli import build_parser
from open_trader.futu_universe import (
    FutuQuoteUniverse,
    FutuUniverseItem,
    SkippedFutuUniverseRow,
)
from open_trader.futu_watch import FutuWatchResult
from open_trader.futu_watch import QuoteSnapshot


def test_watch_futu_help_includes_expected_options(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["watch-futu", "--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "--watchlist" in output
    assert "--data-dir" in output
    assert "--date" in output
    assert "--host" in output
    assert "--port" in output
    assert "--poll-seconds" in output
    assert "--once" in output


def test_watch_futu_main_wires_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}

    class FakeFutuQuoteClient:
        def __init__(self, *, host: str, port: int) -> None:
            captured["host"] = host
            captured["port"] = port

    def fake_run_futu_watch(**kwargs: object) -> FutuWatchResult:
        captured.update(kwargs)
        assert isinstance(kwargs["quote_client"], FakeFutuQuoteClient)
        data_dir = kwargs["data_dir"]
        assert isinstance(data_dir, Path)
        return FutuWatchResult(
            run_date="2026-06-15",
            trigger_count=2,
            skipped_count=1,
            alert_count=0,
            alerts_path=data_dir / "runs/2026-06-15/alerts.csv",
        )

    monkeypatch.setattr(cli, "FutuQuoteClient", FakeFutuQuoteClient)
    monkeypatch.setattr(cli, "run_futu_watch", fake_run_futu_watch)

    result = cli.main(
        [
            "watch-futu",
            "--watchlist",
            "watchlist.csv",
            "--data-dir",
            str(tmp_path / "data"),
            "--date",
            "2026-06-15",
            "--host",
            "127.0.0.1",
            "--port",
            "11111",
            "--poll-seconds",
            "1.5",
            "--once",
        ]
    )

    assert result == 0
    assert captured["watchlist_path"] == Path("watchlist.csv")
    assert captured["data_dir"] == tmp_path / "data"
    assert captured["run_date"] == "2026-06-15"
    assert captured["poll_seconds"] == 1.5
    assert captured["once"] is True
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 11111
    output = capsys.readouterr().out
    assert "run_date: 2026-06-15" in output
    assert "triggers: 2" in output
    assert "alerts: 0" in output
    assert "alerts_csv:" in output


def test_watch_futu_main_reports_runner_error_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeFutuQuoteClient:
        def __init__(self, *, host: str, port: int) -> None:
            pass

    def fake_run_futu_watch(**kwargs: object) -> FutuWatchResult:
        raise RuntimeError("OpenD connection failed")

    monkeypatch.setattr(cli, "FutuQuoteClient", FakeFutuQuoteClient)
    monkeypatch.setattr(cli, "run_futu_watch", fake_run_futu_watch)

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["watch-futu", "--once"])

    assert exc_info.value.code == 2
    stderr = capsys.readouterr().err
    assert "OpenD connection failed" in stderr
    assert "Traceback" not in stderr


def test_check_futu_quotes_help_includes_expected_options(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["check-futu-quotes", "--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "--portfolio" in output
    assert "--host" in output
    assert "--port" in output


def test_check_futu_quotes_main_excludes_universe_skips_and_reports_quotes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}

    class FakeFutuQuoteClient:
        def __init__(self, *, host: str, port: int) -> None:
            captured["host"] = host
            captured["port"] = port
            self.closed = False

        def get_snapshots(
            self, futu_symbols: list[str]
        ) -> dict[str, QuoteSnapshot]:
            captured["symbols"] = futu_symbols
            return {
                "US.MSFT": QuoteSnapshot(
                    futu_symbol="US.MSFT",
                    last_price=Decimal("400"),
                )
            }

        def close(self) -> None:
            captured["closed"] = True

    def fake_load_futu_quote_universe(path: Path) -> FutuQuoteUniverse:
        captured["portfolio_path"] = path
        return FutuQuoteUniverse(
            items=[
                FutuUniverseItem(
                    row_number=2,
                    market="US",
                    asset_class="stock",
                    symbol="MSFT",
                    futu_symbol="US.MSFT",
                    name="Microsoft",
                ),
                FutuUniverseItem(
                    row_number=3,
                    market="US",
                    asset_class="stock",
                    symbol="MISSING",
                    futu_symbol="US.MISSING",
                    name="Missing",
                ),
            ],
            skipped=[
                SkippedFutuUniverseRow(
                    row_number=4,
                    market="HK",
                    asset_class="money_market_fund",
                    symbol="HK0000951506.HKD",
                    reason="excluded_asset_class",
                )
            ],
        )

    monkeypatch.setattr(cli, "FutuQuoteClient", FakeFutuQuoteClient)
    monkeypatch.setattr(
        cli, "load_futu_quote_universe", fake_load_futu_quote_universe
    )

    result = cli.main(
        [
            "check-futu-quotes",
            "--portfolio",
            str(tmp_path / "portfolio.csv"),
            "--host",
            "127.0.0.1",
            "--port",
            "11111",
        ]
    )

    assert result == 0
    assert captured["portfolio_path"] == tmp_path / "portfolio.csv"
    assert captured["symbols"] == ["US.MISSING", "US.MSFT"]
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 11111
    assert captured["closed"] is True
    output = capsys.readouterr().out
    assert "loaded 2 quoteable position(s)" in output
    assert "quote US.MSFT last_price=400" in output
    assert "warning: missing quote for US.MISSING" in output
    assert "quotes: 1" in output
    assert "missing: 1" in output
    assert (
        "skipped HK.HK0000951506.HKD asset_class=money_market_fund "
        "reason=excluded_asset_class"
    ) in output
    assert "skipped: 1" in output
