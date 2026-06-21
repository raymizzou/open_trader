# Kline Technical Facts Plugin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a cached `趋势 / K 线` dashboard plugin that shows objective TradingAgents technical facts with explicit data dates and indicator timeframes.

**Architecture:** Add a focused `technical_facts` module that reads `trading_advice.csv`, extracts `raw_decision.state.market_report`, calls a strict-JSON LLM extractor only when the source hash changed, and writes dated/latest JSON artifacts. Extend `run_premarket()` and a manual CLI command to generate the cache, then merge valid cache rows into `/api/dashboard` for the static frontend to render without calling the LLM.

**Tech Stack:** Python stdlib JSON/CSV/hashlib/dataclasses, existing OpenAI-compatible DeepSeek client pattern, existing `MarketScope` path helpers, static dashboard JavaScript/CSS, pytest with fake extractor clients.

---

## File Structure

- Create `src/open_trader/technical_facts.py`: source parsing, schema normalization, cache reuse, LLM extraction orchestration, artifact writing, and dashboard cache loading helpers.
- Create `tests/test_technical_facts.py`: unit tests for parsing, strict extraction, cache reuse, stale detection, market-scoped paths, and artifact promotion.
- Modify `src/open_trader/advice/premarket.py`: call a technical facts generator after `trading_advice.csv` is written and before latest promotion completes.
- Modify `tests/test_premarket_pipeline.py`: assert premarket runs generate technical facts artifacts and preserve latest on failure.
- Modify `src/open_trader/cli.py`: add `extract-technical-facts` CLI and wire the default extractor into `run-premarket`.
- Modify `tests/test_premarket_cli.py`: cover manual extraction command with a fake extractor.
- Modify `src/open_trader/dashboard.py`: load `technical_facts.json`, compare hashes against latest advice rows, and attach `technical_facts` to each holding.
- Modify `tests/test_dashboard.py`: cover available, missing, stale, and missing-timeframe dashboard states.
- Modify `src/open_trader/dashboard_static/dashboard.js`: render the `趋势 / K 线` plugin from cached technical facts instead of placeholder text.
- Modify `src/open_trader/dashboard_static/dashboard.css`: add compact styles for timeframe/date/status fields inside the plugin.
- Modify `tests/test_dashboard_web.py` or existing frontend asset tests: assert static asset text includes the new Chinese state labels.

---

### Task 1: Core Technical Facts Cache Module

**Files:**
- Create: `src/open_trader/technical_facts.py`
- Test: `tests/test_technical_facts.py`

- [ ] **Step 1: Write failing tests for source parsing and hashing**

Add this to `tests/test_technical_facts.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_technical_facts.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'open_trader.technical_facts'`.

- [ ] **Step 3: Implement source parsing and hashing**

Create `src/open_trader/technical_facts.py` with:

```python
from __future__ import annotations

import csv
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
.venv/bin/python -m pytest tests/test_technical_facts.py -q
```

Expected: PASS for the parsing and hashing tests.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/technical_facts.py tests/test_technical_facts.py
git commit -m "feat: parse technical fact sources"
```

---

### Task 2: LLM Extraction, Cache Reuse, And Artifact Writing

**Files:**
- Modify: `src/open_trader/technical_facts.py`
- Modify: `tests/test_technical_facts.py`

- [ ] **Step 1: Add failing tests for extraction, ignored transaction proposals, cache reuse, and market-scoped paths**

Append to `tests/test_technical_facts.py`:

```python
from open_trader.market_scope import MarketScope
from open_trader.technical_facts import (
    TechnicalFactsExtractor,
    build_freshness,
    generate_technical_facts,
    load_technical_facts_cache,
    technical_facts_latest_path,
    technical_facts_run_path,
)


class FakeExtractor:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def extract(self, *, market: str, symbol: str, run_date: str, market_report: str) -> dict[str, object]:
        self.calls.append(market_report)
        assert "FINAL TRANSACTION PROPOSAL" in market_report
        return {
            "schema_version": "open_trader.technical_facts.v1",
            "status": "present",
            "source_date": run_date,
            "market_data_as_of": "2026-06-18",
            "symbol": f"{market}.{symbol}",
            "timeframes": [
                {
                    "timeframe": "daily",
                    "timeframe_label": "日线",
                    "current_price": "411.60",
                    "trend_summary": "价格高于主要均线。",
                    "moving_averages": {"ema_10": "398.15", "sma_50": "368.24"},
                    "macd": {"crossover": "6月17日金叉"},
                    "rsi": {"value": "56.88"},
                    "bollinger": {},
                    "atr": {"value": "33.17", "percent_of_price": "8.1%"},
                    "volume": {},
                    "support_resistance": {"support_levels": [], "resistance_levels": []},
                    "price_action": {"timeline": []},
                    "risks": [],
                    "evidence_quotes": ["MACD line crossed above Signal line on June 17."],
                }
            ],
        }


def test_build_freshness_prefers_timeframe_and_market_data_date() -> None:
    freshness = build_freshness(
        market_data_as_of="2026-06-18",
        run_date="2026-06-19",
        has_unknown_timeframe=False,
    )

    assert freshness == {
        "status": "fresh",
        "message": "日线数据截至 2026-06-18",
    }


def test_build_freshness_marks_missing_date() -> None:
    freshness = build_freshness(
        market_data_as_of="",
        run_date="2026-06-19",
        has_unknown_timeframe=False,
    )

    assert freshness["status"] == "missing_date"
    assert freshness["message"] == "行情日期缺失，报告生成于 2026-06-19"


def test_generate_technical_facts_writes_run_and_latest_cache(tmp_path: Path) -> None:
    advice_path = tmp_path / "data/runs/2026-06-19/trading_advice.csv"
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
                "advice_action": "Buy",
                "advice_summary": "",
                "raw_decision": raw_decision_with_market_report(
                    "Daily MACD crossed. FINAL TRANSACTION PROPOSAL: BUY"
                ),
                "status": "ok",
                "error": "",
                "source_status": "ok",
                "fallback_reason": "",
                "fallback_from_date": "",
            }
        ],
    )
    extractor = FakeExtractor()

    result = generate_technical_facts(
        advice_path=advice_path,
        data_dir=tmp_path / "data",
        run_date="2026-06-19",
        extractor=extractor,
        update_latest=True,
        market=None,
    )

    assert result.records == 1
    assert result.extracted == 1
    assert result.reused == 0
    assert result.run_path == tmp_path / "data/runs/2026-06-19/technical_facts.json"
    assert result.latest_path == tmp_path / "data/latest/technical_facts.json"
    cache = load_technical_facts_cache(result.latest_path)
    row = cache["records"][0]
    assert row["market"] == "HK"
    assert row["symbol"] == "02476"
    assert row["extraction_status"] == "ok"
    assert row["freshness"]["message"] == "日线数据截至 2026-06-18"
    assert "BUY" not in json.dumps(row["facts"], ensure_ascii=False)


def test_generate_technical_facts_reuses_matching_latest_cache(tmp_path: Path) -> None:
    advice_path = tmp_path / "data/runs/2026-06-19/trading_advice.csv"
    report = "Daily RSI 56.88"
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
                "raw_decision": raw_decision_with_market_report(report),
                "status": "ok",
                "error": "",
                "source_status": "ok",
                "fallback_reason": "",
                "fallback_from_date": "",
            }
        ],
    )
    extractor = FakeExtractor()
    first = generate_technical_facts(
        advice_path=advice_path,
        data_dir=tmp_path / "data",
        run_date="2026-06-19",
        extractor=extractor,
        update_latest=True,
        market=None,
    )

    second_extractor = FakeExtractor()
    second = generate_technical_facts(
        advice_path=advice_path,
        data_dir=tmp_path / "data",
        run_date="2026-06-19",
        extractor=second_extractor,
        update_latest=True,
        market=None,
    )

    assert first.extracted == 1
    assert second.extracted == 0
    assert second.reused == 1
    assert second_extractor.calls == []


def test_technical_facts_paths_support_market_scope(tmp_path: Path) -> None:
    assert technical_facts_run_path(
        tmp_path / "data", "2026-06-19", MarketScope.HK
    ) == tmp_path / "data/runs/2026-06-19/HK/technical_facts.json"
    assert technical_facts_latest_path(
        tmp_path / "data", MarketScope.HK
    ) == tmp_path / "data/latest/HK/technical_facts.json"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_technical_facts.py -q
```

Expected: FAIL with missing `TechnicalFactsExtractor`, `generate_technical_facts`, and path helpers.

- [ ] **Step 3: Implement cache generation**

Extend `src/open_trader/technical_facts.py` with:

```python
from datetime import datetime
from tempfile import NamedTemporaryFile
from typing import Protocol
from zoneinfo import ZoneInfo

from .market_scope import (
    MarketScope,
    market_run_dir,
    market_scoped_latest_path,
)

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


class TechnicalFactsExtractor(Protocol):
    def extract(
        self,
        *,
        market: str,
        symbol: str,
        run_date: str,
        market_report: str,
    ) -> dict[str, object]:
        raise NotImplementedError


@dataclass(frozen=True)
class TechnicalFactsResult:
    records: int
    extracted: int
    reused: int
    failed: int
    run_path: Path
    latest_path: Path | None


def technical_facts_run_path(
    data_dir: Path,
    run_date: str,
    market: MarketScope | None,
) -> Path:
    if market is not None:
        return market_run_dir(data_dir, run_date, market) / "technical_facts.json"
    return data_dir / "runs" / run_date / "technical_facts.json"


def technical_facts_latest_path(
    data_dir: Path,
    market: MarketScope | None,
) -> Path:
    if market is not None:
        return market_scoped_latest_path(data_dir, market, "technical_facts.json")
    return data_dir / "latest" / "technical_facts.json"


def load_technical_facts_cache(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "schema_version": TECHNICAL_FACTS_SCHEMA_VERSION,
            "records": [],
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {
            "schema_version": TECHNICAL_FACTS_SCHEMA_VERSION,
            "records": [],
        }
    if not isinstance(payload, dict):
        return {
            "schema_version": TECHNICAL_FACTS_SCHEMA_VERSION,
            "records": [],
        }
    records = payload.get("records")
    if not isinstance(records, list):
        payload["records"] = []
    return payload


def build_freshness(
    *,
    market_data_as_of: str,
    run_date: str,
    has_unknown_timeframe: bool,
) -> dict[str, str]:
    if has_unknown_timeframe:
        return {
            "status": "missing_timeframe",
            "message": "指标周期缺失，需复核",
        }
    if market_data_as_of:
        return {
            "status": "fresh",
            "message": f"日线数据截至 {market_data_as_of}",
        }
    if run_date:
        return {
            "status": "missing_date",
            "message": f"行情日期缺失，报告生成于 {run_date}",
        }
    return {
        "status": "missing_date",
        "message": "行情日期缺失",
    }


def generate_technical_facts(
    *,
    advice_path: Path,
    data_dir: Path,
    run_date: str,
    extractor: TechnicalFactsExtractor,
    update_latest: bool,
    market: MarketScope | None,
) -> TechnicalFactsResult:
    sources = load_advice_sources(advice_path)
    latest_path = technical_facts_latest_path(data_dir, market)
    run_path = technical_facts_run_path(data_dir, run_date, market)
    previous_records = _records_by_identity(load_technical_facts_cache(latest_path))
    records: list[dict[str, Any]] = []
    extracted = 0
    reused = 0
    failed = 0
    for source in sources:
        identity = (source.market, source.symbol, source.source_advice_hash)
        previous = previous_records.get(identity)
        if previous is not None:
            records.append(previous)
            reused += 1
            continue
        record = _extract_record(source, extractor)
        if record["extraction_status"] == "ok":
            extracted += 1
        else:
            failed += 1
        records.append(record)
    payload = {
        "schema_version": TECHNICAL_FACTS_SCHEMA_VERSION,
        "run_date": run_date,
        "market": market.value if market is not None else "",
        "generated_at": _now_text(),
        "records": records,
    }
    _atomic_write_json(run_path, payload)
    promoted_latest = latest_path if update_latest else None
    if update_latest:
        _atomic_write_json(latest_path, payload)
    return TechnicalFactsResult(
        records=len(records),
        extracted=extracted,
        reused=reused,
        failed=failed,
        run_path=run_path,
        latest_path=promoted_latest,
    )


def _extract_record(
    source: AdviceSource,
    extractor: TechnicalFactsExtractor,
) -> dict[str, Any]:
    base = {
        "market": source.market,
        "symbol": source.symbol,
        "run_date": source.run_date,
        "source": "tradingagents_market_report",
        "source_advice_hash": source.source_advice_hash,
        "source_status": source.source_status,
        "extracted_at": _now_text(),
    }
    if not source.market_report.strip():
        return {
            **base,
            "extraction_status": "missing_source",
            "market_data_as_of": "",
            "freshness": {"status": "missing_source", "message": "缺少 TradingAgents 技术报告"},
            "facts": {},
            "error": "raw_decision.state.market_report is missing",
        }
    try:
        facts = extractor.extract(
            market=source.market,
            symbol=source.symbol,
            run_date=source.run_date,
            market_report=source.market_report,
        )
        _validate_facts(facts)
    except Exception as exc:
        return {
            **base,
            "extraction_status": "extraction_failed",
            "market_data_as_of": "",
            "freshness": {"status": "extraction_failed", "message": "抽取失败，需查看日志"},
            "facts": {},
            "error": str(exc),
        }
    market_data_as_of = str(facts.get("market_data_as_of") or "")
    has_unknown_timeframe = _has_unknown_timeframe(facts)
    return {
        **base,
        "extraction_status": "ok",
        "market_data_as_of": market_data_as_of,
        "freshness": build_freshness(
            market_data_as_of=market_data_as_of,
            run_date=source.run_date,
            has_unknown_timeframe=has_unknown_timeframe,
        ),
        "facts": facts,
        "error": "",
    }


def _records_by_identity(payload: dict[str, Any]) -> dict[tuple[str, str, str], dict[str, Any]]:
    records: dict[tuple[str, str, str], dict[str, Any]] = {}
    for record in payload.get("records", []):
        if not isinstance(record, dict):
            continue
        market = str(record.get("market") or "").upper()
        symbol = str(record.get("symbol") or "").upper()
        hash_value = str(record.get("source_advice_hash") or "")
        if market and symbol and hash_value:
            records[(market, symbol, hash_value)] = record
    return records


def _validate_facts(facts: dict[str, object]) -> None:
    if facts.get("schema_version") != FACTS_SCHEMA_VERSION:
        raise ValueError("technical facts schema_version is invalid")
    timeframes = facts.get("timeframes")
    if not isinstance(timeframes, list):
        raise ValueError("technical facts timeframes must be a list")


def _has_unknown_timeframe(facts: dict[str, object]) -> bool:
    timeframes = facts.get("timeframes")
    if not isinstance(timeframes, list):
        return True
    if not timeframes:
        return True
    for item in timeframes:
        if not isinstance(item, dict):
            return True
        if str(item.get("timeframe") or "unknown") == "unknown":
            return True
    return False


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        temp_path.replace(path)
    except Exception:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()
        raise


def _now_text() -> str:
    return datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds")
```

- [ ] **Step 4: Remove transaction proposal leakage in fake test expectation**

The `FakeExtractor` proves the source report may contain transaction text. Add this assertion inside `generate_technical_facts()` before calling the extractor:

```python
market_report = _strip_transaction_proposal(source.market_report)
```

Add helper:

```python
def _strip_transaction_proposal(report: str) -> str:
    marker = "FINAL TRANSACTION PROPOSAL"
    index = report.upper().find(marker)
    if index == -1:
        return report
    return report[:index].rstrip()
```

Then update the `FakeExtractor.extract()` assertion to:

```python
assert "FINAL TRANSACTION PROPOSAL" not in market_report
```

- [ ] **Step 5: Run focused tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_technical_facts.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/open_trader/technical_facts.py tests/test_technical_facts.py
git commit -m "feat: cache technical facts"
```

---

### Task 3: Default LLM Extractor Client

**Files:**
- Modify: `src/open_trader/technical_facts.py`
- Modify: `tests/test_technical_facts.py`

- [ ] **Step 1: Add failing tests for strict LLM client parsing**

Append to `tests/test_technical_facts.py`:

```python
from open_trader.technical_facts import LLMTechnicalFactsExtractor


class FakeLLMClient:
    def __init__(self, content: str) -> None:
        self.content = content
        self.messages: list[list[dict[str, str]]] = []

    def create(self, *, messages: list[dict[str, str]], temperature: float) -> str:
        self.messages.append(messages)
        return self.content


def test_llm_extractor_parses_strict_json() -> None:
    client = FakeLLMClient(
        json.dumps(
            {
                "schema_version": "open_trader.technical_facts.v1",
                "status": "present",
                "source_date": "2026-06-19",
                "market_data_as_of": "2026-06-18",
                "symbol": "HK.02476",
                "timeframes": [
                    {
                        "timeframe": "daily",
                        "timeframe_label": "日线",
                        "current_price": "411.60",
                    }
                ],
            }
        )
    )
    extractor = LLMTechnicalFactsExtractor(client=client)

    facts = extractor.extract(
        market="HK",
        symbol="02476",
        run_date="2026-06-19",
        market_report="Daily technical report. FINAL TRANSACTION PROPOSAL: BUY",
    )

    assert facts["schema_version"] == "open_trader.technical_facts.v1"
    assert facts["market_data_as_of"] == "2026-06-18"
    prompt_text = json.dumps(client.messages, ensure_ascii=False)
    assert "只抽取客观技术面事实" in prompt_text
    assert "忽略 FINAL TRANSACTION PROPOSAL" in prompt_text


def test_llm_extractor_rejects_non_json_response() -> None:
    extractor = LLMTechnicalFactsExtractor(client=FakeLLMClient("not json"))

    try:
        extractor.extract(
            market="HK",
            symbol="02476",
            run_date="2026-06-19",
            market_report="Daily technical report",
        )
    except ValueError as exc:
        assert "valid JSON" in str(exc)
    else:
        raise AssertionError("expected ValueError")
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_technical_facts.py::test_llm_extractor_parses_strict_json tests/test_technical_facts.py::test_llm_extractor_rejects_non_json_response -q
```

Expected: FAIL because `LLMTechnicalFactsExtractor` does not exist.

- [ ] **Step 3: Implement default OpenAI-compatible extractor**

Extend `src/open_trader/technical_facts.py` with:

```python
import os
from collections.abc import Callable

from openai import OpenAI

from .advice.change_classifier import DEEPSEEK_BASE_URL, DEFAULT_CLASSIFIER_MODEL


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
        )
        content = response.choices[0].message.content
        return content or ""


class LLMTechnicalFactsExtractor:
    def __init__(self, *, client: object | None = None) -> None:
        self.client = client or OpenAITextClient()

    def extract(
        self,
        *,
        market: str,
        symbol: str,
        run_date: str,
        market_report: str,
    ) -> dict[str, object]:
        messages = [
            {
                "role": "system",
                "content": _technical_facts_system_prompt(),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "market": market,
                        "symbol": symbol,
                        "run_date": run_date,
                        "market_report": _strip_transaction_proposal(market_report),
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        content = self.client.create(messages=messages, temperature=0)
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError("LLM technical facts response must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("LLM technical facts response must be a JSON object")
        _validate_facts(payload)
        return payload


def _technical_facts_system_prompt() -> str:
    return (
        "你是 open_trader 的技术面事实抽取器。只抽取客观技术面事实，输出严格 JSON。"
        "忽略 FINAL TRANSACTION PROPOSAL、BUY、SELL、HOLD、Underweight、仓位建议、"
        "交易建议和执行建议。每个 RSI、MACD、均线、布林带、ATR、成交量信号都必须带"
        "timeframe。若报告没有明确周期，timeframe 使用 unknown，timeframe_label 使用"
        "\"周期缺失\"。缺失字段使用空字符串或空数组，不要猜测。schema_version 必须是 "
        f"{FACTS_SCHEMA_VERSION}。"
    )
```

- [ ] **Step 4: Run focused tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_technical_facts.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/technical_facts.py tests/test_technical_facts.py
git commit -m "feat: add technical facts llm extractor"
```

---

### Task 4: Premarket Pipeline Integration

**Files:**
- Modify: `src/open_trader/advice/premarket.py`
- Modify: `tests/test_premarket_pipeline.py`

- [ ] **Step 1: Add failing pipeline test**

Append to `tests/test_premarket_pipeline.py`:

```python
from open_trader.technical_facts import TechnicalFactsResult


class FakeTechnicalFactsGenerator:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def __call__(
        self,
        *,
        advice_path: Path,
        data_dir: Path,
        run_date: str,
        update_latest: bool,
        market,
    ) -> TechnicalFactsResult:
        self.calls.append(
            {
                "advice_path": advice_path,
                "data_dir": data_dir,
                "run_date": run_date,
                "update_latest": update_latest,
                "market": market,
            }
        )
        run_path = advice_path.with_name("technical_facts.json")
        run_path.write_text(
            '{"schema_version":"open_trader.technical_facts_cache.v1","records":[]}\n',
            encoding="utf-8",
        )
        latest_path = data_dir / "latest" / "technical_facts.json" if update_latest else None
        if latest_path is not None:
            latest_path.parent.mkdir(parents=True, exist_ok=True)
            latest_path.write_text(run_path.read_text(encoding="utf-8"), encoding="utf-8")
        return TechnicalFactsResult(
            records=0,
            extracted=0,
            reused=0,
            failed=0,
            run_path=run_path,
            latest_path=latest_path,
        )


def test_run_premarket_generates_technical_facts_after_advice(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    reports_dir = tmp_path / "reports"
    portfolio_path = data_dir / "latest" / "portfolio.csv"
    write_portfolio(portfolio_path)
    generator = FakeTechnicalFactsGenerator()

    result = run_premarket(
        run_date="2026-06-19",
        portfolio_path=portfolio_path,
        data_dir=data_dir,
        reports_dir=reports_dir,
        advice_runner=FakeAdviceRunner(),
        classifier=FakeClassifier(),
        technical_facts_generator=generator,
    )

    assert generator.calls == [
        {
            "advice_path": result.advice_path,
            "data_dir": data_dir,
            "run_date": "2026-06-19",
            "update_latest": True,
            "market": None,
        }
    ]
    assert (data_dir / "runs/2026-06-19/technical_facts.json").exists()
    assert (data_dir / "latest/technical_facts.json").exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
.venv/bin/python -m pytest tests/test_premarket_pipeline.py::test_run_premarket_generates_technical_facts_after_advice -q
```

Expected: FAIL because `run_premarket()` does not accept `technical_facts_generator`.

- [ ] **Step 3: Modify `run_premarket()` signature and call site**

In `src/open_trader/advice/premarket.py`, add imports:

```python
from open_trader.technical_facts import (
    LLMTechnicalFactsExtractor,
    TechnicalFactsResult,
    generate_technical_facts,
)
```

Add type alias near `DeadlineReached`:

```python
TechnicalFactsGenerator = Callable[..., TechnicalFactsResult]
```

Extend `run_premarket()` signature:

```python
    technical_facts_generator: TechnicalFactsGenerator | None = None,
) -> PremarketResult:
```

After `advice_path = _write_trading_advice_run(...)` in the normal records branch and before `if update_latest:`, add:

```python
    _generate_technical_facts_after_advice(
        advice_path=advice_path,
        data_dir=data_dir,
        run_date=run_date,
        update_latest=update_latest,
        market=market_scope,
        technical_facts_generator=technical_facts_generator,
    )
```

Add helper:

```python
def _generate_technical_facts_after_advice(
    *,
    advice_path: Path,
    data_dir: Path,
    run_date: str,
    update_latest: bool,
    market: MarketScope | None,
    technical_facts_generator: TechnicalFactsGenerator | None,
) -> TechnicalFactsResult:
    generator = technical_facts_generator
    if generator is None:
        extractor = LLMTechnicalFactsExtractor()

        def generator(**kwargs: object) -> TechnicalFactsResult:
            return generate_technical_facts(extractor=extractor, **kwargs)  # type: ignore[arg-type]

    return generator(
        advice_path=advice_path,
        data_dir=data_dir,
        run_date=run_date,
        update_latest=update_latest,
        market=market,
    )
```

Do not call this helper in the no-eligible early return branch yet; that branch writes empty advice and can be covered later if desired.

- [ ] **Step 4: Run focused tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_premarket_pipeline.py::test_run_premarket_generates_technical_facts_after_advice tests/test_technical_facts.py -q
```

Expected: PASS.

- [ ] **Step 5: Run full premarket pipeline tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_premarket_pipeline.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/open_trader/advice/premarket.py tests/test_premarket_pipeline.py
git commit -m "feat: extract technical facts after premarket"
```

---

### Task 5: Manual CLI Backfill Command

**Files:**
- Modify: `src/open_trader/cli.py`
- Modify: `tests/test_premarket_cli.py`

- [ ] **Step 1: Add failing CLI test**

Add to `tests/test_premarket_cli.py`:

```python
def test_extract_technical_facts_cli_writes_cache(tmp_path: Path, monkeypatch, capsys) -> None:
    advice_path = tmp_path / "data/latest/trading_advice.csv"
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

    class FakeExtractor:
        def extract(self, *, market: str, symbol: str, run_date: str, market_report: str) -> dict[str, object]:
            return {
                "schema_version": "open_trader.technical_facts.v1",
                "status": "present",
                "source_date": run_date,
                "market_data_as_of": "2026-06-18",
                "symbol": f"{market}.{symbol}",
                "timeframes": [{"timeframe": "daily", "timeframe_label": "日线"}],
            }

    monkeypatch.setattr(cli, "LLMTechnicalFactsExtractor", lambda: FakeExtractor())

    exit_code = cli.main(
        [
            "extract-technical-facts",
            "--advice",
            str(advice_path),
            "--data-dir",
            str(tmp_path / "data"),
            "--date",
            "2026-06-19",
            "--update-latest",
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "technical_facts:" in output
    assert (tmp_path / "data/runs/2026-06-19/technical_facts.json").exists()
    assert (tmp_path / "data/latest/technical_facts.json").exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
.venv/bin/python -m pytest tests/test_premarket_cli.py::test_extract_technical_facts_cli_writes_cache -q
```

Expected: FAIL because command `extract-technical-facts` is unknown.

- [ ] **Step 3: Add CLI parser and handler**

In `src/open_trader/cli.py`, add imports:

```python
from .technical_facts import LLMTechnicalFactsExtractor, generate_technical_facts
from .market_scope import parse_market_scope
```

Add parser near other commands:

```python
    extract_facts_parser = subparsers.add_parser(
        "extract-technical-facts",
        help="Extract cached technical facts from TradingAgents advice",
    )
    extract_facts_parser.add_argument(
        "--advice",
        type=Path,
        default=Path("data/latest/trading_advice.csv"),
        help="Input trading_advice.csv",
    )
    extract_facts_parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("data"),
        help="Data directory",
    )
    extract_facts_parser.add_argument(
        "--date",
        required=True,
        help="Run date for dated technical facts output",
    )
    extract_facts_parser.add_argument(
        "--market",
        choices=["HK", "US"],
        help="Optional market-scoped cache output",
    )
    extract_facts_parser.add_argument(
        "--update-latest",
        action="store_true",
        help="Promote the generated technical facts cache to data/latest",
    )
```

Add handler in `main()`:

```python
    if args.command == "extract-technical-facts":
        market = parse_market_scope(args.market) if args.market else None
        result = generate_technical_facts(
            advice_path=args.advice,
            data_dir=args.data_dir,
            run_date=args.date,
            extractor=LLMTechnicalFactsExtractor(),
            update_latest=args.update_latest,
            market=market,
        )
        print(f"technical_facts: {result.records}")
        print(f"extracted: {result.extracted}")
        print(f"reused: {result.reused}")
        print(f"failed: {result.failed}")
        print(f"technical_facts_json: {result.run_path}")
        if result.latest_path is not None:
            print(f"latest: {result.latest_path}")
        return 0
```

- [ ] **Step 4: Run CLI tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_premarket_cli.py::test_extract_technical_facts_cli_writes_cache -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/cli.py tests/test_premarket_cli.py
git commit -m "feat: add technical facts extraction cli"
```

---

### Task 6: Dashboard Backend Merge

**Files:**
- Modify: `src/open_trader/technical_facts.py`
- Modify: `src/open_trader/dashboard.py`
- Modify: `tests/test_dashboard.py`

- [ ] **Step 1: Add failing dashboard tests**

Add to `tests/test_dashboard.py`:

```python
import hashlib
import json


def technical_hash(report: str) -> str:
    return "sha256:" + hashlib.sha256(report.encode("utf-8")).hexdigest()


def write_technical_facts(path: Path, report: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": "open_trader.technical_facts_cache.v1",
                "run_date": "2026-06-19",
                "records": [
                    {
                        "market": "HK",
                        "symbol": "02476",
                        "run_date": "2026-06-19",
                        "source": "tradingagents_market_report",
                        "source_advice_hash": technical_hash(report),
                        "source_status": "ok",
                        "extraction_status": "ok",
                        "market_data_as_of": "2026-06-18",
                        "extracted_at": "2026-06-21T09:40:00+08:00",
                        "freshness": {
                            "status": "fresh",
                            "message": "日线数据截至 2026-06-18",
                        },
                        "facts": {
                            "schema_version": "open_trader.technical_facts.v1",
                            "timeframes": [
                                {
                                    "timeframe": "daily",
                                    "timeframe_label": "日线",
                                    "rsi": {"value": "56.88"},
                                }
                            ],
                        },
                        "error": "",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_dashboard_merges_fresh_technical_facts(tmp_path: Path) -> None:
    config = dashboard_config(tmp_path)
    write_portfolio(config.portfolio_path, market="HK", symbol="02476")
    report = "Daily RSI 56.88"
    write_trading_advice(config.data_dir / "latest/trading_advice.csv", report=report)
    write_technical_facts(config.data_dir / "latest/technical_facts.json", report)

    state = load_dashboard_state(config).to_dict()

    holding = state["holdings"][0]
    facts = holding["technical_facts"]
    assert facts["available"] is True
    assert facts["freshness"]["message"] == "日线数据截至 2026-06-18"
    assert facts["facts"]["timeframes"][0]["timeframe_label"] == "日线"


def test_dashboard_hides_stale_technical_values(tmp_path: Path) -> None:
    config = dashboard_config(tmp_path)
    write_portfolio(config.portfolio_path, market="HK", symbol="02476")
    write_trading_advice(config.data_dir / "latest/trading_advice.csv", report="New report")
    write_technical_facts(config.data_dir / "latest/technical_facts.json", "Old report")

    state = load_dashboard_state(config).to_dict()

    facts = state["holdings"][0]["technical_facts"]
    assert facts["available"] is False
    assert facts["freshness"]["status"] == "stale"
    assert facts["facts"] == {}
```

If `tests/test_dashboard.py` has different helper names, adapt the helper calls to its existing fixtures while preserving these assertions.

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard.py::test_dashboard_merges_fresh_technical_facts tests/test_dashboard.py::test_dashboard_hides_stale_technical_values -q
```

Expected: FAIL because holdings do not include `technical_facts`.

- [ ] **Step 3: Add dashboard cache helpers**

In `src/open_trader/technical_facts.py`, add:

```python
def records_by_market_symbol(payload: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    records: dict[tuple[str, str], dict[str, Any]] = {}
    for record in payload.get("records", []):
        if not isinstance(record, dict):
            continue
        market = str(record.get("market") or "").strip().upper()
        symbol = str(record.get("symbol") or "").strip().upper()
        if market and symbol:
            records[(market, symbol)] = record
    return records


def unavailable_technical_facts(message: str = "暂无 K 线事实缓存") -> dict[str, Any]:
    return {
        "available": False,
        "extraction_status": "missing",
        "freshness": {"status": "missing", "message": message},
        "facts": {},
        "error": "",
    }


def stale_technical_facts() -> dict[str, Any]:
    return {
        "available": False,
        "extraction_status": "stale",
        "freshness": {"status": "stale", "message": "缓存已过期，需重新抽取"},
        "facts": {},
        "error": "",
    }
```

- [ ] **Step 4: Merge cache in `dashboard.py`**

Import helpers:

```python
from .technical_facts import (
    extract_market_report,
    load_technical_facts_cache,
    records_by_market_symbol,
    source_hash,
    stale_technical_facts,
    technical_facts_latest_path,
    unavailable_technical_facts,
)
```

In `load_dashboard_state()`, after loading `trading_advice`, load:

```python
    technical_facts_path = config.data_dir / "latest" / "technical_facts.json"
    technical_facts_by_holding = records_by_market_symbol(
        load_technical_facts_cache(technical_facts_path)
    )
```

Pass `technical_facts_by_holding` into `_merge_holding()`.

Extend `_merge_holding()` signature:

```python
    technical_facts_by_holding: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
```

Inside `_merge_holding()`, after `holding["trade_action"] = ...`, add:

```python
    holding["technical_facts"] = _technical_facts_detail(
        agent_report=agent_report,
        technical_facts=technical_facts_by_holding.get(key) if key is not None else None,
    )
```

Add helper:

```python
def _technical_facts_detail(
    *,
    agent_report: dict[str, str] | None,
    technical_facts: dict[str, Any] | None,
) -> dict[str, Any]:
    if technical_facts is None:
        return unavailable_technical_facts()
    if agent_report is None:
        return stale_technical_facts()
    current_report = extract_market_report(agent_report.get("raw_decision", ""))
    if technical_facts.get("source_advice_hash") != source_hash(current_report):
        return stale_technical_facts()
    return {
        "available": technical_facts.get("extraction_status") == "ok",
        "extraction_status": technical_facts.get("extraction_status", ""),
        "freshness": technical_facts.get("freshness", {}),
        "facts": technical_facts.get("facts", {}),
        "market_data_as_of": technical_facts.get("market_data_as_of", ""),
        "run_date": technical_facts.get("run_date", ""),
        "extracted_at": technical_facts.get("extracted_at", ""),
        "error": technical_facts.get("error", ""),
    }
```

Market-scoped dashboard paths are not currently in `DashboardConfig`; use top-level `data/latest/technical_facts.json` in this task. Add market-scoped dashboard loading later only if the dashboard itself becomes market-scoped.

- [ ] **Step 5: Run dashboard tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/open_trader/technical_facts.py src/open_trader/dashboard.py tests/test_dashboard.py
git commit -m "feat: attach technical facts to dashboard"
```

---

### Task 7: Dashboard Frontend Rendering

**Files:**
- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Modify: `src/open_trader/dashboard_static/dashboard.css`
- Modify: `tests/test_dashboard_web.py`

- [ ] **Step 1: Add static asset test for Chinese technical facts states**

Add to `tests/test_dashboard_web.py` inside the existing static asset test:

```python
    assert "日线数据截至" in js
    assert "指标周期缺失，需复核" in js
    assert "缓存已过期，需重新抽取" in js
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py::test_dashboard_static_assets_include_local_shell -q
```

Expected: FAIL because `dashboard.js` does not contain those labels.

- [ ] **Step 3: Replace K line placeholder plugin data**

In `src/open_trader/dashboard_static/dashboard.js`, add helper functions near `renderTradingDecisionPlugins()`:

```javascript
function klinePlugin(holding) {
  const technical = holding.technical_facts || {};
  const freshness = technical.freshness || {};
  const facts = technical.facts || {};
  const firstFrame = firstTechnicalTimeframe(facts);
  if (!technical.available) {
    const message = freshness.message || "暂无 K 线事实缓存";
    const status = freshness.status === "stale" ? "需重算" : "待抽取";
    return {
      title: "趋势 / K 线",
      status,
      tone: freshness.status === "stale" ? "partial" : "muted",
      score: freshness.status === "stale" ? "!" : "-",
      headline: message,
      detail: "每天 TradingAgents 报告生成后自动抽取并缓存。",
      condition: "条件：缓存必须匹配最新 TradingAgents 技术报告，且指标需包含周期和行情日期。",
    };
  }
  const timeframeLabel = firstFrame.timeframe_label || timeframeLabelText(firstFrame.timeframe);
  const rsi = firstFrame.rsi && firstFrame.rsi.value ? firstFrame.rsi.value : "-";
  const atr = firstFrame.atr && firstFrame.atr.value ? firstFrame.atr.value : "-";
  const macd = firstFrame.macd && firstFrame.macd.crossover ? firstFrame.macd.crossover : "-";
  return {
    title: "趋势 / K 线",
    status: "已缓存",
    tone: freshness.status === "missing_timeframe" ? "partial" : "ok",
    score: timeframeLabel ? timeframeLabel.slice(0, 1) : "K",
    headline: freshness.message || `${timeframeLabel || "周期缺失"}数据已缓存`,
    detail: `${timeframeLabel || "周期缺失"} RSI ${rsi}；MACD ${macd}；ATR ${atr}。`,
    condition: technicalConditionText(technical, firstFrame),
  };
}

function firstTechnicalTimeframe(facts) {
  const timeframes = Array.isArray(facts.timeframes) ? facts.timeframes : [];
  return timeframes.length ? timeframes[0] : {};
}

function timeframeLabelText(value) {
  const labels = {
    daily: "日线",
    weekly: "周线",
    monthly: "月线",
    yearly: "年线",
    intraday: "分时",
    unknown: "周期缺失",
  };
  return labels[value] || "周期缺失";
}

function technicalConditionText(technical, frame) {
  const freshness = technical.freshness || {};
  if (freshness.status === "missing_timeframe") {
    return "指标周期缺失，需复核。";
  }
  if (freshness.status === "stale") {
    return "缓存已过期，需重新抽取。";
  }
  const timeframe = frame.timeframe_label || timeframeLabelText(frame.timeframe);
  return `事实来源：TradingAgents market_report；${timeframe}指标只读展示，不生成交易建议。`;
}
```

In `renderTradingDecisionPlugins(holding)`, replace the hard-coded first plugin object with:

```javascript
    klinePlugin(holding),
```

- [ ] **Step 4: Add detail section for technical facts**

In `renderSymbolDetail(holding, index)`, after `${renderTradingDecisionPlugins(holding)}`, insert:

```javascript
      ${renderTechnicalFactsSection(holding)}
```

Add:

```javascript
function renderTechnicalFactsSection(holding) {
  const technical = holding.technical_facts || {};
  const freshness = technical.freshness || {};
  if (!technical.available) {
    return `
      <section class="detail-section trading-decision-section">
        <div class="trading-decision-section-header">
          <div>
            <h3>趋势 / K 线事实详情</h3>
            <p>${escapeHtml(freshness.message || "暂无 K 线事实缓存")}</p>
          </div>
          <span class="status-pill status-partial">${escapeHtml(freshness.status === "stale" ? "需重算" : "待抽取")}</span>
        </div>
      </section>
    `;
  }
  const frames = Array.isArray(technical.facts && technical.facts.timeframes)
    ? technical.facts.timeframes
    : [];
  return `
    <section class="detail-section trading-decision-section">
      <div class="trading-decision-section-header">
        <div>
          <h3>趋势 / K 线事实详情</h3>
          <p>${escapeHtml(freshness.message || "技术面事实已缓存")}</p>
        </div>
        <span class="status-pill status-ok">缓存有效</span>
      </div>
      ${frames.map((frame) => renderTechnicalTimeframe(frame)).join("")}
    </section>
  `;
}

function renderTechnicalTimeframe(frame) {
  const label = frame.timeframe_label || timeframeLabelText(frame.timeframe);
  const cells = [
    ["周期", label],
    ["当前价", frame.current_price],
    ["RSI", frame.rsi && frame.rsi.value],
    ["MACD", frame.macd && frame.macd.crossover],
    ["ATR", frame.atr && [frame.atr.value, frame.atr.percent_of_price].filter(Boolean).join(" / ")],
    ["均线结构", frame.moving_averages && frame.moving_averages.ma_alignment],
    ["布林带位置", frame.bollinger && frame.bollinger.price_position],
    ["成交量", frame.volume && frame.volume.volume_confirmation],
  ];
  return `
    <div class="technical-timeframe">
      <div class="technical-timeframe-title">${escapeHtml(label || "周期缺失")}</div>
      <div class="technical-fact-grid">
        ${cells.map(([labelText, value]) => renderTechnicalFactCell(labelText, value)).join("")}
      </div>
      ${renderTechnicalEvidence(frame)}
    </div>
  `;
}

function renderTechnicalFactCell(label, value) {
  return `
    <article class="technical-fact-cell">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(formatPlain(value || "-"))}</strong>
    </article>
  `;
}

function renderTechnicalEvidence(frame) {
  const evidence = Array.isArray(frame.evidence_quotes) ? frame.evidence_quotes.slice(0, 2) : [];
  if (!evidence.length) {
    return "";
  }
  return evidence.map((quote) => `<p class="technical-evidence">${escapeHtml(quote)}</p>`).join("");
}
```

- [ ] **Step 5: Add CSS**

Append to `src/open_trader/dashboard_static/dashboard.css`:

```css
.technical-timeframe {
  border-top: 1px solid var(--line);
  display: grid;
  gap: 10px;
  padding-top: 10px;
}

.technical-timeframe + .technical-timeframe {
  margin-top: 12px;
}

.technical-timeframe-title {
  background: #eef7f2;
  border: 1px solid #cae1d3;
  border-radius: 8px;
  color: var(--accent-strong);
  font-size: 13px;
  font-weight: 800;
  padding: 8px 10px;
}

.technical-fact-grid {
  display: grid;
  gap: 10px;
  grid-template-columns: repeat(4, minmax(0, 1fr));
}

.technical-fact-cell {
  background: var(--surface-soft);
  border: 1px solid var(--line);
  border-radius: 8px;
  display: grid;
  gap: 5px;
  min-width: 0;
  padding: 9px;
}

.technical-fact-cell span {
  color: var(--muted);
  font-size: 11px;
}

.technical-fact-cell strong {
  font-size: 13px;
  overflow-wrap: anywhere;
}

.technical-evidence {
  border-left: 3px solid var(--accent);
  color: var(--text);
  font-size: 13px;
  line-height: 1.55;
  margin: 0;
  padding-left: 9px;
}
```

Inside existing mobile media blocks where `.decision-plugin-grid` is made single-column, add:

```css
  .technical-fact-grid {
    grid-template-columns: 1fr;
  }
```

- [ ] **Step 6: Run frontend/static tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py tests/test_dashboard.py -q
```

Expected: PASS.

- [ ] **Step 7: Verify with browser**

Start dashboard:

```bash
.venv/bin/python -m open_trader dashboard --portfolio data/latest/portfolio.csv --data-dir data --reports-dir reports --poll-seconds 5 --host 127.0.0.1 --port 8766
```

Open `http://127.0.0.1:8766`, select a holding with technical facts, and verify:

- `趋势 / K 线` card no longer says `占位`
- card shows `日线数据截至 <date>` or a clear missing-date warning
- RSI/MACD/ATR labels include timeframe
- stale cache hides old values
- mobile width stacks technical fact cells without overlap

Use Playwright or the in-app browser for screenshots before claiming completion.

- [ ] **Step 8: Commit**

```bash
git add src/open_trader/dashboard_static/dashboard.js src/open_trader/dashboard_static/dashboard.css tests/test_dashboard_web.py
git commit -m "feat: render technical facts plugin"
```

---

### Task 8: Final Verification And Docs

**Files:**
- Modify: `README.zh-CN.md`
- Modify: `README.md`

- [ ] **Step 1: Document the cache artifact and backfill command**

In `README.zh-CN.md`, add a short dashboard subsection:

```markdown
### K 线技术面事实缓存

每日 TradingAgents 报告生成后，Open Trader 会从 `raw_decision.state.market_report`
抽取客观技术面事实，并写入：

- `data/runs/<日期>/technical_facts.json`
- `data/latest/technical_facts.json`

分市场运行时，路径为 `data/runs/<日期>/<市场>/technical_facts.json` 和
`data/latest/<市场>/technical_facts.json`。Dashboard 只读取缓存，不在页面打开时调用
LLM。K 线插件会显示指标周期和行情数据截至日期；如果周期或日期缺失，会提示人工复核。

手动回填：

```bash
.venv/bin/python -m open_trader extract-technical-facts \
  --advice data/latest/trading_advice.csv \
  --data-dir data \
  --date 2026-06-19 \
  --update-latest
```
```

In `README.md`, add the English equivalent:

```markdown
### Cached Technical Facts

After each TradingAgents run writes `trading_advice.csv`, Open Trader extracts
objective technical facts from `raw_decision.state.market_report` and writes
`technical_facts.json` under both the dated run directory and `data/latest`.
The dashboard reads this cache only; it does not call the LLM on page load.
Each displayed indicator includes its timeframe and market-data cutoff date.
Missing timeframe or date fields are shown as review warnings.
```

- [ ] **Step 2: Run full focused suite**

Run:

```bash
.venv/bin/python -m pytest tests/test_technical_facts.py tests/test_premarket_pipeline.py tests/test_premarket_cli.py tests/test_dashboard.py tests/test_dashboard_web.py -q
```

Expected: PASS.

- [ ] **Step 3: Run full test suite**

Run:

```bash
.venv/bin/python -m pytest -q
```

Expected: PASS.

- [ ] **Step 4: Commit docs**

```bash
git add README.md README.zh-CN.md
git commit -m "docs: document technical facts cache"
```

- [ ] **Step 5: Final manual artifact check**

Run a fake-free manual command only if `DEEPSEEK_API_KEY` is configured:

```bash
.venv/bin/python -m open_trader extract-technical-facts \
  --advice data/latest/trading_advice.csv \
  --data-dir data \
  --date 2026-06-21 \
  --update-latest
```

Expected output includes:

```text
technical_facts: <number>
technical_facts_json: data/runs/2026-06-21/technical_facts.json
latest: data/latest/technical_facts.json
```

If `DEEPSEEK_API_KEY` is not configured, skip this command and state that live LLM extraction was not run.

---

## Self-Review Checklist

- Spec coverage:
  - Objective facts only: Tasks 2, 3, and 7 strip or ignore transaction proposals and render only fact fields.
  - Cache after TradingAgents: Task 4 wires extraction after `trading_advice.csv`.
  - Avoid repeated calculation: Task 2 reuses records by `(market, symbol, source_advice_hash)`.
  - Dates: Tasks 2, 6, and 7 carry `market_data_as_of`, `run_date`, and `extracted_at`.
  - Timeframes: Tasks 2, 3, and 7 require and render timeframe labels.
  - Dashboard read-only: Tasks 6 and 7 read cached `technical_facts`, with no frontend LLM call.
  - Manual backfill: Task 5 adds `extract-technical-facts`.
  - Tests: every implementation task has focused pytest coverage.
- Placeholder scan:
  - No incomplete marker phrases are intentionally present.
  - The only optional item is the final live LLM manual check, guarded by `DEEPSEEK_API_KEY`.
- Type consistency:
  - Cache schema uses `open_trader.technical_facts_cache.v1`.
  - Facts schema uses `open_trader.technical_facts.v1`.
  - Function names introduced in earlier tasks match later usage: `generate_technical_facts`, `load_technical_facts_cache`, `source_hash`, and `LLMTechnicalFactsExtractor`.
