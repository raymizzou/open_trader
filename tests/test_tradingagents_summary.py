from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from open_trader.trade_actions import TRADE_ACTION_FIELDNAMES
from open_trader.trading_plan import TRADING_PLAN_FIELDNAMES
from open_trader.tradingagents_summary import (
    TRADINGAGENTS_SUMMARY_SCHEMA_VERSION,
    build_missing_reason_fields,
    generate_tradingagents_summary,
    load_tradingagents_summary_cache,
    tradingagents_summary_latest_path,
    tradingagents_summary_run_path,
    validate_tradingagents_summary_record,
)


ADVICE_FIELDS = [
    "run_date",
    "symbol",
    "market",
    "asset_class",
    "portfolio_weight_hkd",
    "risk_flag",
    "source",
    "advice_action",
    "advice_summary",
    "raw_decision",
    "status",
    "error",
    "source_status",
    "fallback_reason",
    "fallback_from_date",
]

DISPLAY_FIELDS = [
    "ta_view",
    "current_action",
    "core_reason",
    "ta_report_date",
    "latest_run_date",
]


class FakeExtractor:
    def __init__(self, payload: dict[str, object] | None = None) -> None:
        self.payload = payload or {
            "schema_version": TRADINGAGENTS_SUMMARY_SCHEMA_VERSION,
            "core_reason": (
                "内存超级周期仍在，但价格极度延伸、MACD 背离且财报前情绪拥挤，"
                "所以 TA 建议降低仓位而非清仓。"
            ),
            "reason_fields": {
                "main_judgment": "结构性主题仍成立，但短期风险回报转差",
                "evidence_1": "价格远高于均线并出现 MACD 背离",
                "evidence_2": "财报前情绪拥挤，失望风险放大",
                "risk_or_counterpoint": "AI 内存超级周期仍支撑保留部分仓位",
                "action_logic": "减仓锁定收益，而不是完全清仓",
            },
        }
        self.calls: list[dict[str, str]] = []

    def extract(
        self,
        *,
        market: str,
        symbol: str,
        latest_run_date: str,
        ta_report_date: str,
        advice_action: str,
        current_action: str,
        advice_summary: str,
        final_trade_decision: str,
    ) -> dict[str, object]:
        if "memory supercycle is intact" not in advice_summary:
            raise AssertionError("TradingAgents rationale was not passed to extractor")
        if advice_summary == "Current price is at or above target 1.":
            raise AssertionError("extractor received price-trigger reason only")
        self.calls.append(
            {
                "market": market,
                "symbol": symbol,
                "latest_run_date": latest_run_date,
                "ta_report_date": ta_report_date,
                "advice_action": advice_action,
                "current_action": current_action,
                "advice_summary": advice_summary,
                "final_trade_decision": final_trade_decision,
            }
        )
        return self.payload


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def raw_decision(final_trade_decision: str = "FINAL TRANSACTION PROPOSAL: HOLD") -> str:
    return json.dumps(
        {"state": {"final_trade_decision": final_trade_decision}},
        ensure_ascii=False,
    )


def advice_row(**overrides: str) -> dict[str, str]:
    row = {
        "run_date": "2026-06-23",
        "symbol": "DRAM",
        "market": "US",
        "asset_class": "etf",
        "portfolio_weight_hkd": "7.11%",
        "risk_flag": "normal",
        "source": "tradingagents",
        "advice_action": "Underweight",
        "advice_summary": (
            "评级：Underweight\n"
            "操作计划：Trim current exposure.\n"
            "理由：The memory supercycle is intact, but price is extended and "
            "MACD divergence raises event risk."
        ),
        "raw_decision": raw_decision("Rating: Underweight because price is extended."),
        "status": "ok",
        "error": "",
        "source_status": "fallback",
        "fallback_reason": "Too Many Requests",
        "fallback_from_date": "2026-06-22",
    }
    row.update(overrides)
    return row


def plan_row(**overrides: str) -> dict[str, str]:
    row = {
        "run_date": "2026-06-23",
        "symbol": "DRAM",
        "market": "US",
        "source_status": "fallback",
        "fallback_reason": "Too Many Requests",
        "fallback_from_date": "2026-06-22",
        "rating": "Underweight",
        "entry_zone_low": "",
        "entry_zone_high": "",
        "add_price": "",
        "stop_loss": "70",
        "target_1": "76",
        "target_2": "",
        "max_weight": "",
        "catalyst": "",
        "time_horizon": "",
        "plan_text": "",
        "agent_reason": "TradingAgents建议减仓，理由是技术动能转弱、风险回报不利。",
        "agent_excerpt": "The memory supercycle is intact, but price is extended.",
        "status": "active",
        "error": "",
    }
    row.update(overrides)
    return row


def action_row(**overrides: str) -> dict[str, str]:
    row = {
        "run_date": "2026-06-23",
        "symbol": "DRAM",
        "market": "US",
        "futu_symbol": "US.DRAM",
        "action": "TRIM",
        "priority": "normal",
        "last_price": "80",
        "trigger_status": "target_1_hit",
        "suggested_quantity": "10",
        "suggested_notional": "800",
        "notional_currency": "USD",
        "current_quantity": "100",
        "current_weight": "7.11%",
        "avg_cost_price": "55",
        "target_max_weight": "5.00%",
        "cash_available": "1000",
        "limit_price": "80",
        "stop_price": "70",
        "post_trade_quantity": "90",
        "post_trade_weight": "5.00%",
        "post_trade_avg_cost": "55",
        "risk_to_stop": "10",
        "agent_reason": "TradingAgents建议减仓，理由是技术动能转弱、风险回报不利。",
        "agent_excerpt": "",
        "trigger_reason": "Current price is at or above target 1.",
        "reason": "Current price is at or above target 1.",
        "source_plan": "trading_plan.csv",
        "status": "ready",
        "error": "",
    }
    row.update(overrides)
    return row


def test_paths_are_market_scoped(tmp_path: Path) -> None:
    assert tradingagents_summary_run_path(tmp_path, "2026-06-23", "US") == (
        tmp_path / "runs" / "2026-06-23" / "US" / "tradingagents_summary.json"
    )
    assert tradingagents_summary_latest_path(tmp_path, "US") == (
        tmp_path / "latest" / "US" / "tradingagents_summary.json"
    )


def test_generate_summary_uses_fallback_date_and_fixed_fields(tmp_path: Path) -> None:
    advice_path = tmp_path / "data" / "latest" / "US" / "trading_advice.csv"
    plan_path = tmp_path / "data" / "latest" / "US" / "trading_plan.csv"
    actions_path = tmp_path / "data" / "latest" / "US" / "trade_actions.csv"
    write_csv(advice_path, ADVICE_FIELDS, [advice_row()])
    write_csv(plan_path, TRADING_PLAN_FIELDNAMES, [plan_row()])
    write_csv(actions_path, list(TRADE_ACTION_FIELDNAMES), [action_row()])

    extractor = FakeExtractor()
    result = generate_tradingagents_summary(
        advice_path=advice_path,
        plan_path=plan_path,
        actions_path=actions_path,
        data_dir=tmp_path / "data",
        run_date="2026-06-23",
        market="US",
        extractor=extractor,
        update_latest=True,
    )

    payload = load_tradingagents_summary_cache(result.latest_path)
    record = payload["records"][0]
    assert payload["schema_version"] == TRADINGAGENTS_SUMMARY_SCHEMA_VERSION
    assert payload["latest_run_date"] == "2026-06-23"
    assert record["schema_version"] == TRADINGAGENTS_SUMMARY_SCHEMA_VERSION
    assert all(isinstance(record[field], str) for field in DISPLAY_FIELDS)
    assert record["latest_run_date"] == "2026-06-23"
    assert record["ta_report_date"] == "2026-06-22"
    assert record["ta_view"] == "低配"
    assert record["current_action"] == "减仓"
    assert result.records == 1
    assert result.extracted == 1
    call = extractor.calls[0]
    assert "memory supercycle is intact" in call["advice_summary"]
    assert call["advice_summary"] != action_row()["reason"]
    assert call["final_trade_decision"].startswith("Rating: Underweight")


def test_validate_rejects_price_trigger_only_reason() -> None:
    record = {
        "schema_version": TRADINGAGENTS_SUMMARY_SCHEMA_VERSION,
        "market": "US",
        "symbol": "DRAM",
        "latest_run_date": "2026-06-23",
        "ta_report_date": "2026-06-22",
        "ta_view": "低配",
        "current_action": "减仓",
        "core_reason": "当前价格已达到或高于第一目标价。",
        "reason_fields": build_missing_reason_fields(),
        "source_hash": "sha256:" + "a" * 64,
        "error": "",
    }

    with pytest.raises(ValueError, match="price trigger"):
        validate_tradingagents_summary_record(record)


def test_failed_llm_keeps_all_display_fields(tmp_path: Path) -> None:
    advice_path = tmp_path / "data" / "latest" / "US" / "trading_advice.csv"
    plan_path = tmp_path / "data" / "latest" / "US" / "trading_plan.csv"
    actions_path = tmp_path / "data" / "latest" / "US" / "trade_actions.csv"
    write_csv(advice_path, ADVICE_FIELDS, [advice_row()])
    write_csv(plan_path, TRADING_PLAN_FIELDNAMES, [plan_row()])
    write_csv(actions_path, list(TRADE_ACTION_FIELDNAMES), [action_row()])

    class BrokenExtractor:
        def extract(self, **kwargs: str) -> dict[str, object]:
            raise ValueError("bad json")

    result = generate_tradingagents_summary(
        advice_path=advice_path,
        plan_path=plan_path,
        actions_path=actions_path,
        data_dir=tmp_path / "data",
        run_date="2026-06-23",
        market="US",
        extractor=BrokenExtractor(),
        update_latest=False,
    )

    payload = load_tradingagents_summary_cache(result.run_path)
    record = payload["records"][0]
    assert all(field in record for field in DISPLAY_FIELDS)
    assert record["ta_view"] == "低配"
    assert record["current_action"] == "减仓"
    assert record["core_reason"].startswith("TradingAgents建议减仓")
    assert record["ta_report_date"] == "2026-06-22"
    assert record["latest_run_date"] == "2026-06-23"
    assert record["error"] == "bad json"
