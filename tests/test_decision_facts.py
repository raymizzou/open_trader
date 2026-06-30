from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from open_trader import decision_facts as decision_facts_module
from open_trader.decision_facts import (
    DECISION_FACTS_SCHEMA_VERSION,
    KLINE_FIELDS,
    NEWS_SENTIMENT_FIELDS,
    MISSING_VALUE,
    DecisionFactsExtractor,
    LLMDecisionFactsExtractor,
    OpenAITextClient,
    build_missing_fields,
    decision_facts_latest_path,
    decision_facts_run_path,
    extract_decision_sources,
    generate_decision_facts,
    load_decision_facts_cache,
    validate_decision_facts_record,
)
from open_trader.technical_facts import source_hash


ADVICE_FIELDNAMES = [
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


class FakeExtractor:
    def __init__(self, payload: dict[str, object] | None = None) -> None:
        self.payload = payload or {
            "schema_version": "open_trader.decision_facts.v1",
            "kline": {
                "status": "ok",
                "fields": {
                    "trend": "过热拉升",
                    "position": "高于主要均线",
                    "momentum": "RSI 高位，MACD 偏强",
                    "key_levels": "支撑 580，压力缺失",
                    "risk": "超买风险",
                },
            },
            "news_sentiment": {
                "status": "ok",
                "fields": {
                    "direction": "偏多",
                    "change": "较上次转强",
                    "catalyst": "AI 基建需求",
                    "risk": "估值过高",
                    "attention": "关注度升高",
                },
            },
        }
        self.calls: list[dict[str, str]] = []

    def extract(
        self,
        *,
        market: str,
        symbol: str,
        run_date: str,
        kline_source: str,
        news_sentiment_source: str,
    ) -> dict[str, object]:
        self.calls.append(
            {
                "market": market,
                "symbol": symbol,
                "run_date": run_date,
                "kline_source": kline_source,
                "news_sentiment_source": news_sentiment_source,
            }
        )
        return self.payload


def write_advice(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ADVICE_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def raw_decision(
    *,
    market_report: str = "K line source",
    sentiment_report: str = "Sentiment source",
    news_report: str = "News source",
) -> str:
    return json.dumps(
        {
            "state": {
                "market_report": market_report,
                "sentiment_report": sentiment_report,
                "news_report": news_report,
            }
        },
        ensure_ascii=False,
    )


def advice_row(symbol: str = "SOXX", raw: str | None = None) -> dict[str, str]:
    return {
        "run_date": "2026-06-22",
        "symbol": symbol,
        "market": "US",
        "asset_class": "etf",
        "portfolio_weight_hkd": "10.0%",
        "risk_flag": "",
        "source": "tradingagents",
        "advice_action": "HOLD",
        "advice_summary": "",
        "raw_decision": raw if raw is not None else raw_decision(),
        "status": "ok",
        "error": "",
        "source_status": "ok",
        "fallback_reason": "",
        "fallback_from_date": "",
    }


def valid_decision_facts_record() -> dict[str, object]:
    return {
        "schema_version": DECISION_FACTS_SCHEMA_VERSION,
        "run_date": "2026-06-22",
        "market": "US",
        "symbol": "SOXX",
        "source_status": "ok",
        "kline": {
            "status": "ok",
            "source_hash": source_hash("kline"),
            "fields": build_missing_fields(KLINE_FIELDS),
        },
        "news_sentiment": {
            "status": "ok",
            "source_hash": source_hash("news"),
            "fields": build_missing_fields(NEWS_SENTIMENT_FIELDS),
        },
        "error": "",
    }


def test_extract_decision_sources_reads_tradingagents_state() -> None:
    sources = extract_decision_sources(
        raw_decision(
            market_report="technical report",
            sentiment_report="sentiment report",
            news_report="news report",
        )
    )

    assert sources.kline_source == "technical report"
    assert sources.news_sentiment_source == "## sentiment_report\n\nsentiment report\n\n## news_report\n\nnews report"
    assert sources.kline_hash == source_hash("technical report")
    assert sources.news_sentiment_hash == source_hash(
        "## sentiment_report\n\nsentiment report\n\n## news_report\n\nnews report"
    )


def test_build_missing_fields_uses_fixed_missing_value() -> None:
    assert build_missing_fields(KLINE_FIELDS) == {
        "trend": MISSING_VALUE,
        "position": MISSING_VALUE,
        "momentum": MISSING_VALUE,
        "key_levels": MISSING_VALUE,
        "risk": MISSING_VALUE,
    }
    assert build_missing_fields(NEWS_SENTIMENT_FIELDS)["direction"] == MISSING_VALUE


def test_openai_text_client_sets_sdk_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class FakeCompletions:
        def create(self, **kwargs: object) -> object:
            captured.update(kwargs)

            class Message:
                content = "{}"

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

    monkeypatch.setattr(decision_facts_module, "OpenAI", FakeOpenAI)

    client = OpenAITextClient(
        api_key="test-key",
        base_url="https://example.test",
        model="model-x",
        timeout_seconds=12.5,
    )
    content = client.create(messages=[{"role": "user", "content": "hi"}], temperature=0)

    assert content == "{}"
    assert captured["api_key"] == "test-key"
    assert captured["base_url"] == "https://example.test"
    assert captured["client_timeout"] == 12.5
    assert captured["timeout"] == 12.5


def test_validate_decision_facts_record_rejects_missing_fixed_field() -> None:
    record = valid_decision_facts_record()
    del record["kline"]["fields"]["trend"]

    with pytest.raises(ValueError, match="kline fields are invalid"):
        validate_decision_facts_record(record)


def test_validate_decision_facts_record_rejects_english_only_value() -> None:
    record = valid_decision_facts_record()
    record["news_sentiment"]["fields"]["direction"] = "Bullish retail sentiment"

    with pytest.raises(ValueError, match="field values must be Chinese or 缺失"):
        validate_decision_facts_record(record)


def test_validate_decision_facts_record_rejects_chinese_trading_instruction() -> None:
    record = valid_decision_facts_record()
    record["news_sentiment"]["fields"]["direction"] = "建议买入"

    with pytest.raises(ValueError, match="field values must not contain trading guidance"):
        validate_decision_facts_record(record)


@pytest.mark.parametrize(
    "value",
    [
        "建议买入",
        "建议卖出",
        "请下单",
        "自动执行",
        "加仓至五成",
        "减仓一半",
        "目标价 100 后买入",
        "止损价 90 自动卖出",
    ],
)
def test_validate_decision_facts_record_rejects_chinese_trading_instructions(
    value: str,
) -> None:
    record = valid_decision_facts_record()
    record["news_sentiment"]["fields"]["direction"] = value

    with pytest.raises(ValueError, match="field values must not contain trading guidance"):
        validate_decision_facts_record(record)


@pytest.mark.parametrize(
    "value",
    [
        "趋势偏强 buy 100 shares",
        "趋势偏强 place a buy order for 100 shares",
        "趋势偏强 add 100 shares",
        "趋势偏强 reduce position by half",
        "趋势偏强 trim half the position",
    ],
)
def test_validate_decision_facts_record_rejects_mixed_english_trading_prose(
    value: str,
) -> None:
    record = valid_decision_facts_record()
    record["kline"]["fields"]["trend"] = value

    with pytest.raises(ValueError, match="field values must not contain trading guidance"):
        validate_decision_facts_record(record)


@pytest.mark.parametrize(
    "value",
    [
        "RSI 高位，MACD 偏强",
        "AI 基建需求增强",
        "ETF 资金流入增加",
        "IPO 预期提升热度",
        "机构仓位拥挤带来波动风险",
        "期权仓位显示避险需求上升",
        "目标价上调带动关注度升高",
    ],
)
def test_validate_decision_facts_record_allows_neutral_facts(
    value: str,
) -> None:
    record = valid_decision_facts_record()
    record["kline"]["fields"]["momentum"] = value

    validate_decision_facts_record(record)


@pytest.mark.parametrize(
    "bad_hash",
    [
        None,
        "",
        "not-a-source-hash",
        "sha256:x",
        "sha256:" + ("g" * 64),
        "sha256:" + ("a" * 63),
    ],
)
def test_validate_decision_facts_record_rejects_ok_module_with_invalid_source_hash(
    bad_hash: object,
) -> None:
    record = valid_decision_facts_record()
    record["kline"]["source_hash"] = bad_hash
    record["news_sentiment"]["status"] = "missing_source"
    record["news_sentiment"]["source_hash"] = ""

    with pytest.raises(ValueError, match="kline source_hash is invalid"):
        validate_decision_facts_record(record)


def test_validate_decision_facts_record_rejects_unknown_status() -> None:
    record = valid_decision_facts_record()
    record["kline"]["status"] = "unexpected"
    record["news_sentiment"]["status"] = "missing_source"
    record["news_sentiment"]["source_hash"] = ""

    with pytest.raises(ValueError, match="kline status is invalid"):
        validate_decision_facts_record(record)


def test_validate_decision_facts_record_rejects_missing_top_level_field() -> None:
    record = valid_decision_facts_record()
    del record["symbol"]

    with pytest.raises(ValueError, match="decision facts symbol is missing"):
        validate_decision_facts_record(record)


def test_llm_decision_facts_extractor_accepts_hashless_module_payload() -> None:
    class FakeClient:
        def create(self, *, messages: list[dict[str, str]], temperature: float) -> str:
            assert temperature == 0
            user_payload = json.loads(messages[1]["content"])
            assert "kline_source_hash" not in user_payload
            assert "news_sentiment_source_hash" not in user_payload
            return json.dumps(
                {
                    "schema_version": DECISION_FACTS_SCHEMA_VERSION,
                    "kline": {
                        "status": "ok",
                        "fields": {
                            "trend": "趋势偏强",
                            "position": "位于均线上方",
                            "momentum": "动量改善",
                            "key_levels": "关键位缺失",
                            "risk": "波动风险",
                        },
                    },
                    "news_sentiment": {
                        "status": "ok",
                        "fields": {
                            "direction": "偏多",
                            "change": "情绪改善",
                            "catalyst": "需求预期改善",
                            "risk": "估值压力",
                            "attention": "关注度升高",
                        },
                    },
                },
                ensure_ascii=False,
            )

    payload = LLMDecisionFactsExtractor(client=FakeClient()).extract(
        market="US",
        symbol="SOXX",
        run_date="2026-06-22",
        kline_source="technical source",
        news_sentiment_source="news source",
    )

    assert payload["kline"]["fields"]["trend"] == "趋势偏强"
    assert "source_hash" not in payload["kline"]


def test_llm_decision_facts_extractor_strips_extra_keys() -> None:
    class FakeClient:
        def create(self, *, messages: list[dict[str, str]], temperature: float) -> str:
            return json.dumps(
                {
                    "schema_version": DECISION_FACTS_SCHEMA_VERSION,
                    "raw_english": "Buy now target price 100",
                    "kline": {
                        "status": "ok",
                        "raw_english": "Buy now target price 100",
                        "fields": {
                            "trend": "趋势偏强",
                            "position": "位于均线上方",
                            "momentum": "动量改善",
                            "key_levels": "关键位缺失",
                            "risk": "波动风险",
                        },
                    },
                    "news_sentiment": {
                        "status": "ok",
                        "raw_english": "Buy now target price 100",
                        "fields": {
                            "direction": "偏多",
                            "change": "情绪改善",
                            "catalyst": "需求预期改善",
                            "risk": "估值压力",
                            "attention": "关注度升高",
                        },
                    },
                },
                ensure_ascii=False,
            )

    payload = LLMDecisionFactsExtractor(client=FakeClient()).extract(
        market="US",
        symbol="SOXX",
        run_date="2026-06-22",
        kline_source="technical source",
        news_sentiment_source="news source",
    )

    assert set(payload) == {"schema_version", "kline", "news_sentiment"}
    assert set(payload["kline"]) == {"status", "fields"}
    assert set(payload["news_sentiment"]) == {"status", "fields"}


def test_llm_decision_facts_extractor_coerces_bad_module_and_keeps_good_module() -> None:
    class FakeClient:
        def create(self, *, messages: list[dict[str, str]], temperature: float) -> str:
            return json.dumps(
                {
                    "schema_version": DECISION_FACTS_SCHEMA_VERSION,
                    "kline": {
                        "status": "ok",
                        "fields": {
                            "trend": "趋势偏强 Buy now target price 100",
                            "position": "位于均线上方",
                            "momentum": "RSI 高位，MACD 偏强",
                            "key_levels": "关键位缺失",
                            "risk": "波动风险",
                        },
                    },
                    "news_sentiment": {
                        "status": "ok",
                        "fields": {
                            "direction": "偏多",
                            "change": "情绪改善",
                            "catalyst": "需求预期改善",
                            "risk": "估值压力",
                            "attention": "关注度升高",
                        },
                    },
                },
                ensure_ascii=False,
            )

    payload = LLMDecisionFactsExtractor(client=FakeClient()).extract(
        market="US",
        symbol="SOXX",
        run_date="2026-06-22",
        kline_source="technical source",
        news_sentiment_source="news source",
    )

    assert payload["kline"]["status"] == "error"
    assert payload["kline"]["fields"] == build_missing_fields(KLINE_FIELDS)
    assert payload["news_sentiment"]["status"] == "ok"
    assert payload["news_sentiment"]["fields"]["direction"] == "偏多"


def test_generate_decision_facts_writes_run_and_latest_artifacts(tmp_path: Path) -> None:
    advice_path = tmp_path / "data/latest/US/trading_advice.csv"
    write_advice(advice_path, [advice_row()])
    extractor = FakeExtractor()

    result = generate_decision_facts(
        advice_path=advice_path,
        data_dir=tmp_path / "data",
        run_date="2026-06-22",
        extractor=extractor,
        update_latest=True,
        market="US",
    )

    assert result.run_date == "2026-06-22"
    assert result.records == 1
    assert result.extracted == 1
    assert result.failed == 0
    assert result.run_path == tmp_path / "data/runs/2026-06-22/US/decision_facts.json"
    assert result.latest_path == tmp_path / "data/latest/US/decision_facts.json"
    cache = load_decision_facts_cache(result.latest_path)
    record = cache["records"][0]
    assert cache["schema_version"] == DECISION_FACTS_SCHEMA_VERSION
    assert record["kline"]["fields"]["trend"] == "过热拉升"
    assert record["news_sentiment"]["fields"]["direction"] == "偏多"
    assert record["kline"]["source_hash"] == source_hash("K line source")
    assert record["news_sentiment"]["source_hash"] == source_hash(
        "## sentiment_report\n\nSentiment source\n\n## news_report\n\nNews source"
    )
    assert extractor.calls[0]["symbol"] == "SOXX"


def test_generate_decision_facts_missing_sources_use_missing_values(tmp_path: Path) -> None:
    advice_path = tmp_path / "data/latest/US/trading_advice.csv"
    write_advice(
        advice_path,
        [advice_row(raw=json.dumps({"state": {}}, ensure_ascii=False))],
    )

    result = generate_decision_facts(
        advice_path=advice_path,
        data_dir=tmp_path / "data",
        run_date="2026-06-22",
        extractor=FakeExtractor(),
        update_latest=False,
        market="US",
    )

    record = load_decision_facts_cache(result.run_path)["records"][0]
    assert record["kline"]["status"] == "missing_source"
    assert set(record["kline"]["fields"].values()) == {MISSING_VALUE}
    assert record["news_sentiment"]["status"] == "missing_source"
    assert set(record["news_sentiment"]["fields"].values()) == {MISSING_VALUE}


def test_generate_decision_facts_does_not_persist_extra_module_keys(
    tmp_path: Path,
) -> None:
    advice_path = tmp_path / "data/latest/US/trading_advice.csv"
    write_advice(advice_path, [advice_row()])
    extractor = FakeExtractor(
        {
            "schema_version": DECISION_FACTS_SCHEMA_VERSION,
            "kline": {
                "status": "ok",
                "raw_english": "Buy now target price 100",
                "fields": {
                    "trend": "趋势偏强",
                    "position": "位于均线上方",
                    "momentum": "动量改善",
                    "key_levels": "关键位缺失",
                    "risk": "波动风险",
                },
            },
            "news_sentiment": {
                "status": "ok",
                "raw_english": "Buy now target price 100",
                "fields": {
                    "direction": "偏多",
                    "change": "情绪改善",
                    "catalyst": "需求预期改善",
                    "risk": "估值压力",
                    "attention": "关注度升高",
                },
            },
        }
    )

    result = generate_decision_facts(
        advice_path=advice_path,
        data_dir=tmp_path / "data",
        run_date="2026-06-22",
        extractor=extractor,
        update_latest=False,
        market="US",
    )

    record = load_decision_facts_cache(result.run_path)["records"][0]
    assert set(record["kline"]) == {"status", "source_hash", "fields"}
    assert set(record["news_sentiment"]) == {"status", "source_hash", "fields"}


def test_generate_decision_facts_malformed_kline_keeps_valid_news_sentiment(
    tmp_path: Path,
) -> None:
    advice_path = tmp_path / "data/latest/US/trading_advice.csv"
    write_advice(advice_path, [advice_row()])
    extractor = FakeExtractor(
        {
            "schema_version": DECISION_FACTS_SCHEMA_VERSION,
            "kline": {"status": "ok"},
            "news_sentiment": {
                "status": "ok",
                "fields": {
                    "direction": "偏多",
                    "change": "较上次转强",
                    "catalyst": "AI 基建需求",
                    "risk": "估值过高",
                    "attention": "关注度升高",
                },
            },
        }
    )

    result = generate_decision_facts(
        advice_path=advice_path,
        data_dir=tmp_path / "data",
        run_date="2026-06-22",
        extractor=extractor,
        update_latest=False,
        market="US",
    )

    record = load_decision_facts_cache(result.run_path)["records"][0]
    assert result.extracted == 0
    assert result.failed == 1
    assert record["kline"]["status"] == "error"
    assert set(record["kline"]["fields"].values()) == {MISSING_VALUE}
    assert record["news_sentiment"]["status"] == "ok"
    assert record["news_sentiment"]["fields"]["direction"] == "偏多"
    assert "kline" in record["error"]


def test_generate_decision_facts_extractor_failure_status_counts_as_failed(
    tmp_path: Path,
) -> None:
    advice_path = tmp_path / "data/latest/US/trading_advice.csv"
    write_advice(advice_path, [advice_row()])
    extractor = FakeExtractor(
        {
            "schema_version": DECISION_FACTS_SCHEMA_VERSION,
            "kline": {
                "status": "extraction_failed",
                "fields": build_missing_fields(KLINE_FIELDS),
            },
            "news_sentiment": {
                "status": "ok",
                "fields": {
                    "direction": "偏多",
                    "change": "较上次转强",
                    "catalyst": "AI 基建需求",
                    "risk": "估值过高",
                    "attention": "关注度升高",
                },
            },
        }
    )

    result = generate_decision_facts(
        advice_path=advice_path,
        data_dir=tmp_path / "data",
        run_date="2026-06-22",
        extractor=extractor,
        update_latest=False,
        market="US",
    )

    record = load_decision_facts_cache(result.run_path)["records"][0]
    assert result.extracted == 0
    assert result.failed == 1
    assert record["kline"]["status"] == "extraction_failed"
    assert set(record["kline"]["fields"].values()) == {MISSING_VALUE}
    assert record["news_sentiment"]["status"] == "ok"
    assert record["news_sentiment"]["fields"]["direction"] == "偏多"
    assert "kline" in record["error"]
