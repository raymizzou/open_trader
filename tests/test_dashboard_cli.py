from __future__ import annotations

from pathlib import Path

import pytest

import open_trader.cli as cli
from open_trader.dashboard import DashboardConfig
from open_trader.cli import build_parser


def test_dashboard_parser_defaults() -> None:
    parser = build_parser()

    args = parser.parse_args(["dashboard"])

    assert args.command == "dashboard"
    assert args.host == "127.0.0.1"
    assert args.port == 8765
    assert args.portfolio == Path("data/latest/portfolio.csv")
    assert args.data_dir == Path("data")
    assert args.reports_dir == Path("reports")
    assert args.poll_seconds == 5.0
    assert args.futu_host == "127.0.0.1"
    assert args.futu_port == 11111


def test_dashboard_main_delegates_to_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_serve_dashboard(
        config: DashboardConfig,
        *,
        host: str,
        port: int,
    ) -> None:
        captured["config"] = config
        captured["host"] = host
        captured["port"] = port

    monkeypatch.setattr(cli, "serve_dashboard", fake_serve_dashboard)

    result = cli.main(
        [
            "dashboard",
            "--host",
            "0.0.0.0",
            "--port",
            "9000",
            "--portfolio",
            str(tmp_path / "portfolio.csv"),
            "--data-dir",
            str(tmp_path / "data"),
            "--reports-dir",
            str(tmp_path / "reports"),
            "--poll-seconds",
            "2.5",
            "--futu-host",
            "192.0.2.10",
            "--futu-port",
            "22222",
        ]
    )

    assert result == 0
    assert captured["host"] == "0.0.0.0"
    assert captured["port"] == 9000
    config = captured["config"]
    assert isinstance(config, DashboardConfig)
    assert config.portfolio_path == tmp_path / "portfolio.csv"
    assert config.data_dir == tmp_path / "data"
    assert config.reports_dir == tmp_path / "reports"
    assert config.poll_seconds == 2.5
    assert config.futu_host == "192.0.2.10"
    assert config.futu_port == 22222


def test_dashboard_help_includes_expected_options(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["dashboard", "--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "--host" in output
    assert "--port" in output
    assert "--portfolio" in output
    assert "--data-dir" in output
    assert "--reports-dir" in output
    assert "--poll-seconds" in output
    assert "--futu-host" in output
    assert "--futu-port" in output
