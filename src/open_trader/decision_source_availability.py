from __future__ import annotations

from dataclasses import dataclass

from .decision_facts import KLINE_FIELDS, NEWS_SENTIMENT_FIELDS, extract_decision_sources
from .technical_facts import (
    extract_market_report,
    source_hash,
    technical_facts_has_missing_timeframe,
)
from .tradingagents_summary import validate_tradingagents_summary_record


@dataclass(frozen=True)
class SourceFailure:
    market: str
    symbol: str
    source: str
    error: str


def tradingagents_available(record: dict[str, object] | None, run_date: str) -> bool:
    if record is None or record.get("latest_run_date") != run_date or record.get("error"):
        return False
    try:
        validate_tradingagents_summary_record(record)
    except ValueError:
        return False
    return True


def technical_facts_available(
    record: dict[str, object] | None,
    advice_row: dict[str, str] | None,
) -> bool:
    if record is None or advice_row is None:
        return False
    facts = record.get("facts")
    freshness = record.get("freshness")
    current_source_hash = source_hash(
        extract_market_report(advice_row.get("raw_decision", ""))
    )
    record_source_hash = record.get("source_hash") or record.get("source_advice_hash")
    source_matches = (
        record.get("source_type") == "futu_kline"
        or bool(current_source_hash and record_source_hash == current_source_hash)
    )
    return bool(
        record.get("run_date") == advice_row.get("run_date")
        and source_matches
        and record.get("extraction_status") == "ok"
        and isinstance(facts, dict)
        and not technical_facts_has_missing_timeframe(facts)
        and not (
            isinstance(freshness, dict)
            and freshness.get("status") == "missing_timeframe"
        )
    )


def decision_module_available(
    module: object,
    *,
    fields: tuple[str, ...],
    current_source_hash: str,
) -> bool:
    return bool(
        isinstance(module, dict)
        and current_source_hash
        and module.get("source_hash") == current_source_hash
        and module.get("status") == "ok"
        and isinstance(module.get("fields"), dict)
        and set(module["fields"]) == set(fields)
    )


def futu_module_available(
    module: object,
    record_run_date: str | None = None,
    advice_run_date: str | None = None,
) -> bool:
    available = isinstance(module, dict) and module.get("status") in {"ok", "partial"}
    if record_run_date is None and advice_run_date is None:
        return available
    return bool(available and advice_run_date and record_run_date == advice_run_date)


def futu_module_unsupported(module: object) -> bool:
    return bool(
        isinstance(module, dict)
        and (
            module.get("status") == "not_applicable"
            or (
                module.get("status") == "error"
                and str(module.get("summary") or "").startswith("富途接口不支持")
            )
        )
    )


def evaluate_required_sources(
    *,
    advice_rows: list[dict[str, str]],
    technical_records: dict[tuple[str, str], dict[str, object]],
    decision_records: dict[tuple[str, str], dict[str, object]],
    tradingagents_records: dict[tuple[str, str], dict[str, object]],
    futu_records: dict[tuple[str, str], dict[str, object]],
) -> list[SourceFailure]:
    failures: list[SourceFailure] = []
    rows = sorted(
        advice_rows,
        key=lambda row: (row.get("market", "").upper(), row.get("symbol", "").upper()),
    )
    for row in rows:
        market = row.get("market", "").strip().upper()
        symbol = row.get("symbol", "").strip().upper()
        if not market or not symbol:
            continue
        key = (market, symbol)
        run_date = row.get("run_date", "").strip()
        technical = technical_records.get(key)
        decision = decision_records.get(key)
        tradingagents = tradingagents_records.get(key)
        futu = futu_records.get(key)
        decision_sources = extract_decision_sources(row.get("raw_decision", ""))
        checks = (
            (
                "tradingagents_summary",
                tradingagents,
                tradingagents_available(tradingagents, run_date),
            ),
            ("technical_facts", technical, technical_facts_available(technical, row)),
            (
                "decision_facts.kline",
                _module(decision, "kline"),
                decision_module_available(
                    _module(decision, "kline"),
                    fields=KLINE_FIELDS,
                    current_source_hash=decision_sources.kline_hash,
                ),
            ),
            (
                "decision_facts.news_sentiment",
                _module(decision, "news_sentiment"),
                decision_module_available(
                    _module(decision, "news_sentiment"),
                    fields=NEWS_SENTIMENT_FIELDS,
                    current_source_hash=decision_sources.news_sentiment_hash,
                ),
            ),
            *_futu_checks(futu, run_date),
        )
        for source, subject, available in checks:
            if not available:
                failures.append(SourceFailure(market, symbol, source, _source_error(subject)))
    return failures


def _module(record: dict[str, object] | None, name: str) -> object:
    return record.get(name) if record is not None else None


def _futu_checks(
    record: dict[str, object] | None,
    run_date: str,
) -> tuple[tuple[str, object, bool], ...]:
    current = bool(record and record.get("run_date") == run_date)
    return tuple(
        (
            f"futu_skill_facts.{name}",
            _module(record, name),
            current
            and (
                futu_module_available(_module(record, name))
                or futu_module_unsupported(_module(record, name))
            ),
        )
        for name in (
            "news_sentiment",
            "technical_anomaly",
            "capital_anomaly",
            "derivatives_anomaly",
        )
    )


def _source_error(subject: object) -> str:
    if not isinstance(subject, dict):
        return "数据未生成"
    return str(
        subject.get("error")
        or subject.get("blocking_reason")
        or subject.get("status")
        or subject.get("extraction_status")
        or "数据未生成"
    )
