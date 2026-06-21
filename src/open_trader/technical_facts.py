from __future__ import annotations

import csv
import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from open_trader.market_scope import (
    MarketScope,
    market_run_dir,
    market_scoped_latest_path,
    parse_market_scope,
)


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


@dataclass(frozen=True)
class TechnicalFactsResult:
    run_date: str
    records: int
    extracted: int
    failed: int
    reused: int
    run_path: Path
    latest_path: Path


class TechnicalFactsExtractor(Protocol):
    def extract(
        self,
        *,
        market: str,
        symbol: str,
        run_date: str,
        market_report: str,
    ) -> dict[str, object]:
        ...


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


def technical_facts_run_path(
    data_dir: Path,
    run_date: str,
    market: MarketScope | None = None,
) -> Path:
    if market is not None:
        return market_run_dir(data_dir, run_date, market) / "technical_facts.json"
    return data_dir / "runs" / run_date / "technical_facts.json"


def technical_facts_latest_path(
    data_dir: Path,
    market: MarketScope | None = None,
) -> Path:
    if market is not None:
        return market_scoped_latest_path(data_dir, market, "technical_facts.json")
    return data_dir / "latest" / "technical_facts.json"


def load_technical_facts_cache(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8-sig") as handle:
            payload = json.load(handle)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def build_freshness(
    *,
    market_data_as_of: str,
    run_date: str,
    has_unknown_timeframe: bool,
) -> dict[str, str]:
    if not market_data_as_of:
        return {
            "status": "missing_date",
            "message": f"行情日期缺失，报告生成于 {run_date}",
        }
    if has_unknown_timeframe:
        return {
            "status": "missing_timeframe",
            "message": "指标周期缺失，需复核",
        }
    return {
        "status": "fresh",
        "message": f"日线数据截至 {market_data_as_of}",
    }


def generate_technical_facts(
    *,
    advice_path: Path,
    data_dir: Path,
    run_date: str | None,
    extractor: TechnicalFactsExtractor,
    update_latest: bool,
    market: str | None = None,
) -> TechnicalFactsResult:
    sources = load_advice_sources(advice_path)
    effective_run_date = run_date or _latest_run_date(sources)
    market_scope = parse_market_scope(market) if market is not None else None
    filtered_sources = [
        source
        for source in sources
        if not source.run_date or source.run_date == effective_run_date
    ]
    if market_scope is not None:
        filtered_sources = [
            source for source in filtered_sources if source.market == market_scope.value
        ]
    if run_date is not None and not filtered_sources:
        raise ValueError(f"no advice rows match run_date {effective_run_date}")

    run_path = technical_facts_run_path(data_dir, effective_run_date, market_scope)
    latest_path = technical_facts_latest_path(data_dir, market_scope)
    reusable_records = _records_by_identity(load_technical_facts_cache(latest_path))

    rows: list[dict[str, Any]] = []
    extracted = 0
    failed = 0
    reused = 0
    for source in filtered_sources:
        identity = (source.market, source.symbol, source.source_advice_hash)
        reusable = reusable_records.get(identity)
        if reusable is not None:
            rows.append(reusable)
            reused += 1
            continue
        rows.append(
            _extract_record(
                source=source,
                run_date=effective_run_date,
                extractor=extractor,
            )
        )
        extraction_status = rows[-1].get("extraction_status")
        if extraction_status == "ok":
            extracted += 1
        elif extraction_status in {"missing_source", "extraction_failed"}:
            failed += 1

    payload = {
        "schema_version": TECHNICAL_FACTS_SCHEMA_VERSION,
        "generated_at": _now_text(),
        "run_date": effective_run_date,
        "market": market_scope.value if market_scope is not None else "",
        "records": rows,
    }
    _atomic_write_json(run_path, payload)
    if update_latest:
        _atomic_write_json(latest_path, payload)
    return TechnicalFactsResult(
        run_date=effective_run_date,
        records=len(rows),
        extracted=extracted,
        failed=failed,
        reused=reused,
        run_path=run_path,
        latest_path=latest_path,
    )


def _extract_record(
    *,
    source: AdviceSource,
    run_date: str,
    extractor: TechnicalFactsExtractor,
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "run_date": run_date,
        "market": source.market,
        "symbol": source.symbol,
        "source_status": source.source_status,
        "source_advice_hash": source.source_advice_hash,
    }
    market_report = _strip_transaction_proposal(source.market_report)
    if not market_report:
        facts = _missing_facts(source, run_date, "market_report_missing")
        return {
            **base,
            "extraction_status": "missing_source",
            "error": "market_report_missing",
            "facts": facts,
            "freshness": build_freshness(
                market_data_as_of="",
                run_date=run_date,
                has_unknown_timeframe=True,
            ),
        }
    try:
        facts = extractor.extract(
            market=source.market,
            symbol=source.symbol,
            run_date=run_date,
            market_report=market_report,
        )
        _validate_facts(facts)
    except Exception as exc:
        error = str(exc) or exc.__class__.__name__
        facts = _missing_facts(source, run_date, error)
        return {
            **base,
            "extraction_status": "extraction_failed",
            "error": error,
            "facts": facts,
            "freshness": build_freshness(
                market_data_as_of="",
                run_date=run_date,
                has_unknown_timeframe=True,
            ),
        }

    market_data_as_of = str(facts.get("market_data_as_of") or "").strip()
    return {
        **base,
        "extraction_status": "ok",
        "error": "",
        "facts": facts,
        "freshness": build_freshness(
            market_data_as_of=market_data_as_of,
            run_date=run_date,
            has_unknown_timeframe=_has_unknown_timeframe(facts),
        ),
    }


def _records_by_identity(
    cache: dict[str, Any],
) -> dict[tuple[str, str, str], dict[str, Any]]:
    records = cache.get("records")
    if not isinstance(records, list):
        return {}
    indexed: dict[tuple[str, str, str], dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        market = str(record.get("market") or "").strip().upper()
        symbol = str(record.get("symbol") or "").strip().upper()
        source_advice_hash = str(record.get("source_advice_hash") or "").strip()
        if market and symbol and source_advice_hash:
            indexed[(market, symbol, source_advice_hash)] = record
    return indexed


def _validate_facts(facts: dict[str, object]) -> None:
    if not isinstance(facts, dict):
        raise ValueError("technical facts must be an object")
    if facts.get("schema_version") != FACTS_SCHEMA_VERSION:
        raise ValueError("technical facts schema_version is invalid")
    if not isinstance(facts.get("status"), str) or not facts.get("status"):
        raise ValueError("technical facts status is missing")
    timeframes = facts.get("timeframes")
    if not isinstance(timeframes, list):
        raise ValueError("technical facts timeframes must be a list")


def _has_unknown_timeframe(facts: dict[str, object]) -> bool:
    timeframes = facts.get("timeframes")
    if not isinstance(timeframes, list) or not timeframes:
        return True
    for timeframe in timeframes:
        if not isinstance(timeframe, dict):
            return True
        if not str(timeframe.get("timeframe") or "").strip():
            return True
    return False


def _strip_transaction_proposal(report: str) -> str:
    marker = "FINAL TRANSACTION PROPOSAL"
    index = report.upper().find(marker)
    if index == -1:
        return report
    return report[:index].rstrip()


def _missing_facts(source: AdviceSource, run_date: str, reason: str) -> dict[str, Any]:
    return {
        "schema_version": FACTS_SCHEMA_VERSION,
        "status": "missing",
        "source_date": run_date,
        "market_data_as_of": "",
        "symbol": f"{source.market}.{source.symbol}",
        "timeframes": [],
        "reason": reason,
    }


def _latest_run_date(sources: list[AdviceSource]) -> str:
    dates = sorted({source.run_date for source in sources if source.run_date})
    if not dates:
        raise ValueError("--date is required when advice file has no run_date rows")
    return dates[-1]


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as temp_file:
        temp_path = Path(temp_file.name)
        json.dump(payload, temp_file, ensure_ascii=False, indent=2, sort_keys=True)
        temp_file.write("\n")
    try:
        temp_path.replace(path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _now_text() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
