from __future__ import annotations

import json
from pathlib import Path

import pytest

from open_trader.kelly_order_intents import (
    build_kelly_order_intents,
    build_kelly_order_intents_payload,
    write_kelly_order_intents,
)
from open_trader.kelly_order_risk import build_kelly_order_risk_checks_payload
from open_trader.kelly_strategy_stats import build_kelly_strategy_stats_payload


def test_build_kelly_order_intents_payload_from_pending_lifecycle_states() -> None:
    experiments = [
        {
            "experiment_id": "trend_exp",
            "experiment_name": "趋势回调第一批",
            "strategy_id": "trend_pullback_20d",
            "strategy_version": "v1",
            "status": "running",
            "market": "US",
            "market_capital_pool": {
                "market": "US",
                "amount": 30000,
                "currency": "USD",
                "enabled": True,
            },
            "budget_currency": "USD",
            "participants": [
                {
                    "market": "US",
                    "symbol": "RAM",
                    "per_symbol_budget": "25000",
                    "budget_currency": "USD",
                },
                {
                    "market": "US",
                    "symbol": "SOXX",
                    "per_symbol_budget": "25000",
                    "budget_currency": "USD",
                },
            ],
            "stats": {
                "suggested_position_pct": "3%",
                "parameter_source": "futu_paper_order_samples",
                "last_recomputed_at": "2026-07-11 12:01",
                "source_trade_samples_generated_at": "2026-07-11 12:00",
                "source_trade_samples_digest": "a" * 64,
            },
            "lifecycle_states": [
                {
                    "status": "pending_entry_order",
                    "market": "US",
                    "symbol": "RAM",
                    "reason": "入场规则触发，Kelly 建议单标的仓位 4%，风控通过。",
                    "action": "准备提交模拟盘买入订单",
                    "updated_at": "2026-07-10 10:01",
                },
                {
                    "status": "pending_exit_order",
                    "market": "US",
                    "symbol": "SOXX",
                    "reason": "止盈触发。",
                    "action": "准备卖出 50%",
                    "updated_at": "2026-07-10 10:02",
                },
                {
                    "status": "watching",
                    "market": "US",
                    "symbol": "MSFT",
                },
            ],
        },
        {
            "experiment_id": "paused_exp",
            "strategy_id": "breakout_10d",
            "strategy_version": "v1",
            "status": "paused",
            "lifecycle_states": [
                {
                    "status": "pending_entry_order",
                    "market": "US",
                    "symbol": "TSM",
                }
            ],
        },
    ]

    payload = build_kelly_order_intents_payload(
        experiments,
        created_at="2026-07-10 13:30",
    )

    assert payload == {
        "schema_version": "open_trader.kelly_order_intents.v1",
        "created_at": "2026-07-10 13:30",
        "intent_count": 2,
        "intents": [
            {
                "intent_id": "trend_exp:US:RAM:entry",
                "experiment_id": "trend_exp",
                "experiment_name": "趋势回调第一批",
                "strategy_id": "trend_pullback_20d",
                "strategy_version": "v1",
                "experiment_market": "US",
                "market_capital_pool": {
                    "market": "US",
                    "amount": 30000,
                    "currency": "USD",
                    "enabled": True,
                },
                "market": "US",
                "symbol": "RAM",
                "intent_type": "entry",
                "side": "buy",
                "execution_status": "pending",
                "risk_status": "not_checked",
                "created_at": "2026-07-10 13:30",
                "source": "kelly_lifecycle",
                "source_status": "pending_entry_order",
                "reason": "入场规则触发，仓位计算与风控检查待执行。",
                "action": "等待仓位计算与风控检查",
                "suggested_position_pct": "3%",
                "parameter_source": "futu_paper_order_samples",
                "strategy_stats_generated_at": "2026-07-11 12:01",
                "strategy_stats_source_samples_generated_at": "2026-07-11 12:00",
                "source_trade_samples_digest": "a" * 64,
                "per_symbol_budget": "25000",
                "budget_currency": "USD",
            },
            {
                "intent_id": "trend_exp:US:SOXX:exit",
                "experiment_id": "trend_exp",
                "experiment_name": "趋势回调第一批",
                "strategy_id": "trend_pullback_20d",
                "strategy_version": "v1",
                "experiment_market": "US",
                "market_capital_pool": {
                    "market": "US",
                    "amount": 30000,
                    "currency": "USD",
                    "enabled": True,
                },
                "market": "US",
                "symbol": "SOXX",
                "intent_type": "exit",
                "side": "sell",
                "execution_status": "pending",
                "risk_status": "not_checked",
                "created_at": "2026-07-10 13:30",
                "source": "kelly_lifecycle",
                "source_status": "pending_exit_order",
                "reason": "止盈触发。",
                "action": "准备卖出 50%",
                "budget_currency": "USD",
            },
        ],
    }

    entry = payload["intents"][0]
    assert "4%" not in f"{entry['reason']} {entry['action']}"
    assert "风控通过" not in f"{entry['reason']} {entry['action']}"


def test_build_kelly_order_intents_blocks_zero_sample_entry_in_risk_checks() -> None:
    payload = build_kelly_order_intents_payload(
        [
            {
                "experiment_id": "trend_exp",
                "experiment_name": "趋势回调第一批",
                "strategy_id": "trend_pullback_20d",
                "strategy_version": "v1",
                "status": "running",
                "market": "US",
                "budget_currency": "USD",
                "participants": [
                    {
                        "market": "US",
                        "symbol": "RAM",
                        "per_symbol_budget": "25000",
                        "budget_currency": "USD",
                    }
                ],
                "stats": {
                    "suggested_position_pct": "0%",
                    "parameter_source": "futu_paper_order_samples",
                    "last_recomputed_at": "2026-07-11 12:01",
                    "source_trade_samples_generated_at": "2026-07-11 12:00",
                },
                "lifecycle_states": [
                    {
                        "status": "pending_entry_order",
                        "market": "US",
                        "symbol": "RAM",
                    }
                ],
            }
        ],
        created_at="2026-07-11 12:02",
    )

    intent = payload["intents"][0]
    assert intent["suggested_position_pct"] == "0%"

    risk_payload = build_kelly_order_risk_checks_payload(
        payload,
        checked_at="2026-07-11 12:03",
    )

    assert risk_payload["checks"][0]["risk_status"] == "blocked"


def test_build_kelly_order_intents_skips_cross_market_lifecycle_state() -> None:
    experiments = [
        {
            "experiment_id": "trend_exp",
            "experiment_name": "趋势回调第一批",
            "strategy_id": "trend_pullback_20d",
            "strategy_version": "v1",
            "status": "running",
            "market": "US",
            "participants": [
                {
                    "market": "US",
                    "symbol": "RAM",
                    "per_symbol_budget": "25000",
                    "budget_currency": "USD",
                },
            ],
            "lifecycle_states": [
                {
                    "status": "pending_entry_order",
                    "market": "HK",
                    "symbol": "02840",
                },
            ],
        },
    ]

    payload = build_kelly_order_intents_payload(
        experiments,
        created_at="2026-07-10 13:30",
    )

    assert payload == {
        "schema_version": "open_trader.kelly_order_intents.v1",
        "created_at": "2026-07-10 13:30",
        "intent_count": 0,
        "intents": [],
    }


def test_write_kelly_order_intents_writes_latest_artifact(tmp_path: Path) -> None:
    payload = {
        "schema_version": "open_trader.kelly_order_intents.v1",
        "created_at": "2026-07-10 13:30",
        "intent_count": 0,
        "intents": [],
    }

    path = write_kelly_order_intents(tmp_path / "data", payload)

    assert path == tmp_path / "data/latest/kelly_order_intents.json"
    assert json.loads(path.read_text(encoding="utf-8")) == payload


def test_build_kelly_order_intents_ignores_malformed_optional_strategy_capital(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    latest_dir = data_dir / "latest"
    latest_dir.mkdir(parents=True)
    (latest_dir / "kelly_strategy_templates.json").write_text(
        json.dumps(
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
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (latest_dir / "kelly_experiments.json").write_text(
        json.dumps(
            {
                "schema_version": "open_trader.kelly_experiments.v1",
                "experiments": [
                    {
                        "experiment_id": "trend_us",
                        "experiment_name": "趋势回调 US",
                        "strategy_id": "trend_pullback_20d",
                        "strategy_version": "v1",
                        "market": "US",
                        "start_date": "2026-07-07",
                        "paper_account": "futu_simulate",
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
                                "symbol": "RAM",
                                "name": "RAM ETF",
                                "source": "holding",
                                "locked": True,
                                "per_symbol_budget": "10000",
                                "budget_currency": "USD",
                            }
                        ],
                        "lifecycle_states": [
                            {
                                "status": "pending_entry_order",
                                "market": "US",
                                "symbol": "RAM",
                            }
                        ],
                        "stats": {"suggested_position_pct": "4%"},
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (latest_dir / "kelly_strategy_capital.json").write_text(
        json.dumps(
            {
                "schema_version": "open_trader.kelly_strategy_capital.v1",
                "strategies": "bad",
            }
        ),
        encoding="utf-8",
    )
    trade_samples_payload = {
        "schema_version": "open_trader.kelly_trade_samples.v1",
        "generated_at": "2026-07-10 13:29",
        "source_orders_synced_at": "2026-07-10 13:28",
        "sample_count": 0,
        "open_position_count": 0,
        "skipped_order_count": 0,
        "stats_by_experiment": {},
        "samples": [],
        "open_positions": [],
        "diagnostics": {"skipped_orders": []},
    }
    (latest_dir / "kelly_trade_samples.json").write_text(
        json.dumps(trade_samples_payload),
        encoding="utf-8",
    )
    (latest_dir / "kelly_strategy_stats.json").write_text(
        json.dumps(
            build_kelly_strategy_stats_payload(
                [
                    {
                        "experiment_id": "trend_us",
                        "experiment_name": "趋势回调 US",
                        "market": "US",
                    }
                ],
                trade_samples_payload,
                generated_at="2026-07-10 13:30",
            ),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    payload = build_kelly_order_intents(
        data_dir,
        created_at="2026-07-10 13:30",
    )

    assert payload["intent_count"] == 1
    assert payload["intents"][0]["intent_id"] == "trend_us:US:RAM:entry"


def _write_entry_exit_lab_fixtures(
    data_dir: Path,
    *,
    strategy_stats_state: str,
) -> None:
    latest = data_dir / "latest"
    latest.mkdir(parents=True)
    (latest / "kelly_strategy_templates.json").write_text(
        json.dumps(
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
            }
        ),
        encoding="utf-8",
    )
    experiments = [
        {
            "experiment_id": "trend_us",
            "experiment_name": "Trend US",
            "strategy_id": "trend_pullback_20d",
            "strategy_version": "v1",
            "market": "US",
            "start_date": "2026-07-07",
            "paper_account": "futu_simulate",
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
                    "symbol": symbol,
                    "name": symbol,
                    "source": "watchlist",
                    "locked": True,
                    "per_symbol_budget": "15000",
                    "budget_currency": "USD",
                }
                for symbol in ("AAPL", "MSFT")
            ],
            "lifecycle_states": [
                {
                    "status": "pending_entry_order",
                    "market": "US",
                    "symbol": "AAPL",
                },
                {
                    "status": "pending_exit_order",
                    "market": "US",
                    "symbol": "MSFT",
                },
            ],
            "stats": {"suggested_position_pct": "9%"},
        }
    ]
    (latest / "kelly_experiments.json").write_text(
        json.dumps(
            {
                "schema_version": "open_trader.kelly_experiments.v1",
                "experiments": experiments,
            }
        ),
        encoding="utf-8",
    )
    trade_samples = {
        "schema_version": "open_trader.kelly_trade_samples.v1",
        "generated_at": "2026-07-11 12:00",
        "source_orders_synced_at": "2026-07-11 11:59",
        "sample_count": 0,
        "open_position_count": 0,
        "skipped_order_count": 0,
        "samples": [],
        "open_positions": [],
        "diagnostics": {"skipped_orders": []},
        "stats_by_experiment": {},
    }
    (latest / "kelly_trade_samples.json").write_text(
        json.dumps(trade_samples),
        encoding="utf-8",
    )
    if strategy_stats_state == "missing":
        return
    if strategy_stats_state == "malformed":
        (latest / "kelly_strategy_stats.json").write_text("{", encoding="utf-8")
        return
    stats_source = trade_samples
    stats_experiments = experiments
    if strategy_stats_state == "stale":
        stats_source = {**trade_samples, "generated_at": "2026-07-11 11:59"}
    elif strategy_stats_state == "incomplete":
        stats_experiments = []
    payload = build_kelly_strategy_stats_payload(
        stats_experiments,
        stats_source,
        generated_at="2026-07-11 12:01",
    )
    (latest / "kelly_strategy_stats.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    "strategy_stats_state",
    ["missing", "malformed", "stale", "incomplete"],
)
def test_build_kelly_order_intents_emits_only_exit_when_strategy_stats_unavailable(
    tmp_path: Path,
    strategy_stats_state: str,
) -> None:
    data_dir = tmp_path / "data"
    _write_entry_exit_lab_fixtures(
        data_dir,
        strategy_stats_state=strategy_stats_state,
    )

    payload = build_kelly_order_intents(data_dir, created_at="2026-07-11 12:02")

    assert [intent["intent_type"] for intent in payload["intents"]] == ["exit"]
    exit_intent = payload["intents"][0]
    for field in (
        "suggested_position_pct",
        "parameter_source",
        "strategy_stats_generated_at",
        "strategy_stats_source_samples_generated_at",
        "source_trade_samples_digest",
        "per_symbol_budget",
    ):
        assert field not in exit_intent
    risk = build_kelly_order_risk_checks_payload(
        payload,
        checked_at="2026-07-11 12:03",
    )
    assert risk["approved_count"] == 1
    assert risk["checks"][0]["risk_status"] == "approved"


def test_build_kelly_order_intents_does_not_hide_template_errors(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_entry_exit_lab_fixtures(data_dir, strategy_stats_state="missing")
    template_path = data_dir / "latest/kelly_strategy_templates.json"
    template_path.write_text(
        json.dumps(
            {
                "schema_version": "open_trader.kelly_strategy_templates.v1",
                "templates": [{}],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="missing required fields"):
        build_kelly_order_intents(data_dir)
