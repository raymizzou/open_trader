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
    assert args.portfolio is None
    assert args.data_dir == Path("data")
    assert args.reports_dir == Path("reports")
    assert args.config == Path("config/daily_premarket.env")
    assert args.poll_seconds == 5.0
    assert args.futu_host == "127.0.0.1"
    assert args.futu_port == 11111
    assert args.public_url == ""


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
        public_url: str,
    ) -> None:
        captured["config"] = config
        captured["host"] = host
        captured["port"] = port
        captured["public_url"] = public_url

    monkeypatch.setattr(cli, "serve_dashboard", fake_serve_dashboard)
    (tmp_path / "dashboard.env").write_text(
        "OPEN_TRADER_EASTMONEY_PDF_PASSWORD=local-secret\n"
        "OPEN_TRADER_TREND_EXECUTOR_HOST=ray-mac\n"
        "TREND_ANIMALS_WARM_TO_HOT_A_SHARE_TM_ID=622466\n"
        "TREND_ANIMALS_WARM_TO_HOT_ETF_TM_ID=697199\n"
        "TREND_ANIMALS_WARM_TO_HOT_US_TM_IDS=622460,705013\n"
        "TREND_ANIMALS_WARM_TO_HOT_HK_TM_IDS=622494,707617\n",
        encoding="utf-8",
    )

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
            "--config",
            str(tmp_path / "dashboard.env"),
            "--poll-seconds",
            "2.5",
            "--futu-host",
            "192.0.2.10",
            "--futu-port",
            "22222",
            "--public-url",
            "http://127.0.0.1:8766/",
        ]
    )

    assert result == 0
    assert captured["host"] == "0.0.0.0"
    assert captured["port"] == 9000
    assert captured["public_url"] == "http://127.0.0.1:8766/"
    config = captured["config"]
    assert isinstance(config, DashboardConfig)
    assert config.portfolio_path == tmp_path / "portfolio.csv"
    assert config.data_dir == tmp_path / "data"
    assert config.reports_dir == tmp_path / "reports"
    assert config.poll_seconds == 2.5
    assert config.futu_host == "192.0.2.10"
    assert config.futu_port == 22222
    assert config.trend_executor_host == "ray-mac"
    assert config.trend_cn_candidate_pool_ids == (622466, 697199)
    assert config.trend_us_candidate_pool_ids == (622460, 705013)
    assert config.trend_hk_candidate_pool_ids == (622494, 707617)
    assert config.trend_candidate_pool_ids("CN") == (622466, 697199)
    assert config.trend_candidate_pool_ids("US") == (622460, 705013)
    assert config.trend_candidate_pool_ids("HK") == (622494, 707617)


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
    assert "--config" in output
    assert "--poll-seconds" in output
    assert "--futu-host" in output
    assert "--futu-port" in output
    assert "--public-url" in output
