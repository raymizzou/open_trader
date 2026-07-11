from __future__ import annotations

import json
from pathlib import Path

import pytest

from open_trader.cli import main


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _valid_trade_sample(*, experiment_id: str = "trend_us") -> dict[str, str]:
    return {
        "experiment_id": experiment_id,
        "market": "US",
        "symbol": "AAPL",
        "entry_order_id": "BUY-1",
        "exit_order_id": "SELL-1",
        "entry_submitted_at": "2026-07-11 09:00",
        "exit_submitted_at": "2026-07-11 10:00",
        "entry_price": "100",
        "exit_price": "110",
        "quantity": "1",
        "entry_notional": "100",
        "exit_notional": "110",
        "gross_pnl": "10",
        "net_pnl_pct": "10%",
        "result": "win",
    }


def _write_kelly_lab_artifacts(data_dir: Path) -> None:
    latest_dir = data_dir / "latest"
    _write_json(
        latest_dir / "kelly_strategy_templates.json",
        {
            "schema_version": "open_trader.kelly_strategy_templates.v1",
            "templates": [
                {
                    "strategy_id": "trend_pullback_20d",
                    "strategy_name": "Trend Pullback",
                    "strategy_version": "v1",
                    "entry_rule_description": "Entry",
                    "exit_rule_description": "Exit",
                    "max_holding_days": 20,
                    "order_type": "limit",
                    "market_session": "regular",
                }
            ],
        },
    )
    _write_json(
        latest_dir / "kelly_experiments.json",
        {
            "schema_version": "open_trader.kelly_experiments.v1",
            "experiments": [
                {
                    "experiment_id": "trend_us",
                    "experiment_name": "Trend US",
                    "strategy_id": "trend_pullback_20d",
                    "strategy_version": "v1",
                    "market": "US",
                    "start_date": "2026-07-07",
                    "paper_account": "futu_simulate_us",
                    "experiment_budget": "30000",
                    "budget_currency": "USD",
                    "capital_utilization_pct": "50",
                    "allocation_mode": "equal_weight",
                    "max_open_position_per_symbol": 1,
                    "status": "running",
                    "locked": True,
                    "participants": [
                        {
                            "market": "US",
                            "symbol": "AAPL",
                            "name": "Apple",
                            "source": "watchlist",
                            "locked": True,
                            "per_symbol_budget": "30000",
                            "budget_currency": "USD",
                        }
                    ],
                    "stats": {
                        "completed_samples": 0,
                        "open_samples": 0,
                        "observed_win_rate": "",
                        "sample_stage": "insufficient",
                    },
                }
            ],
        },
    )
    _write_json(
        latest_dir / "kelly_trade_samples.json",
        {
            "schema_version": "open_trader.kelly_trade_samples.v1",
            "generated_at": "2026-07-11 12:00",
            "source_orders_synced_at": "2026-07-11 11:59",
            "sample_count": 0,
            "open_position_count": 0,
            "skipped_order_count": 0,
            "stats_by_experiment": {},
            "samples": [],
            "open_positions": [],
            "diagnostics": {"skipped_orders": []},
        },
    )


def test_build_strategy_stats_cli_writes_latest_artifact(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_kelly_lab_artifacts(data_dir)

    result = main(
        [
            "kelly",
            "build-strategy-stats",
            "--data-dir",
            str(data_dir),
            "--generated-at",
            "2026-07-11 12:01",
        ]
    )

    payload = json.loads(
        (data_dir / "latest" / "kelly_strategy_stats.json").read_text()
    )
    assert result == 0
    assert payload["generated_at"] == "2026-07-11 12:01"


def test_build_strategy_stats_cli_rejects_incomplete_trade_sample_evidence(
    tmp_path: Path,
    capsys,
) -> None:
    data_dir = tmp_path / "data"
    _write_kelly_lab_artifacts(data_dir)
    _write_json(
        data_dir / "latest" / "kelly_trade_samples.json",
        {
            "schema_version": "open_trader.kelly_trade_samples.v1",
            "generated_at": "2026-07-11 12:00",
            "source_orders_synced_at": "2026-07-11 11:59",
            "stats_by_experiment": {},
            "samples": [],
            "open_positions": [],
            "diagnostics": {"skipped_orders": []},
        },
    )

    with pytest.raises(SystemExit, match="2"):
        main(
            [
                "kelly",
                "build-strategy-stats",
                "--data-dir",
                str(data_dir),
            ]
        )

    assert "sample_count" in capsys.readouterr().err
    assert not (data_dir / "latest" / "kelly_strategy_stats.json").exists()


def test_build_strategy_stats_cli_rejects_malformed_trade_sample_record(
    tmp_path: Path,
    capsys,
) -> None:
    data_dir = tmp_path / "data"
    _write_kelly_lab_artifacts(data_dir)
    _write_json(
        data_dir / "latest" / "kelly_trade_samples.json",
        {
            "schema_version": "open_trader.kelly_trade_samples.v1",
            "generated_at": "2026-07-11 12:00",
            "source_orders_synced_at": "2026-07-11 11:59",
            "sample_count": 1,
            "open_position_count": 0,
            "skipped_order_count": 0,
            "stats_by_experiment": {},
            "samples": [{}],
            "open_positions": [],
            "diagnostics": {"skipped_orders": []},
        },
    )

    with pytest.raises(SystemExit, match="2"):
        main(
            [
                "kelly",
                "build-strategy-stats",
                "--data-dir",
                str(data_dir),
            ]
        )

    assert "samples[0]" in capsys.readouterr().err
    assert not (data_dir / "latest" / "kelly_strategy_stats.json").exists()


def test_build_strategy_stats_cli_rejects_unknown_trade_sample_experiment(
    tmp_path: Path,
    capsys,
) -> None:
    data_dir = tmp_path / "data"
    _write_kelly_lab_artifacts(data_dir)
    _write_json(
        data_dir / "latest" / "kelly_trade_samples.json",
        {
            "schema_version": "open_trader.kelly_trade_samples.v1",
            "generated_at": "2026-07-11 12:00",
            "source_orders_synced_at": "2026-07-11 11:59",
            "sample_count": 1,
            "open_position_count": 0,
            "skipped_order_count": 0,
            "stats_by_experiment": {},
            "samples": [_valid_trade_sample(experiment_id="missing_experiment")],
            "open_positions": [],
            "diagnostics": {"skipped_orders": []},
        },
    )

    with pytest.raises(SystemExit, match="2"):
        main(
            [
                "kelly",
                "build-strategy-stats",
                "--data-dir",
                str(data_dir),
            ]
        )

    assert "unknown experiment" in capsys.readouterr().err
    assert not (data_dir / "latest" / "kelly_strategy_stats.json").exists()
