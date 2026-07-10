from __future__ import annotations

import json
from pathlib import Path

from open_trader.kelly_order_intents import (
    build_kelly_order_intents_payload,
    write_kelly_order_intents,
)


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
                "suggested_position_pct": "4%",
            },
            "lifecycle_states": [
                {
                    "status": "pending_entry_order",
                    "market": "US",
                    "symbol": "RAM",
                    "reason": "入场规则触发。",
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
                "reason": "入场规则触发。",
                "action": "准备提交模拟盘买入订单",
                "suggested_position_pct": "4%",
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
                "suggested_position_pct": "4%",
                "per_symbol_budget": "25000",
                "budget_currency": "USD",
            },
        ],
    }


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
