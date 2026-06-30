from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

import open_trader.tradingagents_summary as tradingagents_summary_module
from open_trader.trade_actions import TRADE_ACTION_FIELDNAMES
from open_trader.trading_plan import TRADING_PLAN_FIELDNAMES
from open_trader.tradingagents_summary import (
    MISSING_VALUE,
    REASON_FIELD_NAMES,
    TRADINGAGENTS_SUMMARY_SCHEMA_VERSION,
    ActionSummarySource,
    AdviceSummarySource,
    LLMTradingAgentsSummaryExtractor,
    OpenAITextClient,
    PlanSummarySource,
    build_missing_reason_fields,
    generate_tradingagents_summary,
    index_tradingagents_summary_by_market_symbol,
    load_action_summary_sources,
    load_advice_summary_sources,
    load_plan_summary_sources,
    load_tradingagents_summary_cache,
    normalize_current_action,
    normalize_ta_view,
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


def write_raw_csv(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


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


def valid_llm_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
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
    payload.update(overrides)
    return payload


def test_paths_are_market_scoped(tmp_path: Path) -> None:
    assert tradingagents_summary_run_path(tmp_path, "2026-06-23", "US") == (
        tmp_path / "runs" / "2026-06-23" / "US" / "tradingagents_summary.json"
    )
    assert tradingagents_summary_latest_path(tmp_path, "US") == (
        tmp_path / "latest" / "US" / "tradingagents_summary.json"
    )


def test_public_helpers_load_and_index_sources(tmp_path: Path) -> None:
    advice_path = tmp_path / "trading_advice.csv"
    plan_path = tmp_path / "trading_plan.csv"
    actions_path = tmp_path / "trade_actions.csv"
    write_csv(advice_path, ADVICE_FIELDS, [advice_row()])
    write_csv(plan_path, TRADING_PLAN_FIELDNAMES, [plan_row()])
    write_csv(actions_path, list(TRADE_ACTION_FIELDNAMES), [action_row()])

    advice_sources = load_advice_summary_sources(advice_path)
    plan_sources = load_plan_summary_sources(plan_path)
    action_sources = load_action_summary_sources(actions_path)

    assert REASON_FIELD_NAMES == (
        "main_judgment",
        "evidence_1",
        "evidence_2",
        "risk_or_counterpoint",
        "action_logic",
    )
    assert isinstance(advice_sources[0], AdviceSummarySource)
    assert isinstance(plan_sources[0], PlanSummarySource)
    assert isinstance(action_sources[0], ActionSummarySource)
    assert advice_sources[0].symbol == "DRAM"
    assert plan_sources[0].agent_reason.startswith("TradingAgents建议减仓")
    assert action_sources[0].current_action == "减仓"
    assert normalize_ta_view("Underweight") == "低配"
    assert normalize_current_action("TRIM") == "减仓"

    indexed = index_tradingagents_summary_by_market_symbol(
        {
            "records": [
                {
                    "market": "US",
                    "symbol": "DRAM",
                    "core_reason": "结构性主题仍成立，但短期风险回报转差。",
                }
            ]
        }
    )
    assert indexed[("US", "DRAM")]["core_reason"].startswith("结构性主题")


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        ("Underweight", "低配"),
        ("UNDERWEIGHT", "低配"),
        ("under_weight", "低配"),
        ("over-weight", "超配"),
        ("Buy", "买入"),
        ("hold", "持有"),
        ("低配", "低配"),
        ("unknown", MISSING_VALUE),
    ],
)
def test_normalize_ta_view_variants(raw_value: str, expected: str) -> None:
    assert normalize_ta_view(raw_value) == expected


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        ("trim", "减仓"),
        ("TRIM", "减仓"),
        ("sell-stop", "止损卖出"),
        ("sell stop", "止损卖出"),
        ("take_profit", "止盈"),
        ("Review", "人工复核"),
        ("持有", "持有"),
        ("unknown", MISSING_VALUE),
    ],
)
def test_normalize_current_action_variants(raw_value: str, expected: str) -> None:
    assert normalize_current_action(raw_value) == expected


@pytest.mark.parametrize(
    ("loader_name", "fieldnames", "row", "missing_column", "match"),
    [
        (
            "load_advice_summary_sources",
            ADVICE_FIELDS,
            advice_row(),
            "raw_decision",
            "advice CSV missing required column\\(s\\): raw_decision",
        ),
        (
            "load_plan_summary_sources",
            TRADING_PLAN_FIELDNAMES,
            plan_row(),
            "agent_excerpt",
            "plan CSV missing required column\\(s\\): agent_excerpt",
        ),
        (
            "load_action_summary_sources",
            list(TRADE_ACTION_FIELDNAMES),
            action_row(),
            "agent_reason",
            "action CSV missing required column\\(s\\): agent_reason",
        ),
    ],
)
def test_summary_source_loaders_reject_missing_required_columns(
    tmp_path: Path,
    loader_name: str,
    fieldnames: list[str],
    row: dict[str, str],
    missing_column: str,
    match: str,
) -> None:
    path = tmp_path / f"{loader_name}.csv"
    reduced_fields = [field for field in fieldnames if field != missing_column]
    write_csv(path, reduced_fields, [{field: value for field, value in row.items() if field != missing_column}])

    loader = getattr(tradingagents_summary_module, loader_name)
    with pytest.raises(ValueError, match=match):
        loader(path)


@pytest.mark.parametrize(
    ("content", "match"),
    [
        (
            "run_date,symbol,symbol,market,advice_action,advice_summary,raw_decision\n"
            "2026-06-23,DRAM,DRAM,US,Underweight,summary,{}\n",
            "advice CSV has duplicate header\\(s\\): symbol",
        ),
        (
            "run_date,,symbol,market,advice_action,advice_summary,raw_decision\n"
            "2026-06-23,,DRAM,US,Underweight,summary,{}\n",
            "advice CSV has blank header",
        ),
        (
            "run_date,symbol,market,advice_action,advice_summary,raw_decision\n"
            "2026-06-23,DRAM,US,Underweight,summary,{},extra\n",
            "advice CSV row 2 has extra cell\\(s\\)",
        ),
    ],
)
def test_advice_loader_rejects_malformed_csv_schema(
    tmp_path: Path,
    content: str,
    match: str,
) -> None:
    path = tmp_path / "advice.csv"
    write_raw_csv(path, content)

    with pytest.raises(ValueError, match=match):
        load_advice_summary_sources(path)


def test_summary_source_loaders_tolerate_additive_columns(tmp_path: Path) -> None:
    advice_path = tmp_path / "advice.csv"
    plan_path = tmp_path / "plan.csv"
    actions_path = tmp_path / "actions.csv"
    write_csv(advice_path, [*ADVICE_FIELDS, "extra_context"], [{**advice_row(), "extra_context": "ok"}])
    write_csv(plan_path, [*TRADING_PLAN_FIELDNAMES, "extra_context"], [{**plan_row(), "extra_context": "ok"}])
    write_csv(
        actions_path,
        [*list(TRADE_ACTION_FIELDNAMES), "extra_context"],
        [{**action_row(), "extra_context": "ok"}],
    )

    assert load_advice_summary_sources(advice_path)[0].symbol == "DRAM"
    assert load_plan_summary_sources(plan_path)[0].symbol == "DRAM"
    assert load_action_summary_sources(actions_path)[0].symbol == "DRAM"


class FakeTextClient:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[dict[str, object]] = []

    def create(self, *, messages: list[dict[str, str]], temperature: float) -> str:
        self.calls.append({"messages": messages, "temperature": temperature})
        return self.content


def test_llm_extractor_sends_prompt_payload_and_parses_valid_json() -> None:
    client = FakeTextClient(json.dumps(valid_llm_payload(), ensure_ascii=False))
    extractor = LLMTradingAgentsSummaryExtractor(client=client)

    result = extractor.extract(
        market="US",
        symbol="DRAM",
        latest_run_date="2026-06-23",
        ta_report_date="2026-06-22",
        advice_action="Underweight",
        current_action="减仓",
        advice_summary="完整 TradingAgents advice summary",
        final_trade_decision="final decision text",
    )

    assert result["schema_version"] == TRADINGAGENTS_SUMMARY_SCHEMA_VERSION
    assert result["core_reason"] != MISSING_VALUE
    assert client.calls[0]["temperature"] == 0
    messages = client.calls[0]["messages"]
    user_payload = json.loads(messages[1]["content"])
    assert user_payload["advice_summary"] == "完整 TradingAgents advice summary"
    assert user_payload["final_trade_decision"] == "final decision text"


@pytest.mark.parametrize(
    ("content", "match"),
    [
        ("not json", "must be valid JSON"),
        ("[]", "must be a JSON object"),
        (
            json.dumps(valid_llm_payload(schema_version="wrong"), ensure_ascii=False),
            "schema_version is invalid",
        ),
        (
            json.dumps(
                valid_llm_payload(reason_fields={"main_judgment": "结构性主题仍成立"}),
                ensure_ascii=False,
            ),
            "reason_fields are invalid",
        ),
        (
            json.dumps(
                valid_llm_payload(
                    reason_fields={
                        "main_judgment": "not Chinese",
                        "evidence_1": "价格远高于均线",
                        "evidence_2": "财报前情绪拥挤",
                        "risk_or_counterpoint": "仍有结构性支撑",
                        "action_logic": "减仓锁定收益",
                    }
                ),
                ensure_ascii=False,
            ),
            "must be Chinese",
        ),
    ],
)
def test_llm_extractor_rejects_invalid_responses(content: str, match: str) -> None:
    extractor = LLMTradingAgentsSummaryExtractor(client=FakeTextClient(content))

    with pytest.raises(ValueError, match=match):
        extractor.extract(
            market="US",
            symbol="DRAM",
            latest_run_date="2026-06-23",
            ta_report_date="2026-06-22",
            advice_action="Underweight",
            current_action="减仓",
            advice_summary="完整 TradingAgents advice summary",
            final_trade_decision="final decision text",
        )


def test_openai_text_client_uses_json_response_format(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class FakeCompletions:
        def create(self, **kwargs: object) -> object:
            captured.update(kwargs)

            class Message:
                content = '{"ok": true}'

            class Choice:
                message = Message()

            class Response:
                choices = [Choice()]

            return Response()

    class FakeOpenAI:
        def __init__(
            self,
            *,
            api_key: str | None,
            base_url: str,
            timeout: float,
        ) -> None:
            captured["api_key"] = api_key
            captured["base_url"] = base_url
            captured["client_timeout"] = timeout
            self.chat = type(
                "Chat",
                (),
                {"completions": FakeCompletions()},
            )()

    monkeypatch.setattr(tradingagents_summary_module, "OpenAI", FakeOpenAI)

    client = OpenAITextClient(
        api_key="test-key",
        base_url="https://example.test",
        model="model-x",
        timeout_seconds=12.5,
    )
    content = client.create(messages=[{"role": "user", "content": "hi"}], temperature=0)

    assert content == '{"ok": true}'
    assert captured["api_key"] == "test-key"
    assert captured["base_url"] == "https://example.test"
    assert captured["client_timeout"] == 12.5
    assert captured["model"] == "model-x"
    assert captured["messages"] == [{"role": "user", "content": "hi"}]
    assert captured["temperature"] == 0
    assert captured["response_format"] == {"type": "json_object"}
    assert captured["timeout"] == 12.5


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

    record["core_reason"] = "达到第一目标价"
    with pytest.raises(ValueError, match="price trigger"):
        validate_tradingagents_summary_record(record)


def test_validate_rejects_unexpected_top_level_fields() -> None:
    record = {
        "schema_version": TRADINGAGENTS_SUMMARY_SCHEMA_VERSION,
        "market": "US",
        "symbol": "DRAM",
        "latest_run_date": "2026-06-23",
        "ta_report_date": "2026-06-22",
        "ta_view": "低配",
        "current_action": "减仓",
        "core_reason": "结构性主题仍成立，但短期风险回报转差。",
        "reason_fields": build_missing_reason_fields(),
        "source_hash": "sha256:" + "a" * 64,
        "error": "",
        "source_status": "fallback",
    }

    with pytest.raises(ValueError, match="unexpected"):
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


def test_failed_llm_uses_missing_when_fallback_reason_is_english(tmp_path: Path) -> None:
    advice_path = tmp_path / "data" / "latest" / "US" / "trading_advice.csv"
    plan_path = tmp_path / "data" / "latest" / "US" / "trading_plan.csv"
    actions_path = tmp_path / "data" / "latest" / "US" / "trade_actions.csv"
    write_csv(advice_path, ADVICE_FIELDS, [advice_row()])
    write_csv(
        plan_path,
        TRADING_PLAN_FIELDNAMES,
        [
            plan_row(
                agent_reason="The memory supercycle is intact, but price is extended.",
                plan_text="Current price is at or above target 1.",
                agent_excerpt="Current price is at or above target 1.",
            )
        ],
    )
    write_csv(
        actions_path,
        list(TRADE_ACTION_FIELDNAMES),
        [
            action_row(
                agent_reason="Current price is at or above target 1.",
                agent_excerpt="Current price is at or above target 1.",
            )
        ],
    )

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

    record = load_tradingagents_summary_cache(result.run_path)["records"][0]
    assert all(field in record for field in DISPLAY_FIELDS)
    assert record["core_reason"] == "缺失"
    assert record["ta_view"] == "低配"
    assert record["current_action"] == "减仓"
    assert record["error"] == "bad json"
