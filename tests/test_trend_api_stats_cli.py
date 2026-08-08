from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import open_trader.cli as cli_module
from open_trader.cli import build_parser


def test_trend_review_sync_stats_parser_exposes_real_api_workflow() -> None:
    args = build_parser().parse_args([
        "trend-review", "sync-stats",
        "--market", "CN",
        "--as-of-date", "2026-08-08",
        "--force", "--actor", "ray", "--reason", "accepted rollout repair",
        "--config", "config/daily.env",
        "--tiger-config-dir", "/tmp/tiger",
        "--tiger-account", "U1",
    ])

    assert args.command == "trend-review"
    assert args.trend_review_command == "sync-stats"
    assert args.market == "CN"
    assert args.as_of_date == "2026-08-08"
    assert args.force is True
    assert args.actor == "ray"
    assert args.reason == "accepted rollout repair"
    assert args.config == Path("config/daily.env")
    assert args.tiger_config_dir == Path("/tmp/tiger")
    assert args.tiger_account == "U1"


def test_trend_review_sync_stats_main_wires_only_selected_cn_simulation(
    monkeypatch, tmp_path: Path, capsys,
) -> None:
    config = SimpleNamespace(
        data_dir=tmp_path / "data", reports_dir=tmp_path / "reports",
        repo=tmp_path,
        futu_host="127.0.0.1", futu_port=11111,
        trend_review_cn_simulate_acc_id=101,
        trend_review_hk_simulate_acc_id=102,
        trend_review_us_simulate_acc_id=103,
    )
    created: list[tuple[str, int]] = []
    cycle_calls: list[dict[str, object]] = []

    class FakeFutu:
        def __init__(self, **kwargs: object) -> None:
            created.append((str(kwargs["trd_market"]), int(kwargs["simulate_acc_id"])))

        def close(self) -> None:
            pass

    class FakeTiger:
        def __init__(self, **_: object) -> None:
            raise AssertionError("CN cycle must not initialize Tiger")

        def close(self) -> None:
            pass

    monkeypatch.setattr(cli_module, "load_env_config", lambda *_args, **_kwargs: config)
    monkeypatch.setattr(cli_module, "FutuSimulateFillClient", FakeFutu)
    monkeypatch.setattr(cli_module, "TigerActualFillClient", FakeTiger)
    monkeypatch.setattr(
        cli_module,
        "run_trend_statistics_cycle",
        lambda **kwargs: cycle_calls.append(kwargs) or {
            "status": "completed",
            "statistics_cutoff_at": "2026-08-08T15:00:00+08:00",
        },
    )

    result = cli_module.main([
        "trend-review", "sync-stats",
        "--market", "CN", "--as-of-date", "2026-08-08",
    ])

    assert result == 0
    assert created == [("CN", 101)]
    assert cycle_calls[0]["data_dir"] == tmp_path / "data"
    assert cycle_calls[0]["reports_dir"] == tmp_path / "reports"
    assert cycle_calls[0]["market"] == "CN"
    assert cycle_calls[0]["as_of_date"] == "2026-08-08"
    assert cycle_calls[0]["tiger_client"] is None
    output = capsys.readouterr().out
    assert "status: completed" in output
    assert "statistics_cutoff_at: 2026-08-08T15:00:00+08:00" in output


def test_trend_review_sync_stats_us_initializes_one_futu_and_one_tiger(
    monkeypatch, tmp_path: Path,
) -> None:
    config = SimpleNamespace(
        data_dir=tmp_path / "data", reports_dir=tmp_path / "reports",
        repo=tmp_path,
        futu_host="127.0.0.1", futu_port=11111,
        trend_review_cn_simulate_acc_id=101,
        trend_review_hk_simulate_acc_id=102,
        trend_review_us_simulate_acc_id=103,
    )
    created: list[str] = []
    cycle_calls: list[dict[str, object]] = []

    class FakeFutu:
        def __init__(self, **kwargs: object) -> None:
            self.market = str(kwargs["trd_market"])
            created.append(self.market)

        def close(self) -> None:
            pass

    class FakeTiger:
        def __init__(self, **_: object) -> None:
            created.append("tiger")

        def close(self) -> None:
            pass

    monkeypatch.setattr(cli_module, "load_env_config", lambda *_args, **_kwargs: config)
    monkeypatch.setattr(cli_module, "load_tiger_account_config", lambda **_kwargs: object())
    monkeypatch.setattr(cli_module, "FutuSimulateFillClient", FakeFutu)
    monkeypatch.setattr(cli_module, "TigerActualFillClient", FakeTiger)
    monkeypatch.setattr(
        cli_module,
        "run_trend_statistics_cycle",
        lambda **kwargs: cycle_calls.append(kwargs) or {
            "status": "completed",
            "statistics_cutoff_at": "2026-08-08T16:00:00-04:00",
        },
    )

    result = cli_module.main([
        "trend-review", "sync-stats",
        "--market", "US", "--as-of-date", "2026-08-08",
    ])

    assert result == 0
    assert created == ["US", "tiger"]
    assert cycle_calls[0]["tiger_client"] is not None
