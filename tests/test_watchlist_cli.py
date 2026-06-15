from __future__ import annotations

from pathlib import Path

import pytest

import open_trader.cli as cli
from open_trader.cli import build_parser
from open_trader.watchlist import WatchlistResult


def test_build_watchlist_help_includes_expected_options(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["build-watchlist", "--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "--actions" in output
    assert "--data-dir" in output
    assert "--date" in output
    assert "--dry-run" in output


def test_build_watchlist_main_wires_pipeline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}

    def fake_build_watchlist(**kwargs: object) -> WatchlistResult:
        captured.update(kwargs)
        data_dir = kwargs["data_dir"]
        assert isinstance(data_dir, Path)
        return WatchlistResult(
            run_date="2026-06-16",
            watchlist_count=2,
            watchlist_path=data_dir / "runs/2026-06-16/watchlist.csv",
            latest_path=data_dir / "latest/watchlist.csv",
        )

    monkeypatch.setattr(cli, "build_watchlist", fake_build_watchlist)

    result = cli.main(
        [
            "build-watchlist",
            "--actions",
            "premarket_actions.csv",
            "--data-dir",
            str(tmp_path / "data"),
            "--date",
            "2026-06-16",
            "--dry-run",
        ]
    )

    assert result == 0
    assert captured["actions_path"] == Path("premarket_actions.csv")
    assert captured["data_dir"] == tmp_path / "data"
    assert captured["run_date"] == "2026-06-16"
    assert captured["update_latest"] is False

    output = capsys.readouterr().out
    assert "run_date: 2026-06-16" in output
    assert "watchlist: 2" in output
    assert "watchlist_csv:" in output
    assert "latest:" in output


def test_build_watchlist_main_reports_missing_actions_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing_path = tmp_path / "missing.csv"

    with pytest.raises(SystemExit) as exc_info:
        cli.main(
            [
                "build-watchlist",
                "--actions",
                str(missing_path),
                "--data-dir",
                str(tmp_path / "data"),
            ]
        )

    assert exc_info.value.code == 2
    stderr = capsys.readouterr().err
    assert "missing.csv" in stderr
    assert "Traceback" not in stderr


def test_build_watchlist_main_reports_build_value_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fake_build_watchlist(**kwargs: object) -> WatchlistResult:
        raise ValueError("missing action column(s): watch_trigger")

    monkeypatch.setattr(cli, "build_watchlist", fake_build_watchlist)

    with pytest.raises(SystemExit) as exc_info:
        cli.main(
            [
                "build-watchlist",
                "--actions",
                "premarket_actions.csv",
                "--data-dir",
                str(tmp_path / "data"),
            ]
        )

    assert exc_info.value.code == 2
    stderr = capsys.readouterr().err
    assert "missing action column(s): watch_trigger" in stderr
    assert "Traceback" not in stderr
