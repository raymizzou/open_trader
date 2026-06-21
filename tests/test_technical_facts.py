from __future__ import annotations

import csv
import json
from pathlib import Path

from open_trader.technical_facts import (
    extract_market_report,
    load_advice_sources,
    source_hash,
)


def write_advice(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
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
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def raw_decision_with_market_report(report: str) -> str:
    return json.dumps({"state": {"market_report": report}}, ensure_ascii=False)


def test_extract_market_report_reads_raw_decision_state() -> None:
    raw = raw_decision_with_market_report("Technical report text")

    assert extract_market_report(raw) == "Technical report text"


def test_extract_market_report_returns_empty_for_invalid_json() -> None:
    assert extract_market_report("{not-json") == ""


def test_source_hash_is_stable_and_prefixed() -> None:
    first = source_hash("Technical report text")
    second = source_hash("Technical report text")

    assert first == second
    assert first.startswith("sha256:")
    assert source_hash("Other report text") != first


def test_load_advice_sources_reads_rows_with_market_report(tmp_path: Path) -> None:
    advice_path = tmp_path / "trading_advice.csv"
    write_advice(
        advice_path,
        [
            {
                "run_date": "2026-06-19",
                "symbol": "02476",
                "market": "HK",
                "asset_class": "stock",
                "portfolio_weight_hkd": "8.97%",
                "risk_flag": "normal",
                "source": "tradingagents",
                "advice_action": "Underweight",
                "advice_summary": "",
                "raw_decision": raw_decision_with_market_report("Daily RSI 56.88"),
                "status": "ok",
                "error": "",
                "source_status": "ok",
                "fallback_reason": "",
                "fallback_from_date": "",
            }
        ],
    )

    sources = load_advice_sources(advice_path)

    assert len(sources) == 1
    assert sources[0].market == "HK"
    assert sources[0].symbol == "02476"
    assert sources[0].run_date == "2026-06-19"
    assert sources[0].market_report == "Daily RSI 56.88"
    assert sources[0].source_advice_hash == source_hash("Daily RSI 56.88")
