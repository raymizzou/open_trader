from __future__ import annotations

import json
from pathlib import Path

from open_trader.kelly_order_risk import (
    build_kelly_order_risk_checks_payload,
    write_kelly_order_risk_checks,
)


def test_build_kelly_order_risk_checks_approves_valid_entry_and_exit() -> None:
    intent_payload = {
        "schema_version": "open_trader.kelly_order_intents.v1",
        "created_at": "2026-07-10 13:30",
        "intent_count": 2,
        "intents": [
            {
                "intent_id": "trend:US:RAM:entry",
                "experiment_id": "trend",
                "experiment_name": "趋势回调第一批",
                "strategy_id": "trend_pullback_20d",
                "strategy_version": "v1",
                "market": "US",
                "symbol": "RAM",
                "intent_type": "entry",
                "side": "buy",
                "suggested_position_pct": "4%",
                "per_symbol_budget": "25000",
                "budget_currency": "USD",
            },
            {
                "intent_id": "trend:HK:02840:exit",
                "experiment_id": "trend",
                "experiment_name": "趋势回调第一批",
                "strategy_id": "trend_pullback_20d",
                "strategy_version": "v1",
                "market": "HK",
                "symbol": "02840",
                "intent_type": "exit",
                "side": "sell",
                "suggested_position_pct": "4%",
                "per_symbol_budget": "25000",
                "budget_currency": "USD",
            },
        ],
    }

    payload = build_kelly_order_risk_checks_payload(
        intent_payload,
        checked_at="2026-07-10 13:31",
        max_entry_position_pct="4",
    )

    assert payload == {
        "schema_version": "open_trader.kelly_order_risk_checks.v1",
        "checked_at": "2026-07-10 13:31",
        "max_entry_position_pct": "4",
        "intent_count": 2,
        "approved_count": 2,
        "blocked_count": 0,
        "checks": [
            {
                "intent_id": "trend:US:RAM:entry",
                "experiment_id": "trend",
                "experiment_name": "趋势回调第一批",
                "strategy_id": "trend_pullback_20d",
                "strategy_version": "v1",
                "market": "US",
                "symbol": "RAM",
                "intent_type": "entry",
                "side": "buy",
                "risk_status": "approved",
                "execution_status": "ready",
                "checked_at": "2026-07-10 13:31",
                "planned_notional": "1000",
                "budget_currency": "USD",
                "reason": "entry risk checks passed",
                "check_results": [
                    {
                        "check": "per_symbol_budget_positive",
                        "status": "passed",
                        "detail": "25000",
                    },
                    {
                        "check": "suggested_position_pct_positive",
                        "status": "passed",
                        "detail": "4",
                    },
                    {
                        "check": "max_entry_position_pct",
                        "status": "passed",
                        "detail": "4 <= 4",
                    },
                ],
            },
            {
                "intent_id": "trend:HK:02840:exit",
                "experiment_id": "trend",
                "experiment_name": "趋势回调第一批",
                "strategy_id": "trend_pullback_20d",
                "strategy_version": "v1",
                "market": "HK",
                "symbol": "02840",
                "intent_type": "exit",
                "side": "sell",
                "risk_status": "approved",
                "execution_status": "ready",
                "checked_at": "2026-07-10 13:31",
                "planned_notional": "",
                "budget_currency": "USD",
                "reason": "exit intent reduces exposure",
                "check_results": [
                    {
                        "check": "exit_default_allow",
                        "status": "passed",
                        "detail": "sell/exit intents are not blocked in v1",
                    }
                ],
            },
        ],
    }


def test_build_kelly_order_risk_checks_blocks_entry_above_position_cap() -> None:
    intent_payload = {
        "schema_version": "open_trader.kelly_order_intents.v1",
        "created_at": "2026-07-10 13:30",
        "intent_count": 1,
        "intents": [
            {
                "intent_id": "breakout:US:TSM:entry",
                "experiment_id": "breakout",
                "experiment_name": "突破第一批",
                "strategy_id": "breakout_10d",
                "strategy_version": "v1",
                "market": "US",
                "symbol": "TSM",
                "intent_type": "entry",
                "side": "buy",
                "suggested_position_pct": "8%",
                "per_symbol_budget": "15000",
                "budget_currency": "USD",
            }
        ],
    }

    payload = build_kelly_order_risk_checks_payload(
        intent_payload,
        checked_at="2026-07-10 13:31",
        max_entry_position_pct="4",
    )

    assert payload["approved_count"] == 0
    assert payload["blocked_count"] == 1
    assert payload["checks"][0]["risk_status"] == "blocked"
    assert payload["checks"][0]["execution_status"] == "risk_blocked"
    assert payload["checks"][0]["planned_notional"] == "1200"
    assert payload["checks"][0]["reason"] == "entry risk checks failed"
    assert payload["checks"][0]["check_results"][-1] == {
        "check": "max_entry_position_pct",
        "status": "failed",
        "detail": "8 > 4",
    }


def test_build_kelly_order_risk_checks_blocks_entry_with_invalid_budget() -> None:
    intent_payload = {
        "schema_version": "open_trader.kelly_order_intents.v1",
        "created_at": "2026-07-10 13:30",
        "intent_count": 1,
        "intents": [
            {
                "intent_id": "trend:US:RAM:entry",
                "experiment_id": "trend",
                "experiment_name": "趋势回调第一批",
                "strategy_id": "trend_pullback_20d",
                "strategy_version": "v1",
                "market": "US",
                "symbol": "RAM",
                "intent_type": "entry",
                "side": "buy",
                "suggested_position_pct": "4%",
                "per_symbol_budget": "",
                "budget_currency": "USD",
            }
        ],
    }

    payload = build_kelly_order_risk_checks_payload(
        intent_payload,
        checked_at="2026-07-10 13:31",
    )

    assert payload["blocked_count"] == 1
    assert payload["checks"][0]["planned_notional"] == ""
    assert payload["checks"][0]["check_results"][0] == {
        "check": "per_symbol_budget_positive",
        "status": "failed",
        "detail": "",
    }


def test_write_kelly_order_risk_checks_writes_latest_artifact(tmp_path: Path) -> None:
    payload = {
        "schema_version": "open_trader.kelly_order_risk_checks.v1",
        "checked_at": "2026-07-10 13:31",
        "max_entry_position_pct": "4",
        "intent_count": 0,
        "approved_count": 0,
        "blocked_count": 0,
        "checks": [],
    }

    path = write_kelly_order_risk_checks(tmp_path / "data", payload)

    assert path == tmp_path / "data/latest/kelly_order_risk_checks.json"
    assert json.loads(path.read_text(encoding="utf-8")) == payload
