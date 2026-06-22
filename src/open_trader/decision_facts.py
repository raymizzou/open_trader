from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from open_trader.technical_facts import source_hash


DECISION_FACTS_SCHEMA_VERSION = "open_trader.decision_facts.v1"
MISSING_VALUE = "缺失"
KLINE_FIELDS = ("trend", "position", "momentum", "key_levels", "risk")
NEWS_SENTIMENT_FIELDS = ("direction", "change", "catalyst", "risk", "attention")


@dataclass(frozen=True)
class DecisionSources:
    kline_source: str
    news_sentiment_source: str
    kline_hash: str
    news_sentiment_hash: str


@dataclass(frozen=True)
class AdviceSource:
    run_date: str
    market: str
    symbol: str
    source_status: str
    kline_source: str
    news_sentiment_source: str
    kline_hash: str
    news_sentiment_hash: str


@dataclass(frozen=True)
class DecisionFactsResult:
    run_date: str
    records: int
    extracted: int
    failed: int
    reused: int
    run_path: Path
    latest_path: Path


class DecisionFactsExtractor(Protocol):
    def extract(
        self,
        *,
        market: str,
        symbol: str,
        run_date: str,
        kline_source: str,
        news_sentiment_source: str,
    ) -> dict[str, object]:
        ...


def extract_decision_sources(raw_decision: str) -> DecisionSources:
    try:
        payload = json.loads(raw_decision or "{}")
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    state = payload.get("state")
    if not isinstance(state, dict):
        state = {}

    kline_source = _state_string(state, "market_report")
    sentiment_report = _state_string(state, "sentiment_report")
    news_report = _state_string(state, "news_report")
    news_sentiment_source = _combine_news_sentiment_sources(
        sentiment_report=sentiment_report,
        news_report=news_report,
    )

    return DecisionSources(
        kline_source=kline_source,
        news_sentiment_source=news_sentiment_source,
        kline_hash=source_hash(kline_source) if kline_source else "",
        news_sentiment_hash=source_hash(news_sentiment_source) if news_sentiment_source else "",
    )


def build_missing_fields(fields: tuple[str, ...]) -> dict[str, str]:
    return {field: MISSING_VALUE for field in fields}


def decision_facts_run_path(*args: object, **kwargs: object) -> Path:
    raise NotImplementedError("decision facts run path is implemented in a later task")


def decision_facts_latest_path(*args: object, **kwargs: object) -> Path:
    raise NotImplementedError("decision facts latest path is implemented in a later task")


def generate_decision_facts(*args: object, **kwargs: object) -> DecisionFactsResult:
    raise NotImplementedError("decision facts generation is implemented in a later task")


def load_decision_facts_cache(*args: object, **kwargs: object) -> dict[str, Any]:
    return {}


def validate_decision_facts_record(*args: object, **kwargs: object) -> None:
    raise NotImplementedError("decision facts validation is implemented in a later task")


def _state_string(state: dict[object, object], key: str) -> str:
    value = state.get(key)
    return value if isinstance(value, str) else ""


def _combine_news_sentiment_sources(*, sentiment_report: str, news_report: str) -> str:
    parts = []
    if sentiment_report:
        parts.append(f"## sentiment_report\n\n{sentiment_report}")
    if news_report:
        parts.append(f"## news_report\n\n{news_report}")
    return "\n\n".join(parts)
