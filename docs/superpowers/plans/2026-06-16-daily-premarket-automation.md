# Daily Premarket Automation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a macOS-scheduled daily premarket runner that starts at 18:30 Asia/Shanghai, produces advice/plans/Futu checks before 21:10, and falls back per symbol to the latest prior successful advice when today's run cannot produce fresh advice.

**Architecture:** Keep `launchd` as a thin trigger and put orchestration in Python. Extend the existing advice and trading-plan CSV contracts to carry fallback provenance, then add a repo-owned daily runner that performs preflight, locking, premarket analysis, fallback completion, plan generation, Futu quote evaluation, status writing, summary writing, and local notification. The daily runner enforces the 21:10 hard deadline by passing the remaining seconds to each TradingAgents subprocess timeout.

**Tech Stack:** Python 3.12, stdlib `csv`/`json`/`datetime`/`zoneinfo`/`subprocess`/`fcntl`, existing Open Trader CLI/functions, `futu-api`, pytest, macOS `launchd` and `osascript`.

---

## File Structure

- Modify `src/open_trader/advice/models.py`: expand advice status and advice CSV fields with fallback provenance.
- Modify `src/open_trader/advice/store.py`: read/write the expanded advice schema while tolerating older CSVs without new fallback columns.
- Modify `src/open_trader/advice/premarket.py`: add optional deadline and fallback behavior to the existing per-symbol pipeline.
- Modify `src/open_trader/trading_plan.py`: accept fallback advice, preserve fallback provenance in plan rows, and keep old plan CSV reads compatible.
- Create `src/open_trader/daily_premarket.py`: daily config parsing, run lock, status models, report writer, notification adapter, Futu plan checker, and daily orchestration.
- Modify `src/open_trader/cli.py`: add `run-daily-premarket` command and wire it to the daily runner.
- Add `config/daily_premarket.env.example`: local env template without secrets.
- Add `ops/launchd/com.open-trader.premarket.plist.template`: launchd template.
- Add `scripts/install_daily_premarket_launchd.sh`: render/load launchd plist from env.
- Add `scripts/uninstall_daily_premarket_launchd.sh`: unload/remove launchd plist.
- Modify `docs/monthly_portfolio_import.md`: document the daily automation runbook and Mac mini migration.
- Add/modify tests:
  - `tests/test_advice_models.py`
  - `tests/test_advice_store.py`
  - `tests/test_premarket_pipeline.py`
  - `tests/test_trading_plan.py`
  - `tests/test_daily_premarket.py`
  - `tests/test_premarket_cli.py`

## Task 1: Advice Schema Fallback Provenance

**Files:**
- Modify: `src/open_trader/advice/models.py`
- Modify: `src/open_trader/advice/store.py`
- Test: `tests/test_advice_models.py`
- Test: `tests/test_advice_store.py`

- [ ] **Step 1: Add failing model tests for fallback advice rows**

Append to `tests/test_advice_models.py`:

```python
from open_trader.advice.models import TRADING_ADVICE_FIELDNAMES, TradingAdvice


def test_trading_advice_row_includes_fallback_metadata() -> None:
    advice = TradingAdvice(
        run_date="2026-06-17",
        symbol="MSFT",
        market="US",
        asset_class="stock",
        portfolio_weight_hkd="1.13%",
        risk_flag="normal",
        source="tradingagents",
        advice_action="Overweight",
        advice_summary="评级：Overweight",
        raw_decision="{}",
        status="fallback",
        error="",
        source_status="fallback",
        fallback_reason="daily deadline exceeded",
        fallback_from_date="2026-06-16",
    )

    row = advice.to_row()

    assert "source_status" in TRADING_ADVICE_FIELDNAMES
    assert "fallback_reason" in TRADING_ADVICE_FIELDNAMES
    assert "fallback_from_date" in TRADING_ADVICE_FIELDNAMES
    assert row["status"] == "fallback"
    assert row["source_status"] == "fallback"
    assert row["fallback_reason"] == "daily deadline exceeded"
    assert row["fallback_from_date"] == "2026-06-16"
```

- [ ] **Step 2: Add failing store compatibility test**

Append to `tests/test_advice_store.py`:

```python
import csv

from open_trader.advice.store import load_latest_advice_by_symbol, write_trading_advice
from open_trader.advice.models import TradingAdvice


def test_load_latest_advice_accepts_legacy_rows_without_fallback_columns(
    tmp_path: Path,
) -> None:
    latest = tmp_path / "data/latest/trading_advice.csv"
    latest.parent.mkdir(parents=True)
    with latest.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
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
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "run_date": "2026-06-16",
                "symbol": "MSFT",
                "market": "US",
                "asset_class": "stock",
                "portfolio_weight_hkd": "1.13%",
                "risk_flag": "normal",
                "source": "tradingagents",
                "advice_action": "Overweight",
                "advice_summary": "评级：Overweight",
                "raw_decision": "{}",
                "status": "ok",
                "error": "",
            }
        )

    rows = load_latest_advice_by_symbol(tmp_path / "data")

    assert rows["MSFT"]["source_status"] == "ok"
    assert rows["MSFT"]["fallback_reason"] == ""
    assert rows["MSFT"]["fallback_from_date"] == ""


def test_write_trading_advice_writes_fallback_columns(tmp_path: Path) -> None:
    run_path, _ = write_trading_advice(
        run_date="2026-06-17",
        data_dir=tmp_path / "data",
        update_latest=False,
        records=[
            TradingAdvice(
                run_date="2026-06-17",
                symbol="MSFT",
                market="US",
                asset_class="stock",
                portfolio_weight_hkd="1.13%",
                risk_flag="normal",
                source="tradingagents",
                advice_action="Overweight",
                advice_summary="评级：Overweight",
                raw_decision="{}",
                status="fallback",
                error="",
                source_status="fallback",
                fallback_reason="daily deadline exceeded",
                fallback_from_date="2026-06-16",
            )
        ],
    )

    rows = list(csv.DictReader(run_path.open(encoding="utf-8")))

    assert rows[0]["source_status"] == "fallback"
    assert rows[0]["fallback_reason"] == "daily deadline exceeded"
    assert rows[0]["fallback_from_date"] == "2026-06-16"
```

- [ ] **Step 3: Run tests to verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_advice_models.py tests/test_advice_store.py -v
```

Expected: FAIL because `TradingAdvice` does not accept fallback fields and `load_latest_advice_by_symbol` does not populate them.

- [ ] **Step 4: Implement fallback fields in advice models**

In `src/open_trader/advice/models.py`, replace the status alias and advice fieldnames with:

```python
AdviceStatus = Literal["ok", "fallback", "error"]


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
    "source_status",
    "fallback_reason",
    "fallback_from_date",
]
```

Add defaulted fields at the end of `TradingAdvice`:

```python
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
    source_status: str = "ok"
    fallback_reason: str = ""
    fallback_from_date: str = ""
```

In `tests/test_advice_models.py`, update `EXPECTED_TRADING_ADVICE_FIELDNAMES` to
match the expanded schema:

```python
EXPECTED_TRADING_ADVICE_FIELDNAMES = [
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
```

- [ ] **Step 5: Normalize legacy latest advice rows**

In `src/open_trader/advice/store.py`, add:

```python
def _normalize_advice_row(row: dict[str, str]) -> dict[str, str]:
    normalized = {field: row.get(field, "") for field in TRADING_ADVICE_FIELDNAMES}
    if not normalized["source_status"]:
        normalized["source_status"] = normalized["status"] or "ok"
    return normalized
```

Replace `load_latest_advice_by_symbol` with:

```python
def load_latest_advice_by_symbol(data_dir: Path) -> dict[str, dict[str, str]]:
    latest_path = data_dir / "latest" / "trading_advice.csv"
    if not latest_path.exists():
        return {}

    with latest_path.open(encoding="utf-8-sig", newline="") as handle:
        return {
            normalized["symbol"]: normalized
            for row in csv.DictReader(handle)
            if row.get("symbol")
            for normalized in [_normalize_advice_row(row)]
        }
```

- [ ] **Step 6: Run focused tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_advice_models.py tests/test_advice_store.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/open_trader/advice/models.py src/open_trader/advice/store.py tests/test_advice_models.py tests/test_advice_store.py
git commit -m "feat: track fallback advice provenance"
```

## Task 2: Premarket Deadline and Per-Symbol Fallback

**Files:**
- Modify: `src/open_trader/advice/premarket.py`
- Test: `tests/test_premarket_pipeline.py`

- [ ] **Step 1: Add failing test for fallback on symbol failure**

Append to `tests/test_premarket_pipeline.py`:

```python
def test_run_premarket_falls_back_to_latest_ok_advice_on_symbol_failure(
    tmp_path: Path,
) -> None:
    portfolio_path = tmp_path / "portfolio.csv"
    write_portfolio(portfolio_path)
    data_dir = tmp_path / "data"
    reports_dir = tmp_path / "reports"
    latest = data_dir / "latest/trading_advice.csv"
    latest.parent.mkdir(parents=True)
    with latest.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=premarket.TRADING_ADVICE_FIELDNAMES)
        writer.writeheader()
        writer.writerow(
            {
                "run_date": "2026-06-16",
                "symbol": "QQQ",
                "market": "US",
                "asset_class": "stock",
                "portfolio_weight_hkd": "1.40%",
                "risk_flag": "normal",
                "source": "tradingagents",
                "advice_action": "hold",
                "advice_summary": "QQQ prior summary",
                "raw_decision": "{}",
                "status": "ok",
                "error": "",
                "source_status": "ok",
                "fallback_reason": "",
                "fallback_from_date": "",
            }
        )

    result = run_premarket(
        run_date="2026-06-17",
        portfolio_path=portfolio_path,
        data_dir=data_dir,
        reports_dir=reports_dir,
        advice_runner=FakeAdviceRunner(fail_symbols={"QQQ"}),
        classifier=FakeClassifier(),
        symbols=None,
        update_latest=True,
        use_fallback=True,
    )

    rows = list(csv.DictReader(result.advice_path.open(encoding="utf-8")))
    qqq = next(row for row in rows if row["symbol"] == "QQQ")
    assert qqq["run_date"] == "2026-06-17"
    assert qqq["status"] == "fallback"
    assert qqq["source_status"] == "fallback"
    assert qqq["fallback_reason"] == "QQQ analysis failed"
    assert qqq["fallback_from_date"] == "2026-06-16"
    assert qqq["advice_summary"] == "QQQ prior summary"
    assert result.advice_count == 2
```

- [ ] **Step 2: Add failing test for missing fallback producing error row**

Append to `tests/test_premarket_pipeline.py`:

```python
def test_run_premarket_records_error_when_failure_has_no_fallback(
    tmp_path: Path,
) -> None:
    portfolio_path = tmp_path / "portfolio.csv"
    write_portfolio(portfolio_path)

    result = run_premarket(
        run_date="2026-06-17",
        portfolio_path=portfolio_path,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        advice_runner=FakeAdviceRunner(fail_symbols={"QQQ"}),
        classifier=FakeClassifier(),
        symbols=None,
        update_latest=True,
        use_fallback=True,
    )

    rows = list(csv.DictReader(result.advice_path.open(encoding="utf-8")))
    qqq = next(row for row in rows if row["symbol"] == "QQQ")
    assert qqq["status"] == "error"
    assert qqq["error"] == "QQQ analysis failed"
    assert qqq["source_status"] == "error"
    assert qqq["fallback_from_date"] == ""
```

- [ ] **Step 3: Add failing test for deadline fallback before a symbol starts**

Append to `tests/test_premarket_pipeline.py`:

```python
def test_run_premarket_uses_fallback_when_deadline_has_passed_before_symbol(
    tmp_path: Path,
) -> None:
    portfolio_path = tmp_path / "portfolio.csv"
    write_portfolio(portfolio_path)
    data_dir = tmp_path / "data"
    latest = data_dir / "latest/trading_advice.csv"
    latest.parent.mkdir(parents=True)
    with latest.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=premarket.TRADING_ADVICE_FIELDNAMES)
        writer.writeheader()
        writer.writerow(
            {
                "run_date": "2026-06-16",
                "symbol": "VIXY",
                "market": "US",
                "asset_class": "etf",
                "portfolio_weight_hkd": "3.05%",
                "risk_flag": "normal",
                "source": "tradingagents",
                "advice_action": "reduce",
                "advice_summary": "VIXY prior summary",
                "raw_decision": "{}",
                "status": "ok",
                "error": "",
                "source_status": "ok",
                "fallback_reason": "",
                "fallback_from_date": "",
            }
        )

    result = run_premarket(
        run_date="2026-06-17",
        portfolio_path=portfolio_path,
        data_dir=data_dir,
        reports_dir=tmp_path / "reports",
        advice_runner=FakeAdviceRunner(),
        classifier=FakeClassifier(),
        symbols={"VIXY"},
        update_latest=True,
        use_fallback=True,
        deadline_reached=lambda: True,
    )

    rows = list(csv.DictReader(result.advice_path.open(encoding="utf-8")))
    assert rows[0]["symbol"] == "VIXY"
    assert rows[0]["status"] == "fallback"
    assert rows[0]["fallback_reason"] == "daily deadline exceeded"
    assert rows[0]["fallback_from_date"] == "2026-06-16"
```

- [ ] **Step 4: Run tests to verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_premarket_pipeline.py -v
```

Expected: FAIL because `run_premarket` has no `use_fallback` or `deadline_reached` parameters.

- [ ] **Step 5: Add fallback helpers to `premarket.py`**

In `src/open_trader/advice/premarket.py`, import fieldnames for tests and add helper functions:

```python
from .models import TRADING_ADVICE_FIELDNAMES
```

Add below `_SymbolResult`:

```python
DeadlineReached = Callable[[], bool]


def _fallback_or_error_advice(
    *,
    row: PortfolioInputRow,
    run_date: str,
    previous_by_symbol: dict[str, dict[str, str]],
    reason: str,
) -> TradingAdvice:
    previous = previous_by_symbol.get(row.symbol)
    if previous and previous.get("status") == "ok":
        return TradingAdvice(
            run_date=run_date,
            symbol=row.symbol,
            market=row.market,
            asset_class=row.asset_class,
            portfolio_weight_hkd=row.portfolio_weight_hkd,
            risk_flag=row.risk_flag,
            source=previous.get("source", "tradingagents"),
            advice_action=previous.get("advice_action", ""),
            advice_summary=previous.get("advice_summary", ""),
            raw_decision=previous.get("raw_decision", ""),
            status="fallback",
            error="",
            source_status="fallback",
            fallback_reason=reason,
            fallback_from_date=previous.get("run_date", ""),
        )
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
        error=reason,
        source_status="error",
        fallback_reason="",
        fallback_from_date="",
    )
```

- [ ] **Step 6: Extend `run_premarket` signature and pass fallback controls**

Change the `run_premarket` signature:

```python
def run_premarket(
    *,
    run_date: str,
    portfolio_path: Path,
    data_dir: Path,
    reports_dir: Path,
    advice_runner: AdviceRunner | None,
    classifier: Classifier,
    symbols: set[str] | None,
    update_latest: bool,
    max_workers: int = 1,
    advice_runner_factory: AdviceRunnerFactory | None = None,
    excluded_symbols: set[str] | None = None,
    use_fallback: bool = False,
    deadline_reached: DeadlineReached | None = None,
) -> PremarketResult:
```

When calling `_run_symbols`, pass:

```python
use_fallback=use_fallback,
deadline_reached=deadline_reached,
```

- [ ] **Step 7: Update `_run_symbols` and `_run_symbol` to honor deadline/fallback**

Extend `_run_symbols` parameters with `use_fallback` and `deadline_reached`.

In the sequential branch of `_run_symbols`, replace the list comprehension with
an explicit loop:

```python
if max_workers == 1:
    results: list[_SymbolResult] = []
    for index, row in enumerate(rows):
        results.append(
            _run_symbol(
                index=index,
                row=row,
                run_date=run_date,
                advice_runner=advice_runner,
                advice_runner_factory=advice_runner_factory,
                classifier=classifier,
                previous_by_symbol=previous_by_symbol,
                use_fallback=use_fallback,
                deadline_reached=deadline_reached,
            )
        )
    return results
```

In the parallel branch, keep submission simple for this first version and handle failures inside `_run_symbol`; daily runner uses the `deadline_reached` callback before each worker starts. Add this guard at the top of `_run_symbol`:

```python
if deadline_reached is not None and deadline_reached():
    advice = _fallback_or_error_advice(
        row=row,
        run_date=run_date,
        previous_by_symbol=previous_by_symbol,
        reason="daily deadline exceeded",
    )
    return _SymbolResult(
        index=index,
        row=row,
        advice=advice,
        classification=_classification_for_non_ok(row=row, advice=advice, run_date=run_date),
    )
```

Wrap the advice runner call:

```python
try:
    advice = runner.analyze(row, run_date)
except Exception as exc:
    if use_fallback:
        advice = _fallback_or_error_advice(
            row=row,
            run_date=run_date,
            previous_by_symbol=previous_by_symbol,
            reason=str(exc),
        )
    else:
        advice = TradingAdvice(
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
            source_status="error",
            fallback_reason="",
            fallback_from_date="",
        )
```

Add helper:

```python
def _classification_for_non_ok(
    *,
    row: PortfolioInputRow,
    advice: TradingAdvice,
    run_date: str,
) -> ChangeClassification:
    return ChangeClassification(
        run_date=run_date,
        symbol=row.symbol,
        include_in_report=False,
        change_type="no_material_change",
        severity="low",
        suggested_action=advice.advice_action,
        summary="",
        rationale="",
        watch_trigger="",
        status="ok" if advice.status == "fallback" else "error",
        error=advice.error,
    )
```

- [ ] **Step 8: Ensure fallback rows are not action-report items**

In the existing action loop, keep the current condition:

```python
if classification.status == "ok" and classification.include_in_report:
```

Fallback advice should normally have `include_in_report=False` from `_classification_for_non_ok`, so it remains out of `premarket_actions.csv`.

- [ ] **Step 9: Run focused tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_premarket_pipeline.py -v
```

Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add src/open_trader/advice/premarket.py tests/test_premarket_pipeline.py
git commit -m "feat: fallback unfinished premarket advice"
```

## Task 3: Trading Plan Source Status

**Files:**
- Modify: `src/open_trader/trading_plan.py`
- Test: `tests/test_trading_plan.py`

- [ ] **Step 1: Extend test advice fieldnames with fallback columns**

In `tests/test_trading_plan.py`, extend `ADVICE_FIELDNAMES`:

```python
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
```

- [ ] **Step 2: Add failing test for fallback advice becoming active fallback plan**

Append to `tests/test_trading_plan.py`:

```python
def test_build_trading_plan_accepts_fallback_advice_and_preserves_source_status(
    tmp_path: Path,
) -> None:
    advice_path = tmp_path / "advice.csv"
    write_advice(
        advice_path,
        [
            {
                "run_date": "2026-06-17",
                "symbol": "MSFT",
                "market": "US",
                "asset_class": "stock",
                "portfolio_weight_hkd": "1.13%",
                "risk_flag": "normal",
                "source": "tradingagents",
                "advice_action": "Overweight",
                "advice_summary": msft_advice_summary(),
                "raw_decision": "{}",
                "status": "fallback",
                "error": "",
                "source_status": "fallback",
                "fallback_reason": "daily deadline exceeded",
                "fallback_from_date": "2026-06-16",
            }
        ],
    )

    result = build_trading_plan(advice_path, tmp_path / "data")
    rows = list(csv.DictReader(result.plan_path.open(encoding="utf-8")))

    assert rows[0]["symbol"] == "MSFT"
    assert rows[0]["status"] == "active"
    assert rows[0]["source_status"] == "fallback"
    assert rows[0]["fallback_reason"] == "daily deadline exceeded"
    assert rows[0]["fallback_from_date"] == "2026-06-16"
```

- [ ] **Step 3: Add failing compatibility test for legacy plan rows**

Append to `tests/test_trading_plan.py`:

```python
def test_load_trading_plan_rows_accepts_legacy_rows_without_source_status(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy_plan.csv"
    legacy_fieldnames = [field for field in TRADING_PLAN_FIELDNAMES if field not in {"source_status", "fallback_reason", "fallback_from_date"}]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=legacy_fieldnames)
        writer.writeheader()
        writer.writerow(
            {
                "run_date": "2026-06-16",
                "symbol": "MSFT",
                "market": "US",
                "rating": "Overweight",
                "entry_zone_low": "380",
                "entry_zone_high": "400",
                "add_price": "350",
                "stop_loss": "340",
                "target_1": "450",
                "target_2": "500",
                "max_weight": "12%",
                "catalyst": "10月底财报",
                "time_horizon": "3-6个月",
                "plan_text": "plan",
                "status": "active",
                "error": "",
            }
        )

    rows = load_trading_plan_rows(path)

    assert rows[0].source_status == "ok"
    assert rows[0].fallback_reason == ""
    assert rows[0].fallback_from_date == ""
```

- [ ] **Step 4: Run tests to verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_trading_plan.py -v
```

Expected: FAIL because plan fieldnames and dataclass do not have fallback provenance.

- [ ] **Step 5: Extend trading plan fieldnames and dataclass**

In `src/open_trader/trading_plan.py`, add these fields after `market` in `TRADING_PLAN_FIELDNAMES`:

```python
    "source_status",
    "fallback_reason",
    "fallback_from_date",
```

Add matching fields to `TradingPlanRow` after `market`:

```python
    source_status: str
    fallback_reason: str
    fallback_from_date: str
```

- [ ] **Step 6: Accept fallback advice in `_plan_row_from_advice`**

Replace the status gate:

```python
    advice_status = row.get("status", "").strip()
    if advice_status not in {"ok", "fallback"}:
        return _base_plan_row(
            run_date=run_date,
            symbol=symbol,
            market=market,
            source_status=advice_status or "error",
            fallback_reason=row.get("fallback_reason", "").strip(),
            fallback_from_date=row.get("fallback_from_date", "").strip(),
            status="error",
            error=row.get("error", "").strip(),
        )
```

When calling `_base_plan_row` for manual review and active plans, pass:

```python
source_status=row.get("source_status", "").strip() or advice_status,
fallback_reason=row.get("fallback_reason", "").strip(),
fallback_from_date=row.get("fallback_from_date", "").strip(),
```

- [ ] **Step 7: Extend `_base_plan_row` and legacy loader**

Update `_base_plan_row` parameters:

```python
    source_status: str = "ok",
    fallback_reason: str = "",
    fallback_from_date: str = "",
```

Include in returned dict:

```python
        "source_status": source_status,
        "fallback_reason": fallback_reason,
        "fallback_from_date": fallback_from_date,
```

In `load_trading_plan_rows`, allow the three new fields to be optional:

```python
        optional = {"source_status", "fallback_reason", "fallback_from_date"}
        missing = sorted(set(TRADING_PLAN_FIELDNAMES) - optional - set(fieldnames))
```

In `_trading_plan_from_row`, pass:

```python
        source_status=row.get("source_status", "").strip() or "ok",
        fallback_reason=row.get("fallback_reason", "").strip(),
        fallback_from_date=row.get("fallback_from_date", "").strip(),
```

- [ ] **Step 8: Update existing expected `TradingPlanRow` construction in tests**

Every `TradingPlanRow(...)` literal in these files must include source status
arguments:

```text
tests/test_trading_plan.py
tests/test_trading_plan_cli.py
tests/test_trade_actions.py
```

Add these arguments after `market="US",` in each `TradingPlanRow(...)` literal:

```python
source_status="ok",
fallback_reason="",
fallback_from_date="",
```

- [ ] **Step 9: Run focused tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_trading_plan.py tests/test_trading_plan_cli.py tests/test_trade_actions.py -v
```

Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add src/open_trader/trading_plan.py tests/test_trading_plan.py tests/test_trading_plan_cli.py tests/test_trade_actions.py
git commit -m "feat: preserve plan source status"
```

## Task 4: Daily Runner Core

**Files:**
- Create: `src/open_trader/daily_premarket.py`
- Test: `tests/test_daily_premarket.py`

- [ ] **Step 1: Create failing tests for config parsing and required fields**

Create `tests/test_daily_premarket.py`:

```python
from __future__ import annotations

import csv
import json
from decimal import Decimal
from pathlib import Path

import pytest

from open_trader.daily_premarket import (
    DailyPremarketConfig,
    DailyPremarketRunner,
    NullNotifier,
    RunLock,
    load_env_config,
)
from open_trader.futu_quote import FutuQuoteError
from open_trader.futu_watch import QuoteSnapshot
from open_trader.trading_plan import TradingPlanBuildResult


def test_load_env_config_parses_required_values(tmp_path: Path) -> None:
    env = tmp_path / "daily.env"
    env.write_text(
        "\n".join(
            [
                f"OPEN_TRADER_REPO={tmp_path}",
                f"OPEN_TRADER_PYTHON={tmp_path / '.venv/bin/python'}",
                "OPEN_TRADER_TIMEZONE=Asia/Shanghai",
                "OPEN_TRADER_DEADLINE=21:10",
                "OPEN_TRADER_FUTU_HOST=127.0.0.1",
                "OPEN_TRADER_FUTU_PORT=11111",
                "DEEPSEEK_API_KEY=secret",
                "OPENAI_API_KEY=secret",
            ]
        ),
        encoding="utf-8",
    )

    config = load_env_config(env)

    assert config.repo == tmp_path
    assert config.python == tmp_path / ".venv/bin/python"
    assert config.timezone == "Asia/Shanghai"
    assert config.deadline == "21:10"
    assert config.futu_host == "127.0.0.1"
    assert config.futu_port == 11111


def test_load_env_config_rejects_missing_required_values(tmp_path: Path) -> None:
    env = tmp_path / "daily.env"
    env.write_text("OPEN_TRADER_REPO=/tmp/open_trader\n", encoding="utf-8")

    with pytest.raises(ValueError, match="missing config value"):
        load_env_config(env)
```

- [ ] **Step 2: Add failing tests for run lock**

Append:

```python
def test_run_lock_rejects_second_owner(tmp_path: Path) -> None:
    lock_path = tmp_path / "run.lock"
    first = RunLock(lock_path)
    second = RunLock(lock_path)

    with first:
        with pytest.raises(RuntimeError, match="daily premarket run already active"):
            with second:
                pass
```

- [ ] **Step 3: Add failing test for successful runner orchestration**

Append:

```python
class FakePremarket:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs: object):
        self.calls.append(kwargs)
        data_dir = kwargs["data_dir"]
        run_date = kwargs["run_date"]
        assert isinstance(data_dir, Path)
        assert isinstance(run_date, str)
        advice_path = data_dir / "runs" / run_date / "trading_advice.csv"
        advice_path.parent.mkdir(parents=True, exist_ok=True)
        with advice_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
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
                ],
            )
            writer.writeheader()
            writer.writerow(
                {
                    "run_date": run_date,
                    "symbol": "MSFT",
                    "market": "US",
                    "asset_class": "stock",
                    "portfolio_weight_hkd": "1.13%",
                    "risk_flag": "normal",
                    "source": "fake",
                    "advice_action": "Overweight",
                    "advice_summary": "评级：Overweight",
                    "raw_decision": "{}",
                    "status": "ok",
                    "error": "",
                    "source_status": "ok",
                    "fallback_reason": "",
                    "fallback_from_date": "",
                }
            )
        return type(
            "PremarketResult",
            (),
            {
                "eligible_count": 1,
                "advice_count": 1,
                "action_count": 0,
                "advice_path": advice_path,
                "classifications_path": data_dir / "runs" / run_date / "change_classifications.csv",
                "actions_path": data_dir / "runs" / run_date / "premarket_actions.csv",
                "report_path": Path("reports/premarket") / f"{run_date}.md",
            },
        )()


class FakePlanBuilder:
    def __call__(self, advice_path: Path, data_dir: Path, run_date: str, update_latest: bool):
        plan_path = data_dir / "runs" / run_date / "trading_plan.csv"
        latest_path = data_dir / "latest" / "trading_plan.csv"
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        latest_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text(
            "run_date,symbol,market,source_status,fallback_reason,fallback_from_date,rating,entry_zone_low,entry_zone_high,add_price,stop_loss,target_1,target_2,max_weight,catalyst,time_horizon,plan_text,status,error\n"
            f"{run_date},MSFT,US,ok,,,,Overweight,380,400,350,340,450,500,12%,earnings,3-6 months,plan,active,\n",
            encoding="utf-8",
        )
        latest_path.write_text(plan_path.read_text(encoding="utf-8"), encoding="utf-8")
        return TradingPlanBuildResult(
            run_date=run_date,
            plan_count=1,
            plan_path=plan_path,
            latest_path=latest_path,
        )


class FakeQuoteClient:
    def __init__(self, *, host: str, port: int) -> None:
        self.host = host
        self.port = port

    def get_snapshots(self, symbols: list[str]) -> dict[str, QuoteSnapshot]:
        return {"US.MSFT": QuoteSnapshot(futu_symbol="US.MSFT", last_price=Decimal("399"))}

    def close(self) -> None:
        pass


def test_daily_runner_writes_success_status_and_report(tmp_path: Path) -> None:
    config = DailyPremarketConfig(
        repo=tmp_path,
        python=tmp_path / ".venv/bin/python",
        timezone="Asia/Shanghai",
        deadline="21:10",
        futu_host="127.0.0.1",
        futu_port=11111,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        logs_dir=tmp_path / "logs",
        portfolio=tmp_path / "data/latest/portfolio.csv",
        dry_run=False,
    )
    config.portfolio.parent.mkdir(parents=True, exist_ok=True)
    config.portfolio.write_text("symbol\nMSFT\n", encoding="utf-8")

    runner = DailyPremarketRunner(
        config=config,
        premarket_runner=FakePremarket(),
        plan_builder=FakePlanBuilder(),
        quote_client_factory=FakeQuoteClient,
        notifier=NullNotifier(),
    )

    result = runner.run("2026-06-17")

    assert result.status == "success"
    status_path = tmp_path / "data/runs/2026-06-17/daily_run_status.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["status"] == "success"
    assert status["premarket"]["ok"] == 1
    assert status["trading_plan"]["active"] == 1
    assert status["futu_plan_check"]["checked"] == 1
    assert (tmp_path / "reports/daily_runs/2026-06-17.md").exists()
```

- [ ] **Step 4: Add failing test for Futu unavailable partial run**

Append:

```python
class UnavailableQuoteClient:
    def __init__(self, *, host: str, port: int) -> None:
        raise FutuQuoteError("Futu OpenD is not reachable")


def test_daily_runner_marks_partial_when_futu_is_unavailable(tmp_path: Path) -> None:
    config = DailyPremarketConfig(
        repo=tmp_path,
        python=tmp_path / ".venv/bin/python",
        timezone="Asia/Shanghai",
        deadline="21:10",
        futu_host="127.0.0.1",
        futu_port=11111,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        logs_dir=tmp_path / "logs",
        portfolio=tmp_path / "data/latest/portfolio.csv",
        dry_run=False,
    )
    config.portfolio.parent.mkdir(parents=True, exist_ok=True)
    config.portfolio.write_text("symbol\nMSFT\n", encoding="utf-8")

    runner = DailyPremarketRunner(
        config=config,
        premarket_runner=FakePremarket(),
        plan_builder=FakePlanBuilder(),
        quote_client_factory=UnavailableQuoteClient,
        notifier=NullNotifier(),
    )

    result = runner.run("2026-06-17")

    assert result.status == "partial"
    status = json.loads(
        (tmp_path / "data/runs/2026-06-17/daily_run_status.json").read_text(
            encoding="utf-8"
        )
    )
    assert status["futu_plan_check"]["error"] == "Futu OpenD is not reachable"
```

- [ ] **Step 5: Add failing test for preflight failure status**

Append:

```python
def test_daily_runner_writes_failed_status_when_portfolio_is_missing(
    tmp_path: Path,
) -> None:
    config = DailyPremarketConfig(
        repo=tmp_path,
        python=tmp_path / ".venv/bin/python",
        timezone="Asia/Shanghai",
        deadline="21:10",
        futu_host="127.0.0.1",
        futu_port=11111,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        logs_dir=tmp_path / "logs",
        portfolio=tmp_path / "data/latest/portfolio.csv",
        dry_run=False,
    )

    runner = DailyPremarketRunner(
        config=config,
        premarket_runner=FakePremarket(),
        plan_builder=FakePlanBuilder(),
        quote_client_factory=FakeQuoteClient,
        notifier=NullNotifier(),
    )

    result = runner.run("2026-06-17")

    assert result.status == "failed"
    status = json.loads(result.status_path.read_text(encoding="utf-8"))
    assert status["status"] == "failed"
    assert "portfolio not found" in status["error"]
```

- [ ] **Step 6: Run tests to verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_daily_premarket.py -v
```

Expected: FAIL because `open_trader.daily_premarket` does not exist.

- [ ] **Step 7: Create daily runner dataclasses and env parser**

Create `src/open_trader/daily_premarket.py` with imports and dataclasses:

```python
from __future__ import annotations

import csv
import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Callable, Protocol
from zoneinfo import ZoneInfo

from .advice.premarket import run_premarket
from .advice.tradingagents_adapter import TradingAgentsSubprocessRunner
from .advice.change_classifier import ChangeClassifier, OpenAIClassifierClient
from .futu_quote import FutuQuoteClient, FutuQuoteError
from .trading_plan import (
    TradingPlanBuildResult,
    build_trading_plan,
    evaluate_plan_quote,
    load_trading_plan_rows,
)
```

Add:

```python
@dataclass(frozen=True)
class DailyPremarketConfig:
    repo: Path
    python: Path
    timezone: str
    deadline: str
    futu_host: str
    futu_port: int
    data_dir: Path
    reports_dir: Path
    logs_dir: Path
    portfolio: Path
    dry_run: bool = False
    max_workers: int = 4
    ta_timeout_seconds: float = 600.0
    ta_max_retries: int = 2
    tradingagents_path: Path = Path("/Users/ray/projects/TradingAgents")
    classifier_model: str = "gpt-5.4-mini"
```

Add env parser:

```python
def load_env_config(path: Path, *, dry_run: bool = False) -> DailyPremarketConfig:
    values = _read_env_file(path)
    required = [
        "OPEN_TRADER_REPO",
        "OPEN_TRADER_PYTHON",
        "OPEN_TRADER_TIMEZONE",
        "OPEN_TRADER_DEADLINE",
        "OPEN_TRADER_FUTU_HOST",
        "OPEN_TRADER_FUTU_PORT",
        "DEEPSEEK_API_KEY",
        "OPENAI_API_KEY",
    ]
    missing = [key for key in required if not values.get(key)]
    if missing:
        raise ValueError(f"missing config value(s): {', '.join(missing)}")
    repo = Path(values["OPEN_TRADER_REPO"])
    return DailyPremarketConfig(
        repo=repo,
        python=Path(values["OPEN_TRADER_PYTHON"]),
        timezone=values["OPEN_TRADER_TIMEZONE"],
        deadline=values["OPEN_TRADER_DEADLINE"],
        futu_host=values["OPEN_TRADER_FUTU_HOST"],
        futu_port=int(values["OPEN_TRADER_FUTU_PORT"]),
        data_dir=repo / values.get("OPEN_TRADER_DATA_DIR", "data"),
        reports_dir=repo / values.get("OPEN_TRADER_REPORTS_DIR", "reports"),
        logs_dir=repo / values.get("OPEN_TRADER_LOGS_DIR", "logs"),
        portfolio=repo / values.get("OPEN_TRADER_PORTFOLIO", "data/latest/portfolio.csv"),
        dry_run=dry_run,
        max_workers=int(values.get("OPEN_TRADER_MAX_WORKERS", "4")),
        ta_timeout_seconds=float(values.get("OPEN_TRADER_TA_TIMEOUT_SECONDS", "600")),
        ta_max_retries=int(values.get("OPEN_TRADER_TA_MAX_RETRIES", "2")),
        tradingagents_path=Path(values.get("OPEN_TRADER_TRADINGAGENTS_PATH", "/Users/ray/projects/TradingAgents")),
        classifier_model=values.get("OPEN_TRADER_CLASSIFIER_MODEL", "gpt-5.4-mini"),
    )
```

Add:

```python
def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values
```

- [ ] **Step 8: Implement lock and notifier**

Add:

```python
class RunLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle = None

    def __enter__(self) -> RunLock:
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("w", encoding="utf-8")
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("daily premarket run already active") from exc
        self._handle.write(str(os.getpid()))
        self._handle.flush()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        import fcntl

        if self._handle is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
```

Add notifier classes:

```python
class Notifier(Protocol):
    def notify(self, title: str, message: str) -> None:
        pass


class NullNotifier:
    def notify(self, title: str, message: str) -> None:
        pass


class MacOSNotifier:
    def notify(self, title: str, message: str) -> None:
        script = f'display notification "{_escape_osascript(message)}" with title "{_escape_osascript(title)}"'
        subprocess.run(["osascript", "-e", script], check=False)


def _escape_osascript(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')
```

- [ ] **Step 9: Implement result/status helpers**

Add:

```python
@dataclass(frozen=True)
class DailyRunResult:
    run_date: str
    status: str
    status_path: Path
    report_path: Path
    log_path: Path
```

Add small helpers:

```python
def _deadline_reached(config: DailyPremarketConfig) -> Callable[[], bool]:
    zone = ZoneInfo(config.timezone)
    hour, minute = [int(part) for part in config.deadline.split(":", 1)]
    def reached() -> bool:
        now = datetime.now(zone)
        return now.time() >= time(hour, minute)
    return reached


def _seconds_until_deadline(config: DailyPremarketConfig) -> float:
    zone = ZoneInfo(config.timezone)
    now = datetime.now(zone)
    hour, minute = [int(part) for part in config.deadline.split(":", 1)]
    deadline_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return max(1.0, (deadline_at - now).total_seconds())


def _count_advice(advice_path: Path) -> dict[str, int]:
    counts = {"ok": 0, "fallback": 0, "error": 0}
    with advice_path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            status = row.get("status", "")
            if status in counts:
                counts[status] += 1
    return counts


def _count_plan(plan_path: Path) -> dict[str, int]:
    counts = {"active": 0, "fallback": 0, "error": 0}
    with plan_path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if row.get("status") == "active":
                counts["active"] += 1
            if row.get("source_status") == "fallback":
                counts["fallback"] += 1
            if row.get("status") == "error":
                counts["error"] += 1
    return counts
```

- [ ] **Step 10: Implement `DailyPremarketRunner.run`**

Add:

```python
class DailyPremarketRunner:
    def __init__(
        self,
        *,
        config: DailyPremarketConfig,
        premarket_runner: Callable[..., object] = run_premarket,
        plan_builder: Callable[..., TradingPlanBuildResult] = build_trading_plan,
        quote_client_factory: Callable[..., object] = FutuQuoteClient,
        notifier: Notifier | None = None,
    ) -> None:
        self.config = config
        self.premarket_runner = premarket_runner
        self.plan_builder = plan_builder
        self.quote_client_factory = quote_client_factory
        self.notifier = notifier or MacOSNotifier()

    def run(self, run_date: str) -> DailyRunResult:
        started_at = datetime.now(ZoneInfo(self.config.timezone)).isoformat()
        lock_path = self.config.data_dir / "runs" / ".daily_premarket.lock"
        log_path = self.config.logs_dir / "daily_premarket" / f"{run_date}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with RunLock(lock_path):
            try:
                return self._run_locked(run_date=run_date, started_at=started_at, log_path=log_path)
            except Exception as exc:
                return self._write_failure(
                    run_date=run_date,
                    started_at=started_at,
                    log_path=log_path,
                    error=str(exc),
                )
```

Implement `_run_locked`:

```python
    def _run_locked(self, *, run_date: str, started_at: str, log_path: Path) -> DailyRunResult:
        if not self.config.portfolio.exists():
            raise FileNotFoundError(f"portfolio not found: {self.config.portfolio}")

        premarket_result = self.premarket_runner(
            run_date=run_date,
            portfolio_path=self.config.portfolio,
            data_dir=self.config.data_dir,
            reports_dir=self.config.reports_dir,
            advice_runner=None,
            advice_runner_factory=self._advice_runner_factory(),
            classifier=ChangeClassifier(
                client=OpenAIClassifierClient(model=self.config.classifier_model)
            ),
            symbols=None,
            excluded_symbols=None,
            update_latest=not self.config.dry_run,
            max_workers=self.config.max_workers,
            use_fallback=True,
            deadline_reached=_deadline_reached(self.config),
        )
        plan_result = self.plan_builder(
            advice_path=premarket_result.advice_path,
            data_dir=self.config.data_dir,
            run_date=run_date,
            update_latest=not self.config.dry_run,
        )
        futu_status = self._check_futu_plan(plan_result.plan_path)
        advice_counts = _count_advice(premarket_result.advice_path)
        plan_counts = _count_plan(plan_result.plan_path)
        status = "success"
        if advice_counts["fallback"] or advice_counts["error"] or futu_status.get("error"):
            status = "partial"
        status_path, report_path = self._write_status_and_report(
            run_date=run_date,
            started_at=started_at,
            status=status,
            premarket_result=premarket_result,
            plan_result=plan_result,
            advice_counts=advice_counts,
            plan_counts=plan_counts,
            futu_status=futu_status,
            log_path=log_path,
        )
        self.notifier.notify(
            "Open Trader daily run",
            _notification_message(status, plan_counts, futu_status, advice_counts),
        )
        return DailyRunResult(
            run_date=run_date,
            status=status,
            status_path=status_path,
            report_path=report_path,
            log_path=log_path,
        )
```

Add `_advice_runner_factory`:

```python
    def _advice_runner_factory(self) -> Callable[[], TradingAgentsSubprocessRunner]:
        def factory() -> TradingAgentsSubprocessRunner:
            timeout_seconds = _seconds_until_deadline(self.config)
            return TradingAgentsSubprocessRunner(
                project_path=self.config.tradingagents_path,
                config_overrides={
                    "llm_provider": "deepseek",
                    "deep_think_llm": "deepseek-v4-pro",
                    "quick_think_llm": "deepseek-v4-flash",
                    "llm_timeout": self.config.ta_timeout_seconds,
                    "llm_max_retries": self.config.ta_max_retries,
                },
                timeout_seconds=timeout_seconds,
            )
        return factory
```

- [ ] **Step 11: Implement Futu plan check and writers**

Add:

```python
    def _check_futu_plan(self, plan_path: Path) -> dict[str, object]:
        quote_client = None
        try:
            plans = [
                plan for plan in load_trading_plan_rows(plan_path)
                if plan.status == "active"
            ]
            quote_client = self.quote_client_factory(
                host=self.config.futu_host,
                port=self.config.futu_port,
            )
            symbols = sorted({plan.futu_symbol for plan in plans})
            snapshots = quote_client.get_snapshots(symbols) if symbols else {}
            statuses: list[dict[str, str]] = []
            missing = 0
            triggered = 0
            plans_by_symbol = {plan.futu_symbol: plan for plan in plans}
            for futu_symbol in symbols:
                quote = snapshots.get(futu_symbol)
                if quote is None:
                    missing += 1
                    statuses.append({"symbol": futu_symbol, "status": "missing_quote", "message": "Futu did not return a quote."})
                    continue
                status = evaluate_plan_quote(plans_by_symbol[futu_symbol], quote.last_price)
                if status.status != "watch":
                    triggered += 1
                statuses.append(
                    {
                        "symbol": status.futu_symbol,
                        "last_price": str(status.last_price),
                        "status": status.status,
                        "message": status.message,
                    }
                )
            return {"checked": len(symbols) - missing, "missing": missing, "triggered": triggered, "items": statuses, "error": ""}
        except FutuQuoteError as exc:
            return {"checked": 0, "missing": 0, "triggered": 0, "items": [], "error": str(exc)}
        finally:
            if quote_client is not None:
                quote_client.close()
```

Add writer methods:

```python
    def _write_status_and_report(
        self,
        *,
        run_date: str,
        started_at: str,
        status: str,
        premarket_result: object,
        plan_result: TradingPlanBuildResult,
        advice_counts: dict[str, int],
        plan_counts: dict[str, int],
        futu_status: dict[str, object],
        log_path: Path,
    ) -> tuple[Path, Path]:
        finished_at = datetime.now(ZoneInfo(self.config.timezone)).isoformat()
        status_path = self.config.data_dir / "runs" / run_date / "daily_run_status.json"
        report_path = self.config.reports_dir / "daily_runs" / f"{run_date}.md"
        payload = {
            "run_date": run_date,
            "started_at": started_at,
            "finished_at": finished_at,
            "deadline_at": self.config.deadline,
            "status": status,
            "premarket": {
                "eligible": premarket_result.eligible_count,
                "advice": premarket_result.advice_count,
                "actions": premarket_result.action_count,
                **advice_counts,
            },
            "trading_plan": plan_counts,
            "futu_plan_check": futu_status,
            "artifacts": {
                "advice_csv": str(premarket_result.advice_path),
                "plan_csv": str(plan_result.plan_path),
                "premarket_report": str(premarket_result.report_path),
                "log": str(log_path),
            },
        }
        status_path.parent.mkdir(parents=True, exist_ok=True)
        status_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(_render_daily_report(payload), encoding="utf-8")
        log_path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
        return status_path, report_path
```

Add failure writer:

```python
    def _write_failure(
        self,
        *,
        run_date: str,
        started_at: str,
        log_path: Path,
        error: str,
    ) -> DailyRunResult:
        finished_at = datetime.now(ZoneInfo(self.config.timezone)).isoformat()
        status_path = self.config.data_dir / "runs" / run_date / "daily_run_status.json"
        report_path = self.config.reports_dir / "daily_runs" / f"{run_date}.md"
        payload = {
            "run_date": run_date,
            "started_at": started_at,
            "finished_at": finished_at,
            "deadline_at": self.config.deadline,
            "status": "failed",
            "error": error,
            "premarket": {"eligible": 0, "advice": 0, "actions": 0, "ok": 0, "fallback": 0, "error": 0},
            "trading_plan": {"active": 0, "fallback": 0, "error": 0},
            "futu_plan_check": {"checked": 0, "missing": 0, "triggered": 0, "items": [], "error": ""},
            "artifacts": {
                "advice_csv": "",
                "plan_csv": "",
                "premarket_report": "",
                "log": str(log_path),
            },
        }
        status_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        status_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        report_path.write_text(_render_daily_report(payload), encoding="utf-8")
        log_path.write_text(error + "\n", encoding="utf-8")
        self.notifier.notify("Open Trader daily run", f"failed: {error}")
        return DailyRunResult(
            run_date=run_date,
            status="failed",
            status_path=status_path,
            report_path=report_path,
            log_path=log_path,
        )
```

Add:

```python
def _render_daily_report(payload: dict[str, object]) -> str:
    premarket = payload["premarket"]
    trading_plan = payload["trading_plan"]
    futu = payload["futu_plan_check"]
    artifacts = payload["artifacts"]
    lines = [
        f"# Daily Premarket Run - {payload['run_date']}",
        "",
        f"Status: {payload['status']}",
        f"Started: {payload['started_at']}",
        f"Finished: {payload['finished_at']}",
        f"Deadline: {payload['deadline_at']}",
        "",
        "## Summary",
        "",
        f"- Advice ok: {premarket['ok']}",
        f"- Advice fallback: {premarket['fallback']}",
        f"- Advice error: {premarket['error']}",
        f"- Active plans: {trading_plan['active']}",
        f"- Futu checked: {futu['checked']}",
        f"- Futu missing: {futu['missing']}",
        f"- Triggered: {futu['triggered']}",
        "",
        "## Futu Plan Checks",
        "",
    ]
    for item in futu.get("items", []):
        lines.append(f"- {item['symbol']}: {item['status']} {item.get('message', '')}".rstrip())
    if futu.get("error"):
        lines.append(f"- Error: {futu['error']}")
    lines.extend(
        [
            "",
            "## Artifacts",
            "",
            f"- Advice CSV: {artifacts['advice_csv']}",
            f"- Plan CSV: {artifacts['plan_csv']}",
            f"- Premarket report: {artifacts['premarket_report']}",
            f"- Log: {artifacts['log']}",
            "",
        ]
    )
    return "\n".join(lines)


def _notification_message(
    status: str,
    plan_counts: dict[str, int],
    futu_status: dict[str, object],
    advice_counts: dict[str, int],
) -> str:
    if status == "success":
        return f"finished: {plan_counts['active']} plans, {futu_status['triggered']} triggered"
    if status == "partial":
        return f"partial: {advice_counts['ok']} ok, {advice_counts['fallback']} fallback, {advice_counts['error']} error"
    return "failed: see daily run logs"
```

- [ ] **Step 12: Run focused tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_daily_premarket.py -v
```

Expected: PASS.

- [ ] **Step 13: Commit**

```bash
git add src/open_trader/daily_premarket.py tests/test_daily_premarket.py
git commit -m "feat: add daily premarket runner"
```

## Task 5: CLI Command

**Files:**
- Modify: `src/open_trader/cli.py`
- Test: `tests/test_premarket_cli.py`

- [ ] **Step 1: Add failing CLI parser and wiring test**

Append to `tests/test_premarket_cli.py`:

```python
def test_run_daily_premarket_help_includes_expected_options(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["run-daily-premarket", "--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "--date" in output
    assert "--config" in output
    assert "--dry-run" in output


def test_run_daily_premarket_main_wires_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}

    class FakeRunner:
        def __init__(self, *, config: object) -> None:
            captured["config"] = config

        def run(self, run_date: str):
            captured["run_date"] = run_date
            return type(
                "DailyRunResult",
                (),
                {
                    "status": "success",
                    "status_path": tmp_path / "data/runs/2026-06-17/daily_run_status.json",
                    "report_path": tmp_path / "reports/daily_runs/2026-06-17.md",
                    "log_path": tmp_path / "logs/daily_premarket/2026-06-17.log",
                },
            )()

    def fake_load_env_config(path: Path, *, dry_run: bool):
        captured["config_path"] = path
        captured["dry_run"] = dry_run
        return object()

    monkeypatch.setattr(cli, "DailyPremarketRunner", FakeRunner)
    monkeypatch.setattr(cli, "load_env_config", fake_load_env_config)

    result = cli.main(
        [
            "run-daily-premarket",
            "--date",
            "2026-06-17",
            "--config",
            str(tmp_path / "daily.env"),
            "--dry-run",
        ]
    )

    assert result == 0
    assert captured["config_path"] == tmp_path / "daily.env"
    assert captured["dry_run"] is True
    assert captured["run_date"] == "2026-06-17"
    output = capsys.readouterr().out
    assert "status: success" in output
    assert "status_json:" in output
    assert "report:" in output
    assert "log:" in output
```

- [ ] **Step 2: Add failing test for `today` date**

Append:

```python
def test_run_daily_premarket_accepts_today_date(monkeypatch: pytest.MonkeyPatch) -> None:
    parser = build_parser()

    args = parser.parse_args(["run-daily-premarket", "--date", "today"])

    assert args.date == "today"
```

- [ ] **Step 3: Run tests to verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_premarket_cli.py -v
```

Expected: FAIL because `run-daily-premarket` does not exist.

- [ ] **Step 4: Import daily runner in CLI**

In `src/open_trader/cli.py`, add:

```python
from datetime import datetime
from zoneinfo import ZoneInfo

from .daily_premarket import DailyPremarketRunner, load_env_config
```

- [ ] **Step 5: Add parser command**

In `build_parser`, add:

```python
    daily_parser = subparsers.add_parser(
        "run-daily-premarket",
        help="Run the scheduled daily premarket automation workflow",
    )
    daily_parser.add_argument(
        "--date",
        required=True,
        help="Run date, YYYY-MM-DD, or today",
    )
    daily_parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/daily_premarket.env"),
    )
    daily_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Write dated outputs but do not update latest artifacts",
    )
```

- [ ] **Step 6: Add command handling**

In `main`, before `parser.error`, add:

```python
    if args.command == "run-daily-premarket":
        try:
            config = load_env_config(args.config, dry_run=args.dry_run)
            run_date = (
                datetime.now(ZoneInfo(config.timezone)).date().isoformat()
                if args.date == "today"
                else canonical_date(args.date)
            )
            result = DailyPremarketRunner(config=config).run(run_date)
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            parser.error(str(exc))
        print(f"status: {result.status}")
        print(f"status_json: {result.status_path}")
        print(f"report: {result.report_path}")
        print(f"log: {result.log_path}")
        return 0
```

- [ ] **Step 7: Run focused tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_premarket_cli.py -v
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/open_trader/cli.py tests/test_premarket_cli.py
git commit -m "feat: add daily premarket command"
```

## Task 6: launchd Assets and Install Scripts

**Files:**
- Create: `config/daily_premarket.env.example`
- Create: `ops/launchd/com.open-trader.premarket.plist.template`
- Create: `scripts/install_daily_premarket_launchd.sh`
- Create: `scripts/uninstall_daily_premarket_launchd.sh`
- Test: `tests/test_daily_premarket.py`

- [ ] **Step 1: Add failing test for launchd template contents**

Append to `tests/test_daily_premarket.py`:

```python
def test_launchd_template_runs_daily_premarket_command() -> None:
    template = Path("ops/launchd/com.open-trader.premarket.plist.template").read_text(
        encoding="utf-8"
    )

    assert "com.open-trader.premarket" in template
    assert "run-daily-premarket" in template
    assert "<key>Hour</key>" in template
    assert "<integer>18</integer>" in template
    assert "<key>Minute</key>" in template
    assert "<integer>30</integer>" in template
    assert "OPEN_TRADER_REPO" in template
```

- [ ] **Step 2: Add failing test for env example**

Append:

```python
def test_daily_env_example_has_required_keys_without_real_secrets() -> None:
    text = Path("config/daily_premarket.env.example").read_text(encoding="utf-8")

    for key in [
        "OPEN_TRADER_REPO",
        "OPEN_TRADER_PYTHON",
        "OPEN_TRADER_TIMEZONE",
        "OPEN_TRADER_DEADLINE",
        "OPEN_TRADER_FUTU_HOST",
        "OPEN_TRADER_FUTU_PORT",
        "DEEPSEEK_API_KEY",
        "OPENAI_API_KEY",
    ]:
        assert key in text
    assert "sk-" not in text
```

- [ ] **Step 3: Run tests to verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_daily_premarket.py::test_launchd_template_runs_daily_premarket_command tests/test_daily_premarket.py::test_daily_env_example_has_required_keys_without_real_secrets -v
```

Expected: FAIL because files do not exist.

- [ ] **Step 4: Add env example**

Create `config/daily_premarket.env.example`:

```bash
# Copy to config/daily_premarket.env and fill local values.
# Do not commit real API keys.
OPEN_TRADER_REPO=/Users/ray/projects/open_trader
OPEN_TRADER_PYTHON=/Users/ray/projects/open_trader/.venv/bin/python
OPEN_TRADER_TIMEZONE=Asia/Shanghai
OPEN_TRADER_DEADLINE=21:10
OPEN_TRADER_FUTU_HOST=127.0.0.1
OPEN_TRADER_FUTU_PORT=11111
OPEN_TRADER_DATA_DIR=data
OPEN_TRADER_REPORTS_DIR=reports
OPEN_TRADER_LOGS_DIR=logs
OPEN_TRADER_PORTFOLIO=data/latest/portfolio.csv
OPEN_TRADER_MAX_WORKERS=4
OPEN_TRADER_TA_TIMEOUT_SECONDS=600
OPEN_TRADER_TA_MAX_RETRIES=2
OPEN_TRADER_TRADINGAGENTS_PATH=/Users/ray/projects/TradingAgents
OPEN_TRADER_CLASSIFIER_MODEL=gpt-5.4-mini
DEEPSEEK_API_KEY=<local secret>
OPENAI_API_KEY=<local secret>
```

- [ ] **Step 5: Add launchd plist template**

Create `ops/launchd/com.open-trader.premarket.plist.template`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.open-trader.premarket</string>

  <key>WorkingDirectory</key>
  <string>OPEN_TRADER_REPO</string>

  <key>ProgramArguments</key>
  <array>
    <string>OPEN_TRADER_PYTHON</string>
    <string>-m</string>
    <string>open_trader</string>
    <string>run-daily-premarket</string>
    <string>--date</string>
    <string>today</string>
    <string>--config</string>
    <string>OPEN_TRADER_REPO/config/daily_premarket.env</string>
  </array>

  <key>StartCalendarInterval</key>
  <array>
    <dict><key>Weekday</key><integer>1</integer><key>Hour</key><integer>18</integer><key>Minute</key><integer>30</integer></dict>
    <dict><key>Weekday</key><integer>2</integer><key>Hour</key><integer>18</integer><key>Minute</key><integer>30</integer></dict>
    <dict><key>Weekday</key><integer>3</integer><key>Hour</key><integer>18</integer><key>Minute</key><integer>30</integer></dict>
    <dict><key>Weekday</key><integer>4</integer><key>Hour</key><integer>18</integer><key>Minute</key><integer>30</integer></dict>
    <dict><key>Weekday</key><integer>5</integer><key>Hour</key><integer>18</integer><key>Minute</key><integer>30</integer></dict>
  </array>

  <key>StandardOutPath</key>
  <string>OPEN_TRADER_REPO/logs/daily_premarket/launchd.out.log</string>
  <key>StandardErrorPath</key>
  <string>OPEN_TRADER_REPO/logs/daily_premarket/launchd.err.log</string>
</dict>
</plist>
```

- [ ] **Step 6: Add install script**

Create `scripts/install_daily_premarket_launchd.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$REPO_ROOT/config/daily_premarket.env"
TEMPLATE="$REPO_ROOT/ops/launchd/com.open-trader.premarket.plist.template"
TARGET="$HOME/Library/LaunchAgents/com.open-trader.premarket.plist"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "missing $ENV_FILE; copy config/daily_premarket.env.example first" >&2
  exit 2
fi

OPEN_TRADER_REPO="$(grep '^OPEN_TRADER_REPO=' "$ENV_FILE" | head -1 | cut -d= -f2-)"
OPEN_TRADER_PYTHON="$(grep '^OPEN_TRADER_PYTHON=' "$ENV_FILE" | head -1 | cut -d= -f2-)"

if [[ -z "$OPEN_TRADER_REPO" || -z "$OPEN_TRADER_PYTHON" ]]; then
  echo "OPEN_TRADER_REPO and OPEN_TRADER_PYTHON are required in $ENV_FILE" >&2
  exit 2
fi

mkdir -p "$HOME/Library/LaunchAgents" "$OPEN_TRADER_REPO/logs/daily_premarket"
rendered="$(sed \
  -e "s#OPEN_TRADER_REPO#$OPEN_TRADER_REPO#g" \
  -e "s#OPEN_TRADER_PYTHON#$OPEN_TRADER_PYTHON#g" \
  "$TEMPLATE")"

if [[ "$DRY_RUN" == "1" ]]; then
  printf '%s\n' "$rendered"
  exit 0
fi

printf '%s\n' "$rendered" > "$TARGET"
launchctl unload "$TARGET" >/dev/null 2>&1 || true
launchctl load "$TARGET"
echo "installed $TARGET"
```

- [ ] **Step 7: Add uninstall script**

Create `scripts/uninstall_daily_premarket_launchd.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

TARGET="$HOME/Library/LaunchAgents/com.open-trader.premarket.plist"

if [[ -f "$TARGET" ]]; then
  launchctl unload "$TARGET" >/dev/null 2>&1 || true
  rm -f "$TARGET"
  echo "removed $TARGET"
else
  echo "$TARGET is not installed"
fi
```

- [ ] **Step 8: Make scripts executable**

Run:

```bash
chmod +x scripts/install_daily_premarket_launchd.sh scripts/uninstall_daily_premarket_launchd.sh
```

- [ ] **Step 9: Run focused tests and dry-run script**

Run:

```bash
.venv/bin/python -m pytest tests/test_daily_premarket.py -v
scripts/install_daily_premarket_launchd.sh --dry-run >/tmp/open_trader_premarket.plist
plutil -lint /tmp/open_trader_premarket.plist
```

Expected: pytest PASS and `plutil` reports `OK`.

- [ ] **Step 10: Commit**

```bash
git add config/daily_premarket.env.example ops/launchd/com.open-trader.premarket.plist.template scripts/install_daily_premarket_launchd.sh scripts/uninstall_daily_premarket_launchd.sh tests/test_daily_premarket.py
git commit -m "feat: add launchd daily premarket assets"
```

## Task 7: Documentation and Verification

**Files:**
- Modify: `docs/monthly_portfolio_import.md`
- Test/verification: full pytest suite and CLI help/dry-run commands

- [ ] **Step 1: Add automation docs**

Append this section to `docs/monthly_portfolio_import.md` after "Futu Quote Watch":

````markdown
## Daily Premarket Automation

The automated daily workflow is designed for a Mac that stays online with Futu
OpenD logged in. During development it can run on the Mac Air; the same setup can
later move to the Mac mini.

Copy the local env template:

```bash
cp config/daily_premarket.env.example config/daily_premarket.env
```

Fill in local paths and API keys in `config/daily_premarket.env`. Do not commit
the real env file.

Run one manual dry run:

```bash
.venv/bin/python -m open_trader run-daily-premarket \
  --date today \
  --config config/daily_premarket.env \
  --dry-run
```

Run one real manual check with Futu OpenD connected:

```bash
.venv/bin/python -m open_trader run-daily-premarket \
  --date today \
  --config config/daily_premarket.env
```

Install the launchd job:

```bash
scripts/install_daily_premarket_launchd.sh
```

The job runs Monday through Friday at 18:30 Asia/Shanghai. The daily runner uses
21:10 Asia/Shanghai as the hard deadline. If a symbol has no fresh advice by the
deadline, the runner reuses the latest prior successful advice for that symbol
and marks the row as `fallback`.

Daily outputs:

```text
data/runs/<YYYY-MM-DD>/daily_run_status.json
reports/daily_runs/<YYYY-MM-DD>.md
logs/daily_premarket/<YYYY-MM-DD>.log
```

To uninstall:

```bash
scripts/uninstall_daily_premarket_launchd.sh
```

Mac mini migration checklist:

1. Clone or copy this repo.
2. Recreate `.venv` and install dependencies.
3. Install and log in to Futu OpenD.
4. Confirm `check-futu-plan` can connect to `127.0.0.1:11111`.
5. Fill `config/daily_premarket.env`.
6. Run `run-daily-premarket --dry-run`.
7. Run one real manual `run-daily-premarket`.
8. Install launchd.
````

- [ ] **Step 2: Add `.gitignore` entry for local env**

Append this line to `.gitignore` unless the exact line is already present:

```gitignore
config/daily_premarket.env
```

- [ ] **Step 3: Run full tests**

Run:

```bash
.venv/bin/python -m pytest
```

Expected: PASS.

- [ ] **Step 4: Verify CLI help**

Run:

```bash
.venv/bin/python -m open_trader run-daily-premarket --help
```

Expected: output includes `--date`, `--config`, and `--dry-run`.

- [ ] **Step 5: Verify launchd dry-run renders valid plist**

Run:

```bash
cp config/daily_premarket.env.example /tmp/open_trader_daily_premarket.env
sed "s#OPEN_TRADER_REPO=/Users/ray/projects/open_trader#OPEN_TRADER_REPO=$(pwd)#" /tmp/open_trader_daily_premarket.env > config/daily_premarket.env
scripts/install_daily_premarket_launchd.sh --dry-run >/tmp/open_trader_premarket.plist
plutil -lint /tmp/open_trader_premarket.plist
rm -f config/daily_premarket.env
```

Expected: `plutil` reports `OK`; local secret file is removed.

- [ ] **Step 6: Commit**

```bash
git add docs/monthly_portfolio_import.md .gitignore
git commit -m "docs: document daily premarket automation"
```

## Final Verification

- [ ] Run full tests:

```bash
.venv/bin/python -m pytest
```

- [ ] Run CLI help:

```bash
.venv/bin/python -m open_trader run-daily-premarket --help
```

- [ ] Run launchd template validation:

```bash
cp config/daily_premarket.env.example /tmp/open_trader_daily_premarket.env
sed "s#OPEN_TRADER_REPO=/Users/ray/projects/open_trader#OPEN_TRADER_REPO=$(pwd)#" /tmp/open_trader_daily_premarket.env > config/daily_premarket.env
scripts/install_daily_premarket_launchd.sh --dry-run >/tmp/open_trader_premarket.plist
plutil -lint /tmp/open_trader_premarket.plist
rm -f config/daily_premarket.env
```

- [ ] Confirm git status is clean:

```bash
git status --short
```
