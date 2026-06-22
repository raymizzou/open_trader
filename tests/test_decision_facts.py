from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from open_trader.decision_facts import (
    DECISION_FACTS_SCHEMA_VERSION,
    KLINE_FIELDS,
    NEWS_SENTIMENT_FIELDS,
    MISSING_VALUE,
    DecisionFactsExtractor,
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
