from __future__ import annotations

import json

import pytest

from open_trader.decision_facts import KLINE_FIELDS, NEWS_SENTIMENT_FIELDS, extract_decision_sources
from open_trader.decision_source_availability import evaluate_required_sources
from open_trader.technical_facts import source_hash


RUN_DATE = "2026-06-19"
RAW_DECISION = json.dumps(
    {
        "state": {
            "market_report": "market report",
            "sentiment_report": "sentiment report",
            "news_report": "news report",
        }
    }
)


def _records() -> dict[str, dict[tuple[str, str], dict[str, object]]]:
    sources = extract_decision_sources(RAW_DECISION)
    key = ("US", "MSFT")
    return {
        "technical_records": {
            key: {
                "run_date": RUN_DATE,
                "source_hash": source_hash("market report"),
                "extraction_status": "ok",
                "facts": {"timeframes": [{"timeframe": "daily"}]},
                "freshness": {"status": "current"},
            }
        },
        "decision_records": {
            key: {
                "run_date": RUN_DATE,
                "kline": {
                    "status": "ok",
                    "source_hash": sources.kline_hash,
                    "fields": {field: "值" for field in KLINE_FIELDS},
                },
                "news_sentiment": {
                    "status": "ok",
                    "source_hash": sources.news_sentiment_hash,
                    "fields": {field: "值" for field in NEWS_SENTIMENT_FIELDS},
                },
            }
        },
        "tradingagents_records": {
            key: {
                "schema_version": "open_trader.tradingagents_summary.v1",
                "market": "US",
                "symbol": "MSFT",
                "latest_run_date": RUN_DATE,
                "ta_report_date": RUN_DATE,
                "ta_view": "看多",
                "current_action": "持有",
                "core_reason": "基本面稳健",
                "reason_fields": {
                    "main_judgment": "基本面稳健",
                    "evidence_1": "盈利增长",
                    "evidence_2": "现金流充足",
                    "risk_or_counterpoint": "估值偏高",
                    "action_logic": "继续持有",
                },
                "source_hash": "sha256:" + "a" * 64,
                "error": "",
            }
        },
        "futu_records": {
            key: {
                "run_date": RUN_DATE,
                "news_sentiment": {"status": "ok"},
                "technical_anomaly": {"status": "ok"},
                "capital_anomaly": {"status": "partial"},
                "derivatives_anomaly": {"status": "ok"},
            }
        },
    }


def test_evaluate_required_sources_accepts_complete_current_records() -> None:
    assert evaluate_required_sources(
        advice_rows=[
            {"run_date": RUN_DATE, "market": "US", "symbol": "MSFT", "raw_decision": RAW_DECISION}
        ],
        **_records(),
    ) == []


def test_evaluate_required_sources_accepts_explicit_futu_unsupported_module() -> None:
    records = _records()
    records["futu_records"][("US", "MSFT")]["technical_anomaly"] = {
        "status": "not_applicable",
        "summary": "富途接口不支持技术异动：US.MSFT",
    }

    assert evaluate_required_sources(
        advice_rows=[
            {"run_date": RUN_DATE, "market": "US", "symbol": "MSFT", "raw_decision": RAW_DECISION}
        ],
        **records,
    ) == []

    records["futu_records"][("US", "MSFT")]["technical_anomaly"]["status"] = "error"
    assert evaluate_required_sources(
        advice_rows=[
            {"run_date": RUN_DATE, "market": "US", "symbol": "MSFT", "raw_decision": RAW_DECISION}
        ],
        **records,
    ) == []


@pytest.mark.parametrize("status", ["not_applicable", "error"])
def test_evaluate_required_sources_rejects_unsupported_news_sentiment(status: str) -> None:
    records = _records()
    records["futu_records"][("US", "MSFT")]["news_sentiment"] = {
        "status": status,
        "summary": "富途接口不支持新闻舆情：US.MSFT",
    }

    failures = evaluate_required_sources(
        advice_rows=[
            {"run_date": RUN_DATE, "market": "US", "symbol": "MSFT", "raw_decision": RAW_DECISION}
        ],
        **records,
    )

    assert [failure.source for failure in failures] == ["futu_skill_facts.news_sentiment"]


@pytest.mark.parametrize(
    ("source", "mutate", "expected_error"),
    [
        ("tradingagents_summary", lambda records: records["tradingagents_records"].clear(), "数据未生成"),
        ("technical_facts", lambda records: records["technical_records"][('US', 'MSFT')].update(error="技术抽取失败", extraction_status="extraction_failed"), "技术抽取失败"),
        ("decision_facts.kline", lambda records: records["decision_records"][('US', 'MSFT')]["kline"].update(error="K线缺失", status="error"), "K线缺失"),
        ("decision_facts.news_sentiment", lambda records: records["decision_records"][('US', 'MSFT')]["news_sentiment"].update(blocking_reason="新闻过期", status="missing_source"), "新闻过期"),
        ("futu_skill_facts.news_sentiment", lambda records: records["futu_records"][('US', 'MSFT')]["news_sentiment"].update(error="Futu新闻失败", status="error"), "Futu新闻失败"),
        ("futu_skill_facts.technical_anomaly", lambda records: records["futu_records"][('US', 'MSFT')]["technical_anomaly"].update(status="missing"), "missing"),
        ("futu_skill_facts.capital_anomaly", lambda records: records["futu_records"][('US', 'MSFT')]["capital_anomaly"].update(blocking_reason="资金数据缺失", status="error"), "资金数据缺失"),
        ("futu_skill_facts.derivatives_anomaly", lambda records: records["futu_records"][('US', 'MSFT')]["derivatives_anomaly"].update(error="衍生品失败", status="error"), "衍生品失败"),
    ],
)
def test_evaluate_required_sources_reports_each_canonical_source(
    source: str,
    mutate: object,
    expected_error: str,
) -> None:
    records = _records()
    mutate(records)  # type: ignore[operator]

    assert [failure.__dict__ for failure in evaluate_required_sources(
        advice_rows=[
            {"run_date": RUN_DATE, "market": "US", "symbol": "MSFT", "raw_decision": RAW_DECISION}
        ],
        **records,
    )] == [{"market": "US", "symbol": "MSFT", "source": source, "error": expected_error}]


def test_evaluate_required_sources_uses_canonical_source_names() -> None:
    records = _records()
    records["technical_records"].clear()
    records["decision_records"].clear()
    records["tradingagents_records"].clear()
    records["futu_records"].clear()

    failures = evaluate_required_sources(
        advice_rows=[
            {"run_date": RUN_DATE, "market": "US", "symbol": "MSFT", "raw_decision": RAW_DECISION}
        ],
        **records,
    )

    assert [failure.source for failure in failures] == [
        "tradingagents_summary",
        "technical_facts",
        "decision_facts.kline",
        "decision_facts.news_sentiment",
        "futu_skill_facts.news_sentiment",
        "futu_skill_facts.technical_anomaly",
        "futu_skill_facts.capital_anomaly",
        "futu_skill_facts.derivatives_anomaly",
    ]
