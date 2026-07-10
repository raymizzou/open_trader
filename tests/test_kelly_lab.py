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


def minimal_template_payload() -> dict[str, object]:
    return {
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
    }


def minimal_experiment_payload(
    *,
    experiment_market: str = "US",
    experiment_budget_currency: str = "USD",
    participant_market: str = "US",
    participant_symbol: str = "RAM",
    participant_budget_currency: str = "USD",
) -> dict[str, object]:
    return {
        "schema_version": "open_trader.kelly_experiments.v1",
        "experiments": [
            {
                "experiment_id": "trend_us",
                "experiment_name": "趋势回调 US",
                "strategy_id": "trend_pullback_20d",
                "strategy_version": "v1",
                "market": experiment_market,
                "start_date": "2026-07-07",
                "paper_account": "futu_simulate",
                "experiment_budget": "100000",
                "budget_currency": experiment_budget_currency,
                "capital_utilization_pct": "50",
                "allocation_mode": "equal_weight",
                "max_open_position_per_symbol": 1,
                "status": "running",
                "locked": True,
                "participants": [
                    {
                        "market": participant_market,
                        "symbol": participant_symbol,
                        "name": "RAM",
                        "source": "holding",
                        "locked": True,
                        "per_symbol_budget": "25000",
                        "budget_currency": participant_budget_currency,
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
    }


def test_load_kelly_lab_state_rejects_mixed_market_experiment(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    write_json(
        data_dir / "latest" / "kelly_strategy_templates.json",
        minimal_template_payload(),
    )
    write_json(
        data_dir / "latest" / "kelly_experiments.json",
        minimal_experiment_payload(participant_market="HK", participant_symbol="02840"),
    )

    with pytest.raises(
        ValueError,
        match="trend_us participant HK.02840 must match experiment market US",
    ):
        load_kelly_lab_state(data_dir)


def test_load_kelly_lab_state_attaches_market_capital_pool(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    write_json(
        data_dir / "latest" / "kelly_strategy_templates.json",
        minimal_template_payload(),
    )
    write_json(
        data_dir / "latest" / "kelly_experiments.json",
        minimal_experiment_payload(
            experiment_market="us",
            participant_market="us",
            participant_symbol="ram",
        ),
    )

    state = load_kelly_lab_state(data_dir).to_dict()

    experiment = state["experiments"][0]
    assert experiment["market"] == "US"
    assert experiment["budget_currency"] == "USD"
    assert experiment["market_capital_pool"] == {
        "market": "US",
        "amount": "100000",
        "currency": "USD",
        "enabled": True,
    }
    assert experiment["participants"][0]["market"] == "US"
    assert experiment["participants"][0]["symbol"] == "RAM"


def test_load_kelly_lab_state_rejects_experiment_budget_currency_mismatch(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    write_json(
        data_dir / "latest" / "kelly_strategy_templates.json",
        minimal_template_payload(),
    )
    write_json(
        data_dir / "latest" / "kelly_experiments.json",
        minimal_experiment_payload(experiment_budget_currency="HKD"),
    )

    with pytest.raises(
        ValueError,
        match="trend_us budget_currency HKD must match market US currency USD",
    ):
        load_kelly_lab_state(data_dir)


def test_load_kelly_lab_state_rejects_participant_budget_currency_mismatch(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    write_json(
        data_dir / "latest" / "kelly_strategy_templates.json",
        minimal_template_payload(),
    )
    write_json(
        data_dir / "latest" / "kelly_experiments.json",
        minimal_experiment_payload(participant_budget_currency="HKD"),
    )

    with pytest.raises(
        ValueError,
        match=(
            "trend_us participant US.RAM budget_currency HKD "
            "must match market US currency USD"
        ),
    ):
        load_kelly_lab_state(data_dir)


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
                    "market": "US",
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
                            "market": "US",
                            "symbol": "MSFT",
                            "name": "Microsoft",
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
                    "market": "US",
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
                    "market": "US",
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
                            "market": "US",
                            "symbol": "RAM",
                            "name": "RAM ETF",
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
                            "market": "US",
                            "symbol": "RAM",
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
        ("US", "RAM"),
    ]


def test_load_kelly_lab_state_attaches_paper_orders_by_experiment_id(
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
                    "market": "US",
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
                            "symbol": "RAM",
                            "name": "RAM ETF",
                            "source": "holding",
                            "locked": True,
                            "per_symbol_budget": "25000",
                            "budget_currency": "USD",
                        }
                    ],
                    "order_sync": {
                        "status": "success",
                        "environment": "SIMULATE",
                        "last_synced_at": "2026-07-08 10:08",
                        "order_count": 1,
                        "fill_count": 1,
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
    write_json(
        data_dir / "latest" / "kelly_paper_orders.json",
        {
            "schema_version": "open_trader.kelly_paper_orders.v1",
            "orders": [
                {
                    "experiment_id": "trend_pullback_20d_exp_20260707",
                    "market": "US",
                    "symbol": "RAM",
                    "side": "buy",
                    "submitted_at": "2026-07-08 10:01",
                    "order_price": "12.34",
                    "order_qty": "800",
                    "filled_qty": "800",
                    "avg_fill_price": "12.34",
                    "status": "filled",
                    "order_id": "SIM-10001",
                },
                {
                    "experiment_id": "other_experiment",
                    "market": "US",
                    "symbol": "MSFT",
                    "side": "buy",
                    "order_id": "SIM-99999",
                },
            ],
        },
    )

    state = load_kelly_lab_state(data_dir).to_dict()

    orders = state["experiments"][0]["order_sync"]["orders"]
    assert orders == [
        {
            "experiment_id": "trend_pullback_20d_exp_20260707",
            "market": "US",
            "symbol": "RAM",
            "side": "buy",
            "submitted_at": "2026-07-08 10:01",
            "order_price": "12.34",
            "order_qty": "800",
            "filled_qty": "800",
            "avg_fill_price": "12.34",
            "status": "filled",
            "order_id": "SIM-10001",
        }
    ]


def test_load_kelly_lab_state_filters_attached_paper_orders_to_experiment_market(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    write_json(
        data_dir / "latest" / "kelly_strategy_templates.json",
        minimal_template_payload(),
    )
    write_json(
        data_dir / "latest" / "kelly_experiments.json",
        minimal_experiment_payload(),
    )
    write_json(
        data_dir / "latest" / "kelly_paper_orders.json",
        {
            "schema_version": "open_trader.kelly_paper_orders.v1",
            "orders": [
                {
                    "experiment_id": "trend_us",
                    "market": "US",
                    "symbol": "RAM",
                    "side": "buy",
                    "status": "filled",
                    "order_id": "SIM-US",
                },
                {
                    "experiment_id": "trend_us",
                    "market": "HK",
                    "symbol": "02840",
                    "side": "sell",
                    "status": "submitted",
                    "order_id": "SIM-HK",
                },
            ],
        },
    )

    state = load_kelly_lab_state(data_dir).to_dict()

    orders = state["experiments"][0]["order_sync"]["orders"]
    assert [(order["market"], order["symbol"]) for order in orders] == [("US", "RAM")]


def test_load_kelly_lab_state_filters_attached_order_executions_to_experiment_market(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    write_json(
        data_dir / "latest" / "kelly_strategy_templates.json",
        minimal_template_payload(),
    )
    write_json(
        data_dir / "latest" / "kelly_experiments.json",
        minimal_experiment_payload(),
    )
    write_json(
        data_dir / "latest" / "kelly_order_executions.json",
        {
            "schema_version": "open_trader.kelly_order_executions.v1",
            "environment": "SIMULATE",
            "source": "test",
            "executed_at": "2026-07-10 15:28",
            "executions": [
                {
                    "experiment_id": "trend_us",
                    "market": "US",
                    "symbol": "RAM",
                    "side": "buy",
                    "execution_status": "skipped",
                },
                {
                    "experiment_id": "trend_us",
                    "market": "HK",
                    "symbol": "02840",
                    "side": "sell",
                    "execution_status": "submitted",
                },
            ],
        },
    )

    state = load_kelly_lab_state(data_dir).to_dict()

    order_execution = state["experiments"][0]["order_execution"]
    assert order_execution["execution_count"] == 1
    assert order_execution["submitted_count"] == 0
    assert order_execution["skipped_count"] == 1
    assert [
        (execution["market"], execution["symbol"])
        for execution in order_execution["executions"]
    ] == [("US", "RAM")]


def test_load_kelly_lab_state_keeps_existing_order_sync_when_paper_orders_missing(
    tmp_path: Path,
) -> None:
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
                    "market": "US",
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
                    "order_sync": {
                        "status": "success",
                        "environment": "SIMULATE",
                        "order_count": 0,
                        "fill_count": 0,
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

    assert state["available"] is True
    assert state["experiments"][0]["order_sync"] == {
        "status": "success",
        "environment": "SIMULATE",
        "order_count": 0,
        "fill_count": 0,
    }


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
                    "market": "US",
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
                    "market": "US",
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


def test_load_checked_in_kelly_data_is_available() -> None:
    state = load_kelly_lab_state(Path("data")).to_dict()

    assert state["available"] is True
    assert state["experiment_count"] >= 1


def test_latest_kelly_experiments_are_single_market() -> None:
    state = load_kelly_lab_state(Path("data")).to_dict()

    allowed_markets = {"US", "HK", "CN"}
    for experiment in state["experiments"]:
        experiment_market = experiment["market"]
        assert experiment_market in allowed_markets
        assert experiment["market_capital_pool"]["market"] == experiment_market
        for participant in experiment["participants"]:
            assert participant["market"] == experiment_market


def test_latest_kelly_experiments_split_trend_pullback_mock_by_market() -> None:
    state = load_kelly_lab_state(Path("data")).to_dict()
    experiments = {
        experiment["experiment_id"]: experiment for experiment in state["experiments"]
    }

    assert "trend_pullback_20d_mock_20260707" not in experiments

    trend_us = experiments["trend_pullback_20d_us_mock_20260707"]
    assert trend_us["experiment_name"] == "趋势回调 20D Mock US 第一批"
    assert trend_us["market"] == "US"
    assert trend_us["experiment_budget"] == "100000"
    assert trend_us["budget_currency"] == "USD"
    assert trend_us["market_capital_pool"] == {
        "market": "US",
        "amount": "100000",
        "currency": "USD",
        "enabled": True,
    }
    assert [
        (participant["market"], participant["symbol"], participant["per_symbol_budget"])
        for participant in trend_us["participants"]
    ] == [
        ("US", "DRAM", "33333.33"),
        ("US", "RAM", "33333.33"),
        ("US", "SOXX", "33333.33"),
    ]
    assert {
        (state["market"], state["symbol"]) for state in trend_us["lifecycle_states"]
    } == {
        ("US", "DRAM"),
        ("US", "RAM"),
        ("US", "SOXX"),
    }
    assert trend_us["order_sync"]["order_count"] == 1
    assert trend_us["order_sync"]["fill_count"] == 1
    assert [
        (order["market"], order["symbol"]) for order in trend_us["order_sync"]["orders"]
    ] == [("US", "RAM")]

    trend_hk = experiments["trend_pullback_20d_hk_mock_20260707"]
    assert trend_hk["experiment_name"] == "趋势回调 20D Mock HK 第一批"
    assert trend_hk["market"] == "HK"
    assert trend_hk["experiment_budget"] == "500000"
    assert trend_hk["budget_currency"] == "HKD"
    assert trend_hk["market_capital_pool"] == {
        "market": "HK",
        "amount": "500000",
        "currency": "HKD",
        "enabled": True,
    }
    assert [
        (participant["market"], participant["symbol"], participant["per_symbol_budget"])
        for participant in trend_hk["participants"]
    ] == [("HK", "02840", "500000")]
    assert [
        (state["market"], state["symbol"], state["status"])
        for state in trend_hk["lifecycle_states"]
    ] == [("HK", "02840", "pending_exit_order")]
    assert trend_hk["order_sync"]["order_count"] == len(
        trend_hk["order_sync"]["orders"]
    )
    assert trend_hk["order_sync"]["fill_count"] == sum(
        1
        for order in trend_hk["order_sync"]["orders"]
        if str(order.get("filled_qty", "")).strip() not in {"", "0", "-"}
    )


def test_load_checked_in_kelly_data_has_scoped_order_and_lifecycle_metadata() -> None:
    state = load_kelly_lab_state(Path("data")).to_dict()
    experiments = {
        experiment["experiment_id"]: experiment for experiment in state["experiments"]
    }

    for experiment in experiments.values():
        order_sync = experiment.get("order_sync")
        if isinstance(order_sync, dict) and "orders" in order_sync:
            orders = order_sync["orders"]
            assert order_sync["order_count"] == len(orders)
            assert order_sync["fill_count"] == sum(
                1
                for order in orders
                if str(order.get("filled_qty", "")).strip() not in {"", "0", "-"}
            )

    for experiment in experiments.values():
        participants = {
            (participant["market"], participant["symbol"])
            for participant in experiment["participants"]
        }
        lifecycle_states = {
            (state["market"], state["symbol"])
            for state in experiment["lifecycle_states"]
        }
        assert lifecycle_states <= participants
