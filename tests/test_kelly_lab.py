from __future__ import annotations

import json
from pathlib import Path

import pytest

from open_trader.kelly_lab import (
    index_kelly_experiments_by_market_symbol,
    load_kelly_lab_state,
)


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_load_kelly_lab_state_returns_locked_experiments(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    write_json(
        data_dir / "latest" / "kelly_strategy_templates.json",
        {
            "schema_version": "open_trader.kelly_strategy_templates.v1",
            "templates": [
                {
                    "strategy_id": "trend_pullback_20d",
                    "strategy_name": "趋势回调 20D",
                    "strategy_version": "v1",
                    "entry_rule_description": "价格回调到 20 日均线附近。",
                    "exit_rule_description": "目标价、止损或 20 个交易日到期。",
                    "max_holding_days": 20,
                    "order_type": "limit",
                    "market_session": "regular",
                }
            ],
        },
    )
    write_json(
        data_dir / "latest" / "kelly_experiments.json",
        {
            "schema_version": "open_trader.kelly_experiments.v1",
            "experiments": [
                {
                    "experiment_id": "trend_pullback_20d_exp_20260707",
                    "experiment_name": "趋势回调 20D 第一批",
                    "strategy_id": "trend_pullback_20d",
                    "strategy_version": "v1",
                    "start_date": "2026-07-07",
                    "paper_account": "futu_simulate",
                    "experiment_budget": "100000",
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
                            "name": "Apple Inc.",
                            "source": "holding+watchlist",
                            "locked": True,
                            "per_symbol_budget": "25000",
                            "budget_currency": "USD",
                        },
                        {
                            "market": "HK",
                            "symbol": "00700",
                            "name": "腾讯控股",
                            "source": "watchlist",
                            "locked": True,
                            "per_symbol_budget": "25000",
                            "budget_currency": "USD",
                        },
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

    state = load_kelly_lab_state(data_dir).to_dict()

    assert state["available"] is True
    assert state["template_count"] == 1
    assert state["experiment_count"] == 1
    experiment = state["experiments"][0]
    assert experiment["experiment_id"] == "trend_pullback_20d_exp_20260707"
    assert experiment["locked"] is True
    assert experiment["template"]["strategy_name"] == "趋势回调 20D"
    assert experiment["participants"][0]["symbol"] == "AAPL"
    assert experiment["stats"]["sample_stage"] == "insufficient"


def test_load_kelly_lab_state_generates_lifecycle_states_from_symbol_facts(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    write_json(
        data_dir / "latest" / "kelly_strategy_templates.json",
        {
            "schema_version": "open_trader.kelly_strategy_templates.v1",
            "templates": [
                {
                    "strategy_id": "trend_pullback_20d",
                    "strategy_name": "趋势回调 20D",
                    "strategy_version": "v1",
                    "entry_rule_description": "价格回调到 20 日均线附近。",
                    "exit_rule_description": "目标价、止损或 20 个交易日到期。",
                    "max_holding_days": 20,
                    "order_type": "limit",
                    "market_session": "regular",
                    "rules": {
                        "entry": {
                            "type": "pullback_to_moving_average",
                            "ma_days": 20,
                            "tolerance_pct": 1,
                            "trend_filter": {
                                "type": "moving_average_slope",
                                "ma_days": 50,
                                "direction": "up",
                            },
                        },
                        "take_profit": {
                            "type": "risk_multiple",
                            "trigger_r": 2,
                            "sell_pct": 50,
                        },
                    },
                }
            ],
        },
    )
    write_json(
        data_dir / "latest" / "kelly_experiments.json",
        {
            "schema_version": "open_trader.kelly_experiments.v1",
            "experiments": [
                {
                    "experiment_id": "trend_pullback_20d_exp_20260707",
                    "experiment_name": "趋势回调 20D 第一批",
                    "strategy_id": "trend_pullback_20d",
                    "strategy_version": "v1",
                    "start_date": "2026-07-07",
                    "paper_account": "futu_simulate",
                    "experiment_budget": "100000",
                    "budget_currency": "USD",
                    "capital_utilization_pct": "50",
                    "allocation_mode": "equal_weight",
                    "max_open_position_per_symbol": 1,
                    "status": "running",
                    "locked": True,
                    "participants": [
                        {
                            "market": "us",
                            "symbol": "aapl",
                            "name": "Apple Inc.",
                            "source": "watchlist",
                            "locked": True,
                            "per_symbol_budget": "25000",
                            "budget_currency": "USD",
                        }
                    ],
                    "symbol_facts": {
                        "US.AAPL": {
                            "price": 99.4,
                            "moving_averages": {"20": 100},
                            "moving_average_slopes": {"50": "up"},
                            "updated_at": "2026-07-08 13:00",
                        }
                    },
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

    state = load_kelly_lab_state(data_dir).to_dict()

    lifecycle_states = state["experiments"][0]["lifecycle_states"]
    assert lifecycle_states == [
        {
            "market": "US",
            "symbol": "AAPL",
            "status": "pending_entry_order",
            "reason": "入场规则触发，Kelly 仓位已计算，风控通过。",
            "action": "准备提交模拟盘买入订单",
            "updated_at": "2026-07-08 13:00",
        }
    ]


def test_load_kelly_lab_state_filters_manual_lifecycle_states_to_participants(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    write_json(
        data_dir / "latest" / "kelly_strategy_templates.json",
        {
            "schema_version": "open_trader.kelly_strategy_templates.v1",
            "templates": [
                {
                    "strategy_id": "trend_pullback_20d",
                    "strategy_name": "趋势回调 20D",
                    "strategy_version": "v1",
                    "entry_rule_description": "价格回调到 20 日均线附近。",
                    "exit_rule_description": "目标价、止损或 20 个交易日到期。",
                    "max_holding_days": 20,
                    "order_type": "limit",
                    "market_session": "regular",
                }
            ],
        },
    )
    write_json(
        data_dir / "latest" / "kelly_experiments.json",
        {
            "schema_version": "open_trader.kelly_experiments.v1",
            "experiments": [
                {
                    "experiment_id": "trend_pullback_20d_exp_20260707",
                    "experiment_name": "趋势回调 20D 第一批",
                    "strategy_id": "trend_pullback_20d",
                    "strategy_version": "v1",
                    "start_date": "2026-07-07",
                    "paper_account": "futu_simulate",
                    "experiment_budget": "100000",
                    "budget_currency": "USD",
                    "capital_utilization_pct": "50",
                    "allocation_mode": "equal_weight",
                    "max_open_position_per_symbol": 1,
                    "status": "running",
                    "locked": True,
                    "participants": [
                        {
                            "market": "US",
                            "symbol": "DRAM",
                            "name": "DRAM ETF",
                            "source": "holding",
                            "locked": True,
                            "per_symbol_budget": "25000",
                            "budget_currency": "USD",
                        },
                        {
                            "market": "HK",
                            "symbol": "02840",
                            "name": "SPDR Gold",
                            "source": "holding",
                            "locked": True,
                            "per_symbol_budget": "25000",
                            "budget_currency": "USD",
                        },
                    ],
                    "lifecycle_states": [
                        {
                            "market": "US",
                            "symbol": "DRAM",
                            "status": "watching",
                            "reason": "属于该策略。",
                        },
                        {
                            "market": "HK",
                            "symbol": "02840",
                            "status": "holding",
                            "reason": "属于该策略。",
                        },
                        {
                            "market": "US",
                            "symbol": "MSFT",
                            "status": "completed",
                            "reason": "混入其他策略。",
                        },
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

    state = load_kelly_lab_state(data_dir).to_dict()

    lifecycle_states = state["experiments"][0]["lifecycle_states"]
    assert [(item["market"], item["symbol"]) for item in lifecycle_states] == [
        ("US", "DRAM"),
        ("HK", "02840"),
    ]


def test_load_kelly_lab_state_missing_files_is_unavailable(tmp_path: Path) -> None:
    state = load_kelly_lab_state(tmp_path / "data").to_dict()

    assert state["available"] is False
    assert state["template_count"] == 0
    assert state["experiment_count"] == 0
    assert state["experiments"] == []
    assert "kelly_strategy_templates.json not found" in state["error"]


def test_load_kelly_lab_state_rejects_unknown_template_version(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    write_json(
        data_dir / "latest" / "kelly_strategy_templates.json",
        {
            "schema_version": "open_trader.kelly_strategy_templates.v1",
            "templates": [
                {
                    "strategy_id": "trend_pullback_20d",
                    "strategy_name": "趋势回调 20D",
                    "strategy_version": "v1",
                    "entry_rule_description": "价格回调到 20 日均线附近。",
                    "exit_rule_description": "目标价、止损或 20 个交易日到期。",
                    "max_holding_days": 20,
                    "order_type": "limit",
                    "market_session": "regular",
                }
            ],
        },
    )
    write_json(
        data_dir / "latest" / "kelly_experiments.json",
        {
            "schema_version": "open_trader.kelly_experiments.v1",
            "experiments": [
                {
                    "experiment_id": "trend_pullback_20d_exp_20260707",
                    "experiment_name": "趋势回调 20D 第二版",
                    "strategy_id": "trend_pullback_20d",
                    "strategy_version": "v2",
                    "start_date": "2026-07-07",
                    "paper_account": "futu_simulate",
                    "experiment_budget": "100000",
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
                            "name": "Apple Inc.",
                            "source": "holding",
                            "locked": True,
                            "per_symbol_budget": "25000",
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

    with pytest.raises(
        ValueError,
        match="unknown strategy template .*trend_pullback_20d.*v2",
    ):
        load_kelly_lab_state(data_dir)


def test_index_kelly_experiments_by_market_symbol(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    write_json(
        data_dir / "latest" / "kelly_strategy_templates.json",
        {
            "schema_version": "open_trader.kelly_strategy_templates.v1",
            "templates": [
                {
                    "strategy_id": "breakout_10d",
                    "strategy_name": "突破 10D",
                    "strategy_version": "v1",
                    "entry_rule_description": "突破区间。",
                    "exit_rule_description": "跌回突破位或 10 天到期。",
                    "max_holding_days": 10,
                    "order_type": "limit",
                    "market_session": "regular",
                }
            ],
        },
    )
    write_json(
        data_dir / "latest" / "kelly_experiments.json",
        {
            "schema_version": "open_trader.kelly_experiments.v1",
            "experiments": [
                {
                    "experiment_id": "breakout_10d_exp_20260707",
                    "experiment_name": "突破 10D 第一批",
                    "strategy_id": "breakout_10d",
                    "strategy_version": "v1",
                    "start_date": "2026-07-07",
                    "paper_account": "futu_simulate",
                    "experiment_budget": "50000",
                    "budget_currency": "USD",
                    "capital_utilization_pct": "40",
                    "allocation_mode": "equal_weight",
                    "max_open_position_per_symbol": 1,
                    "status": "running",
                    "locked": True,
                    "participants": [
                        {
                            "market": "US",
                            "symbol": "MSFT",
                            "name": "Microsoft",
                            "source": "holding",
                            "locked": True,
                            "per_symbol_budget": "20000",
                            "budget_currency": "USD",
                        }
                    ],
                    "stats": {
                        "completed_samples": 12,
                        "open_samples": 1,
                        "observed_win_rate": "58.33%",
                        "sample_stage": "insufficient",
                    },
                }
            ],
        },
    )
    state = load_kelly_lab_state(data_dir)

    indexed = index_kelly_experiments_by_market_symbol(state.experiments)

    assert list(indexed) == [("US", "MSFT")]
    assert indexed[("US", "MSFT")][0]["experiment_id"] == "breakout_10d_exp_20260707"
