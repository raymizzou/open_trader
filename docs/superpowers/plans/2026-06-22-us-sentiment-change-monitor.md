# US Sentiment Change Monitor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a US-only `舆论变化` monitor that compares TradingAgents sentiment/news reports against the previous report and seven-day local baseline, then surfaces manual-review-only changes in artifacts and the dashboard.

**Architecture:** Add a focused `sentiment_changes` module that reads existing `trading_advice.csv` rows, extracts `raw_decision.state.sentiment_report` and `raw_decision.state.news_report`, finds prior local baselines, uses a strict JSON comparator, and writes dated/latest JSON plus Markdown reports. Wire it into the US premarket path and dashboard while skipping HK quietly.

**Tech Stack:** Python 3.12 stdlib CSV/JSON/dataclasses/hashlib/datetime/pathlib, existing OpenAI-compatible DeepSeek pattern, existing market-scoped artifact paths, static dashboard JavaScript/CSS, pytest with fake comparators.

---

## File Structure

- Create `src/open_trader/sentiment_changes.py`: source extraction, monitored symbol selection, baseline discovery, strict comparator interface, result validation, artifact writing, Markdown rendering, and cache loading helpers.
- Create `tests/test_sentiment_changes.py`: unit tests for source parsing, US-only filtering, monitored symbol selection, previous/seven-day baselines, no-signal detection, comparator validation, artifact writing, and latest promotion.
- Modify `src/open_trader/cli.py`: add `detect-sentiment-changes` and wire the real comparator.
- Modify `tests/test_premarket_cli.py`: cover the new CLI parser and command behavior with a fake detector/comparator.
- Modify `src/open_trader/advice/premarket.py`: call sentiment change detection after `trading_advice.csv` for US only and return the artifact path.
- Modify `tests/test_premarket_pipeline.py`: cover US generation and HK skip behavior in `run_premarket()`.
- Modify `src/open_trader/daily_premarket.py`: promote `sentiment_changes.json` as part of the US latest set and include it in status/report artifacts.
- Modify `tests/test_daily_premarket.py`: cover US latest promotion and rollback behavior.
- Modify `src/open_trader/dashboard.py`: load `data/latest/US/sentiment_changes.json` and attach per-holding `sentiment_changes`.
- Modify `tests/test_dashboard.py`: cover attached, missing, and HK unsupported dashboard states.
- Modify `src/open_trader/dashboard_static/dashboard.js`: replace the `新闻 / 舆论` placeholder with a `舆论变化` plugin card.
- Modify `src/open_trader/dashboard_static/dashboard.css`: add compact topic-chip styles for the sentiment change card.
- Modify `tests/test_dashboard_web.py`: assert frontend rendering for US change states and HK quiet state.
- Modify `README.md` and `README.zh-CN.md`: document the manual-review-only sentiment change monitor and CLI.

---

### Task 1: Core Source Parsing And Monitored Symbol Selection

**Files:**
- Create: `src/open_trader/sentiment_changes.py`
- Test: `tests/test_sentiment_changes.py`

- [ ] **Step 1: Write failing tests for source extraction, hashing, US-only filtering, and monitored symbol selection**

Create `tests/test_sentiment_changes.py`:

```python
from __future__ import annotations

import csv
import json
from pathlib import Path

from open_trader.sentiment_changes import (
    SENTIMENT_CHANGES_SCHEMA_VERSION,
    AdviceSentimentSource,
    build_monitored_us_symbols,
    extract_sentiment_source_text,
    load_current_sentiment_sources,
    source_hash,
)


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


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def raw_decision(*, sentiment: str = "", news: str = "") -> str:
    return json.dumps(
        {
            "state": {
                "sentiment_report": sentiment,
                "news_report": news,
                "market_report": "not used by this module",
            }
        },
        ensure_ascii=False,
    )


def advice_row(
    *,
    run_date: str = "2026-06-22",
    market: str = "US",
    symbol: str = "VIXY",
    sentiment: str = "StockTwits became more bearish.",
    news: str = "Yahoo Finance covered VIX reliability.",
) -> dict[str, str]:
    return {
        "run_date": run_date,
        "symbol": symbol,
        "market": market,
        "asset_class": "stock",
        "portfolio_weight_hkd": "1.0%",
        "risk_flag": "normal",
        "source": "tradingagents",
        "advice_action": "Hold",
        "advice_summary": "",
        "raw_decision": raw_decision(sentiment=sentiment, news=news),
        "status": "ok",
        "error": "",
        "source_status": "ok",
        "fallback_reason": "",
        "fallback_from_date": "",
    }


def test_extract_sentiment_source_text_combines_sentiment_and_news() -> None:
    text = extract_sentiment_source_text(
        raw_decision(sentiment="Sentiment block", news="News block")
    )

    assert "## sentiment_report" in text
    assert "Sentiment block" in text
    assert "## news_report" in text
    assert "News block" in text


def test_extract_sentiment_source_text_returns_empty_for_invalid_json() -> None:
    assert extract_sentiment_source_text("{not-json") == ""


def test_source_hash_is_stable_and_prefixed() -> None:
    first = source_hash("same text")
    second = source_hash("same text")

    assert first == second
    assert first.startswith("sha256:")
    assert source_hash("different") != first


def test_load_current_sentiment_sources_filters_to_monitored_us_symbols(tmp_path: Path) -> None:
    advice_path = tmp_path / "trading_advice.csv"
    write_csv(
        advice_path,
        ADVICE_FIELDNAMES,
        [
            advice_row(symbol="VIXY"),
            advice_row(symbol="QQQ"),
            advice_row(market="HK", symbol="02476"),
        ],
    )

    sources = load_current_sentiment_sources(
        advice_path=advice_path,
        monitored_symbols={"VIXY"},
    )

    assert sources == [
        AdviceSentimentSource(
            run_date="2026-06-22",
            market="US",
            symbol="VIXY",
            source_status="ok",
            source_text=extract_sentiment_source_text(
                raw_decision(
                    sentiment="StockTwits became more bearish.",
                    news="Yahoo Finance covered VIX reliability.",
                )
            ),
            source_hash=source_hash(
                extract_sentiment_source_text(
                    raw_decision(
                        sentiment="StockTwits became more bearish.",
                        news="Yahoo Finance covered VIX reliability.",
                    )
                )
            ),
        )
    ]


def test_build_monitored_us_symbols_uses_portfolio_and_plan(tmp_path: Path) -> None:
    portfolio_path = tmp_path / "portfolio.csv"
    trading_plan_path = tmp_path / "trading_plan.csv"
    watchlist_path = tmp_path / "watchlist.csv"
    write_csv(
        portfolio_path,
        ["market", "symbol", "asset_class"],
        [
            {"market": "US", "symbol": "VIXY", "asset_class": "stock"},
            {"market": "HK", "symbol": "02476", "asset_class": "stock"},
            {"market": "CASH", "symbol": "USD_CASH", "asset_class": "cash"},
        ],
    )
    write_csv(
        trading_plan_path,
        ["market", "symbol", "status"],
        [
            {"market": "US", "symbol": "QQQ", "status": "active"},
            {"market": "HK", "symbol": "00700", "status": "active"},
        ],
    )
    write_csv(
        watchlist_path,
        ["market", "symbol", "status"],
        [{"market": "US", "symbol": "MSFT", "status": "watch"}],
    )

    assert build_monitored_us_symbols(
        portfolio_path=portfolio_path,
        trading_plan_path=trading_plan_path,
        watchlist_path=watchlist_path,
    ) == {"MSFT", "QQQ", "VIXY"}


def test_schema_version_constant_is_v1() -> None:
    assert SENTIMENT_CHANGES_SCHEMA_VERSION == "open_trader.sentiment_changes.v1"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_sentiment_changes.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'open_trader.sentiment_changes'`.

- [ ] **Step 3: Implement source parsing and monitored symbol selection**

Create `src/open_trader/sentiment_changes.py`:

```python
from __future__ import annotations

import csv
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SENTIMENT_CHANGES_SCHEMA_VERSION = "open_trader.sentiment_changes.v1"
SENTIMENT_CHANGES_SOURCE = "tradingagents_sentiment_and_news_reports"


@dataclass(frozen=True)
class AdviceSentimentSource:
    run_date: str
    market: str
    symbol: str
    source_status: str
    source_text: str
    source_hash: str


def extract_sentiment_source_text(raw_decision: str) -> str:
    try:
        payload = json.loads(raw_decision or "{}")
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, dict):
        return ""
    state = payload.get("state")
    if not isinstance(state, dict):
        return ""
    sentiment = state.get("sentiment_report")
    news = state.get("news_report")
    sentiment_text = sentiment.strip() if isinstance(sentiment, str) else ""
    news_text = news.strip() if isinstance(news, str) else ""
    parts: list[str] = []
    if sentiment_text:
        parts.append(f"## sentiment_report\n\n{sentiment_text}")
    if news_text:
        parts.append(f"## news_report\n\n{news_text}")
    return "\n\n".join(parts)


def source_hash(text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def load_current_sentiment_sources(
    *,
    advice_path: Path,
    monitored_symbols: set[str],
) -> list[AdviceSentimentSource]:
    if not advice_path.exists():
        raise FileNotFoundError(f"advice CSV not found: {advice_path}")
    monitored = {symbol.strip().upper() for symbol in monitored_symbols if symbol.strip()}
    csv.field_size_limit(sys.maxsize)
    sources: list[AdviceSentimentSource] = []
    with advice_path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            market = str(row.get("market") or "").strip().upper()
            symbol = str(row.get("symbol") or "").strip().upper()
            if market != "US" or not symbol or symbol not in monitored:
                continue
            source_text = extract_sentiment_source_text(row.get("raw_decision") or "")
            sources.append(
                AdviceSentimentSource(
                    run_date=str(row.get("run_date") or "").strip(),
                    market=market,
                    symbol=symbol,
                    source_status=str(
                        row.get("source_status") or row.get("status") or ""
                    ).strip(),
                    source_text=source_text,
                    source_hash=source_hash(source_text),
                )
            )
    return sources


def build_monitored_us_symbols(
    *,
    portfolio_path: Path,
    trading_plan_path: Path,
    watchlist_path: Path,
) -> set[str]:
    symbols: set[str] = set()
    for path in (portfolio_path, trading_plan_path, watchlist_path):
        if not path.exists():
            continue
        csv.field_size_limit(sys.maxsize)
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                market = str(row.get("market") or "").strip().upper()
                symbol = str(row.get("symbol") or "").strip().upper()
                asset_class = str(row.get("asset_class") or "").strip().lower()
                if market == "US" and symbol and asset_class not in {"cash", "money_market_fund"}:
                    symbols.add(symbol)
    return symbols
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
.venv/bin/python -m pytest tests/test_sentiment_changes.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/sentiment_changes.py tests/test_sentiment_changes.py
git commit -m "feat: parse sentiment change sources"
```

---

### Task 2: Baseline Discovery And No-Signal Detection

**Files:**
- Modify: `src/open_trader/sentiment_changes.py`
- Modify: `tests/test_sentiment_changes.py`

- [ ] **Step 1: Add failing tests for previous/seven-day baselines and no-signal detection**

Append to `tests/test_sentiment_changes.py`:

```python
from open_trader.sentiment_changes import (
    BaselineInfo,
    find_previous_baseline,
    find_seven_day_baseline,
    is_no_signal_source,
    load_historical_sentiment_sources,
)


def write_run_advice(data_dir: Path, run_date: str, rows: list[dict[str, str]]) -> Path:
    path = data_dir / "runs" / run_date / "US" / "trading_advice.csv"
    write_csv(path, ADVICE_FIELDNAMES, rows)
    return path


def test_load_historical_sentiment_sources_reads_market_scoped_runs(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    write_run_advice(
        data_dir,
        "2026-06-19",
        [advice_row(run_date="2026-06-19", symbol="VIXY", sentiment="old", news="old news")],
    )

    sources = load_historical_sentiment_sources(data_dir=data_dir, symbol="VIXY", before_date="2026-06-22")

    assert [source.run_date for source in sources] == ["2026-06-19"]
    assert sources[0].symbol == "VIXY"


def test_find_previous_baseline_returns_most_recent_before_current(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    write_run_advice(data_dir, "2026-06-17", [advice_row(run_date="2026-06-17", symbol="VIXY")])
    write_run_advice(data_dir, "2026-06-20", [advice_row(run_date="2026-06-20", symbol="VIXY")])

    baseline = find_previous_baseline(data_dir=data_dir, symbol="VIXY", current_run_date="2026-06-22")

    assert baseline.status == "available"
    assert baseline.run_date == "2026-06-20"
    assert baseline.samples == 1


def test_find_previous_baseline_marks_insufficient_history(tmp_path: Path) -> None:
    baseline = find_previous_baseline(
        data_dir=tmp_path / "data",
        symbol="VIXY",
        current_run_date="2026-06-22",
    )

    assert baseline == BaselineInfo(
        status="insufficient_history",
        run_date="",
        start_date="",
        end_date="",
        samples=0,
        source_hash="",
        source_texts=[],
    )


def test_find_seven_day_baseline_requires_two_samples(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    write_run_advice(data_dir, "2026-06-20", [advice_row(run_date="2026-06-20", symbol="VIXY")])

    baseline = find_seven_day_baseline(data_dir=data_dir, symbol="VIXY", current_run_date="2026-06-22")

    assert baseline.status == "insufficient_history"
    assert baseline.samples == 1


def test_find_seven_day_baseline_returns_available_window(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    write_run_advice(data_dir, "2026-06-18", [advice_row(run_date="2026-06-18", symbol="VIXY")])
    write_run_advice(data_dir, "2026-06-20", [advice_row(run_date="2026-06-20", symbol="VIXY")])

    baseline = find_seven_day_baseline(data_dir=data_dir, symbol="VIXY", current_run_date="2026-06-22")

    assert baseline.status == "available"
    assert baseline.start_date == "2026-06-15"
    assert baseline.end_date == "2026-06-21"
    assert baseline.samples == 2


def test_is_no_signal_source_detects_empty_unavailable_reports() -> None:
    text = "All three pre-fetched data sources returned no actionable content. No news found. StockTwits unavailable. Zero mentions on Reddit."

    assert is_no_signal_source(text) is True
    assert is_no_signal_source("StockTwits discussion shifted around VIX reliability.") is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_sentiment_changes.py -q
```

Expected: FAIL with import errors for `BaselineInfo` and baseline functions.

- [ ] **Step 3: Implement baseline discovery and no-signal detection**

Append to `src/open_trader/sentiment_changes.py`:

```python
from datetime import date, timedelta


@dataclass(frozen=True)
class BaselineInfo:
    status: str
    run_date: str
    start_date: str
    end_date: str
    samples: int
    source_hash: str
    source_texts: list[str]


def _parse_run_date(value: str) -> date:
    return date.fromisoformat(value)


def load_historical_sentiment_sources(
    *,
    data_dir: Path,
    symbol: str,
    before_date: str,
) -> list[AdviceSentimentSource]:
    runs_dir = data_dir / "runs"
    if not runs_dir.exists():
        return []
    before = _parse_run_date(before_date)
    sources: list[AdviceSentimentSource] = []
    target = symbol.strip().upper()
    for run_dir in sorted(runs_dir.iterdir()):
        if not run_dir.is_dir():
            continue
        try:
            run_dt = _parse_run_date(run_dir.name)
        except ValueError:
            continue
        if run_dt >= before:
            continue
        advice_path = run_dir / "US" / "trading_advice.csv"
        if not advice_path.exists():
            advice_path = run_dir / "trading_advice.csv"
        if not advice_path.exists():
            continue
        sources.extend(
            source
            for source in load_current_sentiment_sources(
                advice_path=advice_path,
                monitored_symbols={target},
            )
            if source.run_date and source.run_date < before_date
        )
    return sorted(sources, key=lambda source: source.run_date)


def find_previous_baseline(
    *,
    data_dir: Path,
    symbol: str,
    current_run_date: str,
) -> BaselineInfo:
    sources = load_historical_sentiment_sources(
        data_dir=data_dir,
        symbol=symbol,
        before_date=current_run_date,
    )
    if not sources:
        return BaselineInfo(
            status="insufficient_history",
            run_date="",
            start_date="",
            end_date="",
            samples=0,
            source_hash="",
            source_texts=[],
        )
    latest = sources[-1]
    return BaselineInfo(
        status="available",
        run_date=latest.run_date,
        start_date="",
        end_date="",
        samples=1,
        source_hash=latest.source_hash,
        source_texts=[latest.source_text],
    )


def find_seven_day_baseline(
    *,
    data_dir: Path,
    symbol: str,
    current_run_date: str,
) -> BaselineInfo:
    current = _parse_run_date(current_run_date)
    start = current - timedelta(days=7)
    end = current - timedelta(days=1)
    sources = [
        source
        for source in load_historical_sentiment_sources(
            data_dir=data_dir,
            symbol=symbol,
            before_date=current_run_date,
        )
        if start <= _parse_run_date(source.run_date) <= end
    ]
    status = "available" if len(sources) >= 2 else "insufficient_history"
    return BaselineInfo(
        status=status,
        run_date="",
        start_date=start.isoformat(),
        end_date=end.isoformat(),
        samples=len(sources),
        source_hash=source_hash("\n\n".join(source.source_hash for source in sources)),
        source_texts=[source.source_text for source in sources],
    )


def is_no_signal_source(source_text: str) -> bool:
    normalized = source_text.casefold()
    no_data_markers = [
        "no actionable content",
        "no news found",
        "stocktwits unavailable",
        "zero mentions",
        "no reddit posts",
        "no data from news, stocktwits, or reddit",
    ]
    signal_markers = [
        "shifted",
        "became",
        "new theme",
        "lawsuit",
        "regulatory",
        "short report",
        "product incident",
    ]
    return (
        sum(1 for marker in no_data_markers if marker in normalized) >= 2
        and not any(marker in normalized for marker in signal_markers)
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
.venv/bin/python -m pytest tests/test_sentiment_changes.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/sentiment_changes.py tests/test_sentiment_changes.py
git commit -m "feat: find sentiment change baselines"
```

---

### Task 3: Comparator Validation And Artifact Generation

**Files:**
- Modify: `src/open_trader/sentiment_changes.py`
- Modify: `tests/test_sentiment_changes.py`

- [ ] **Step 1: Add failing tests for fake comparator results, trading recommendation rejection, errors, JSON/Markdown writing, and latest promotion**

Append to `tests/test_sentiment_changes.py`:

```python
from open_trader.sentiment_changes import (
    SentimentChangeComparator,
    SentimentChangesResult,
    generate_sentiment_changes,
    load_sentiment_changes_cache,
    sentiment_changes_latest_path,
    sentiment_changes_run_path,
)


class FakeComparator:
    def __init__(self, payload: dict[str, object] | Exception) -> None:
        self.payload = payload
        self.calls: list[dict[str, object]] = []

    def compare(
        self,
        *,
        current: AdviceSentimentSource,
        previous: BaselineInfo,
        seven_day: BaselineInfo,
    ) -> dict[str, object]:
        self.calls.append(
            {
                "symbol": current.symbol,
                "previous": previous.status,
                "seven_day": seven_day.status,
            }
        )
        if isinstance(self.payload, Exception):
            raise self.payload
        return dict(self.payload)


def comparator_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "status": "changed",
        "severity": "review",
        "change_vs_previous": "StockTwits discussion became more focused on VIXY lagging.",
        "change_vs_7d_baseline": "VIX reliability is more prominent than recent baseline.",
        "new_topics": ["VIX reliability"],
        "intensified_topics": ["VIXY lagging"],
        "faded_topics": [],
        "risk_flags": ["discussion_change_requires_review"],
        "evidence": [
            {
                "source": "tradingagents_sentiment_report",
                "excerpt": "Retail traders expect a VIX crush.",
                "source_run_date": "2026-06-22",
            }
        ],
    }
    payload.update(overrides)
    return payload


def test_generate_sentiment_changes_writes_run_report_and_latest(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    reports_dir = tmp_path / "reports"
    advice_path = data_dir / "runs/2026-06-22/US/trading_advice.csv"
    write_csv(advice_path, ADVICE_FIELDNAMES, [advice_row(symbol="VIXY")])
    write_run_advice(data_dir, "2026-06-20", [advice_row(run_date="2026-06-20", symbol="VIXY")])
    write_run_advice(data_dir, "2026-06-19", [advice_row(run_date="2026-06-19", symbol="VIXY")])
    portfolio_path = data_dir / "latest/portfolio.csv"
    write_csv(portfolio_path, ["market", "symbol", "asset_class"], [{"market": "US", "symbol": "VIXY", "asset_class": "stock"}])

    result = generate_sentiment_changes(
        advice_path=advice_path,
        portfolio_path=portfolio_path,
        trading_plan_path=data_dir / "latest/US/trading_plan.csv",
        watchlist_path=data_dir / "latest/US/watchlist.csv",
        data_dir=data_dir,
        reports_dir=reports_dir,
        run_date="2026-06-22",
        comparator=FakeComparator(comparator_payload()),
        update_latest=True,
    )

    assert result == SentimentChangesResult(
        run_date="2026-06-22",
        market="US",
        records=1,
        changed=1,
        review_required=1,
        failed=0,
        run_path=data_dir / "runs/2026-06-22/US/sentiment_changes.json",
        report_path=reports_dir / "sentiment_changes/2026-06-22-US.md",
        latest_path=data_dir / "latest/US/sentiment_changes.json",
    )
    cache = load_sentiment_changes_cache(result.latest_path)
    assert cache["schema_version"] == SENTIMENT_CHANGES_SCHEMA_VERSION
    assert cache["records"][0]["symbol"] == "VIXY"
    assert "仅用于人工复核" in result.report_path.read_text(encoding="utf-8")


def test_generate_sentiment_changes_rejects_trading_recommendation(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    reports_dir = tmp_path / "reports"
    advice_path = data_dir / "runs/2026-06-22/US/trading_advice.csv"
    write_csv(advice_path, ADVICE_FIELDNAMES, [advice_row(symbol="VIXY")])
    portfolio_path = data_dir / "latest/portfolio.csv"
    write_csv(portfolio_path, ["market", "symbol", "asset_class"], [{"market": "US", "symbol": "VIXY", "asset_class": "stock"}])

    result = generate_sentiment_changes(
        advice_path=advice_path,
        portfolio_path=portfolio_path,
        trading_plan_path=data_dir / "latest/US/trading_plan.csv",
        watchlist_path=data_dir / "latest/US/watchlist.csv",
        data_dir=data_dir,
        reports_dir=reports_dir,
        run_date="2026-06-22",
        comparator=FakeComparator(comparator_payload(change_vs_previous="Sell VIXY now.")),
        update_latest=False,
    )

    cache = load_sentiment_changes_cache(result.run_path)
    record = cache["records"][0]
    assert record["status"] == "error"
    assert "trading recommendation" in record["error"]


def test_generate_sentiment_changes_marks_no_signal_without_comparator(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    reports_dir = tmp_path / "reports"
    advice_path = data_dir / "runs/2026-06-22/US/trading_advice.csv"
    write_csv(
        advice_path,
        ADVICE_FIELDNAMES,
        [
            advice_row(
                symbol="VIXY",
                sentiment="No actionable content. StockTwits unavailable. Zero mentions on Reddit.",
                news="No news found.",
            )
        ],
    )
    portfolio_path = data_dir / "latest/portfolio.csv"
    write_csv(portfolio_path, ["market", "symbol", "asset_class"], [{"market": "US", "symbol": "VIXY", "asset_class": "stock"}])
    comparator = FakeComparator(comparator_payload())

    result = generate_sentiment_changes(
        advice_path=advice_path,
        portfolio_path=portfolio_path,
        trading_plan_path=data_dir / "latest/US/trading_plan.csv",
        watchlist_path=data_dir / "latest/US/watchlist.csv",
        data_dir=data_dir,
        reports_dir=reports_dir,
        run_date="2026-06-22",
        comparator=comparator,
        update_latest=False,
    )

    record = load_sentiment_changes_cache(result.run_path)["records"][0]
    assert record["status"] == "no_signal"
    assert comparator.calls == []


def test_sentiment_change_paths_are_market_scoped(tmp_path: Path) -> None:
    assert sentiment_changes_run_path(tmp_path / "data", "2026-06-22") == tmp_path / "data/runs/2026-06-22/US/sentiment_changes.json"
    assert sentiment_changes_latest_path(tmp_path / "data") == tmp_path / "data/latest/US/sentiment_changes.json"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_sentiment_changes.py -q
```

Expected: FAIL with import errors for `generate_sentiment_changes` and result types.

- [ ] **Step 3: Implement comparator validation and artifact generation**

Append to `src/open_trader/sentiment_changes.py`:

```python
from tempfile import NamedTemporaryFile
from typing import Protocol


@dataclass(frozen=True)
class SentimentChangesResult:
    run_date: str
    market: str
    records: int
    changed: int
    review_required: int
    failed: int
    run_path: Path
    report_path: Path
    latest_path: Path


class SentimentChangeComparator(Protocol):
    def compare(
        self,
        *,
        current: AdviceSentimentSource,
        previous: BaselineInfo,
        seven_day: BaselineInfo,
    ) -> dict[str, object]:
        ...


def sentiment_changes_run_path(data_dir: Path, run_date: str) -> Path:
    return data_dir / "runs" / run_date / "US" / "sentiment_changes.json"


def sentiment_changes_latest_path(data_dir: Path) -> Path:
    return data_dir / "latest" / "US" / "sentiment_changes.json"


def sentiment_changes_report_path(reports_dir: Path, run_date: str) -> Path:
    return reports_dir / "sentiment_changes" / f"{run_date}-US.md"


def load_sentiment_changes_cache(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def generate_sentiment_changes(
    *,
    advice_path: Path,
    portfolio_path: Path,
    trading_plan_path: Path,
    watchlist_path: Path,
    data_dir: Path,
    reports_dir: Path,
    run_date: str,
    comparator: SentimentChangeComparator,
    update_latest: bool,
) -> SentimentChangesResult:
    monitored = build_monitored_us_symbols(
        portfolio_path=portfolio_path,
        trading_plan_path=trading_plan_path,
        watchlist_path=watchlist_path,
    )
    sources = load_current_sentiment_sources(
        advice_path=advice_path,
        monitored_symbols=monitored,
    )
    records = [
        _build_record(
            source=source,
            data_dir=data_dir,
            run_date=run_date,
            comparator=comparator,
        )
        for source in sources
    ]
    payload = {
        "schema_version": SENTIMENT_CHANGES_SCHEMA_VERSION,
        "run_date": run_date,
        "market": "US",
        "source": SENTIMENT_CHANGES_SOURCE,
        "records": records,
    }
    run_path = sentiment_changes_run_path(data_dir, run_date)
    report_path = sentiment_changes_report_path(reports_dir, run_date)
    latest_path = sentiment_changes_latest_path(data_dir)
    _write_json_atomic(run_path, payload)
    _write_text_atomic(report_path, _render_report(run_date, records))
    if update_latest:
        _write_json_atomic(latest_path, payload)
    return SentimentChangesResult(
        run_date=run_date,
        market="US",
        records=len(records),
        changed=sum(1 for record in records if record["status"] == "changed"),
        review_required=sum(1 for record in records if record["severity"] in {"review", "high_review"}),
        failed=sum(1 for record in records if record["status"] == "error"),
        run_path=run_path,
        report_path=report_path,
        latest_path=latest_path,
    )


def _build_record(
    *,
    source: AdviceSentimentSource,
    data_dir: Path,
    run_date: str,
    comparator: SentimentChangeComparator,
) -> dict[str, Any]:
    previous = find_previous_baseline(
        data_dir=data_dir,
        symbol=source.symbol,
        current_run_date=run_date,
    )
    seven_day = find_seven_day_baseline(
        data_dir=data_dir,
        symbol=source.symbol,
        current_run_date=run_date,
    )
    base = {
        "market": source.market,
        "symbol": source.symbol,
        "current_source_hash": source.source_hash,
        "previous_baseline": _baseline_payload(previous),
        "seven_day_baseline": _baseline_payload(seven_day),
        "decision_use": "manual_review_only",
        "source": SENTIMENT_CHANGES_SOURCE,
        "error": "",
    }
    if not source.source_text:
        return _fallback_record(base, "missing_source", "none", "missing sentiment/news source")
    if is_no_signal_source(source.source_text):
        return _fallback_record(base, "no_signal", "none", "no effective external discussion signal")
    try:
        comparison = comparator.compare(
            current=source,
            previous=previous,
            seven_day=seven_day,
        )
        record = {**base, **_validate_comparison(comparison)}
    except Exception as exc:
        return _fallback_record(base, "error", "none", str(exc))
    if previous.status != "available" or seven_day.status != "available":
        record["status"] = (
            "changed"
            if record["status"] == "changed" and record["severity"] in {"review", "high_review"}
            else "insufficient_baseline"
        )
    return record


def _baseline_payload(baseline: BaselineInfo) -> dict[str, object]:
    return {
        "status": baseline.status,
        "run_date": baseline.run_date,
        "start_date": baseline.start_date,
        "end_date": baseline.end_date,
        "samples": baseline.samples,
        "source_hash": baseline.source_hash,
    }


def _fallback_record(
    base: dict[str, Any],
    status: str,
    severity: str,
    error: str,
) -> dict[str, Any]:
    return {
        **base,
        "status": status,
        "severity": severity,
        "change_vs_previous": "",
        "change_vs_7d_baseline": "",
        "new_topics": [],
        "intensified_topics": [],
        "faded_topics": [],
        "risk_flags": [],
        "evidence": [],
        "error": error,
    }


def _validate_comparison(payload: dict[str, object]) -> dict[str, Any]:
    status = str(payload.get("status") or "unchanged")
    severity = str(payload.get("severity") or "none")
    allowed_status = {"changed", "unchanged", "no_signal", "insufficient_baseline", "missing_source", "error", "skipped"}
    allowed_severity = {"none", "info", "review", "high_review"}
    if status not in allowed_status:
        raise ValueError(f"invalid sentiment change status: {status}")
    if severity not in allowed_severity:
        raise ValueError(f"invalid sentiment change severity: {severity}")
    record = {
        "status": status,
        "severity": severity,
        "change_vs_previous": str(payload.get("change_vs_previous") or ""),
        "change_vs_7d_baseline": str(payload.get("change_vs_7d_baseline") or ""),
        "new_topics": _string_list(payload.get("new_topics")),
        "intensified_topics": _string_list(payload.get("intensified_topics")),
        "faded_topics": _string_list(payload.get("faded_topics")),
        "risk_flags": _string_list(payload.get("risk_flags")),
        "evidence": _evidence_list(payload.get("evidence")),
    }
    joined = " ".join(
        [
            record["change_vs_previous"],
            record["change_vs_7d_baseline"],
            " ".join(record["new_topics"]),
            " ".join(record["intensified_topics"]),
            " ".join(record["risk_flags"]),
        ]
    ).casefold()
    if any(token in joined for token in ("buy ", "sell ", "stop-loss", "target price", "加仓", "买入", "卖出", "止损")):
        raise ValueError("sentiment comparison contains trading recommendation")
    return record


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _evidence_list(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    evidence: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        evidence.append(
            {
                "source": str(item.get("source") or ""),
                "excerpt": str(item.get("excerpt") or "")[:500],
                "source_run_date": str(item.get("source_run_date") or ""),
            }
        )
    return evidence


def _render_report(run_date: str, records: list[dict[str, Any]]) -> str:
    lines = [
        f"# US Sentiment Changes - {run_date}",
        "",
        "仅用于人工复核，不改变交易动作。",
        "",
    ]
    if not records:
        lines.append("No monitored US sentiment changes.")
    for record in records:
        lines.extend(
            [
                f"## US.{record['symbol']}",
                "",
                f"- Status: {record['status']}",
                f"- Severity: {record['severity']}",
                f"- Previous: {record['change_vs_previous'] or '-'}",
                f"- 7d baseline: {record['change_vs_7d_baseline'] or '-'}",
                f"- New topics: {', '.join(record['new_topics']) or '-'}",
                f"- Intensified topics: {', '.join(record['intensified_topics']) or '-'}",
                f"- Error: {record['error'] or '-'}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        temp_path = Path(handle.name)
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    temp_path.replace(path)


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        temp_path = Path(handle.name)
        handle.write(text)
    temp_path.replace(path)
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
.venv/bin/python -m pytest tests/test_sentiment_changes.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/sentiment_changes.py tests/test_sentiment_changes.py
git commit -m "feat: generate sentiment change artifacts"
```

---

### Task 4: Real LLM Comparator And CLI

**Files:**
- Modify: `src/open_trader/sentiment_changes.py`
- Modify: `src/open_trader/cli.py`
- Modify: `tests/test_premarket_cli.py`

- [ ] **Step 1: Add failing tests for the CLI command**

Append to `tests/test_premarket_cli.py`:

```python
def test_detect_sentiment_changes_help_includes_expected_options(capsys) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["detect-sentiment-changes", "--help"])

    assert excinfo.value.code == 0
    output = capsys.readouterr().out
    assert "--advice" in output
    assert "--portfolio" in output
    assert "--data-dir" in output
    assert "--reports-dir" in output
    assert "--update-latest" in output


def test_detect_sentiment_changes_main_wires_generator(monkeypatch, tmp_path: Path, capsys) -> None:
    captured: dict[str, object] = {}

    class FakeComparator:
        pass

    def fake_generate_sentiment_changes(**kwargs: object):
        captured.update(kwargs)
        return cli.SentimentChangesResult(
            run_date="2026-06-22",
            market="US",
            records=2,
            changed=1,
            review_required=1,
            failed=0,
            run_path=tmp_path / "data/runs/2026-06-22/US/sentiment_changes.json",
            report_path=tmp_path / "reports/sentiment_changes/2026-06-22-US.md",
            latest_path=tmp_path / "data/latest/US/sentiment_changes.json",
        )

    monkeypatch.setattr(cli, "LLMSentimentChangeComparator", FakeComparator)
    monkeypatch.setattr(cli, "generate_sentiment_changes", fake_generate_sentiment_changes)
    advice = tmp_path / "advice.csv"
    advice.write_text("run_date,market,symbol,raw_decision\n", encoding="utf-8")

    result = cli.main(
        [
            "detect-sentiment-changes",
            "--advice",
            str(advice),
            "--portfolio",
            str(tmp_path / "portfolio.csv"),
            "--data-dir",
            str(tmp_path / "data"),
            "--reports-dir",
            str(tmp_path / "reports"),
            "--date",
            "2026-06-22",
            "--update-latest",
        ]
    )

    assert result == 0
    assert captured["advice_path"] == advice
    assert captured["run_date"] == "2026-06-22"
    assert captured["update_latest"] is True
    output = capsys.readouterr().out
    assert "sentiment_changes: 2" in output
    assert "changed: 1" in output
    assert "review_required: 1" in output
```

Add these imports at the top of `tests/test_premarket_cli.py` if they are not already present:

```python
from pathlib import Path

import pytest

import open_trader.cli as cli
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_premarket_cli.py::test_detect_sentiment_changes_help_includes_expected_options tests/test_premarket_cli.py::test_detect_sentiment_changes_main_wires_generator -q
```

Expected: FAIL because the CLI command and comparator are not wired.

- [ ] **Step 3: Implement the LLM comparator**

Append to `src/open_trader/sentiment_changes.py`:

```python
import os

from .advice.change_classifier import DEEPSEEK_BASE_URL, DEFAULT_CLASSIFIER_MODEL


class LLMSentimentChangeComparator:
    def __init__(
        self,
        *,
        model: str = DEFAULT_CLASSIFIER_MODEL,
        api_key: str | None = None,
        base_url: str = DEEPSEEK_BASE_URL,
    ) -> None:
        from openai import OpenAI

        self._client = OpenAI(
            api_key=api_key or os.environ.get("DEEPSEEK_API_KEY"),
            base_url=base_url,
        )
        self._model = model

    def compare(
        self,
        *,
        current: AdviceSentimentSource,
        previous: BaselineInfo,
        seven_day: BaselineInfo,
    ) -> dict[str, object]:
        response = self._client.chat.completions.create(
            model=self._model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _comparison_system_prompt()},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "symbol": current.symbol,
                            "run_date": current.run_date,
                            "current_report": current.source_text,
                            "previous_baseline": {
                                "status": previous.status,
                                "run_date": previous.run_date,
                                "reports": previous.source_texts,
                            },
                            "seven_day_baseline": {
                                "status": seven_day.status,
                                "start_date": seven_day.start_date,
                                "end_date": seven_day.end_date,
                                "reports": seven_day.source_texts,
                            },
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            temperature=0,
        )
        content = response.choices[0].message.content or ""
        payload = json.loads(content)
        if not isinstance(payload, dict):
            raise ValueError("sentiment comparison response must be a JSON object")
        return payload


def _comparison_system_prompt() -> str:
    return """You compare TradingAgents sentiment/news reports for a US symbol.
Return strict JSON with keys: status, severity, change_vs_previous,
change_vs_7d_baseline, new_topics, intensified_topics, faded_topics, risk_flags,
evidence.

Allowed status values: changed, unchanged, no_signal, insufficient_baseline,
missing_source, error, skipped.
Allowed severity values: none, info, review, high_review.

Focus only on changes in external discussion, news framing, source coverage,
source quality, and topics. Do not recommend buying, selling, trimming, adding,
position sizing, target prices, or stop losses. If the current source mostly says
no data was found, return status no_signal and severity none. Evidence excerpts
must be short and must come from the supplied reports."""
```

- [ ] **Step 4: Wire the CLI command**

Modify `src/open_trader/cli.py` imports:

```python
from .sentiment_changes import (
    LLMSentimentChangeComparator,
    SentimentChangesResult,
    generate_sentiment_changes,
)
```

Add parser after `extract-technical-facts`:

```python
    sentiment_changes_parser = subparsers.add_parser(
        "detect-sentiment-changes",
        help="Detect US sentiment/news changes from TradingAgents advice",
    )
    sentiment_changes_parser.add_argument(
        "--advice",
        type=Path,
        required=True,
        help="US TradingAgents trading advice CSV path",
    )
    sentiment_changes_parser.add_argument(
        "--portfolio",
        type=Path,
        default=Path("data/latest/portfolio.csv"),
    )
    sentiment_changes_parser.add_argument("--data-dir", type=Path, default=Path("data"))
    sentiment_changes_parser.add_argument("--reports-dir", type=Path, default=Path("reports"))
    sentiment_changes_parser.add_argument("--date", type=canonical_date, required=True)
    sentiment_changes_parser.add_argument(
        "--model",
        default="deepseek-v4-flash",
        help="DeepSeek model for sentiment change comparison",
    )
    sentiment_changes_parser.add_argument(
        "--update-latest",
        action="store_true",
        help="Update data/latest/US/sentiment_changes.json after writing dated artifact",
    )
```

Add command handling after `extract-technical-facts` handling:

```python
    if args.command == "detect-sentiment-changes":
        if not args.advice.exists():
            parser.error(f"advice CSV not found: {args.advice}")
        try:
            result = generate_sentiment_changes(
                advice_path=args.advice,
                portfolio_path=args.portfolio,
                trading_plan_path=args.data_dir / "latest" / "US" / "trading_plan.csv",
                watchlist_path=args.data_dir / "latest" / "US" / "watchlist.csv",
                data_dir=args.data_dir,
                reports_dir=args.reports_dir,
                run_date=args.date,
                comparator=LLMSentimentChangeComparator(model=args.model),
                update_latest=args.update_latest,
            )
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            parser.error(str(exc))
        print(f"run_date: {result.run_date}")
        print(f"sentiment_changes: {result.records}")
        print(f"changed: {result.changed}")
        print(f"review_required: {result.review_required}")
        print(f"failed: {result.failed}")
        print(f"sentiment_changes_json: {result.run_path}")
        print(f"report: {result.report_path}")
        print(f"latest: {result.latest_path}")
        return 0
```

- [ ] **Step 5: Run CLI tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_premarket_cli.py::test_detect_sentiment_changes_help_includes_expected_options tests/test_premarket_cli.py::test_detect_sentiment_changes_main_wires_generator -q
```

Expected: PASS.

- [ ] **Step 6: Run sentiment unit tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_sentiment_changes.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/open_trader/sentiment_changes.py src/open_trader/cli.py tests/test_premarket_cli.py
git commit -m "feat: add sentiment change CLI"
```

---

### Task 5: Premarket And Daily Pipeline Integration

**Files:**
- Modify: `src/open_trader/advice/premarket.py`
- Modify: `src/open_trader/daily_premarket.py`
- Modify: `tests/test_premarket_pipeline.py`
- Modify: `tests/test_daily_premarket.py`

- [ ] **Step 1: Add failing premarket tests for US generation and HK skip**

Append to `tests/test_premarket_pipeline.py`:

```python
def test_run_premarket_generates_sentiment_changes_for_us_after_advice(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    reports_dir = tmp_path / "reports"
    portfolio_path = data_dir / "latest/portfolio.csv"
    write_portfolio(portfolio_path, [portfolio_row(symbol="VIXY", market="US")])
    calls: list[dict[str, object]] = []

    def fake_sentiment_changes_generator(**kwargs: object):
        calls.append(kwargs)
        run_path = data_dir / "runs/2026-06-22/US/sentiment_changes.json"
        report_path = reports_dir / "sentiment_changes/2026-06-22-US.md"
        latest_path = data_dir / "latest/US/sentiment_changes.json"
        run_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        run_path.write_text('{"schema_version":"open_trader.sentiment_changes.v1","records":[]}', encoding="utf-8")
        report_path.write_text("# report\n", encoding="utf-8")
        return SentimentChangesResult(
            run_date="2026-06-22",
            market="US",
            records=0,
            changed=0,
            review_required=0,
            failed=0,
            run_path=run_path,
            report_path=report_path,
            latest_path=latest_path,
        )

    result = run_premarket(
        run_date="2026-06-22",
        portfolio_path=portfolio_path,
        data_dir=data_dir,
        reports_dir=reports_dir,
        advice_runner=FakeAdviceRunner(),
        classifier=FakeClassifier(),
        market="US",
        technical_facts_generator=FakeTechnicalFactsGenerator(),
        sentiment_changes_generator=fake_sentiment_changes_generator,
    )

    assert calls
    assert calls[0]["run_date"] == "2026-06-22"
    assert result.sentiment_changes_path == data_dir / "runs/2026-06-22/US/sentiment_changes.json"


def test_run_premarket_skips_sentiment_changes_for_hk(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    reports_dir = tmp_path / "reports"
    portfolio_path = data_dir / "latest/portfolio.csv"
    write_portfolio(portfolio_path, [portfolio_row(symbol="02476", market="HK")])
    calls: list[dict[str, object]] = []

    result = run_premarket(
        run_date="2026-06-22",
        portfolio_path=portfolio_path,
        data_dir=data_dir,
        reports_dir=reports_dir,
        advice_runner=FakeAdviceRunner(),
        classifier=FakeClassifier(),
        market="HK",
        technical_facts_generator=FakeTechnicalFactsGenerator(),
        sentiment_changes_generator=lambda **kwargs: calls.append(kwargs),
    )

    assert calls == []
    assert result.sentiment_changes_path is None
```

Add the imports required by the appended tests at the top of `tests/test_premarket_pipeline.py`:

```python
from open_trader.sentiment_changes import SentimentChangesResult
```

Add this local helper near the other test helpers:

```python
def sentiment_portfolio_row(symbol: str, market: str) -> dict[str, str]:
    return {
        "broker": "futu",
        "brokers": "futu",
        "symbol": symbol,
        "analysis_symbol": symbol,
        "market": market,
        "asset_class": "stock",
        "quantity": "10",
        "currency": "USD" if market == "US" else "HKD",
        "cost_basis": "10",
        "market_value": "100",
        "market_value_hkd": "780",
        "portfolio_weight_hkd": "1.0%",
        "risk_flag": "normal",
    }
```

- [ ] **Step 2: Run premarket tests to verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_premarket_pipeline.py::test_run_premarket_generates_sentiment_changes_for_us_after_advice tests/test_premarket_pipeline.py::test_run_premarket_skips_sentiment_changes_for_hk -q
```

Expected: FAIL because `sentiment_changes_generator` and `sentiment_changes_path` do not exist.

- [ ] **Step 3: Modify premarket result and generator hook**

In `src/open_trader/advice/premarket.py`, import:

```python
from open_trader.sentiment_changes import (
    LLMSentimentChangeComparator,
    SentimentChangesResult,
    generate_sentiment_changes,
)
```

Add type alias near `TechnicalFactsGenerator`:

```python
SentimentChangesGenerator = Callable[..., SentimentChangesResult]
```

Add field to `PremarketResult`:

```python
    sentiment_changes_path: Path | None = None
```

Add parameter to `run_premarket()`:

```python
    sentiment_changes_generator: SentimentChangesGenerator | None = None,
```

After `_generate_technical_facts_after_advice(...)`, add:

```python
    sentiment_changes_result = _generate_sentiment_changes_after_advice(
        advice_path=advice_path,
        portfolio_path=portfolio_path,
        data_dir=data_dir,
        reports_dir=reports_dir,
        run_date=run_date,
        update_latest=False,
        market=market_scope,
        sentiment_changes_generator=sentiment_changes_generator,
    )
```

Pass to `_promote_latest_outputs()`:

```python
            sentiment_changes_path=(
                sentiment_changes_result.run_path
                if sentiment_changes_result is not None
                else None
            ),
```

Return:

```python
        sentiment_changes_path=(
            sentiment_changes_result.run_path
            if sentiment_changes_result is not None
            else None
        ),
```

Add helper:

```python
def _generate_sentiment_changes_after_advice(
    *,
    advice_path: Path,
    portfolio_path: Path,
    data_dir: Path,
    reports_dir: Path,
    run_date: str,
    update_latest: bool,
    market: MarketScope | None,
    sentiment_changes_generator: SentimentChangesGenerator | None,
) -> SentimentChangesResult | None:
    if market is not MarketScope.US:
        return None
    generator = sentiment_changes_generator
    if generator is None:
        comparator = LLMSentimentChangeComparator()

        def generator(**kwargs: object) -> SentimentChangesResult:
            return generate_sentiment_changes(comparator=comparator, **kwargs)  # type: ignore[arg-type]

    return generator(
        advice_path=advice_path,
        portfolio_path=portfolio_path,
        trading_plan_path=data_dir / "latest" / "US" / "trading_plan.csv",
        watchlist_path=data_dir / "latest" / "US" / "watchlist.csv",
        data_dir=data_dir,
        reports_dir=reports_dir,
        run_date=run_date,
        update_latest=update_latest,
    )
```

Update `_promote_latest_outputs()` in `src/open_trader/advice/premarket.py` and `_promote_latest_set()` in `src/open_trader/daily_premarket.py` to accept `sentiment_changes_path: Path | None = None`, and append a promotion to `latest_dir / "sentiment_changes.json"` only when the path is not `None`.

- [ ] **Step 4: Run premarket tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_premarket_pipeline.py::test_run_premarket_generates_sentiment_changes_for_us_after_advice tests/test_premarket_pipeline.py::test_run_premarket_skips_sentiment_changes_for_hk -q
```

Expected: PASS.

- [ ] **Step 5: Add failing daily latest promotion tests**

Append to `tests/test_daily_premarket.py`:

```python
def test_daily_runner_promotes_us_sentiment_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / "data"
    reports_dir = tmp_path / "reports"
    sentiment_path = data_dir / "runs/2026-06-22/US/sentiment_changes.json"
    sentiment_path.parent.mkdir(parents=True, exist_ok=True)
    sentiment_path.write_text('{"schema_version":"open_trader.sentiment_changes.v1","records":[]}', encoding="utf-8")

    _promote_latest_set(
        advice_path=_write_file(data_dir / "runs/2026-06-22/US/trading_advice.csv", "a"),
        actions_path=_write_file(data_dir / "runs/2026-06-22/US/premarket_actions.csv", "b"),
        plan_path=_write_file(data_dir / "runs/2026-06-22/US/trading_plan.csv", "c"),
        trade_actions_path=_write_file(data_dir / "runs/2026-06-22/US/trade_actions.csv", "d"),
        technical_facts_path=_write_file(data_dir / "runs/2026-06-22/US/technical_facts.json", "{}"),
        sentiment_changes_path=sentiment_path,
        data_dir=data_dir,
        market="US",
    )

    assert (data_dir / "latest/US/sentiment_changes.json").read_text(encoding="utf-8") == sentiment_path.read_text(encoding="utf-8")
```

Add this helper near the daily premarket test helpers:

```python
def _write_file(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path
```

- [ ] **Step 6: Run daily promotion test to verify it fails**

Run:

```bash
.venv/bin/python -m pytest tests/test_daily_premarket.py::test_daily_runner_promotes_us_sentiment_changes -q
```

Expected: FAIL because `_promote_latest_set()` does not accept `sentiment_changes_path`.

- [ ] **Step 7: Modify daily promotion and reports**

In `src/open_trader/daily_premarket.py`, update `_promote_latest_set()` signature:

```python
    sentiment_changes_path: Path | None = None,
```

Append promotion:

```python
    if sentiment_changes_path is not None:
        promotions.append(
            _LatestPromotion(
                source_path=sentiment_changes_path,
                latest_path=latest_dir / "sentiment_changes.json",
            )
        )
```

Where daily status artifacts are collected, add keys:

```python
            "sentiment_changes": str(premarket_result.sentiment_changes_path or ""),
            "latest_sentiment_changes": str(latest_dir / "sentiment_changes.json") if market_scope is MarketScope.US else "",
```

Where the daily Markdown artifact list is rendered, include `sentiment_changes` and `latest_sentiment_changes` in the artifact key order.

- [ ] **Step 8: Run pipeline tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_premarket_pipeline.py tests/test_daily_premarket.py -q
```

Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/open_trader/advice/premarket.py src/open_trader/daily_premarket.py tests/test_premarket_pipeline.py tests/test_daily_premarket.py
git commit -m "feat: run sentiment changes in US pipeline"
```

---

### Task 6: Dashboard Payload Attachment

**Files:**
- Modify: `src/open_trader/dashboard.py`
- Modify: `tests/test_dashboard.py`

- [ ] **Step 1: Add failing dashboard tests**

Append to `tests/test_dashboard.py`:

```python
def test_dashboard_attaches_us_sentiment_changes(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    portfolio = data_dir / "latest/portfolio.csv"
    write_csv(
        portfolio,
        ["market", "symbol", "asset_class", "market_value_hkd"],
        [{"market": "US", "symbol": "VIXY", "asset_class": "stock", "market_value_hkd": "1000"}],
    )
    sentiment_path = data_dir / "latest/US/sentiment_changes.json"
    sentiment_path.parent.mkdir(parents=True, exist_ok=True)
    sentiment_path.write_text(
        json.dumps(
            {
                "schema_version": "open_trader.sentiment_changes.v1",
                "run_date": "2026-06-22",
                "market": "US",
                "records": [
                    {
                        "market": "US",
                        "symbol": "VIXY",
                        "status": "changed",
                        "severity": "review",
                        "change_vs_previous": "Discussion changed.",
                        "change_vs_7d_baseline": "Topic intensity rose.",
                        "new_topics": ["VIX reliability"],
                        "intensified_topics": ["VIXY lagging"],
                        "faded_topics": [],
                        "risk_flags": ["discussion_change_requires_review"],
                        "decision_use": "manual_review_only",
                        "error": "",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    state = load_dashboard_state(
        DashboardConfig(
            portfolio_path=portfolio,
            data_dir=data_dir,
            reports_dir=tmp_path / "reports",
            poll_seconds=5,
            futu_host="127.0.0.1",
            futu_port=11111,
        )
    )

    assert state.holdings[0]["sentiment_changes"]["available"] is True
    assert state.holdings[0]["sentiment_changes"]["status"] == "changed"
    assert state.holdings[0]["sentiment_changes"]["decision_use"] == "manual_review_only"


def test_dashboard_marks_hk_sentiment_changes_unsupported(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    portfolio = data_dir / "latest/portfolio.csv"
    write_csv(
        portfolio,
        ["market", "symbol", "asset_class", "market_value_hkd"],
        [{"market": "HK", "symbol": "02476", "asset_class": "stock", "market_value_hkd": "1000"}],
    )

    state = load_dashboard_state(
        DashboardConfig(
            portfolio_path=portfolio,
            data_dir=data_dir,
            reports_dir=tmp_path / "reports",
            poll_seconds=5,
            futu_host="127.0.0.1",
            futu_port=11111,
        )
    )

    assert state.holdings[0]["sentiment_changes"] == {
        "available": False,
        "status": "unsupported_market",
        "error": "",
        "decision_use": "manual_review_only",
    }
```

Add the imports required by the appended tests at the top of `tests/test_dashboard.py`:

```python
import json
from pathlib import Path

from open_trader.dashboard import DashboardConfig, load_dashboard_state
```

Add this helper near the existing dashboard test helpers:

```python
def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard.py::test_dashboard_attaches_us_sentiment_changes tests/test_dashboard.py::test_dashboard_marks_hk_sentiment_changes_unsupported -q
```

Expected: FAIL because `sentiment_changes` is not attached.

- [ ] **Step 3: Attach sentiment changes in dashboard state**

In `src/open_trader/dashboard.py`, import:

```python
from .sentiment_changes import load_sentiment_changes_cache, sentiment_changes_latest_path
```

Add helper:

```python
def _latest_sentiment_changes_by_holding(data_dir: Path) -> dict[tuple[str, str], dict[str, Any]]:
    cache = load_sentiment_changes_cache(sentiment_changes_latest_path(data_dir))
    records = cache.get("records")
    if not isinstance(records, list):
        return {}
    indexed: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        market = str(record.get("market") or "").strip().upper()
        symbol = str(record.get("symbol") or "").strip().upper()
        if market == "US" and symbol:
            indexed[(market, symbol)] = record
    return indexed
```

In `load_dashboard_state()`, compute:

```python
    sentiment_changes_by_holding = _latest_sentiment_changes_by_holding(config.data_dir)
```

Pass it into `_merge_holding()`.

Update `_merge_holding()` signature:

```python
    sentiment_changes_by_holding: dict[tuple[str, str], dict[str, Any]],
```

Set:

```python
    holding["sentiment_changes"] = _sentiment_changes_detail(
        sentiment_changes_by_holding.get(key) if key is not None else None,
        market=key[0] if key is not None else row.get("market", ""),
    )
```

Add helper:

```python
def _sentiment_changes_detail(
    record: dict[str, Any] | None,
    *,
    market: str,
) -> dict[str, Any]:
    if market.strip().upper() != "US":
        return {
            "available": False,
            "status": "unsupported_market",
            "error": "",
            "decision_use": "manual_review_only",
        }
    if record is None:
        return {
            "available": False,
            "status": "missing_record",
            "error": "sentiment_changes.json record not found",
            "decision_use": "manual_review_only",
        }
    return {
        "available": True,
        "status": str(record.get("status") or ""),
        "severity": str(record.get("severity") or ""),
        "change_vs_previous": str(record.get("change_vs_previous") or ""),
        "change_vs_7d_baseline": str(record.get("change_vs_7d_baseline") or ""),
        "new_topics": record.get("new_topics") if isinstance(record.get("new_topics"), list) else [],
        "intensified_topics": record.get("intensified_topics") if isinstance(record.get("intensified_topics"), list) else [],
        "faded_topics": record.get("faded_topics") if isinstance(record.get("faded_topics"), list) else [],
        "risk_flags": record.get("risk_flags") if isinstance(record.get("risk_flags"), list) else [],
        "decision_use": "manual_review_only",
        "error": str(record.get("error") or ""),
    }
```

- [ ] **Step 4: Run dashboard tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard.py::test_dashboard_attaches_us_sentiment_changes tests/test_dashboard.py::test_dashboard_marks_hk_sentiment_changes_unsupported -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/dashboard.py tests/test_dashboard.py
git commit -m "feat: attach sentiment changes to dashboard"
```

---

### Task 7: Dashboard Frontend Rendering

**Files:**
- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Modify: `src/open_trader/dashboard_static/dashboard.css`
- Modify: `tests/test_dashboard_web.py`

- [ ] **Step 1: Add failing frontend tests**

Append to `tests/test_dashboard_web.py`:

```python
def test_dashboard_renders_sentiment_change_plugin_for_us() -> None:
    html = render_dashboard_with_holding(
        {
            "market": "US",
            "symbol": "VIXY",
            "name": "VIXY",
            "sentiment_changes": {
                "available": True,
                "status": "changed",
                "severity": "review",
                "change_vs_previous": "StockTwits discussion changed.",
                "change_vs_7d_baseline": "VIX reliability became more prominent.",
                "new_topics": ["VIX reliability"],
                "intensified_topics": ["VIXY lagging"],
                "decision_use": "manual_review_only",
                "error": "",
            },
        }
    )

    assert "舆论变化" in html
    assert "需复核" in html
    assert "StockTwits discussion changed." in html
    assert "VIX reliability" in html
    assert "仅用于人工复核，不改变交易动作" in html
    assert "新闻 / 舆论" not in html


def test_dashboard_renders_quiet_hk_sentiment_unsupported_state() -> None:
    html = render_dashboard_with_holding(
        {
            "market": "HK",
            "symbol": "02476",
            "name": "HK holding",
            "sentiment_changes": {
                "available": False,
                "status": "unsupported_market",
                "error": "",
                "decision_use": "manual_review_only",
            },
        }
    )

    assert "舆论变化" in html
    assert "暂不覆盖港股" in html
    assert "不可用" not in html
```

Add this helper to `tests/test_dashboard_web.py`:

```python
def render_dashboard_with_holding(holding: dict[str, object]) -> str:
    payload = {
        "summary": {},
        "holdings": [holding],
        "broker_summaries": [],
        "source_statuses": [],
        "cash_rows": [],
        "broker_positions": [],
        "cash_details": [],
        "trade_actions": [],
        "poll_seconds": 5,
    }
    script = f"""
const fs = require("fs");
const vm = require("vm");
const code = fs.readFileSync("src/open_trader/dashboard_static/dashboard.js", "utf8");
const sandbox = {{
  document: {{ addEventListener() {{}}, getElementById() {{ return null; }}, querySelector() {{ return null; }} }},
  window: {{ clearInterval() {{}}, setInterval() {{ return 1; }} }},
  fetch() {{ throw new Error("fetch disabled"); }},
  console,
}};
vm.createContext(sandbox);
vm.runInContext(code, sandbox);
sandbox.state.dashboard = {json.dumps(payload, ensure_ascii=False)};
sandbox.state.selectedHoldingKey = "{holding.get("market", "")}.{holding.get("symbol", "")}";
const html = sandbox.renderSymbolDetail(sandbox.state.dashboard.holdings[0], 0);
console.log(html);
"""
    result = subprocess.run(
        ["node", "-e", script],
        cwd=Path.cwd(),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout
```

Add these imports at the top of `tests/test_dashboard_web.py`:

```python
import json
import subprocess
from pathlib import Path
```

- [ ] **Step 2: Run frontend tests to verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py::test_dashboard_renders_sentiment_change_plugin_for_us tests/test_dashboard_web.py::test_dashboard_renders_quiet_hk_sentiment_unsupported_state -q
```

Expected: FAIL because the placeholder still renders.

- [ ] **Step 3: Replace the placeholder plugin**

In `src/open_trader/dashboard_static/dashboard.js`, replace the hard-coded `新闻 / 舆论` object inside `renderTradingDecisionPlugins()` with:

```javascript
    sentimentChangesPlugin(holding),
```

Add functions near `klineTechnicalFactsPlugin()`:

```javascript
function sentimentChangesPlugin(holding) {
  const detail = holding && typeof holding.sentiment_changes === "object"
    ? holding.sentiment_changes
    : null;
  const status = detail && hasValue(detail.status) ? String(detail.status) : "missing_record";
  if (status === "unsupported_market") {
    return {
      title: "舆论变化",
      status: "暂不覆盖",
      tone: "muted",
      score: "-",
      headline: "暂不覆盖港股",
      detail: "当前仅监测美股持仓和美股计划标的。",
      condition: "仅用于人工复核，不改变交易动作。",
    };
  }
  if (!detail || detail.available !== true) {
    return {
      title: "舆论变化",
      status: "基线不足",
      tone: "partial",
      score: "-",
      headline: "暂无变化记录",
      detail: firstPresent(detail && detail.error, "尚未生成 sentiment_changes.json。"),
      condition: "仅用于人工复核，不改变交易动作。",
    };
  }
  const labels = {
    changed: "有变化",
    unchanged: "无变化",
    no_signal: "无有效信号",
    insufficient_baseline: "基线不足",
    missing_source: "缺少来源",
    error: "错误",
    skipped: "跳过",
  };
  const tones = {
    changed: detail.severity === "high_review" ? "failed" : "partial",
    unchanged: "ok",
    no_signal: "muted",
    insufficient_baseline: "partial",
    missing_source: "failed",
    error: "failed",
    skipped: "muted",
  };
  const topics = []
    .concat(Array.isArray(detail.new_topics) ? detail.new_topics : [])
    .concat(Array.isArray(detail.intensified_topics) ? detail.intensified_topics : [])
    .filter(Boolean)
    .slice(0, 4);
  return {
    title: "舆论变化",
    status: labels[status] || "未知",
    tone: tones[status] || "partial",
    score: "变化",
    headline: firstPresent(detail.change_vs_previous, labels[status], "暂无变化摘要"),
    detail: firstPresent(detail.change_vs_7d_baseline, detail.error, "用于提示外部讨论变化。"),
    bodyHtml: renderSentimentTopicChips(topics),
    condition: "仅用于人工复核，不改变交易动作。",
  };
}


function renderSentimentTopicChips(topics) {
  if (!topics.length) {
    return "";
  }
  return `
    <div class="sentiment-topic-row">
      ${topics.map((topic) => `<span>${escapeHtml(String(topic))}</span>`).join("")}
    </div>
  `;
}
```

- [ ] **Step 4: Add compact CSS for topic chips**

Add to `src/open_trader/dashboard_static/dashboard.css` near plugin styles:

```css
.sentiment-topic-row {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 10px;
}

.sentiment-topic-row span {
  border: 1px solid #d8dee6;
  border-radius: 8px;
  color: #3b4756;
  font-size: 12px;
  line-height: 1.4;
  padding: 3px 7px;
}
```

- [ ] **Step 5: Run frontend tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py::test_dashboard_renders_sentiment_change_plugin_for_us tests/test_dashboard_web.py::test_dashboard_renders_quiet_hk_sentiment_unsupported_state -q
```

Expected: PASS.

- [ ] **Step 6: Run dashboard-related tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard.py tests/test_dashboard_web.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/open_trader/dashboard_static/dashboard.js src/open_trader/dashboard_static/dashboard.css tests/test_dashboard_web.py
git commit -m "feat: render sentiment change dashboard card"
```

---

### Task 8: Documentation And Final Verification

**Files:**
- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Review: all touched source and test files

- [ ] **Step 1: Add docs for the sentiment change monitor**

In `README.zh-CN.md`, add a short section near the dashboard or daily automation docs:

```markdown
### 美股舆论变化监测

美股每日流程会在 TradingAgents 建议生成后，读取每个美股标的的
`sentiment_report` 和 `news_report`，并和上一次报告及过去 7 天本地报告进行比较。
输出只用于人工复核，不会改变 trading plan、trade actions 或下单流程。

产物：

```text
data/runs/<YYYY-MM-DD>/US/sentiment_changes.json
data/latest/US/sentiment_changes.json
reports/sentiment_changes/<YYYY-MM-DD>-US.md
```

手动回填：

```bash
.venv/bin/python -m open_trader detect-sentiment-changes \
  --advice data/latest/US/trading_advice.csv \
  --portfolio data/latest/portfolio.csv \
  --data-dir data \
  --reports-dir reports \
  --date 2026-06-22 \
  --update-latest
```

港股暂不接入舆论变化监测。
```

In `README.md`, add the English equivalent:

```markdown
### US Sentiment Change Monitoring

The US daily workflow can compare TradingAgents `sentiment_report` and
`news_report` text against the previous local report and the recent seven-day
local baseline. The output is manual-review-only and never changes trading
plans, trade actions, or order flow.

Artifacts:

```text
data/runs/<YYYY-MM-DD>/US/sentiment_changes.json
data/latest/US/sentiment_changes.json
reports/sentiment_changes/<YYYY-MM-DD>-US.md
```

Manual backfill:

```bash
.venv/bin/python -m open_trader detect-sentiment-changes \
  --advice data/latest/US/trading_advice.csv \
  --portfolio data/latest/portfolio.csv \
  --data-dir data \
  --reports-dir reports \
  --date 2026-06-22 \
  --update-latest
```

HK sentiment change monitoring is intentionally skipped in the first version.
```

- [ ] **Step 2: Run focused test suite**

Run:

```bash
.venv/bin/python -m pytest tests/test_sentiment_changes.py tests/test_premarket_cli.py tests/test_premarket_pipeline.py tests/test_daily_premarket.py tests/test_dashboard.py tests/test_dashboard_web.py -q
```

Expected: PASS.

- [ ] **Step 3: Run full test suite**

Run:

```bash
.venv/bin/python -m pytest -q
```

Expected: PASS.

- [ ] **Step 4: Run CLI smoke with local fixture-like current data without updating latest**

Run:

```bash
.venv/bin/python -m open_trader detect-sentiment-changes \
  --advice data/latest/trading_advice.csv \
  --portfolio data/latest/portfolio.csv \
  --data-dir data \
  --reports-dir reports \
  --date 2026-06-22
```

Expected: command exits 0 and prints:

```text
run_date: 2026-06-22
sentiment_changes: <number>
changed: <number>
review_required: <number>
failed: <number>
sentiment_changes_json: data/runs/2026-06-22/US/sentiment_changes.json
report: reports/sentiment_changes/2026-06-22-US.md
latest: data/latest/US/sentiment_changes.json
```

Because this smoke may call the real LLM, run it only when `DEEPSEEK_API_KEY` is configured. If not configured, skip this smoke and record that only fake-comparator tests were run.

- [ ] **Step 5: Inspect generated Markdown if smoke was run**

Run:

```bash
sed -n '1,180p' reports/sentiment_changes/2026-06-22-US.md
```

Expected: report includes `仅用于人工复核，不改变交易动作。` and does not include direct buy/sell instructions from the sentiment comparator.

- [ ] **Step 6: Check worktree and secret safety**

Run:

```bash
git status --short
git diff --check
git diff -- README.md README.zh-CN.md src/open_trader tests
```

Expected: only intended source, test, and README changes; no credential values.

- [ ] **Step 7: Commit docs and final integration**

```bash
git add README.md README.zh-CN.md
git commit -m "docs: document sentiment change monitoring"
```

If source/test changes from previous tasks are still uncommitted at this point, stop and make the missing earlier task commit before creating the docs commit.

---

## Self-Review Checklist

- Spec coverage:
  - US-only scope: Tasks 1, 5, 6, and 7 skip or quiet-state HK.
  - Source data from TradingAgents `sentiment_report/news_report`: Task 1.
  - Previous and seven-day baselines: Task 2.
  - JSON/Markdown artifacts and latest promotion: Tasks 3 and 5.
  - Strict manual-review-only comparator: Tasks 3 and 4.
  - Dashboard `舆论变化` card: Tasks 6 and 7.
  - CLI and daily pipeline: Tasks 4 and 5.
  - Documentation and verification: Task 8.
- Placeholder scan: no task contains `TBD`, `TODO`, or an instruction to fill details later.
- Type consistency:
  - Main module is `open_trader.sentiment_changes`.
  - Schema version is `open_trader.sentiment_changes.v1`.
  - Artifact paths are `data/runs/<date>/US/sentiment_changes.json`, `data/latest/US/sentiment_changes.json`, and `reports/sentiment_changes/<date>-US.md`.
  - Result type is `SentimentChangesResult`.
  - Dashboard field is `holding["sentiment_changes"]`.
