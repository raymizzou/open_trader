from __future__ import annotations

import json
from pathlib import Path

from open_trader.cli import main


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


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
