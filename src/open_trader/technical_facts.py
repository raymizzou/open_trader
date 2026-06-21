from __future__ import annotations

import csv
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path


TECHNICAL_FACTS_SCHEMA_VERSION = "open_trader.technical_facts_cache.v1"
FACTS_SCHEMA_VERSION = "open_trader.technical_facts.v1"


@dataclass(frozen=True)
class AdviceSource:
    run_date: str
    market: str
    symbol: str
    source_status: str
    market_report: str
    source_advice_hash: str


def extract_market_report(raw_decision: str) -> str:
    try:
        payload = json.loads(raw_decision or "{}")
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, dict):
        return ""
    state = payload.get("state")
    if not isinstance(state, dict):
        return ""
    report = state.get("market_report")
    return report if isinstance(report, str) else ""


def source_hash(text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def load_advice_sources(advice_path: Path) -> list[AdviceSource]:
    if not advice_path.exists():
        return []
    csv.field_size_limit(sys.maxsize)
    sources: list[AdviceSource] = []
    with advice_path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            market = (row.get("market") or "").strip().upper()
            symbol = (row.get("symbol") or "").strip().upper()
            run_date = (row.get("run_date") or "").strip()
            if not market or not symbol:
                continue
            market_report = extract_market_report(row.get("raw_decision") or "")
            sources.append(
                AdviceSource(
                    run_date=run_date,
                    market=market,
                    symbol=symbol,
                    source_status=(row.get("source_status") or row.get("status") or "").strip(),
                    market_report=market_report,
                    source_advice_hash=source_hash(market_report),
                )
            )
    return sources
