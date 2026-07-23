from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import open_trader.cli as cli_module
from open_trader.cli import build_parser


def test_trend_review_sync_stats_parser_exposes_real_api_workflow() -> None:
    args = build_parser().parse_args([
        "trend-review", "sync-stats",
        "--start", "2026-01-01",
        "--end", "2026-01-31",
        "--config", "config/daily.env",
        "--tiger-config-dir", "/tmp/tiger",
        "--tiger-account", "U1",
    ])

    assert args.command == "trend-review"
    assert args.trend_review_command == "sync-stats"
    assert args.start == "2026-01-01"
    assert args.end == "2026-01-31"
    assert args.config == Path("config/daily.env")
    assert args.tiger_config_dir == Path("/tmp/tiger")
    assert args.tiger_account == "U1"


def test_trend_review_sync_stats_main_wires_all_configured_api_accounts(
    monkeypatch, tmp_path: Path, capsys,
) -> None:
    config = SimpleNamespace(
        data_dir=tmp_path / "data", reports_dir=tmp_path / "reports",
        futu_host="127.0.0.1", futu_port=11111,
        trend_review_cn_simulate_acc_id=101,
        trend_review_hk_simulate_acc_id=102,
        trend_review_us_simulate_acc_id=103,
    )
    created: list[tuple[str, int]] = []
    sync_calls: list[dict[str, object]] = []

    class FakeFutu:
        def __init__(self, **kwargs: object) -> None:
            created.append((str(kwargs["trd_market"]), int(kwargs["simulate_acc_id"])))

        def close(self) -> None:
            pass

    class FakeTiger:
        def __init__(self, **_: object) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr(cli_module, "load_env_config", lambda *_args, **_kwargs: config)
    monkeypatch.setattr(cli_module, "load_tiger_account_config", lambda **_kwargs: object())
    monkeypatch.setattr(cli_module, "FutuSimulateFillClient", FakeFutu)
    monkeypatch.setattr(cli_module, "TigerActualFillClient", FakeTiger)
    monkeypatch.setattr(
        cli_module,
        "sync_trend_api_stats",
        lambda **kwargs: sync_calls.append(kwargs) or {
            "fills": [1, 2], "rounds": [1], "stats": [1, 2],
        },
    )

    result = cli_module.main([
        "trend-review", "sync-stats",
        "--start", "2026-01-01", "--end", "2026-01-31",
    ])

    assert result == 0
    assert created == [("CN", 101), ("HK", 102), ("US", 103)]
    assert sync_calls[0]["data_dir"] == tmp_path / "data"
    assert sync_calls[0]["reports_dir"] == tmp_path / "reports"
    assert sync_calls[0]["start"] == "2026-01-01"
    assert sync_calls[0]["end"] == "2026-01-31"
    assert set(sync_calls[0]["futu_clients"]) == {"CN", "HK", "US"}
    assert "fills: 2" in capsys.readouterr().out


def test_trend_review_sync_stats_closes_clients_when_later_client_init_fails(
    monkeypatch, tmp_path: Path,
) -> None:
    config = SimpleNamespace(
        data_dir=tmp_path / "data", reports_dir=tmp_path / "reports",
        futu_host="127.0.0.1", futu_port=11111,
        trend_review_cn_simulate_acc_id=101,
        trend_review_hk_simulate_acc_id=102,
        trend_review_us_simulate_acc_id=103,
    )
    closed: list[str] = []

    class FakeFutu:
        def __init__(self, **kwargs: object) -> None:
            self.market = str(kwargs["trd_market"])
            if self.market == "HK":
                raise RuntimeError("HK init failed")

        def close(self) -> None:
            closed.append(self.market)

    monkeypatch.setattr(cli_module, "load_env_config", lambda *_args, **_kwargs: config)
    monkeypatch.setattr(cli_module, "FutuSimulateFillClient", FakeFutu)

    result = cli_module.main([
        "trend-review", "sync-stats",
        "--start", "2026-01-01", "--end", "2026-01-31",
    ])

    assert result == 1
    assert closed == ["CN"]
