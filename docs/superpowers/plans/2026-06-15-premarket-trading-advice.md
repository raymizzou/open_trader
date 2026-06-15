# Premarket Trading Advice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the daily premarket advice workflow that runs TradingAgents for each AI-eligible US holding, uses a model prompt to classify material advice changes, and writes Markdown + CSV action reports.

**Architecture:** Add a new `open_trader.advice` package with small modules for portfolio loading, TradingAgents integration, advice state storage, change classification, report writing, and orchestration. The production pipeline uses TradingAgents and an OpenAI structured-output classifier, while tests use fake adapters and fake classifier clients so normal test runs do not call external services.

**Tech Stack:** Python 3.12, standard-library `csv`/`json`/`argparse`/`dataclasses`, optional `openai` SDK for the real change classifier, local `/Users/ray/projects/TradingAgents`, pytest.

---

## Source Spec

Implement:

```text
docs/superpowers/specs/2026-06-15-premarket-trading-advice-design.md
```

Important constraints:

- No automatic order placement.
- No human approve/reject workflow.
- Analyze only rows where `ai_eligible=true`.
- Generate both Markdown and CSV.
- The model classifier, not hard-coded Python rules, decides whether a symbol enters the report.
- Unit tests must not call the real TradingAgents project or an external model.

## File Structure

Create:

- `src/open_trader/advice/__init__.py`: package marker and exports.
- `src/open_trader/advice/models.py`: dataclasses and CSV fieldnames.
- `src/open_trader/advice/portfolio_loader.py`: read and filter portfolio rows.
- `src/open_trader/advice/store.py`: read/write dated run files and latest advice snapshots.
- `src/open_trader/advice/tradingagents_adapter.py`: adapter around local TradingAgents.
- `src/open_trader/advice/change_classifier.py`: prompt rendering, JSON schema validation, fakeable classifier interface, OpenAI implementation.
- `src/open_trader/advice/prompts/change_classifier.md`: version-controlled prompt section for per-symbol change analysis.
- `src/open_trader/advice/report.py`: Markdown and action CSV report helpers.
- `src/open_trader/advice/premarket.py`: orchestration pipeline.
- `tests/test_advice_models.py`
- `tests/test_advice_portfolio_loader.py`
- `tests/test_advice_store.py`
- `tests/test_tradingagents_adapter.py`
- `tests/test_change_classifier.py`
- `tests/test_premarket_report.py`
- `tests/test_premarket_pipeline.py`

Modify:

- `src/open_trader/cli.py`: add `run-premarket` command.
- `pyproject.toml`: add `openai` dependency for the real classifier.
- `docs/monthly_portfolio_import.md`: add a short "Next daily step" section pointing to `run-premarket`.

Generated runtime files remain ignored by existing `data/` ignore rule. Add `reports/` to `.gitignore` if it is not already ignored.

## Task 1: Advice Models And CSV Fieldnames

**Files:**
- Create: `src/open_trader/advice/__init__.py`
- Create: `src/open_trader/advice/models.py`
- Test: `tests/test_advice_models.py`

- [ ] **Step 1: Write failing model tests**

Create `tests/test_advice_models.py`:

```python
from __future__ import annotations

from open_trader.advice.models import (
    CHANGE_CLASSIFICATION_FIELDNAMES,
    PREMARKET_ACTION_FIELDNAMES,
    TRADING_ADVICE_FIELDNAMES,
    ChangeClassification,
    PortfolioInputRow,
    PremarketAction,
    TradingAdvice,
)


def test_trading_advice_to_row_has_stable_csv_fields() -> None:
    advice = TradingAdvice(
        run_date="2026-06-16",
        symbol="VIXY",
        market="US",
        asset_class="etf",
        portfolio_weight_hkd="3.05%",
        risk_flag="normal",
        source="tradingagents",
        advice_action="reduce",
        advice_summary="Trim volatility ETF exposure.",
        raw_decision='{"action":"reduce"}',
        status="ok",
        error="",
    )

    row = advice.to_row()

    assert list(row) == TRADING_ADVICE_FIELDNAMES
    assert row["symbol"] == "VIXY"
    assert row["advice_action"] == "reduce"
    assert row["error"] == ""


def test_change_classification_to_row_has_required_fields() -> None:
    classification = ChangeClassification(
        run_date="2026-06-16",
        symbol="VIXY",
        include_in_report=True,
        change_type="action_changed",
        severity="high",
        suggested_action="reduce",
        summary="VIXY changed from hold to reduce.",
        rationale="The latest advice materially lowers risk appetite.",
        watch_trigger="Open below prior close.",
        status="ok",
        error="",
    )

    row = classification.to_row()

    assert list(row) == CHANGE_CLASSIFICATION_FIELDNAMES
    assert row["include_in_report"] == "true"
    assert row["severity"] == "high"


def test_premarket_action_is_derived_from_portfolio_and_classification() -> None:
    portfolio = PortfolioInputRow(
        symbol="VIXY",
        market="US",
        asset_class="etf",
        name="Volatility ETF",
        portfolio_weight_hkd="3.05%",
        risk_flag="normal",
        analysis_symbol="VIXY",
    )
    classification = ChangeClassification(
        run_date="2026-06-16",
        symbol="VIXY",
        include_in_report=True,
        change_type="action_changed",
        severity="high",
        suggested_action="reduce",
        summary="VIXY changed from hold to reduce.",
        rationale="The latest advice materially lowers risk appetite.",
        watch_trigger="Open below prior close.",
        status="ok",
        error="",
    )

    action = PremarketAction.from_classification(portfolio, classification)

    assert list(action.to_row()) == PREMARKET_ACTION_FIELDNAMES
    assert action.symbol == "VIXY"
    assert action.market == "US"
    assert action.portfolio_weight_hkd == "3.05%"
```

- [ ] **Step 2: Run the failing tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_advice_models.py -v
```

Expected: fail with `ModuleNotFoundError: No module named 'open_trader.advice'`.

- [ ] **Step 3: Create the advice package marker**

Create `src/open_trader/advice/__init__.py`:

```python
"""Daily premarket trading advice workflow."""
```

- [ ] **Step 4: Implement the dataclasses and fieldnames**

Create `src/open_trader/advice/models.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


AdviceStatus = Literal["ok", "error"]
ChangeType = Literal[
    "new_signal",
    "action_changed",
    "risk_changed",
    "trigger_changed",
    "no_material_change",
]
Severity = Literal["low", "medium", "high"]


TRADING_ADVICE_FIELDNAMES = [
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
]

CHANGE_CLASSIFICATION_FIELDNAMES = [
    "run_date",
    "symbol",
    "include_in_report",
    "change_type",
    "severity",
    "suggested_action",
    "summary",
    "rationale",
    "watch_trigger",
    "status",
    "error",
]

PREMARKET_ACTION_FIELDNAMES = [
    "run_date",
    "symbol",
    "market",
    "portfolio_weight_hkd",
    "severity",
    "change_type",
    "suggested_action",
    "summary",
    "rationale",
    "watch_trigger",
]


@dataclass(frozen=True)
class PortfolioInputRow:
    symbol: str
    market: str
    asset_class: str
    name: str
    portfolio_weight_hkd: str
    risk_flag: str
    analysis_symbol: str


@dataclass(frozen=True)
class TradingAdvice:
    run_date: str
    symbol: str
    market: str
    asset_class: str
    portfolio_weight_hkd: str
    risk_flag: str
    source: str
    advice_action: str
    advice_summary: str
    raw_decision: str
    status: AdviceStatus
    error: str

    def to_row(self) -> dict[str, str]:
        return {field: str(getattr(self, field)) for field in TRADING_ADVICE_FIELDNAMES}


@dataclass(frozen=True)
class ChangeClassification:
    run_date: str
    symbol: str
    include_in_report: bool
    change_type: ChangeType
    severity: Severity
    suggested_action: str
    summary: str
    rationale: str
    watch_trigger: str
    status: AdviceStatus
    error: str

    def to_row(self) -> dict[str, str]:
        row = {
            field: str(getattr(self, field))
            for field in CHANGE_CLASSIFICATION_FIELDNAMES
        }
        row["include_in_report"] = "true" if self.include_in_report else "false"
        return row


@dataclass(frozen=True)
class PremarketAction:
    run_date: str
    symbol: str
    market: str
    portfolio_weight_hkd: str
    severity: Severity
    change_type: ChangeType
    suggested_action: str
    summary: str
    rationale: str
    watch_trigger: str

    @classmethod
    def from_classification(
        cls,
        portfolio_row: PortfolioInputRow,
        classification: ChangeClassification,
    ) -> PremarketAction:
        return cls(
            run_date=classification.run_date,
            symbol=classification.symbol,
            market=portfolio_row.market,
            portfolio_weight_hkd=portfolio_row.portfolio_weight_hkd,
            severity=classification.severity,
            change_type=classification.change_type,
            suggested_action=classification.suggested_action,
            summary=classification.summary,
            rationale=classification.rationale,
            watch_trigger=classification.watch_trigger,
        )

    def to_row(self) -> dict[str, str]:
        return {field: str(getattr(self, field)) for field in PREMARKET_ACTION_FIELDNAMES}
```

- [ ] **Step 5: Run the model tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_advice_models.py -v
```

Expected: `3 passed`.

- [ ] **Step 6: Commit**

```bash
git add src/open_trader/advice/__init__.py src/open_trader/advice/models.py tests/test_advice_models.py
git commit -m "feat: add advice data models"
```

## Task 2: Portfolio Loader

**Files:**
- Create: `src/open_trader/advice/portfolio_loader.py`
- Test: `tests/test_advice_portfolio_loader.py`

- [ ] **Step 1: Write failing loader tests**

Create `tests/test_advice_portfolio_loader.py`:

```python
from __future__ import annotations

import csv
from pathlib import Path

import pytest

from open_trader.advice.portfolio_loader import load_eligible_portfolio_rows


FIELDNAMES = [
    "market",
    "asset_class",
    "symbol",
    "name",
    "portfolio_weight_hkd",
    "ai_eligible",
    "analysis_symbol",
    "risk_flag",
]


def write_portfolio(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def test_load_eligible_portfolio_rows_filters_ai_eligible_rows(tmp_path: Path) -> None:
    portfolio_path = tmp_path / "portfolio.csv"
    write_portfolio(
        portfolio_path,
        [
            {
                "market": "US",
                "asset_class": "etf",
                "symbol": "VIXY",
                "name": "Volatility ETF",
                "portfolio_weight_hkd": "3.05%",
                "ai_eligible": "true",
                "analysis_symbol": "VIXY",
                "risk_flag": "normal",
            },
            {
                "market": "HK",
                "asset_class": "stock",
                "symbol": "02476",
                "name": "VGT",
                "portfolio_weight_hkd": "15.20%",
                "ai_eligible": "false",
                "analysis_symbol": "",
                "risk_flag": "overweight",
            },
        ],
    )

    rows = load_eligible_portfolio_rows(portfolio_path)

    assert [row.symbol for row in rows] == ["VIXY"]
    assert rows[0].analysis_symbol == "VIXY"
    assert rows[0].portfolio_weight_hkd == "3.05%"


def test_load_eligible_portfolio_rows_uses_analysis_symbol_when_present(
    tmp_path: Path,
) -> None:
    portfolio_path = tmp_path / "portfolio.csv"
    write_portfolio(
        portfolio_path,
        [
            {
                "market": "US",
                "asset_class": "stock",
                "symbol": "BRK.B",
                "name": "Berkshire Hathaway",
                "portfolio_weight_hkd": "2.00%",
                "ai_eligible": "true",
                "analysis_symbol": "BRK-B",
                "risk_flag": "normal",
            },
        ],
    )

    rows = load_eligible_portfolio_rows(portfolio_path)

    assert rows[0].symbol == "BRK.B"
    assert rows[0].analysis_symbol == "BRK-B"


def test_load_eligible_portfolio_rows_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_eligible_portfolio_rows(tmp_path / "missing.csv")
```

- [ ] **Step 2: Run the failing tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_advice_portfolio_loader.py -v
```

Expected: fail with `ModuleNotFoundError` for `portfolio_loader`.

- [ ] **Step 3: Implement the loader**

Create `src/open_trader/advice/portfolio_loader.py`:

```python
from __future__ import annotations

import csv
from pathlib import Path

from .models import PortfolioInputRow


def load_eligible_portfolio_rows(portfolio_path: Path) -> list[PortfolioInputRow]:
    with portfolio_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))

    eligible: list[PortfolioInputRow] = []
    for row in rows:
        if row.get("ai_eligible", "").lower() != "true":
            continue
        symbol = row.get("symbol", "").strip()
        analysis_symbol = row.get("analysis_symbol", "").strip() or symbol
        eligible.append(
            PortfolioInputRow(
                symbol=symbol,
                market=row.get("market", "").strip(),
                asset_class=row.get("asset_class", "").strip(),
                name=row.get("name", "").strip(),
                portfolio_weight_hkd=row.get("portfolio_weight_hkd", "").strip(),
                risk_flag=row.get("risk_flag", "").strip(),
                analysis_symbol=analysis_symbol,
            )
        )
    return eligible
```

- [ ] **Step 4: Run the loader tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_advice_portfolio_loader.py -v
```

Expected: `3 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/advice/portfolio_loader.py tests/test_advice_portfolio_loader.py
git commit -m "feat: load eligible portfolio rows"
```

## Task 3: Advice Store And Atomic Latest Promotion

**Files:**
- Create: `src/open_trader/advice/store.py`
- Test: `tests/test_advice_store.py`

- [ ] **Step 1: Write failing store tests**

Create `tests/test_advice_store.py`:

```python
from __future__ import annotations

import csv
from pathlib import Path

from open_trader.advice.models import ChangeClassification, TradingAdvice
from open_trader.advice.store import (
    load_latest_advice_by_symbol,
    write_change_classifications,
    write_trading_advice,
)


def advice(symbol: str, action: str = "hold") -> TradingAdvice:
    return TradingAdvice(
        run_date="2026-06-16",
        symbol=symbol,
        market="US",
        asset_class="etf",
        portfolio_weight_hkd="3.05%",
        risk_flag="normal",
        source="tradingagents",
        advice_action=action,
        advice_summary=f"{symbol} {action}",
        raw_decision='{"action":"hold"}',
        status="ok",
        error="",
    )


def classification(symbol: str) -> ChangeClassification:
    return ChangeClassification(
        run_date="2026-06-16",
        symbol=symbol,
        include_in_report=True,
        change_type="new_signal",
        severity="medium",
        suggested_action="watch",
        summary=f"{symbol} watch",
        rationale="New symbol in advice store.",
        watch_trigger="",
        status="ok",
        error="",
    )


def test_write_trading_advice_writes_run_and_latest_files(tmp_path: Path) -> None:
    run_path, latest_path = write_trading_advice(
        run_date="2026-06-16",
        records=[advice("VIXY"), advice("QQQ")],
        data_dir=tmp_path / "data",
        update_latest=True,
    )

    assert run_path == tmp_path / "data" / "runs" / "2026-06-16" / "trading_advice.csv"
    assert latest_path == tmp_path / "data" / "latest" / "trading_advice.csv"
    assert run_path.read_text(encoding="utf-8") == latest_path.read_text(encoding="utf-8")

    rows = list(csv.DictReader(run_path.open(encoding="utf-8")))
    assert [row["symbol"] for row in rows] == ["VIXY", "QQQ"]


def test_write_trading_advice_dry_run_does_not_update_latest(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    write_trading_advice(
        run_date="2026-06-15",
        records=[advice("OLD")],
        data_dir=data_dir,
        update_latest=True,
    )
    original_latest = (data_dir / "latest" / "trading_advice.csv").read_text(
        encoding="utf-8"
    )

    write_trading_advice(
        run_date="2026-06-16",
        records=[advice("NEW")],
        data_dir=data_dir,
        update_latest=False,
    )

    assert (data_dir / "latest" / "trading_advice.csv").read_text(
        encoding="utf-8"
    ) == original_latest


def test_load_latest_advice_by_symbol_returns_empty_when_missing(tmp_path: Path) -> None:
    assert load_latest_advice_by_symbol(tmp_path / "data") == {}


def test_load_latest_advice_by_symbol_indexes_existing_latest(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    write_trading_advice(
        run_date="2026-06-16",
        records=[advice("VIXY", "reduce")],
        data_dir=data_dir,
        update_latest=True,
    )

    latest = load_latest_advice_by_symbol(data_dir)

    assert latest["VIXY"]["advice_action"] == "reduce"


def test_write_change_classifications_writes_run_file(tmp_path: Path) -> None:
    path = write_change_classifications(
        run_date="2026-06-16",
        records=[classification("VIXY")],
        data_dir=tmp_path / "data",
    )

    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    assert rows[0]["symbol"] == "VIXY"
    assert rows[0]["include_in_report"] == "true"
```

- [ ] **Step 2: Run the failing tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_advice_store.py -v
```

Expected: fail with missing `store` module.

- [ ] **Step 3: Implement the store**

Create `src/open_trader/advice/store.py`:

```python
from __future__ import annotations

import csv
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Iterable

from open_trader.csv_io import write_rows

from .models import (
    CHANGE_CLASSIFICATION_FIELDNAMES,
    TRADING_ADVICE_FIELDNAMES,
    ChangeClassification,
    TradingAdvice,
)


def write_trading_advice(
    *,
    run_date: str,
    records: Iterable[TradingAdvice],
    data_dir: Path,
    update_latest: bool,
) -> tuple[Path, Path]:
    rows = [record.to_row() for record in records]
    run_path = data_dir / "runs" / run_date / "trading_advice.csv"
    latest_path = data_dir / "latest" / "trading_advice.csv"
    write_rows(run_path, TRADING_ADVICE_FIELDNAMES, rows)
    if update_latest:
        _atomic_write_latest(latest_path, TRADING_ADVICE_FIELDNAMES, rows)
    return run_path, latest_path


def write_change_classifications(
    *,
    run_date: str,
    records: Iterable[ChangeClassification],
    data_dir: Path,
) -> Path:
    run_path = data_dir / "runs" / run_date / "change_classifications.csv"
    write_rows(
        run_path,
        CHANGE_CLASSIFICATION_FIELDNAMES,
        (record.to_row() for record in records),
    )
    return run_path


def load_latest_advice_by_symbol(data_dir: Path) -> dict[str, dict[str, str]]:
    latest_path = data_dir / "latest" / "trading_advice.csv"
    if not latest_path.exists():
        return {}
    with latest_path.open(encoding="utf-8", newline="") as handle:
        return {
            row["symbol"]: row
            for row in csv.DictReader(handle)
            if row.get("symbol")
        }


def _atomic_write_latest(
    path: Path,
    fieldnames: list[str],
    rows: list[dict[str, str]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temp_path = Path(handle.name)
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temp_path.replace(path)
```

- [ ] **Step 4: Run the store tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_advice_store.py -v
```

Expected: `5 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/advice/store.py tests/test_advice_store.py
git commit -m "feat: store daily trading advice"
```

## Task 4: TradingAgents Adapter

**Files:**
- Create: `src/open_trader/advice/tradingagents_adapter.py`
- Test: `tests/test_tradingagents_adapter.py`

- [ ] **Step 1: Write failing adapter tests**

Create `tests/test_tradingagents_adapter.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from open_trader.advice.models import PortfolioInputRow
from open_trader.advice.tradingagents_adapter import TradingAgentsAdapter


class FakeGraph:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def propagate(self, symbol: str, run_date: str) -> tuple[dict[str, str], dict[str, str]]:
        self.calls.append((symbol, run_date))
        return {"symbol": symbol}, {
            "action": "hold",
            "summary": f"Hold {symbol}",
        }


def portfolio_row(symbol: str = "VIXY") -> PortfolioInputRow:
    return PortfolioInputRow(
        symbol=symbol,
        market="US",
        asset_class="etf",
        name="Volatility ETF",
        portfolio_weight_hkd="3.05%",
        risk_flag="normal",
        analysis_symbol=symbol,
    )


def test_adapter_calls_graph_and_normalizes_success() -> None:
    graph = FakeGraph()
    adapter = TradingAgentsAdapter.from_graph(graph)

    advice = adapter.analyze(portfolio_row("VIXY"), "2026-06-16")

    assert graph.calls == [("VIXY", "2026-06-16")]
    assert advice.symbol == "VIXY"
    assert advice.source == "tradingagents"
    assert advice.advice_action == "hold"
    assert advice.advice_summary == "Hold VIXY"
    assert advice.status == "ok"
    assert '"action": "hold"' in advice.raw_decision


def test_adapter_records_symbol_failure_as_error() -> None:
    class FailingGraph:
        def propagate(self, symbol: str, run_date: str) -> tuple[dict[str, str], dict[str, str]]:
            raise RuntimeError("network unavailable")

    adapter = TradingAgentsAdapter.from_graph(FailingGraph())

    advice = adapter.analyze(portfolio_row("QQQ"), "2026-06-16")

    assert advice.symbol == "QQQ"
    assert advice.status == "error"
    assert advice.error == "network unavailable"
    assert advice.raw_decision == ""


def test_adapter_rejects_missing_tradingagents_path(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        TradingAgentsAdapter.from_project_path(tmp_path / "missing")
```

- [ ] **Step 2: Run the failing tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_tradingagents_adapter.py -v
```

Expected: fail with missing module.

- [ ] **Step 3: Implement the adapter**

Create `src/open_trader/advice/tradingagents_adapter.py`:

```python
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from .models import PortfolioInputRow, TradingAdvice


class TradingAgentsAdapter:
    def __init__(self, graph: Any) -> None:
        self._graph = graph

    @classmethod
    def from_graph(cls, graph: Any) -> TradingAgentsAdapter:
        return cls(graph)

    @classmethod
    def from_project_path(cls, project_path: Path) -> TradingAgentsAdapter:
        if not project_path.exists():
            raise FileNotFoundError(project_path)
        project_path_str = str(project_path)
        if project_path_str not in sys.path:
            sys.path.insert(0, project_path_str)
        from tradingagents.default_config import DEFAULT_CONFIG
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        graph = TradingAgentsGraph(debug=False, config=DEFAULT_CONFIG.copy())
        return cls(graph)

    def analyze(self, row: PortfolioInputRow, run_date: str) -> TradingAdvice:
        try:
            _, decision = self._graph.propagate(row.analysis_symbol, run_date)
        except Exception as exc:
            return TradingAdvice(
                run_date=run_date,
                symbol=row.symbol,
                market=row.market,
                asset_class=row.asset_class,
                portfolio_weight_hkd=row.portfolio_weight_hkd,
                risk_flag=row.risk_flag,
                source="tradingagents",
                advice_action="",
                advice_summary="",
                raw_decision="",
                status="error",
                error=str(exc),
            )

        return TradingAdvice(
            run_date=run_date,
            symbol=row.symbol,
            market=row.market,
            asset_class=row.asset_class,
            portfolio_weight_hkd=row.portfolio_weight_hkd,
            risk_flag=row.risk_flag,
            source="tradingagents",
            advice_action=_extract_action(decision),
            advice_summary=_extract_summary(decision),
            raw_decision=json.dumps(decision, ensure_ascii=False, sort_keys=True),
            status="ok",
            error="",
        )


def _extract_action(decision: Any) -> str:
    if isinstance(decision, dict):
        for key in ("action", "decision", "recommendation", "signal"):
            value = decision.get(key)
            if value:
                return str(value)
    return ""


def _extract_summary(decision: Any) -> str:
    if isinstance(decision, dict):
        for key in ("summary", "reasoning", "rationale", "analysis"):
            value = decision.get(key)
            if value:
                return str(value)
    return str(decision)
```

- [ ] **Step 4: Run the adapter tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_tradingagents_adapter.py -v
```

Expected: `3 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/advice/tradingagents_adapter.py tests/test_tradingagents_adapter.py
git commit -m "feat: add tradingagents advice adapter"
```

## Task 5: Change Classifier Prompt And Structured Model Client

**Files:**
- Modify: `pyproject.toml`
- Create: `src/open_trader/advice/change_classifier.py`
- Create: `src/open_trader/advice/prompts/change_classifier.md`
- Test: `tests/test_change_classifier.py`

- [ ] **Step 1: Write failing classifier tests**

Create `tests/test_change_classifier.py`:

```python
from __future__ import annotations

import json
from pathlib import Path

import pytest

from open_trader.advice.change_classifier import (
    ChangeClassifier,
    InvalidClassificationError,
    build_classifier_payload,
    load_prompt,
    validate_classifier_output,
)
from open_trader.advice.models import PortfolioInputRow, TradingAdvice


def portfolio_row() -> PortfolioInputRow:
    return PortfolioInputRow(
        symbol="VIXY",
        market="US",
        asset_class="etf",
        name="Volatility ETF",
        portfolio_weight_hkd="3.05%",
        risk_flag="normal",
        analysis_symbol="VIXY",
    )


def latest_advice(action: str = "reduce") -> TradingAdvice:
    return TradingAdvice(
        run_date="2026-06-16",
        symbol="VIXY",
        market="US",
        asset_class="etf",
        portfolio_weight_hkd="3.05%",
        risk_flag="normal",
        source="tradingagents",
        advice_action=action,
        advice_summary=f"Latest action is {action}.",
        raw_decision='{"action":"reduce"}',
        status="ok",
        error="",
    )


def test_load_prompt_reads_version_controlled_prompt() -> None:
    prompt = load_prompt()

    assert "include_in_report" in prompt
    assert "previous advice" in prompt.lower()
    assert "latest tradingagents advice" in prompt.lower()


def test_build_classifier_payload_includes_previous_and_latest_advice() -> None:
    payload = build_classifier_payload(
        run_date="2026-06-16",
        portfolio_row=portfolio_row(),
        previous_advice={"advice_action": "hold", "advice_summary": "Old hold."},
        latest_advice=latest_advice(),
    )

    assert payload["portfolio"]["symbol"] == "VIXY"
    assert payload["previous_advice"]["advice_action"] == "hold"
    assert payload["latest_advice"]["advice_action"] == "reduce"


def test_validate_classifier_output_accepts_valid_json() -> None:
    output = validate_classifier_output(
        json.dumps(
            {
                "include_in_report": True,
                "change_type": "action_changed",
                "severity": "high",
                "suggested_action": "reduce",
                "summary": "VIXY changed from hold to reduce.",
                "rationale": "Latest advice materially changed.",
                "watch_trigger": "If price loses prior support.",
            }
        )
    )

    assert output.include_in_report is True
    assert output.change_type == "action_changed"
    assert output.severity == "high"


def test_validate_classifier_output_rejects_invalid_enum() -> None:
    with pytest.raises(InvalidClassificationError, match="change_type"):
        validate_classifier_output(
            json.dumps(
                {
                    "include_in_report": True,
                    "change_type": "urgent",
                    "severity": "high",
                    "suggested_action": "reduce",
                    "summary": "summary",
                    "rationale": "rationale",
                    "watch_trigger": "",
                }
            )
        )


def test_change_classifier_uses_client_response() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.payloads: list[dict[str, object]] = []

        def classify(self, prompt: str, payload: dict[str, object]) -> str:
            self.payloads.append(payload)
            return json.dumps(
                {
                    "include_in_report": True,
                    "change_type": "new_signal",
                    "severity": "medium",
                    "suggested_action": "watch",
                    "summary": "New watch item.",
                    "rationale": "No previous advice exists.",
                    "watch_trigger": "",
                }
            )

    client = FakeClient()
    classifier = ChangeClassifier(client=client)

    result = classifier.classify(
        run_date="2026-06-16",
        portfolio_row=portfolio_row(),
        previous_advice=None,
        latest_advice=latest_advice("watch"),
    )

    assert result.symbol == "VIXY"
    assert result.status == "ok"
    assert result.include_in_report is True
    assert client.payloads[0]["previous_advice"] is None
```

- [ ] **Step 2: Run the failing tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_change_classifier.py -v
```

Expected: fail with missing `change_classifier` module.

- [ ] **Step 3: Add the OpenAI dependency**

Modify `pyproject.toml` dependencies:

```toml
dependencies = [
    "openai>=2.0.0",
    "pdfplumber>=0.11.9",
]
```

Then run:

```bash
.venv/bin/python -m pip install -e ".[dev]"
```

Expected: command exits `0`.

- [ ] **Step 4: Create the prompt file**

Create `src/open_trader/advice/prompts/change_classifier.md`:

```markdown
# Premarket Change Classifier

You are reviewing one portfolio holding before market open.

Compare the previous advice, latest TradingAgents advice, and current portfolio
context. Decide whether the latest advice is a material change that should
appear in today's premarket action report.

Include an item in the report only when a trader should actively notice it
today. Do not include routine restatements with no material change.

Return exactly one JSON object with these keys:

- include_in_report: boolean
- change_type: one of new_signal, action_changed, risk_changed, trigger_changed, no_material_change
- severity: one of low, medium, high
- suggested_action: short action phrase, such as hold, watch, reduce, add, exit
- summary: one concise sentence for the report
- rationale: short explanation of why this matters now
- watch_trigger: optional trigger condition; empty string if none

Do not recommend automatic order placement. This system only writes reports.
```

- [ ] **Step 5: Implement classifier validation and client interface**

Create `src/open_trader/advice/change_classifier.py`:

```python
from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from .models import ChangeClassification, PortfolioInputRow, TradingAdvice


PROMPT_PATH = Path(__file__).parent / "prompts" / "change_classifier.md"
CHANGE_TYPES = {
    "new_signal",
    "action_changed",
    "risk_changed",
    "trigger_changed",
    "no_material_change",
}
SEVERITIES = {"low", "medium", "high"}


class InvalidClassificationError(ValueError):
    pass


class ClassifierClient(Protocol):
    def classify(self, prompt: str, payload: dict[str, object]) -> str:
        pass


class ChangeClassifier:
    def __init__(self, client: ClassifierClient) -> None:
        self._client = client
        self._prompt = load_prompt()

    def classify(
        self,
        *,
        run_date: str,
        portfolio_row: PortfolioInputRow,
        previous_advice: dict[str, str] | None,
        latest_advice: TradingAdvice,
    ) -> ChangeClassification:
        if latest_advice.status != "ok":
            return ChangeClassification(
                run_date=run_date,
                symbol=latest_advice.symbol,
                include_in_report=False,
                change_type="no_material_change",
                severity="low",
                suggested_action="",
                summary="",
                rationale="",
                watch_trigger="",
                status="error",
                error=latest_advice.error,
            )
        try:
            payload = build_classifier_payload(
                run_date=run_date,
                portfolio_row=portfolio_row,
                previous_advice=previous_advice,
                latest_advice=latest_advice,
            )
            parsed = validate_classifier_output(self._client.classify(self._prompt, payload))
        except Exception as exc:
            return ChangeClassification(
                run_date=run_date,
                symbol=latest_advice.symbol,
                include_in_report=False,
                change_type="no_material_change",
                severity="low",
                suggested_action="",
                summary="",
                rationale="",
                watch_trigger="",
                status="error",
                error=str(exc),
            )
        return ChangeClassification(
            run_date=run_date,
            symbol=latest_advice.symbol,
            include_in_report=parsed.include_in_report,
            change_type=parsed.change_type,
            severity=parsed.severity,
            suggested_action=parsed.suggested_action,
            summary=parsed.summary,
            rationale=parsed.rationale,
            watch_trigger=parsed.watch_trigger,
            status="ok",
            error="",
        )


def load_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def build_classifier_payload(
    *,
    run_date: str,
    portfolio_row: PortfolioInputRow,
    previous_advice: dict[str, str] | None,
    latest_advice: TradingAdvice,
) -> dict[str, object]:
    return {
        "run_date": run_date,
        "portfolio": {
            "symbol": portfolio_row.symbol,
            "market": portfolio_row.market,
            "asset_class": portfolio_row.asset_class,
            "name": portfolio_row.name,
            "portfolio_weight_hkd": portfolio_row.portfolio_weight_hkd,
            "risk_flag": portfolio_row.risk_flag,
        },
        "previous_advice": previous_advice,
        "latest_advice": latest_advice.to_row(),
    }


def validate_classifier_output(raw: str) -> _ParsedClassification:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InvalidClassificationError(f"invalid JSON: {exc}") from exc
    required = {
        "include_in_report",
        "change_type",
        "severity",
        "suggested_action",
        "summary",
        "rationale",
        "watch_trigger",
    }
    missing = sorted(required - set(data))
    if missing:
        raise InvalidClassificationError(f"missing field(s): {', '.join(missing)}")
    if not isinstance(data["include_in_report"], bool):
        raise InvalidClassificationError("include_in_report must be boolean")
    if data["change_type"] not in CHANGE_TYPES:
        raise InvalidClassificationError(f"invalid change_type: {data['change_type']}")
    if data["severity"] not in SEVERITIES:
        raise InvalidClassificationError(f"invalid severity: {data['severity']}")
    return _ParsedClassification(
        include_in_report=data["include_in_report"],
        change_type=data["change_type"],
        severity=data["severity"],
        suggested_action=str(data["suggested_action"]),
        summary=str(data["summary"]),
        rationale=str(data["rationale"]),
        watch_trigger=str(data["watch_trigger"]),
    )


class _ParsedClassification:
    def __init__(
        self,
        *,
        include_in_report: bool,
        change_type: str,
        severity: str,
        suggested_action: str,
        summary: str,
        rationale: str,
        watch_trigger: str,
    ) -> None:
        self.include_in_report = include_in_report
        self.change_type = change_type
        self.severity = severity
        self.suggested_action = suggested_action
        self.summary = summary
        self.rationale = rationale
        self.watch_trigger = watch_trigger
```

- [ ] **Step 6: Add the OpenAI classifier client**

Append to `src/open_trader/advice/change_classifier.py`:

```python
CLASSIFICATION_JSON_SCHEMA = {
    "name": "premarket_change_classification",
    "schema": {
        "type": "object",
        "properties": {
            "include_in_report": {"type": "boolean"},
            "change_type": {"type": "string", "enum": sorted(CHANGE_TYPES)},
            "severity": {"type": "string", "enum": sorted(SEVERITIES)},
            "suggested_action": {"type": "string"},
            "summary": {"type": "string"},
            "rationale": {"type": "string"},
            "watch_trigger": {"type": "string"},
        },
        "required": [
            "include_in_report",
            "change_type",
            "severity",
            "suggested_action",
            "summary",
            "rationale",
            "watch_trigger",
        ],
        "additionalProperties": False,
    },
    "strict": True,
}


class OpenAIClassifierClient:
    def __init__(self, *, model: str = "gpt-5.4-mini") -> None:
        from openai import OpenAI

        self._client = OpenAI()
        self._model = model

    def classify(self, prompt: str, payload: dict[str, object]) -> str:
        response = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": prompt},
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False, sort_keys=True),
                },
            ],
            response_format={
                "type": "json_schema",
                "json_schema": CLASSIFICATION_JSON_SCHEMA,
            },
        )
        content = response.choices[0].message.content
        if not content:
            raise InvalidClassificationError("model returned empty content")
        return content
```

- [ ] **Step 7: Run classifier tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_change_classifier.py -v
```

Expected: `5 passed`. These tests must not require `OPENAI_API_KEY`.

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml src/open_trader/advice/change_classifier.py src/open_trader/advice/prompts/change_classifier.md tests/test_change_classifier.py
git commit -m "feat: classify advice changes with model prompt"
```

## Task 6: Premarket Report Writer

**Files:**
- Create: `src/open_trader/advice/report.py`
- Test: `tests/test_premarket_report.py`

- [ ] **Step 1: Write failing report tests**

Create `tests/test_premarket_report.py`:

```python
from __future__ import annotations

import csv
from pathlib import Path

from open_trader.advice.models import PremarketAction
from open_trader.advice.report import write_premarket_outputs


def action(
    symbol: str,
    severity: str = "medium",
    weight: str = "3.05%",
) -> PremarketAction:
    return PremarketAction(
        run_date="2026-06-16",
        symbol=symbol,
        market="US",
        portfolio_weight_hkd=weight,
        severity=severity,  # type: ignore[arg-type]
        change_type="action_changed",
        suggested_action="reduce",
        summary=f"{symbol} needs action.",
        rationale=f"{symbol} latest advice changed.",
        watch_trigger="Watch the open.",
    )


def test_write_premarket_outputs_writes_actions_csv_and_markdown(tmp_path: Path) -> None:
    actions_csv, latest_csv, report_path = write_premarket_outputs(
        run_date="2026-06-16",
        actions=[action("QQQ", "low", "1.40%"), action("VIXY", "high", "3.05%")],
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
    )

    rows = list(csv.DictReader(actions_csv.open(encoding="utf-8")))
    assert [row["symbol"] for row in rows] == ["VIXY", "QQQ"]
    assert latest_csv.read_text(encoding="utf-8") == actions_csv.read_text(
        encoding="utf-8"
    )

    markdown = report_path.read_text(encoding="utf-8")
    assert "# Premarket Trading Brief - 2026-06-16" in markdown
    assert "## Action Items" in markdown
    assert "VIXY" in markdown
    assert "QQQ" in markdown


def test_write_premarket_outputs_handles_no_actions(tmp_path: Path) -> None:
    _, _, report_path = write_premarket_outputs(
        run_date="2026-06-16",
        actions=[],
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
    )

    markdown = report_path.read_text(encoding="utf-8")
    assert "No material trading advice changes" in markdown
```

- [ ] **Step 2: Run the failing tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_premarket_report.py -v
```

Expected: fail with missing `report` module.

- [ ] **Step 3: Implement the report writer**

Create `src/open_trader/advice/report.py`:

```python
from __future__ import annotations

import csv
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Iterable

from open_trader.csv_io import write_rows

from .models import PREMARKET_ACTION_FIELDNAMES, PremarketAction


SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def write_premarket_outputs(
    *,
    run_date: str,
    actions: Iterable[PremarketAction],
    data_dir: Path,
    reports_dir: Path,
) -> tuple[Path, Path, Path]:
    sorted_actions = sorted(
        actions,
        key=lambda item: (
            SEVERITY_ORDER[item.severity],
            _negative_weight(item.portfolio_weight_hkd),
            item.symbol,
        ),
    )
    run_actions_path = data_dir / "runs" / run_date / "premarket_actions.csv"
    latest_actions_path = data_dir / "latest" / "premarket_actions.csv"
    rows = [action.to_row() for action in sorted_actions]
    write_rows(run_actions_path, PREMARKET_ACTION_FIELDNAMES, rows)
    _atomic_write_csv(latest_actions_path, PREMARKET_ACTION_FIELDNAMES, rows)

    report_path = reports_dir / "premarket" / f"{run_date}.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        _render_markdown(run_date, sorted_actions),
        encoding="utf-8",
    )
    return run_actions_path, latest_actions_path, report_path


def _render_markdown(run_date: str, actions: list[PremarketAction]) -> str:
    lines = [f"# Premarket Trading Brief - {run_date}", ""]
    if not actions:
        lines.append("No material trading advice changes were generated.")
        lines.append("")
        return "\n".join(lines)

    lines.extend(["## Action Items", ""])
    for index, action in enumerate(actions, start=1):
        lines.extend(
            [
                f"### {index}. {action.symbol}",
                "",
                f"- Severity: {action.severity}",
                f"- Current weight: {action.portfolio_weight_hkd}",
                f"- Change type: {action.change_type}",
                f"- Suggested action: {action.suggested_action}",
                f"- Summary: {action.summary}",
                f"- Rationale: {action.rationale}",
            ]
        )
        if action.watch_trigger:
            lines.append(f"- Watch trigger: {action.watch_trigger}")
        lines.append("")
    return "\n".join(lines)


def _negative_weight(value: str) -> float:
    try:
        return -float(value.rstrip("%"))
    except ValueError:
        return 0.0


def _atomic_write_csv(
    path: Path,
    fieldnames: list[str],
    rows: list[dict[str, str]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temp_path = Path(handle.name)
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temp_path.replace(path)
```

- [ ] **Step 4: Run report tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_premarket_report.py -v
```

Expected: `2 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/advice/report.py tests/test_premarket_report.py
git commit -m "feat: write premarket action reports"
```

## Task 7: Premarket Pipeline Orchestration

**Files:**
- Create: `src/open_trader/advice/premarket.py`
- Test: `tests/test_premarket_pipeline.py`

- [ ] **Step 1: Write failing pipeline tests**

Create `tests/test_premarket_pipeline.py`:

```python
from __future__ import annotations

import csv
from pathlib import Path

from open_trader.advice.models import ChangeClassification, PortfolioInputRow, TradingAdvice
from open_trader.advice.premarket import PremarketResult, run_premarket


PORTFOLIO_FIELDNAMES = [
    "market",
    "asset_class",
    "symbol",
    "name",
    "portfolio_weight_hkd",
    "ai_eligible",
    "analysis_symbol",
    "risk_flag",
]


class FakeAdviceRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def analyze(self, row: PortfolioInputRow, run_date: str) -> TradingAdvice:
        self.calls.append((row.symbol, run_date))
        return TradingAdvice(
            run_date=run_date,
            symbol=row.symbol,
            market=row.market,
            asset_class=row.asset_class,
            portfolio_weight_hkd=row.portfolio_weight_hkd,
            risk_flag=row.risk_flag,
            source="fake",
            advice_action="reduce" if row.symbol == "VIXY" else "hold",
            advice_summary=f"{row.symbol} summary",
            raw_decision="{}",
            status="ok",
            error="",
        )


class FakeClassifier:
    def classify(
        self,
        *,
        run_date: str,
        portfolio_row: PortfolioInputRow,
        previous_advice: dict[str, str] | None,
        latest_advice: TradingAdvice,
    ) -> ChangeClassification:
        return ChangeClassification(
            run_date=run_date,
            symbol=portfolio_row.symbol,
            include_in_report=portfolio_row.symbol == "VIXY",
            change_type="action_changed" if portfolio_row.symbol == "VIXY" else "no_material_change",
            severity="high" if portfolio_row.symbol == "VIXY" else "low",
            suggested_action=latest_advice.advice_action,
            summary=f"{portfolio_row.symbol} changed",
            rationale="Fake classifier rationale.",
            watch_trigger="",
            status="ok",
            error="",
        )


def write_portfolio(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=PORTFOLIO_FIELDNAMES)
        writer.writeheader()
        writer.writerows(
            [
                {
                    "market": "US",
                    "asset_class": "etf",
                    "symbol": "VIXY",
                    "name": "Volatility ETF",
                    "portfolio_weight_hkd": "3.05%",
                    "ai_eligible": "true",
                    "analysis_symbol": "VIXY",
                    "risk_flag": "normal",
                },
                {
                    "market": "US",
                    "asset_class": "stock",
                    "symbol": "QQQ",
                    "name": "Nasdaq ETF",
                    "portfolio_weight_hkd": "1.40%",
                    "ai_eligible": "true",
                    "analysis_symbol": "QQQ",
                    "risk_flag": "normal",
                },
                {
                    "market": "HK",
                    "asset_class": "stock",
                    "symbol": "02476",
                    "name": "VGT",
                    "portfolio_weight_hkd": "15.20%",
                    "ai_eligible": "false",
                    "analysis_symbol": "",
                    "risk_flag": "overweight",
                },
            ]
        )


def test_run_premarket_writes_full_advice_classifications_and_actions(
    tmp_path: Path,
) -> None:
    portfolio_path = tmp_path / "portfolio.csv"
    write_portfolio(portfolio_path)
    advice_runner = FakeAdviceRunner()

    result = run_premarket(
        run_date="2026-06-16",
        portfolio_path=portfolio_path,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        advice_runner=advice_runner,
        classifier=FakeClassifier(),
        symbols=None,
        update_latest=True,
    )

    assert isinstance(result, PremarketResult)
    assert result.eligible_count == 2
    assert result.action_count == 1
    assert advice_runner.calls == [("VIXY", "2026-06-16"), ("QQQ", "2026-06-16")]
    assert result.report_path.exists()

    actions = list(csv.DictReader(result.actions_path.open(encoding="utf-8")))
    assert [row["symbol"] for row in actions] == ["VIXY"]

    advice_rows = list(csv.DictReader(result.advice_path.open(encoding="utf-8")))
    assert [row["symbol"] for row in advice_rows] == ["VIXY", "QQQ"]


def test_run_premarket_symbols_subset_limits_analysis(tmp_path: Path) -> None:
    portfolio_path = tmp_path / "portfolio.csv"
    write_portfolio(portfolio_path)
    advice_runner = FakeAdviceRunner()

    result = run_premarket(
        run_date="2026-06-16",
        portfolio_path=portfolio_path,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        advice_runner=advice_runner,
        classifier=FakeClassifier(),
        symbols={"QQQ"},
        update_latest=True,
    )

    assert result.eligible_count == 1
    assert advice_runner.calls == [("QQQ", "2026-06-16")]


def test_run_premarket_dry_run_does_not_update_latest_advice(tmp_path: Path) -> None:
    portfolio_path = tmp_path / "portfolio.csv"
    write_portfolio(portfolio_path)
    data_dir = tmp_path / "data"

    run_premarket(
        run_date="2026-06-16",
        portfolio_path=portfolio_path,
        data_dir=data_dir,
        reports_dir=tmp_path / "reports",
        advice_runner=FakeAdviceRunner(),
        classifier=FakeClassifier(),
        symbols=None,
        update_latest=False,
    )

    assert not (data_dir / "latest" / "trading_advice.csv").exists()
    assert (data_dir / "latest" / "premarket_actions.csv").exists()
```

- [ ] **Step 2: Run the failing tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_premarket_pipeline.py -v
```

Expected: fail with missing `premarket` module.

- [ ] **Step 3: Implement the pipeline**

Create `src/open_trader/advice/premarket.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .models import ChangeClassification, PortfolioInputRow, PremarketAction, TradingAdvice
from .portfolio_loader import load_eligible_portfolio_rows
from .report import write_premarket_outputs
from .store import (
    load_latest_advice_by_symbol,
    write_change_classifications,
    write_trading_advice,
)


class AdviceRunner(Protocol):
    def analyze(self, row: PortfolioInputRow, run_date: str) -> TradingAdvice:
        pass


class Classifier(Protocol):
    def classify(
        self,
        *,
        run_date: str,
        portfolio_row: PortfolioInputRow,
        previous_advice: dict[str, str] | None,
        latest_advice: TradingAdvice,
    ) -> ChangeClassification:
        pass


@dataclass(frozen=True)
class PremarketResult:
    eligible_count: int
    advice_count: int
    action_count: int
    advice_path: Path
    classifications_path: Path
    actions_path: Path
    report_path: Path


def run_premarket(
    *,
    run_date: str,
    portfolio_path: Path,
    data_dir: Path,
    reports_dir: Path,
    advice_runner: AdviceRunner,
    classifier: Classifier,
    symbols: set[str] | None,
    update_latest: bool,
) -> PremarketResult:
    rows = load_eligible_portfolio_rows(portfolio_path)
    if symbols is not None:
        normalized_symbols = {symbol.upper() for symbol in symbols}
        rows = [
            row
            for row in rows
            if row.symbol.upper() in normalized_symbols
            or row.analysis_symbol.upper() in normalized_symbols
        ]

    previous_by_symbol = load_latest_advice_by_symbol(data_dir)
    advice_records: list[TradingAdvice] = []
    classifications: list[ChangeClassification] = []
    actions: list[PremarketAction] = []

    for row in rows:
        advice = advice_runner.analyze(row, run_date)
        advice_records.append(advice)
        classification = classifier.classify(
            run_date=run_date,
            portfolio_row=row,
            previous_advice=previous_by_symbol.get(row.symbol),
            latest_advice=advice,
        )
        classifications.append(classification)
        if classification.status == "ok" and classification.include_in_report:
            actions.append(PremarketAction.from_classification(row, classification))

    advice_path, _ = write_trading_advice(
        run_date=run_date,
        records=advice_records,
        data_dir=data_dir,
        update_latest=update_latest,
    )
    classifications_path = write_change_classifications(
        run_date=run_date,
        records=classifications,
        data_dir=data_dir,
    )
    actions_path, _, report_path = write_premarket_outputs(
        run_date=run_date,
        actions=actions,
        data_dir=data_dir,
        reports_dir=reports_dir,
    )
    return PremarketResult(
        eligible_count=len(rows),
        advice_count=len(advice_records),
        action_count=len(actions),
        advice_path=advice_path,
        classifications_path=classifications_path,
        actions_path=actions_path,
        report_path=report_path,
    )
```

- [ ] **Step 4: Run pipeline tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_premarket_pipeline.py -v
```

Expected: `3 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/advice/premarket.py tests/test_premarket_pipeline.py
git commit -m "feat: orchestrate premarket advice runs"
```

## Task 8: CLI Wiring

**Files:**
- Modify: `src/open_trader/cli.py`
- Test: `tests/test_pipeline.py` or create `tests/test_premarket_cli.py`

- [ ] **Step 1: Write failing CLI tests**

Create `tests/test_premarket_cli.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest

import open_trader.cli as cli
from open_trader.cli import build_parser
from open_trader.advice.premarket import PremarketResult


def test_run_premarket_help_includes_expected_options(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["run-premarket", "--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "--date" in output
    assert "--portfolio" in output
    assert "--tradingagents-path" in output
    assert "--dry-run" in output


def test_run_premarket_main_wires_pipeline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}

    class FakeAdapter:
        @classmethod
        def from_project_path(cls, path: Path) -> FakeAdapter:
            captured["tradingagents_path"] = path
            return cls()

    class FakeOpenAIClassifierClient:
        def __init__(self, *, model: str) -> None:
            captured["model"] = model

    class FakeChangeClassifier:
        def __init__(self, client: object) -> None:
            captured["classifier_client"] = client

    def fake_run_premarket(**kwargs: object) -> PremarketResult:
        captured.update(kwargs)
        data_dir = kwargs["data_dir"]
        reports_dir = kwargs["reports_dir"]
        assert isinstance(data_dir, Path)
        assert isinstance(reports_dir, Path)
        return PremarketResult(
            eligible_count=2,
            advice_count=2,
            action_count=1,
            advice_path=data_dir / "runs" / "2026-06-16" / "trading_advice.csv",
            classifications_path=data_dir
            / "runs"
            / "2026-06-16"
            / "change_classifications.csv",
            actions_path=data_dir / "runs" / "2026-06-16" / "premarket_actions.csv",
            report_path=reports_dir / "premarket" / "2026-06-16.md",
        )

    monkeypatch.setattr(cli, "TradingAgentsAdapter", FakeAdapter)
    monkeypatch.setattr(cli, "OpenAIClassifierClient", FakeOpenAIClassifierClient)
    monkeypatch.setattr(cli, "ChangeClassifier", FakeChangeClassifier)
    monkeypatch.setattr(cli, "run_premarket", fake_run_premarket)

    result = cli.main(
        [
            "run-premarket",
            "--date",
            "2026-06-16",
            "--portfolio",
            "portfolio.csv",
            "--data-dir",
            str(tmp_path / "data"),
            "--reports-dir",
            str(tmp_path / "reports"),
            "--tradingagents-path",
            "/tmp/TradingAgents",
            "--symbols",
            "VIXY,QQQ",
            "--classifier-model",
            "gpt-5.4-mini",
            "--dry-run",
        ]
    )

    assert result == 0
    assert captured["run_date"] == "2026-06-16"
    assert captured["portfolio_path"] == Path("portfolio.csv")
    assert captured["symbols"] == {"VIXY", "QQQ"}
    assert captured["update_latest"] is False
    assert captured["tradingagents_path"] == Path("/tmp/TradingAgents")
    assert captured["model"] == "gpt-5.4-mini"

    output = capsys.readouterr().out
    assert "eligible: 2" in output
    assert "actions: 1" in output
    assert "report:" in output
```

- [ ] **Step 2: Run failing CLI tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_premarket_cli.py -v
```

Expected: fail because `run-premarket` is not registered.

- [ ] **Step 3: Wire imports and CLI arguments**

Modify `src/open_trader/cli.py` imports:

```python
from .advice.change_classifier import ChangeClassifier, OpenAIClassifierClient
from .advice.premarket import run_premarket
from .advice.tradingagents_adapter import TradingAgentsAdapter
```

Inside `build_parser()`, add after the `import-statements` parser:

```python
    premarket_parser = subparsers.add_parser(
        "run-premarket",
        help="Run daily premarket TradingAgents advice and write action report",
    )
    premarket_parser.add_argument("--date", required=True, help="Run date, YYYY-MM-DD")
    premarket_parser.add_argument(
        "--portfolio",
        type=Path,
        default=Path("data/latest/portfolio.csv"),
    )
    premarket_parser.add_argument("--data-dir", type=Path, default=Path("data"))
    premarket_parser.add_argument("--reports-dir", type=Path, default=Path("reports"))
    premarket_parser.add_argument(
        "--tradingagents-path",
        type=Path,
        default=Path("/Users/ray/projects/TradingAgents"),
    )
    premarket_parser.add_argument(
        "--symbols",
        help="Comma-separated subset of symbols to analyze",
    )
    premarket_parser.add_argument(
        "--classifier-model",
        default="gpt-5.4-mini",
        help="OpenAI model for change classification",
    )
    premarket_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Write run outputs but do not update latest trading advice",
    )
```

- [ ] **Step 4: Add CLI execution branch**

Add in `main()` before the final unknown command branch:

```python
    if args.command == "run-premarket":
        symbols = _parse_symbol_subset(args.symbols)
        result = run_premarket(
            run_date=args.date,
            portfolio_path=args.portfolio,
            data_dir=args.data_dir,
            reports_dir=args.reports_dir,
            advice_runner=TradingAgentsAdapter.from_project_path(args.tradingagents_path),
            classifier=ChangeClassifier(
                client=OpenAIClassifierClient(model=args.classifier_model)
            ),
            symbols=symbols,
            update_latest=not args.dry_run,
        )
        print(f"eligible: {result.eligible_count}")
        print(f"advice: {result.advice_count}")
        print(f"actions: {result.action_count}")
        print(f"advice_csv: {result.advice_path}")
        print(f"actions_csv: {result.actions_path}")
        print(f"report: {result.report_path}")
        return 0
```

Add helper near `canonical_month()`:

```python
def _parse_symbol_subset(value: str | None) -> set[str] | None:
    if value is None or not value.strip():
        return None
    return {symbol.strip().upper() for symbol in value.split(",") if symbol.strip()}
```

- [ ] **Step 5: Run CLI tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_premarket_cli.py -v
```

Expected: `2 passed`.

- [ ] **Step 6: Run existing pipeline CLI tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_pipeline.py tests/test_premarket_cli.py -v
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/open_trader/cli.py tests/test_premarket_cli.py
git commit -m "feat: add premarket advice command"
```

## Task 9: Docs, Ignore Rules, And Verification

**Files:**
- Modify: `.gitignore`
- Modify: `docs/monthly_portfolio_import.md`

- [ ] **Step 1: Update `.gitignore`**

Add:

```gitignore
reports/
```

Do not remove existing ignore rules.

- [ ] **Step 2: Update the monthly usage doc**

Append to `docs/monthly_portfolio_import.md`:

    ## Daily Premarket Advice

    After `data/latest/portfolio.csv` exists, run the daily premarket advice workflow:

    ```bash
    .venv/bin/python -m open_trader run-premarket \
      --date 2026-06-16 \
      --portfolio data/latest/portfolio.csv
    ```

    Optional test run for a subset:

    ```bash
    .venv/bin/python -m open_trader run-premarket \
      --date 2026-06-16 \
      --portfolio data/latest/portfolio.csv \
      --symbols VIXY,QQQ \
      --dry-run
    ```

    Main readable output:

    ```text
    reports/premarket/<YYYY-MM-DD>.md
    ```

    Machine-readable action list:

    ```text
    data/latest/premarket_actions.csv
    ```

- [ ] **Step 3: Run full test suite**

Run:

```bash
.venv/bin/python -m pytest -v
```

Expected: all tests pass.

- [ ] **Step 4: Verify CLI help**

Run:

```bash
.venv/bin/python -m open_trader --help
.venv/bin/python -m open_trader run-premarket --help
```

Expected:

- Top-level help includes `run-premarket`.
- `run-premarket` help includes `--date`, `--portfolio`, `--tradingagents-path`, `--symbols`, and `--dry-run`.

- [ ] **Step 5: Run a no-network dry-run with fake-free CLI help only**

Do not run the real TradingAgents/OpenAI workflow in automated verification unless API keys and model settings are known to be configured. The unit suite covers behavior with fakes.

If the user asks for a real smoke after implementation, run:

```bash
.venv/bin/python -m open_trader run-premarket \
  --date 2026-06-16 \
  --portfolio data/latest/portfolio.csv \
  --symbols VIXY \
  --dry-run
```

Expected with valid TradingAgents and OpenAI configuration:

- command exits `0`
- writes `data/runs/2026-06-16/trading_advice.csv`
- writes `data/runs/2026-06-16/change_classifications.csv`
- writes `data/runs/2026-06-16/premarket_actions.csv`
- writes `reports/premarket/2026-06-16.md`
- does not update `data/latest/trading_advice.csv` because of `--dry-run`

- [ ] **Step 6: Commit docs and ignore rules**

```bash
git add .gitignore docs/monthly_portfolio_import.md
git commit -m "docs: record premarket advice command"
```

## Final Review Checklist

Before claiming completion:

- [ ] Run `.venv/bin/python -m pytest -v`.
- [ ] Run `.venv/bin/python -m open_trader --help`.
- [ ] Run `.venv/bin/python -m open_trader run-premarket --help`.
- [ ] Check `git status --short`.
- [ ] Confirm `data/` and `reports/` outputs are ignored.
- [ ] Confirm tests do not require real TradingAgents, network, or model API calls.

## Execution Notes

- Use subagent-driven development for implementation unless the user explicitly chooses inline execution.
- Use a fresh worker per task.
- After each task, run a spec compliance review and then a code quality review.
- Keep real external-service smoke separate from unit-test verification.
