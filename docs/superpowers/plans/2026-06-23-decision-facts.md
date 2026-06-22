# Fixed Decision Facts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build fixed Chinese decision facts for the `趋势 / K 线` and `新闻 / 舆论` plugin cards from existing TradingAgents reports.

**Architecture:** Add a focused `open_trader.decision_facts` module that reads `trading_advice.csv`, extracts source text from `raw_decision`, runs a strict JSON LLM extractor, validates fixed Chinese fields, and writes market-scoped dated/latest `decision_facts.json`. Wire it into the CLI, market premarket pipeline, daily latest promotion, dashboard payload, and dashboard UI. The frontend only renders fixed fields from `decision_facts`; it no longer displays raw English technical/news prose in these two cards.

**Tech Stack:** Python 3.12, standard-library `csv`/`json`/`dataclasses`/`pathlib`, OpenAI-compatible DeepSeek client, pytest, static dashboard JavaScript/CSS.

---

## File Structure

- Create `src/open_trader/decision_facts.py`: source extraction, LLM extractor, schema validation, artifact writing, cache loading, and path helpers.
- Modify `src/open_trader/cli.py`: add `extract-decision-facts` command and wire it to `generate_decision_facts`.
- Modify `src/open_trader/advice/premarket.py`: generate decision facts after TradingAgents advice and promote `decision_facts.json` with premarket outputs.
- Modify `src/open_trader/daily_premarket.py`: include decision facts in market latest promotion, status JSON, and daily Markdown artifacts.
- Modify `src/open_trader/dashboard.py`: load latest decision facts and attach hash-checked `decision_facts` details to holdings.
- Modify `src/open_trader/dashboard_static/dashboard.js`: render fixed-field `趋势 / K 线` and `新闻 / 舆论` cards from `decision_facts`.
- Modify `src/open_trader/dashboard_static/dashboard.css`: style the fixed field grid if existing `technical-fact-grid` classes are insufficient.
- Create `tests/test_decision_facts.py`: unit coverage for extraction, validation, artifacts, and missing source handling.
- Modify `tests/test_premarket_cli.py`: CLI parser and command wiring tests.
- Modify `tests/test_premarket_pipeline.py`: premarket integration tests.
- Modify `tests/test_daily_premarket.py`: daily promotion/artifact listing tests.
- Modify `tests/test_dashboard.py`: dashboard payload tests.
- Modify `tests/test_dashboard_web.py`: frontend render tests.
- Modify `README.md` and `README.zh-CN.md`: document the manual extraction command and artifact paths.

---

### Task 1: Add Decision Facts Module Tests

**Files:**
- Create: `tests/test_decision_facts.py`
- Create later: `src/open_trader/decision_facts.py`

- [ ] **Step 1: Write failing tests for source extraction and fixed missing fields**

Create `tests/test_decision_facts.py`:

```python
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
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_decision_facts.py::test_extract_decision_sources_reads_tradingagents_state tests/test_decision_facts.py::test_build_missing_fields_uses_fixed_missing_value -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'open_trader.decision_facts'`.

- [ ] **Step 3: Implement minimal source extraction helpers**

Create `src/open_trader/decision_facts.py`:

```python
from __future__ import annotations

import csv
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from openai import OpenAI

from .advice.change_classifier import DEEPSEEK_BASE_URL, DEFAULT_CLASSIFIER_MODEL
from .market_scope import MarketScope, market_run_dir, market_scoped_latest_path, parse_market_scope
from .technical_facts import source_hash


DECISION_FACTS_SCHEMA_VERSION = "open_trader.decision_facts.v1"
MISSING_VALUE = "缺失"
KLINE_FIELDS = ("trend", "position", "momentum", "key_levels", "risk")
NEWS_SENTIMENT_FIELDS = ("direction", "change", "catalyst", "risk", "attention")
RUN_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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
    sources: DecisionSources


@dataclass(frozen=True)
class DecisionFactsResult:
    run_date: str
    records: int
    extracted: int
    failed: int
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
    state = payload.get("state") if isinstance(payload, dict) else {}
    if not isinstance(state, dict):
        state = {}
    market_report = state.get("market_report")
    sentiment_report = state.get("sentiment_report")
    news_report = state.get("news_report")
    kline_source = market_report if isinstance(market_report, str) else ""
    news_parts: list[str] = []
    if isinstance(sentiment_report, str) and sentiment_report.strip():
        news_parts.append(f"## sentiment_report\n\n{sentiment_report.strip()}")
    if isinstance(news_report, str) and news_report.strip():
        news_parts.append(f"## news_report\n\n{news_report.strip()}")
    news_sentiment_source = "\n\n".join(news_parts)
    return DecisionSources(
        kline_source=kline_source,
        news_sentiment_source=news_sentiment_source,
        kline_hash=source_hash(kline_source) if kline_source else "",
        news_sentiment_hash=source_hash(news_sentiment_source) if news_sentiment_source else "",
    )


def build_missing_fields(fields: tuple[str, ...]) -> dict[str, str]:
    return {field: MISSING_VALUE for field in fields}
```

- [ ] **Step 4: Run tests and verify they pass**

Run:

```bash
.venv/bin/python -m pytest tests/test_decision_facts.py::test_extract_decision_sources_reads_tradingagents_state tests/test_decision_facts.py::test_build_missing_fields_uses_fixed_missing_value -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add src/open_trader/decision_facts.py tests/test_decision_facts.py
git commit -m "feat: add decision facts source parsing"
```

---

### Task 2: Implement Validation And Artifact Generation

**Files:**
- Modify: `src/open_trader/decision_facts.py`
- Modify: `tests/test_decision_facts.py`

- [ ] **Step 1: Add failing tests for validation and artifact writing**

Append to `tests/test_decision_facts.py`:

```python
def test_validate_decision_facts_record_rejects_missing_fixed_field() -> None:
    record = {
        "schema_version": DECISION_FACTS_SCHEMA_VERSION,
        "kline": {"status": "ok", "fields": build_missing_fields(KLINE_FIELDS)},
        "news_sentiment": {
            "status": "ok",
            "fields": build_missing_fields(NEWS_SENTIMENT_FIELDS),
        },
    }
    del record["kline"]["fields"]["trend"]

    with pytest.raises(ValueError, match="kline fields are invalid"):
        validate_decision_facts_record(record)


def test_validate_decision_facts_record_rejects_english_only_value() -> None:
    record = {
        "schema_version": DECISION_FACTS_SCHEMA_VERSION,
        "kline": {"status": "ok", "fields": build_missing_fields(KLINE_FIELDS)},
        "news_sentiment": {
            "status": "ok",
            "fields": build_missing_fields(NEWS_SENTIMENT_FIELDS),
        },
    }
    record["news_sentiment"]["fields"]["direction"] = "Bullish retail sentiment"

    with pytest.raises(ValueError, match="field values must be Chinese or 缺失"):
        validate_decision_facts_record(record)


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
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_decision_facts.py -q
```

Expected: FAIL with import errors for `generate_decision_facts`, `load_decision_facts_cache`, `decision_facts_run_path`, `decision_facts_latest_path`, or validation helpers.

- [ ] **Step 3: Implement validation, loading, paths, and generator**

Append to `src/open_trader/decision_facts.py`:

```python
def decision_facts_run_path(
    data_dir: Path,
    run_date: str,
    market: MarketScope | str | None = None,
) -> Path:
    scope = _market_scope(market)
    if scope is not None:
        return market_run_dir(data_dir, run_date, scope) / "decision_facts.json"
    return data_dir / "runs" / run_date / "decision_facts.json"


def decision_facts_latest_path(
    data_dir: Path,
    market: MarketScope | str | None = None,
) -> Path:
    scope = _market_scope(market)
    if scope is not None:
        return market_scoped_latest_path(data_dir, scope, "decision_facts.json")
    return data_dir / "latest" / "decision_facts.json"


def load_decision_facts_cache(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8-sig") as handle:
            payload = json.load(handle)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def index_decision_facts_by_market_symbol(
    cache: dict[str, Any],
) -> dict[tuple[str, str], dict[str, Any]]:
    records = cache.get("records")
    if not isinstance(records, list):
        return {}
    indexed: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        market = str(record.get("market") or "").strip().upper()
        symbol = str(record.get("symbol") or "").strip().upper()
        if market and symbol:
            indexed[(market, symbol)] = record
    return indexed


def validate_decision_facts_record(record: dict[str, object]) -> None:
    if record.get("schema_version") != DECISION_FACTS_SCHEMA_VERSION:
        raise ValueError("decision facts schema_version is invalid")
    _validate_module_fields(record.get("kline"), KLINE_FIELDS, "kline")
    _validate_module_fields(
        record.get("news_sentiment"),
        NEWS_SENTIMENT_FIELDS,
        "news_sentiment",
    )


def generate_decision_facts(
    *,
    advice_path: Path,
    data_dir: Path,
    run_date: str | None,
    extractor: DecisionFactsExtractor,
    update_latest: bool,
    market: str | MarketScope | None = None,
) -> DecisionFactsResult:
    sources = _load_advice_sources(advice_path)
    _validate_source_run_dates(sources)
    scope = _market_scope(market)
    market_sources = (
        [source for source in sources if source.market == scope.value]
        if scope is not None
        else sources
    )
    effective_run_date = (
        _validate_run_date(run_date.strip())
        if run_date is not None and run_date.strip()
        else _latest_run_date(market_sources)
    )
    filtered_sources = [
        source
        for source in market_sources
        if not source.run_date or source.run_date == effective_run_date
    ]
    if run_date is not None and not filtered_sources:
        raise ValueError(f"no advice rows match run_date {effective_run_date}")

    rows: list[dict[str, Any]] = []
    extracted = 0
    failed = 0
    for source in filtered_sources:
        record = _extract_record(
            source=source,
            run_date=effective_run_date,
            extractor=extractor,
        )
        rows.append(record)
        if record.get("error"):
            failed += 1
        else:
            extracted += 1

    payload = {
        "schema_version": DECISION_FACTS_SCHEMA_VERSION,
        "generated_at": _now_text(),
        "run_date": effective_run_date,
        "market": scope.value if scope is not None else "",
        "records": rows,
    }
    run_path = decision_facts_run_path(data_dir, effective_run_date, scope)
    latest_path = decision_facts_latest_path(data_dir, scope)
    _atomic_write_json(run_path, payload)
    if update_latest:
        _atomic_write_json(latest_path, payload)
    return DecisionFactsResult(
        run_date=effective_run_date,
        records=len(rows),
        extracted=extracted,
        failed=failed,
        run_path=run_path,
        latest_path=latest_path,
    )


def _load_advice_sources(advice_path: Path) -> list[AdviceSource]:
    if not advice_path.exists():
        raise FileNotFoundError(f"advice CSV not found: {advice_path}")
    csv.field_size_limit(sys.maxsize)
    sources: list[AdviceSource] = []
    with advice_path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            market = (row.get("market") or "").strip().upper()
            symbol = (row.get("symbol") or "").strip().upper()
            if not market or not symbol:
                continue
            sources.append(
                AdviceSource(
                    run_date=(row.get("run_date") or "").strip(),
                    market=market,
                    symbol=symbol,
                    source_status=(row.get("source_status") or row.get("status") or "").strip(),
                    sources=extract_decision_sources(row.get("raw_decision") or ""),
                )
            )
    return sources


def _extract_record(
    *,
    source: AdviceSource,
    run_date: str,
    extractor: DecisionFactsExtractor,
) -> dict[str, Any]:
    base = {
        "schema_version": DECISION_FACTS_SCHEMA_VERSION,
        "run_date": run_date,
        "market": source.market,
        "symbol": source.symbol,
        "source_status": source.source_status,
    }
    try:
        if source.sources.kline_source or source.sources.news_sentiment_source:
            extracted = extractor.extract(
                market=source.market,
                symbol=source.symbol,
                run_date=run_date,
                kline_source=source.sources.kline_source,
                news_sentiment_source=source.sources.news_sentiment_source,
            )
        else:
            extracted = {}
        record = {
            **base,
            "kline": _module_payload(
                extracted.get("kline") if isinstance(extracted, dict) else None,
                fields=KLINE_FIELDS,
                source_hash=source.sources.kline_hash,
                missing_status="missing_source" if not source.sources.kline_source else "ok",
            ),
            "news_sentiment": _module_payload(
                extracted.get("news_sentiment") if isinstance(extracted, dict) else None,
                fields=NEWS_SENTIMENT_FIELDS,
                source_hash=source.sources.news_sentiment_hash,
                missing_status=(
                    "missing_source"
                    if not source.sources.news_sentiment_source
                    else "ok"
                ),
            ),
            "error": "",
        }
        validate_decision_facts_record(record)
        return record
    except Exception as exc:
        error = str(exc) or exc.__class__.__name__
        return {
            **base,
            "kline": _missing_module(source.sources.kline_hash, KLINE_FIELDS, "error"),
            "news_sentiment": _missing_module(
                source.sources.news_sentiment_hash,
                NEWS_SENTIMENT_FIELDS,
                "error",
            ),
            "error": error,
        }


def _module_payload(
    payload: object,
    *,
    fields: tuple[str, ...],
    source_hash: str,
    missing_status: str,
) -> dict[str, Any]:
    if missing_status == "missing_source":
        return _missing_module(source_hash, fields, missing_status)
    if not isinstance(payload, dict):
        return _missing_module(source_hash, fields, "extraction_failed")
    raw_fields = payload.get("fields")
    field_payload = raw_fields if isinstance(raw_fields, dict) else {}
    normalized = {
        field: _clean_field_value(field_payload.get(field))
        for field in fields
    }
    return {
        "status": str(payload.get("status") or "ok"),
        "source_hash": source_hash,
        "fields": normalized,
    }


def _missing_module(
    source_hash_value: str,
    fields: tuple[str, ...],
    status: str,
) -> dict[str, Any]:
    return {
        "status": status,
        "source_hash": source_hash_value,
        "fields": build_missing_fields(fields),
    }


def _clean_field_value(value: object) -> str:
    text = str(value or "").strip()
    return text or MISSING_VALUE


def _validate_module_fields(
    payload: object,
    fields: tuple[str, ...],
    module_name: str,
) -> None:
    if not isinstance(payload, dict):
        raise ValueError(f"{module_name} module is invalid")
    raw_fields = payload.get("fields")
    if not isinstance(raw_fields, dict) or set(raw_fields) != set(fields):
        raise ValueError(f"{module_name} fields are invalid")
    for value in raw_fields.values():
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{module_name} fields must be non-empty strings")
        if value != MISSING_VALUE and _looks_english_only(value):
            raise ValueError("field values must be Chinese or 缺失")


def _looks_english_only(value: str) -> bool:
    letters = sum(1 for char in value if ("a" <= char.lower() <= "z"))
    cjk = sum(1 for char in value if "\u4e00" <= char <= "\u9fff")
    return letters >= 8 and cjk == 0


def _market_scope(market: MarketScope | str | None) -> MarketScope | None:
    if market is None:
        return None
    if isinstance(market, MarketScope):
        return market
    return parse_market_scope(market)


def _latest_run_date(sources: list[AdviceSource]) -> str:
    dates = sorted({source.run_date for source in sources if source.run_date})
    if not dates:
        raise ValueError("run_date must be YYYY-MM-DD")
    return dates[-1]


def _validate_source_run_dates(sources: list[AdviceSource]) -> None:
    for source in sources:
        if source.run_date and not _is_valid_run_date(source.run_date):
            raise ValueError("run_date must be YYYY-MM-DD")


def _validate_run_date(run_date: str) -> str:
    if not _is_valid_run_date(run_date):
        raise ValueError("run_date must be YYYY-MM-DD")
    return run_date


def _is_valid_run_date(run_date: str) -> bool:
    if not RUN_DATE_PATTERN.fullmatch(run_date):
        return False
    try:
        datetime.strptime(run_date, "%Y-%m-%d")
    except ValueError:
        return False
    return True


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
```

- [ ] **Step 4: Run tests and verify they pass**

Run:

```bash
.venv/bin/python -m pytest tests/test_decision_facts.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add src/open_trader/decision_facts.py tests/test_decision_facts.py
git commit -m "feat: generate fixed decision facts"
```

---

### Task 3: Add LLM Extractor And CLI Command

**Files:**
- Modify: `src/open_trader/decision_facts.py`
- Modify: `src/open_trader/cli.py`
- Modify: `tests/test_premarket_cli.py`

- [ ] **Step 1: Add failing CLI tests**

Append to `tests/test_premarket_cli.py`:

```python
def test_extract_decision_facts_help_includes_expected_options(capsys) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["extract-decision-facts", "--help"])

    assert excinfo.value.code == 0
    output = capsys.readouterr().out
    assert "--advice" in output
    assert "--data-dir" in output
    assert "--date" in output
    assert "--market" in output
    assert "--update-latest" in output


def test_extract_decision_facts_main_wires_generator(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    advice = tmp_path / "trading_advice.csv"
    advice.write_text("run_date,symbol,market,raw_decision\n", encoding="utf-8")
    captured: dict[str, object] = {}

    class FakeExtractor:
        pass

    def fake_generate_decision_facts(**kwargs: object):
        captured.update(kwargs)
        return SimpleNamespace(
            run_date="2026-06-22",
            records=2,
            extracted=2,
            failed=0,
            run_path=tmp_path / "data/runs/2026-06-22/US/decision_facts.json",
            latest_path=tmp_path / "data/latest/US/decision_facts.json",
        )

    monkeypatch.setattr(cli, "LLMDecisionFactsExtractor", lambda: FakeExtractor())
    monkeypatch.setattr(cli, "generate_decision_facts", fake_generate_decision_facts)

    result = cli.main(
        [
            "extract-decision-facts",
            "--advice",
            str(advice),
            "--data-dir",
            str(tmp_path / "data"),
            "--date",
            "2026-06-22",
            "--market",
            "US",
            "--update-latest",
        ]
    )

    assert result == 0
    assert captured["advice_path"] == advice
    assert captured["data_dir"] == tmp_path / "data"
    assert captured["run_date"] == "2026-06-22"
    assert captured["market"] == "US"
    assert captured["update_latest"] is True
    output = capsys.readouterr().out
    assert "decision_facts: 2" in output
    assert "decision_facts_json:" in output
```

If `SimpleNamespace`, `pytest`, `Path`, or `cli` are not already imported in `tests/test_premarket_cli.py`, add:

```python
from pathlib import Path
from types import SimpleNamespace

import pytest

import open_trader.cli as cli
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_premarket_cli.py::test_extract_decision_facts_help_includes_expected_options tests/test_premarket_cli.py::test_extract_decision_facts_main_wires_generator -q
```

Expected: FAIL because the command and imports are not wired.

- [ ] **Step 3: Add LLM extractor**

Append to `src/open_trader/decision_facts.py`:

```python
class OpenAITextClient:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = DEEPSEEK_BASE_URL,
        model: str = DEFAULT_CLASSIFIER_MODEL,
    ) -> None:
        self.model = model
        self.client = OpenAI(
            api_key=api_key or os.environ.get("DEEPSEEK_API_KEY"),
            base_url=base_url,
        )

    def create(self, *, messages: list[dict[str, str]], temperature: float) -> str:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=temperature,
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content
        return content or ""


class LLMDecisionFactsExtractor:
    def __init__(self, *, client: object | None = None) -> None:
        self.client = client or OpenAITextClient()

    def extract(
        self,
        *,
        market: str,
        symbol: str,
        run_date: str,
        kline_source: str,
        news_sentiment_source: str,
    ) -> dict[str, object]:
        messages = [
            {"role": "system", "content": _decision_facts_system_prompt()},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "market": market,
                        "symbol": symbol,
                        "run_date": run_date,
                        "kline_source": kline_source,
                        "news_sentiment_source": news_sentiment_source,
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        content = self.client.create(messages=messages, temperature=0)
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError("LLM decision facts response must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("LLM decision facts response must be a JSON object")
        validate_decision_facts_record(payload)
        return payload


def _decision_facts_system_prompt() -> str:
    return (
        "你是 open_trader 的交易决策事实抽取器。只从用户提供的 TradingAgents 报告中"
        "抽取事实，输出严格 JSON，不要输出 markdown。schema_version 必须是 "
        f"{DECISION_FACTS_SCHEMA_VERSION}。必须包含 kline 和 news_sentiment 两个对象。"
        "kline.fields 必须且只能包含 trend、position、momentum、key_levels、risk。"
        "news_sentiment.fields 必须且只能包含 direction、change、catalyst、risk、attention。"
        "所有字段值必须是简短中文，适合看板卡片展示。缺少证据时字段值写 缺失。"
        "不要直接复制英文原文。不要编造来源没有的信息。不要输出买入、卖出、下单、"
        "仓位、数量、价格目标或自动执行建议。"
    )
```

- [ ] **Step 4: Wire CLI imports, parser, and command handler**

In `src/open_trader/cli.py`, add to imports:

```python
from .decision_facts import LLMDecisionFactsExtractor, generate_decision_facts
```

In `build_parser()`, after `extract-technical-facts`, add:

```python
    decision_facts_parser = subparsers.add_parser(
        "extract-decision-facts",
        help="Extract fixed Chinese decision facts from TradingAgents advice CSV",
    )
    decision_facts_parser.add_argument(
        "--advice",
        type=Path,
        required=True,
        help="TradingAgents trading advice CSV path",
    )
    decision_facts_parser.add_argument("--data-dir", type=Path, default=Path("data"))
    decision_facts_parser.add_argument(
        "--date",
        type=canonical_date,
        help="Run date, YYYY-MM-DD. Defaults to latest run_date in advice rows.",
    )
    decision_facts_parser.add_argument(
        "--market",
        type=canonical_market,
        choices=["HK", "US"],
        help="Optional market scope: HK or US",
    )
    decision_facts_parser.add_argument(
        "--update-latest",
        action="store_true",
        help="Update data/latest/<MARKET>/decision_facts.json after writing dated artifact",
    )
```

In `main()`, after the `extract-technical-facts` handler, add:

```python
    if args.command == "extract-decision-facts":
        if not args.advice.exists():
            parser.error(f"advice CSV not found: {args.advice}")
        try:
            extractor = LLMDecisionFactsExtractor()
        except Exception as exc:
            parser.error(str(exc))
        try:
            result = generate_decision_facts(
                advice_path=args.advice,
                data_dir=args.data_dir,
                run_date=args.date,
                extractor=extractor,
                update_latest=args.update_latest,
                market=args.market,
            )
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            parser.error(str(exc))
        print(f"run_date: {result.run_date}")
        print(f"decision_facts: {result.records}")
        print(f"extracted: {result.extracted}")
        print(f"failed: {result.failed}")
        print(f"decision_facts_json: {result.run_path}")
        print(f"latest: {result.latest_path}")
        return 0
```

- [ ] **Step 5: Run CLI and decision facts tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_decision_facts.py tests/test_premarket_cli.py::test_extract_decision_facts_help_includes_expected_options tests/test_premarket_cli.py::test_extract_decision_facts_main_wires_generator -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

Run:

```bash
git add src/open_trader/decision_facts.py src/open_trader/cli.py tests/test_decision_facts.py tests/test_premarket_cli.py
git commit -m "feat: add decision facts CLI"
```

---

### Task 4: Wire Premarket And Daily Pipeline

**Files:**
- Modify: `src/open_trader/advice/premarket.py`
- Modify: `src/open_trader/daily_premarket.py`
- Modify: `tests/test_premarket_pipeline.py`
- Modify: `tests/test_daily_premarket.py`

- [ ] **Step 1: Add failing premarket integration tests**

Append to `tests/test_premarket_pipeline.py`:

```python
from open_trader.decision_facts import DecisionFactsResult


def test_run_premarket_generates_decision_facts_after_advice(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    reports_dir = tmp_path / "reports"
    portfolio = tmp_path / "portfolio.csv"
    write_portfolio(portfolio, [portfolio_row(symbol="SOXX", market="US")])
    calls: list[dict[str, object]] = []

    def fake_decision_facts_generator(**kwargs: object) -> DecisionFactsResult:
        calls.append(kwargs)
        path = data_dir / "runs/2026-06-22/US/decision_facts.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"schema_version":"open_trader.decision_facts.v1","records":[]}', encoding="utf-8")
        return DecisionFactsResult(
            run_date="2026-06-22",
            records=0,
            extracted=0,
            failed=0,
            run_path=path,
            latest_path=data_dir / "latest/US/decision_facts.json",
        )

    result = run_premarket(
        run_date="2026-06-22",
        portfolio_path=portfolio,
        data_dir=data_dir,
        reports_dir=reports_dir,
        advice_runner=FakeAdviceRunner(),
        classifier=FakeClassifier(),
        market="US",
        update_latest=False,
        technical_facts_generator=FakeTechnicalFactsGenerator(),
        decision_facts_generator=fake_decision_facts_generator,
    )

    assert result.decision_facts_path == data_dir / "runs/2026-06-22/US/decision_facts.json"
    assert calls[0]["advice_path"] == result.advice_path
    assert calls[0]["market"].value == "US"
```

Adapt helper names (`write_portfolio`, `portfolio_row`, `FakeAdviceRunner`, `FakeClassifier`, `FakeTechnicalFactsGenerator`) to the existing helper names in `tests/test_premarket_pipeline.py`; do not introduce duplicate helpers if equivalent ones already exist.

- [ ] **Step 2: Add failing daily promotion test**

Append to `tests/test_daily_premarket.py`:

```python
def test_daily_runner_promotes_decision_facts(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    latest_dir = data_dir / "latest" / "US"
    advice = data_dir / "runs/2026-06-22/US/trading_advice.csv"
    actions = data_dir / "runs/2026-06-22/US/premarket_actions.csv"
    plan = data_dir / "runs/2026-06-22/US/trading_plan.csv"
    trade_actions = data_dir / "runs/2026-06-22/US/trade_actions.csv"
    technical_facts = data_dir / "runs/2026-06-22/US/technical_facts.json"
    decision_facts = data_dir / "runs/2026-06-22/US/decision_facts.json"
    for path in [advice, actions, plan, trade_actions, technical_facts, decision_facts]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(path.name, encoding="utf-8")

    _promote_latest_set(
        advice_path=advice,
        actions_path=actions,
        plan_path=plan,
        trade_actions_path=trade_actions,
        technical_facts_path=technical_facts,
        decision_facts_path=decision_facts,
        data_dir=data_dir,
        market="US",
    )

    assert (latest_dir / "decision_facts.json").read_text(encoding="utf-8") == "decision_facts.json"
```

- [ ] **Step 3: Run tests and verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_premarket_pipeline.py::test_run_premarket_generates_decision_facts_after_advice tests/test_daily_premarket.py::test_daily_runner_promotes_decision_facts -q
```

Expected: FAIL because `decision_facts_generator`, `decision_facts_path`, and `decision_facts_path` promotion are not implemented.

- [ ] **Step 4: Implement premarket integration**

In `src/open_trader/advice/premarket.py`, add imports:

```python
from open_trader.decision_facts import (
    DecisionFactsResult,
    LLMDecisionFactsExtractor,
    generate_decision_facts,
)
```

Add type alias near `TechnicalFactsGenerator`:

```python
DecisionFactsGenerator = Callable[..., DecisionFactsResult]
```

Add `decision_facts_path` to `PremarketResult`:

```python
    decision_facts_path: Path | None = None
```

Add parameter to `run_premarket(...)`:

```python
    decision_facts_generator: DecisionFactsGenerator | None = None,
```

After `technical_facts_result = ...`, add:

```python
    decision_facts_result = _generate_decision_facts_after_advice(
        advice_path=advice_path,
        data_dir=data_dir,
        run_date=run_date,
        update_latest=False,
        market=market_scope,
        decision_facts_generator=decision_facts_generator,
    )
```

In `_promote_latest_outputs(...)` call, pass:

```python
            decision_facts_path=decision_facts_result.run_path,
```

In the returned `PremarketResult`, pass:

```python
        decision_facts_path=decision_facts_result.run_path,
```

Add helper:

```python
def _generate_decision_facts_after_advice(
    *,
    advice_path: Path,
    data_dir: Path,
    run_date: str,
    update_latest: bool,
    market: MarketScope | None,
    decision_facts_generator: DecisionFactsGenerator | None,
) -> DecisionFactsResult:
    generator = decision_facts_generator
    if generator is None:
        extractor = LLMDecisionFactsExtractor()

        def generator(**kwargs: object) -> DecisionFactsResult:
            return generate_decision_facts(extractor=extractor, **kwargs)  # type: ignore[arg-type]

    return generator(
        advice_path=advice_path,
        data_dir=data_dir,
        run_date=run_date,
        update_latest=update_latest,
        market=market,
    )
```

Modify `_promote_latest_outputs(...)` signature:

```python
    decision_facts_path: Path | None = None,
```

Append promotion:

```python
    if decision_facts_path is not None:
        promotions.append(
            _LatestPromotion(
                source_path=decision_facts_path,
                latest_path=latest_dir / "decision_facts.json",
            )
        )
```

- [ ] **Step 5: Implement daily promotion and artifact listing**

In `src/open_trader/daily_premarket.py`, after resolving `technical_facts_path`, add:

```python
        decision_facts_path = Path(
            getattr(
                premarket_result,
                "decision_facts_path",
                advice_path.with_name("decision_facts.json"),
            )
        )
        if not decision_facts_path.exists():
            decision_facts_path = None
```

Add latest path:

```python
        latest_decision_facts_path = latest_dir / "decision_facts.json"
```

Add artifacts entries:

```python
            "decision_facts": str(decision_facts_path) if decision_facts_path else "",
            "latest_decision_facts": str(latest_decision_facts_path),
```

Pass to `_promote_latest_set(...)`:

```python
                decision_facts_path=decision_facts_path,
```

Modify `_promote_latest_set(...)` signature:

```python
    decision_facts_path: Path | None = None,
```

Append promotion:

```python
    if decision_facts_path is not None:
        promotions.append(
            _LatestPromotion(
                source_path=decision_facts_path,
                latest_path=latest_dir / "decision_facts.json",
            )
        )
```

In `_render_daily_report(...)` artifact order, add:

```python
        "decision_facts",
        "latest_decision_facts",
```

- [ ] **Step 6: Run pipeline tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_premarket_pipeline.py::test_run_premarket_generates_decision_facts_after_advice tests/test_daily_premarket.py::test_daily_runner_promotes_decision_facts -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

Run:

```bash
git add src/open_trader/advice/premarket.py src/open_trader/daily_premarket.py tests/test_premarket_pipeline.py tests/test_daily_premarket.py
git commit -m "feat: wire decision facts into daily pipeline"
```

---

### Task 5: Attach Decision Facts To Dashboard Payload

**Files:**
- Modify: `src/open_trader/dashboard.py`
- Modify: `tests/test_dashboard.py`

- [ ] **Step 1: Add failing dashboard payload tests**

Append to `tests/test_dashboard.py`:

```python
def write_decision_facts(path: Path, *, kline_hash: str, news_hash: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": "open_trader.decision_facts.v1",
                "run_date": "2026-06-22",
                "market": "US",
                "records": [
                    {
                        "schema_version": "open_trader.decision_facts.v1",
                        "run_date": "2026-06-22",
                        "market": "US",
                        "symbol": "VIXY",
                        "source_status": "ok",
                        "kline": {
                            "status": "ok",
                            "source_hash": kline_hash,
                            "fields": {
                                "trend": "震荡",
                                "position": "接近均线",
                                "momentum": "动能中性",
                                "key_levels": "支撑 45，压力 50",
                                "risk": "波动偏高",
                            },
                        },
                        "news_sentiment": {
                            "status": "ok",
                            "source_hash": news_hash,
                            "fields": {
                                "direction": "中性",
                                "change": "变化不大",
                                "catalyst": "缺失",
                                "risk": "缺失",
                                "attention": "正常",
                            },
                        },
                        "error": "",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def raw_decision_with_all_reports() -> str:
    return json.dumps(
        {
            "state": {
                "market_report": "K report",
                "sentiment_report": "Sentiment report",
                "news_report": "News report",
            }
        },
        ensure_ascii=False,
    )


def test_dashboard_attaches_hash_checked_decision_facts(tmp_path: Path) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    write_csv(
        config.data_dir / "latest" / "US" / "trading_advice.csv",
        TRADING_ADVICE_FIELDNAMES,
        [
            {
                "run_date": "2026-06-22",
                "symbol": "VIXY",
                "market": "US",
                "asset_class": "etf",
                "portfolio_weight_hkd": "97.80%",
                "risk_flag": "overweight",
                "source": "tradingagents",
                "advice_action": "HOLD",
                "advice_summary": "",
                "raw_decision": raw_decision_with_all_reports(),
                "status": "ok",
                "error": "",
                "source_status": "ok",
                "fallback_reason": "",
                "fallback_from_date": "",
            }
        ],
    )
    write_decision_facts(
        config.data_dir / "latest" / "US" / "decision_facts.json",
        kline_hash=source_hash("K report"),
        news_hash=source_hash("## sentiment_report\n\nSentiment report\n\n## news_report\n\nNews report"),
    )

    state = load_dashboard_state(config)

    detail = state.holdings[0]["decision_facts"]
    assert detail["kline"]["available"] is True
    assert detail["kline"]["fields"]["trend"] == "震荡"
    assert detail["news_sentiment"]["available"] is True
    assert detail["news_sentiment"]["fields"]["direction"] == "中性"


def test_dashboard_stale_decision_facts_render_missing_fields(tmp_path: Path) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    write_csv(
        config.data_dir / "latest" / "US" / "trading_advice.csv",
        TRADING_ADVICE_FIELDNAMES,
        [
            {
                "run_date": "2026-06-22",
                "symbol": "VIXY",
                "market": "US",
                "asset_class": "etf",
                "portfolio_weight_hkd": "97.80%",
                "risk_flag": "overweight",
                "source": "tradingagents",
                "advice_action": "HOLD",
                "advice_summary": "",
                "raw_decision": raw_decision_with_all_reports(),
                "status": "ok",
                "error": "",
                "source_status": "ok",
                "fallback_reason": "",
                "fallback_from_date": "",
            }
        ],
    )
    write_decision_facts(
        config.data_dir / "latest" / "US" / "decision_facts.json",
        kline_hash=source_hash("old K report"),
        news_hash=source_hash("old news report"),
    )

    state = load_dashboard_state(config)

    detail = state.holdings[0]["decision_facts"]
    assert detail["kline"]["available"] is False
    assert set(detail["kline"]["fields"].values()) == {"缺失"}
    assert detail["news_sentiment"]["available"] is False
    assert set(detail["news_sentiment"]["fields"].values()) == {"缺失"}
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard.py::test_dashboard_attaches_hash_checked_decision_facts tests/test_dashboard.py::test_dashboard_stale_decision_facts_render_missing_fields -q
```

Expected: FAIL because dashboard does not attach `decision_facts`.

- [ ] **Step 3: Implement dashboard loading and freshness checks**

In `src/open_trader/dashboard.py`, add imports:

```python
from .decision_facts import (
    KLINE_FIELDS,
    MISSING_VALUE,
    NEWS_SENTIMENT_FIELDS,
    build_missing_fields,
    decision_facts_latest_path,
    extract_decision_sources,
    index_decision_facts_by_market_symbol,
    load_decision_facts_cache,
)
```

Add helper near `_latest_technical_facts_for_markets(...)`:

```python
def _latest_decision_facts_for_markets(
    *,
    data_dir: Path,
    markets: set[str],
) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, bool]]:
    records_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    file_exists_by_market: dict[str, bool] = {}
    for market in markets:
        path = decision_facts_latest_path(data_dir, market)
        file_exists_by_market[market] = path.exists()
        if path.exists():
            records_by_key.update(
                index_decision_facts_by_market_symbol(
                    load_decision_facts_cache(path)
                )
            )
    return records_by_key, file_exists_by_market
```

In `load_dashboard_state(...)`, after technical facts load:

```python
    decision_facts_by_holding, decision_facts_file_exists_by_market = (
        _latest_decision_facts_for_markets(
            data_dir=config.data_dir,
            markets=holding_markets,
        )
    )
```

Pass both to `_merge_holding(...)`.

Update `_merge_holding(...)` signature with:

```python
    decision_facts_by_holding: dict[tuple[str, str], dict[str, Any]],
    decision_facts_file_exists_by_market: dict[str, bool],
```

Inside `_merge_holding(...)`, set:

```python
    holding["decision_facts"] = _decision_facts_detail(
        decision_facts_by_holding.get(key) if key is not None else None,
        agent_report,
        cache_file_exists=(
            decision_facts_file_exists_by_market.get(key[0], False)
            if key is not None
            else False
        ),
    )
```

Add helpers near `_technical_facts_detail(...)`:

```python
def _decision_facts_detail(
    record: dict[str, Any] | None,
    advice_row: dict[str, str] | None,
    *,
    cache_file_exists: bool,
) -> dict[str, Any]:
    sources = extract_decision_sources(advice_row.get("raw_decision", "") if advice_row else "")
    if not cache_file_exists or record is None:
        return {
            "kline": _decision_module_unavailable(KLINE_FIELDS, sources.kline_hash),
            "news_sentiment": _decision_module_unavailable(
                NEWS_SENTIMENT_FIELDS,
                sources.news_sentiment_hash,
            ),
        }
    return {
        "kline": _decision_module_detail(
            record.get("kline"),
            fields=KLINE_FIELDS,
            current_source_hash=sources.kline_hash,
        ),
        "news_sentiment": _decision_module_detail(
            record.get("news_sentiment"),
            fields=NEWS_SENTIMENT_FIELDS,
            current_source_hash=sources.news_sentiment_hash,
        ),
    }


def _decision_module_detail(
    payload: object,
    *,
    fields: tuple[str, ...],
    current_source_hash: str,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return _decision_module_unavailable(fields, current_source_hash)
    stored_hash = str(payload.get("source_hash") or "").strip()
    raw_fields = payload.get("fields")
    if (
        not current_source_hash
        or stored_hash != current_source_hash
        or not isinstance(raw_fields, dict)
        or set(raw_fields) != set(fields)
    ):
        return _decision_module_unavailable(fields, current_source_hash, stored_hash)
    normalized = {
        field: str(raw_fields.get(field) or MISSING_VALUE).strip() or MISSING_VALUE
        for field in fields
    }
    return {
        "available": True,
        "status": str(payload.get("status") or "ok"),
        "source_hash": stored_hash,
        "current_source_hash": current_source_hash,
        "fields": normalized,
    }


def _decision_module_unavailable(
    fields: tuple[str, ...],
    current_source_hash: str,
    stored_hash: str = "",
) -> dict[str, Any]:
    return {
        "available": False,
        "status": "missing",
        "source_hash": stored_hash,
        "current_source_hash": current_source_hash,
        "fields": build_missing_fields(fields),
    }
```

- [ ] **Step 4: Run dashboard payload tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard.py::test_dashboard_attaches_hash_checked_decision_facts tests/test_dashboard.py::test_dashboard_stale_decision_facts_render_missing_fields -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add src/open_trader/dashboard.py tests/test_dashboard.py
git commit -m "feat: attach decision facts to dashboard"
```

---

### Task 6: Render Fixed Cards In Frontend

**Files:**
- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Modify: `src/open_trader/dashboard_static/dashboard.css`
- Modify: `tests/test_dashboard_web.py`

- [ ] **Step 1: Add failing frontend tests**

Append to `tests/test_dashboard_web.py`:

```python
def test_dashboard_renders_fixed_decision_fact_cards_in_chinese() -> None:
    js = DASHBOARD_JS.read_text(encoding="utf-8")
    script = f"""
{js}
const holding = {{
  market: "US",
  symbol: "SOXX",
  name: "iShares Semiconductor ETF",
  agent_report: {{available: true}},
  strategy: {{available: false}},
  trade_action: {{available: false}},
  decision_facts: {{
    kline: {{
      available: true,
      fields: {{
        trend: "过热拉升",
        position: "显著高于均线",
        momentum: "RSI 高位",
        key_levels: "支撑 580",
        risk: "超买风险"
      }}
    }},
    news_sentiment: {{
      available: true,
      fields: {{
        direction: "偏多",
        change: "较上次转强",
        catalyst: "AI 基建需求",
        risk: "估值过高",
        attention: "关注度升高"
      }}
    }}
  }}
}};
const html = renderTradingDecisionPlugins(holding);
console.log(html);
"""
    result = run_node(script)
    html = result.stdout
    assert "趋势 / K 线" in html
    assert "新闻 / 舆论" in html
    for label in ["趋势", "位置", "动能", "关键位", "风险", "方向", "变化", "催化", "热度"]:
        assert label in html
    assert "过热拉升" in html
    assert "偏多" in html
    assert "Bullish" not in html


def test_dashboard_missing_decision_facts_show_only_missing_values() -> None:
    js = DASHBOARD_JS.read_text(encoding="utf-8")
    script = f"""
{js}
const holding = {{
  market: "US",
  symbol: "SOXX",
  name: "iShares Semiconductor ETF",
  agent_report: {{available: false}},
  strategy: {{available: false}},
  trade_action: {{available: false}},
  decision_facts: {{
    kline: {{available: false, fields: {{trend: "缺失", position: "缺失", momentum: "缺失", key_levels: "缺失", risk: "缺失"}}}},
    news_sentiment: {{available: false, fields: {{direction: "缺失", change: "缺失", catalyst: "缺失", risk: "缺失", attention: "缺失"}}}}
  }}
}};
const html = renderTradingDecisionPlugins(holding);
console.log(html);
"""
    result = run_node(script)
    html = result.stdout
    assert html.count("缺失") >= 10
    assert "待接入" not in html
    assert "未来确认" not in html
    assert "暂无可用 K 线技术事实" not in html
```

Use existing helper names from `tests/test_dashboard_web.py` for reading JS and running Node. If the file uses different constants than `DASHBOARD_JS` and `run_node`, adapt the test to the existing helpers.

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py::test_dashboard_renders_fixed_decision_fact_cards_in_chinese tests/test_dashboard_web.py::test_dashboard_missing_decision_facts_show_only_missing_values -q
```

Expected: FAIL because cards still use `technical_facts` and the placeholder news card.

- [ ] **Step 3: Add frontend fixed field helpers**

In `src/open_trader/dashboard_static/dashboard.js`, replace `klineTechnicalFactsPlugin(holding)` in the plugin array with:

```javascript
    decisionFactsPlugin(holding, {
      title: "趋势 / K 线",
      moduleKey: "kline",
      fieldOrder: [
        ["trend", "趋势"],
        ["position", "位置"],
        ["momentum", "动能"],
        ["key_levels", "关键位"],
        ["risk", "风险"],
      ],
      score: "K线",
    }),
```

Replace the hard-coded `新闻 / 舆论` placeholder object with:

```javascript
    decisionFactsPlugin(holding, {
      title: "新闻 / 舆论",
      moduleKey: "news_sentiment",
      fieldOrder: [
        ["direction", "方向"],
        ["change", "变化"],
        ["catalyst", "催化"],
        ["risk", "风险"],
        ["attention", "热度"],
      ],
      score: "舆论",
    }),
```

Add helper functions before `renderDecisionPluginCard(plugin)`:

```javascript
function decisionFactsPlugin(holding, config) {
  const module = decisionFactsModule(holding, config.moduleKey);
  const fields = module && module.fields && typeof module.fields === "object"
    ? module.fields
    : {};
  const rows = config.fieldOrder.map(([key, label]) => ({
    label,
    value: hasValue(fields[key]) ? formatPlain(fields[key]) : "缺失",
  }));
  const available = Boolean(module && module.available === true);
  return {
    title: config.title,
    status: available ? "可用" : "缺失",
    tone: available ? "ok" : "partial",
    score: config.score,
    headline: rows[0] ? rows[0].value : "缺失",
    detail: "",
    bodyHtml: renderDecisionFactRows(rows),
    condition: "缺失",
  };
}

function decisionFactsModule(holding, moduleKey) {
  const detail = holding && holding.decision_facts && typeof holding.decision_facts === "object"
    ? holding.decision_facts
    : {};
  const module = detail[moduleKey];
  return module && typeof module === "object" ? module : null;
}

function renderDecisionFactRows(rows) {
  return `
    <div class="decision-fact-grid">
      ${rows.map((row) => `
        <div class="decision-fact-row">
          <span>${escapeHtml(row.label)}</span>
          <strong>${escapeHtml(row.value)}</strong>
        </div>
      `).join("")}
    </div>
  `;
}
```

Leave old technical fact helper functions in place if other tests still reference them directly. Do not use them for the `趋势 / K 线` card.

- [ ] **Step 4: Add CSS for fixed field grid**

In `src/open_trader/dashboard_static/dashboard.css`, near `.technical-fact-grid`, add:

```css
.decision-fact-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
  margin-top: 12px;
}

.decision-fact-row {
  min-height: 84px;
  padding: 10px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--surface-muted);
}

.decision-fact-row span {
  display: block;
  margin-bottom: 6px;
  color: var(--text-muted);
  font-size: 12px;
  font-weight: 700;
}

.decision-fact-row strong {
  display: block;
  color: var(--text);
  font-size: 14px;
  line-height: 1.45;
  word-break: break-word;
}
```

Where responsive rules include `.technical-fact-grid`, add `.decision-fact-grid` to the same selector so the grid becomes one column on narrow screens.

- [ ] **Step 5: Run frontend tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py::test_dashboard_renders_fixed_decision_fact_cards_in_chinese tests/test_dashboard_web.py::test_dashboard_missing_decision_facts_show_only_missing_values -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

Run:

```bash
git add src/open_trader/dashboard_static/dashboard.js src/open_trader/dashboard_static/dashboard.css tests/test_dashboard_web.py
git commit -m "feat: render fixed decision fact cards"
```

---

### Task 7: Documentation And Verification

**Files:**
- Modify: `README.md`
- Modify: `README.zh-CN.md`

- [ ] **Step 1: Update bilingual docs**

In `README.md`, add a short section near existing TradingAgents/technical facts documentation:

```markdown
### Fixed decision facts

After a market-scoped TradingAgents run, Open Trader extracts fixed Chinese
decision fields for the dashboard:

```text
data/runs/<YYYY-MM-DD>/<MARKET>/decision_facts.json
data/latest/<MARKET>/decision_facts.json
```

Manual command:

```bash
.venv/bin/python -m open_trader extract-decision-facts \
  --advice data/latest/US/trading_advice.csv \
  --data-dir data \
  --date 2026-06-22 \
  --market US \
  --update-latest
```

The dashboard uses fixed fields for `Trend / K-line` and `News / Sentiment`.
Missing field values are rendered as `缺失`; the dashboard does not display raw
English TradingAgents prose in those plugin fields.
```

In `README.zh-CN.md`, add:

```markdown
### 固定交易事实字段

按市场运行 TradingAgents 后，Open Trader 会为看板抽取固定中文字段：

```text
data/runs/<YYYY-MM-DD>/<MARKET>/decision_facts.json
data/latest/<MARKET>/decision_facts.json
```

手动命令：

```bash
.venv/bin/python -m open_trader extract-decision-facts \
  --advice data/latest/US/trading_advice.csv \
  --data-dir data \
  --date 2026-06-22 \
  --market US \
  --update-latest
```

看板的 `趋势 / K 线` 和 `新闻 / 舆论` 只展示固定字段。缺少字段值时显示
`缺失`，不会在这些插件字段中直接展示 TradingAgents 英文原文。
```

- [ ] **Step 2: Run focused test suite**

Run:

```bash
.venv/bin/python -m pytest tests/test_decision_facts.py tests/test_premarket_cli.py tests/test_premarket_pipeline.py tests/test_daily_premarket.py tests/test_dashboard.py tests/test_dashboard_web.py -q
```

Expected: PASS.

- [ ] **Step 3: Run manual extractor on current US latest advice**

Run:

```bash
.venv/bin/python -m open_trader extract-decision-facts \
  --advice data/latest/US/trading_advice.csv \
  --data-dir data \
  --date 2026-06-22 \
  --market US \
  --update-latest
```

Expected output contains:

```text
run_date: 2026-06-22
decision_facts:
decision_facts_json: data/runs/2026-06-22/US/decision_facts.json
latest: data/latest/US/decision_facts.json
```

If `DEEPSEEK_API_KEY` is not configured, record that manual live extraction was not run. Do not fake a successful live extraction.

- [ ] **Step 4: Inspect SOXX artifact values**

Run:

```bash
jq '.records[] | select(.symbol=="SOXX") | {kline: .kline.fields, news_sentiment: .news_sentiment.fields}' data/latest/US/decision_facts.json
```

Expected: exactly five K-line fields and five news/sentiment fields, with Chinese text or `缺失`.

- [ ] **Step 5: Verify dashboard in browser**

Start the dashboard:

```bash
.venv/bin/python -m open_trader dashboard \
  --portfolio data/latest/portfolio.csv \
  --data-dir data \
  --reports-dir reports \
  --port 8765
```

Open `http://127.0.0.1:8765`, select `US.SOXX`, and verify:

- `趋势 / K 线` shows `趋势`, `位置`, `动能`, `关键位`, `风险`
- `新闻 / 舆论` shows `方向`, `变化`, `催化`, `风险`, `热度`
- missing values appear as `缺失`
- no English TradingAgents prose appears in these field values

Stop the dashboard process before finishing.

- [ ] **Step 6: Commit docs**

Run:

```bash
git add README.md README.zh-CN.md
git commit -m "docs: document fixed decision facts"
```

---

## Self-Review Checklist

- Spec coverage:
  - Fixed K-line fields are implemented in Tasks 1, 2, 5, and 6.
  - Fixed news/sentiment fields are implemented in Tasks 1, 2, 5, and 6.
  - Missing values as `缺失` are implemented in Tasks 1, 2, 5, and 6.
  - Source hashes and stale dashboard behavior are implemented in Task 5.
  - CLI and pipeline integration are implemented in Tasks 3 and 4.
  - Documentation and manual verification are implemented in Task 7.
- Placeholder scan:
  - No `TBD`, `TODO`, or unspecified implementation steps remain.
  - Every code-changing task includes concrete code or exact replacement snippets.
- Type consistency:
  - `DecisionFactsResult`, `DecisionFactsExtractor`, `LLMDecisionFactsExtractor`, `generate_decision_facts`, and `decision_facts_*_path` are introduced before use.
  - Frontend payload key is consistently `holding.decision_facts`.
  - Module keys are consistently `kline` and `news_sentiment`.
