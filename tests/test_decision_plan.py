from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from open_trader.decision_plan import (
    build_decision_plan,
    load_decision_plans,
    publish_decision_plans,
    validate_decision_plan,
)


def strategy_snapshot(strategy_id: str = "trend_pullback/v1") -> dict[str, object]:
    return {
        "strategy": {"id": strategy_id, "name_zh": "趋势回调", "parameters": {"sma_long": "50"}},
        "facts": {
            "ma20_distance_pct": {"formula": "(close / sma20 - 1) * 100", "inputs": {"close": "63", "sma20": "62"}, "source_date": "2026-07-10", "calculated_value": "1.6129"},
            "rsi14": {"formula": "Wilder RSI(close, 14)", "inputs": {"period": "14"}, "source_date": "2026-07-10", "calculated_value": "52"},
            "bollinger_position": {"formula": "compare(close, bollinger bands)", "inputs": {"close": "63"}, "source_date": "2026-07-10", "calculated_value": "inside"},
            "relative_volume": {"formula": "volume / SMA(previous volume, 20)", "inputs": {"volume": "120", "average_volume": "100"}, "source_date": "2026-07-10", "calculated_value": "1.2"},
        },
        "conditions": [
            {"condition_id": "trend-exit", "priority": "risk", "operator": "<=", "calculated_value": "57", "target_weight": "0", "suggested_action": "退出", "formula": "min(sma50, active_stop)", "inputs": {"sma50": "58", "active_stop": "57"}, "source_date": "2026-07-10"},
            {"condition_id": "trend-add", "priority": "ordinary", "operator": ">=", "calculated_value": "65", "target_weight": "0.10", "suggested_action": "加仓", "formula": "max(previous 5 closes)", "inputs": {"prior5": "65"}, "source_date": "2026-07-10"},
        ],
    }


def backtest(range_name: str, *, passed: bool = True, strategy_id: str = "trend_pullback/v1", excess: str = "2.5") -> dict[str, object]:
    return {
        "strategy_id": strategy_id,
        "range": range_name,
        "gate": {
            "passed": passed,
            "policy_id": "benchmark_outperformance/v1",
            "reasons": [] if passed else ["did_not_outperform_benchmark"],
        },
        "strategy": {"total_return_pct": "8", "max_drawdown_pct": "6", "sharpe_ratio": "1.1"},
        "market_benchmark": {"symbol": "SPY", "total_return_pct": "5.5"},
        "market_excess_return_pct": excess,
        "actual_start": "2025-07-10",
        "actual_end": "2026-07-10",
    }


def position(*, weight: str = "0.078") -> dict[str, str]:
    return {"quantity": "400", "weight": weight, "nav": "323076.92", "price": "63"}


def build_plan(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "run_date": "2026-07-13",
        "market": "US",
        "symbol": "DRAM",
        "position": position(),
        "strategy_snapshots": [strategy_snapshot()],
        "backtests": [backtest("6M"), backtest("1Y")],
        "technical_facts": strategy_snapshot()["facts"],
        "tradingagents_summary": {"current_action": "观察", "core_reason": "等待趋势确认"},
        "effective_at": "2026-07-13T09:30:00-04:00",
        "expires_at": "2026-07-13T16:00:00-04:00",
    }
    values.update(overrides)
    return build_decision_plan(**values)  # type: ignore[arg-type]


def test_builds_validated_plan_only_from_passing_strategy() -> None:
    plan = build_plan()

    assert plan["schema_version"] == "open_trader.decision_plan.v1"
    assert plan["mode"] == "validated_plan"
    assert plan["max_weight"] == "0.10"
    assert plan["conditions"][0]["priority"] == "risk"
    assert plan["conditions"][0]["target_quantity"] == "0"
    assert all(Decimal(item["target_weight"]) <= Decimal("0.10") for item in plan["conditions"])
    assert plan["backtests"][1]["gate"]["passed"] is True
    validate_decision_plan(plan)


def test_failed_gate_produces_non_executable_fallback() -> None:
    plan = build_plan(backtests=[backtest("6M"), backtest("1Y", passed=False)])

    assert plan["mode"] == "fallback_advice"
    assert plan["conditions"] == []
    assert plan["fallback"]["label"] == "非执行型建议"
    assert plan["fallback"]["recommendation"] == "禁止加仓"
    assert [fact["key"] for fact in plan["fallback"]["facts"]] == [
        "ma20_distance_pct", "rsi14", "bollinger_position", "relative_volume",
    ]


def test_existing_overweight_position_keeps_only_non_increasing_conditions() -> None:
    plan = build_plan(position=position(weight="0.12"))

    assert plan["mode"] == "validated_plan"
    assert plan["risk_status"] == "overweight_no_add"
    assert [condition["condition_id"] for condition in plan["conditions"]] == ["trend-exit"]


def test_publication_rejects_duplicate_symbol_without_replacing_latest(tmp_path: Path) -> None:
    latest = tmp_path / "latest/US/decision_plans.json"
    latest.parent.mkdir(parents=True)
    latest.write_text('{"old": true}', encoding="utf-8")
    plan = build_plan()

    with pytest.raises(ValueError, match="重复"):
        publish_decision_plans(
            data_dir=tmp_path, run_date="2026-07-13", market="US",
            records=[plan, plan], update_latest=True,
        )

    assert latest.read_text(encoding="utf-8") == '{"old": true}'


def test_publish_and_load_round_trip_strictly_validates_records(tmp_path: Path) -> None:
    run_path, latest_path = publish_decision_plans(
        data_dir=tmp_path, run_date="2026-07-13", market="US",
        records=[build_plan()], update_latest=True,
    )

    assert run_path == tmp_path / "runs/2026-07-13/US/decision_plans.json"
    assert latest_path == tmp_path / "latest/US/decision_plans.json"
    assert load_decision_plans(latest_path)[0]["plan_id"] == "US.DRAM:2026-07-13:v1"
    assert json.loads(run_path.read_text(encoding="utf-8"))["schema_version"] == "open_trader.decision_plans.v1"


def test_validation_rejects_non_decimal_numeric_string() -> None:
    plan = build_plan()
    plan["current_weight"] = "not-a-number"

    with pytest.raises(ValueError, match="current_weight"):
        validate_decision_plan(plan)


def test_validation_rejects_fallback_fact_without_provenance() -> None:
    plan = build_plan(backtests=[backtest("6M"), backtest("1Y", passed=False)])
    plan["fallback"]["facts"][0]["formula"] = ""

    with pytest.raises(ValueError, match="兜底事实缺少参数来源"):
        validate_decision_plan(plan)


def test_validation_rejects_non_decimal_backtest_metric() -> None:
    plan = build_plan()
    plan["backtests"][0]["strategy"]["max_drawdown_pct"] = "bad"

    with pytest.raises(ValueError, match="max_drawdown_pct"):
        validate_decision_plan(plan)


def test_validation_rejects_non_decimal_calmar_ratio() -> None:
    plan = build_plan()
    plan["backtests"][0]["strategy"]["calmar_ratio"] = "bad"

    with pytest.raises(ValueError, match="calmar_ratio"):
        validate_decision_plan(plan)
