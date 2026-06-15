from __future__ import annotations

from pathlib import Path

import pytest

import open_trader.cli as cli
from open_trader.cli import build_parser
from open_trader.futu_watch import FutuWatchResult


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
