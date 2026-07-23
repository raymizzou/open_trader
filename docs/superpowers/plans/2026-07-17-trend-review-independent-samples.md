# Trend Review Independent Samples Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist discipline simulation, actual execution, and benchmark facts separately, calculate discipline and actual reviews independently after 30 completed holding cycles, and render the approved pairwise Dashboard UI.

**Architecture:** Keep immutable JSON/CSV file storage. Normalize broker fills into one `TradeFill` model, append source facts without overwriting, and rebuild one replaceable projection at the latest continuous date shared by discipline equity, actual equity, and benchmark. The Dashboard consumes only that projection and renders two pairwise comparisons against the same benchmark.

**Tech Stack:** Python 3.12, dataclasses, `Decimal`, JSON/CSV, pytest, vanilla JavaScript/CSS, Playwright-based Dashboard acceptance.

## Global Constraints

- No database and no strategy subledger.
- CN v1 starts `2026-07-16`; US v1 and HK v1 start `2026-07-17`.
- A completed holding cycle runs from first buy through full sell; partial fills merge and a post-close rebuy starts a new cycle.
- Discipline and actual sample counts unlock independently at 30 and continue cumulatively at 31+; never split into batches.
- The comparison interval ends at one continuous common cutoff shared by discipline equity, actual equity, and benchmark.
- Actual fills come from Eastmoney statements for CN, Tiger real-account API for US, and Phillips statements for HK; never infer fills from position differences.
- Keep fees, taxes, interest, and dividends in actual return; exclude only external deposits and withdrawals.
- Existing v1 daily facts remain readable; immutable source artifacts are never edited or replaced.
- The page contains only identity/progress, the full parameter table, `纪律模拟与市场`, and `实际执行与市场`.
- Show exactly five metrics: period net return, market excess return, max drawdown, Calmar, and Sharpe.
- Reuse the current warm semantic tokens and 8px radius; no new palette, gradients, glass, black conclusion card, status card, backtest control, or English status copy.
- The final review gate is `make acceptance`; only `PASS` may be handed off, followed by redeploying the exact accepted Git SHA and verifying PID, cwd, SHA, fresh logs, and HTTP 200.

## File Map

- `src/open_trader/models.py`: shared immutable `TradeFill` value object.
- `src/open_trader/parsers/base.py`: statement parser output includes normalized fills.
- `src/open_trader/parsers/eastmoney.py`: extract CN executions from the real Eastmoney statement layout.
- `src/open_trader/parsers/phillips.py`: extract HK executions from the real Phillips statement layout.
- `src/open_trader/pipeline.py`: preserve normalized fills in the atomic import run.
- `src/open_trader/statement_import.py`: freeze the immutable fill batch after a successful Dashboard statement upload.
- `src/open_trader/tiger_account.py`: fetch paginated US transactions from the Tiger real-account API and normalize them.
- `src/open_trader/trend_review.py`: immutable fact writers/readers, holding-cycle reconstruction, common-cutoff projection, and metrics.
- `src/open_trader/cli.py`: close workflow records separate discipline/benchmark/actual facts and syncs Tiger fills.
- `src/open_trader/a_share_trend.py`: market-specific v1 effective dates in the frozen strategy snapshot.
- `src/open_trader/dashboard.py`: strict validation and safe projection of counts, cutoff, strategy snapshot, and metrics.
- `src/open_trader/dashboard_static/dashboard.js`: approved B header and two pairwise comparison panels.
- `src/open_trader/dashboard_static/dashboard.css`: warm responsive layout using only existing tokens.
- `src/open_trader/dashboard_acceptance.py`: exact DOM/copy/style/geometry/screenshot browser contract.
- `tests/test_eastmoney_parser.py`, `tests/test_parsers_text.py`, `tests/test_pipeline.py`, `tests/test_statement_import.py`, `tests/test_tiger_account.py`: fill ingestion tests.
- `tests/test_trend_review.py`, `tests/test_a_share_trend.py`, `tests/test_premarket_cli.py`: fact/projection/workflow tests.
- `tests/test_dashboard.py`, `tests/test_dashboard_web.py`, `tests/test_dashboard_acceptance.py`: API, HTML/CSS, and live-browser contract tests.

---

### Task 1: Normalize and Persist Actual Fills

**Files:**
- Modify: `src/open_trader/models.py`
- Modify: `src/open_trader/parsers/base.py`
- Modify: `src/open_trader/parsers/eastmoney.py`
- Modify: `src/open_trader/parsers/phillips.py`
- Modify: `src/open_trader/pipeline.py`
- Modify: `src/open_trader/statement_import.py`
- Modify: `src/open_trader/tiger_account.py`
- Test: `tests/test_eastmoney_parser.py`
- Test: `tests/test_parsers_text.py`
- Test: `tests/test_pipeline.py`
- Test: `tests/test_statement_import.py`
- Test: `tests/test_tiger_account.py`

**Interfaces:**
- Produces: `TradeFill(source_id, source_order_id, broker, account_alias, market, symbol, currency, side, quantity, price, fees, executed_at)` and `ParseResult.fills: list[TradeFill]`.
- Produces: `TigerAccountClient.fetch_actual_fills(start_date: str, end_date: str) -> list[TradeFill]`.
- Produces: `extracted_fills.csv` with one row per unique `(broker, source_id)`; Task 2 freezes those rows as actual facts.

- [ ] **Step 1: Add failing normalization tests against real layouts and Tiger transaction objects**

```python
def test_eastmoney_statement_extracts_actual_trade_fills() -> None:
    result = parse_eastmoney_page(REAL_EXECUTION_TEXT, REAL_EXECUTION_TABLES, "2026-07")
    assert [(fill.market.value, fill.symbol, fill.side, fill.quantity) for fill in result.fills] == [
        ("CN", "600900", "BUY", Decimal("2000")),
    ]
    assert result.fills[0].source_id

def test_phillips_statement_extracts_actual_trade_fills() -> None:
    result = parse_phillips_text(REAL_PHILLIPS_EXECUTION_TEXT, "2026-07")
    assert [(fill.market.value, fill.symbol, fill.side) for fill in result.fills] == [
        ("HK", "00700", "SELL"),
    ]

def test_tiger_fetch_transactions_paginates_and_normalizes() -> None:
    fills = TigerAccountClient(
        config=tiger_config(), trade_client_factory=PagedTransactions,
    ).fetch_actual_fills("2026-07-17", "2026-07-17")
    assert [fill.source_id for fill in fills] == ["9001-1", "9002-1"]
    assert all(fill.market is Market.US for fill in fills)
```

- [ ] **Step 2: Run the new parser/API tests and verify RED**

Run: `.venv/bin/pytest tests/test_eastmoney_parser.py tests/test_parsers_text.py tests/test_tiger_account.py -q`

Expected: FAIL because `TradeFill`, `ParseResult.fills`, and `fetch_actual_fills` do not exist.

- [ ] **Step 3: Add the minimal shared fill model and parser result field**

```python
@dataclass(frozen=True)
class TradeFill:
    source_id: str
    source_order_id: str | None
    broker: str
    account_alias: str
    market: Market
    symbol: str
    currency: str
    side: Literal["BUY", "SELL"]
    quantity: Decimal
    price: Decimal
    fees: Decimal | None
    executed_at: str

@dataclass(frozen=True)
class ParseResult:
    statement_id: str
    broker: str
    positions: list[Position] = field(default_factory=list)
    cash_balances: list[CashBalance] = field(default_factory=list)
    fills: list[TradeFill] = field(default_factory=list)
    warnings: list[WarningRecord] = field(default_factory=list)
    page_count: int = 0
```

In each statement parser, accept only explicit execution rows from the inspected real layout, normalize broker IDs, and raise a parser warning for a row with a recognized execution heading but missing ID, side, quantity, price, or timestamp. In `TigerAccountClient.fetch_actual_fills`, call the installed SDK's `get_transactions(account=self.config.account, since_date=start_date, to_date=end_date, limit=100, page_token=page_token)` until the returned token is empty and map transaction ID to `source_id`. Fetch `get_order(order_id=transaction.order_id, show_charges=True)` once per order; store order-level charges on the batch/order fact and leave a fill's `fees` as `None` when one order has multiple fills, rather than inventing an allocation.

- [ ] **Step 4: Add a failing pipeline idempotency test**

```python
def test_uploaded_statement_persists_each_fill_once(tmp_path: Path) -> None:
    result = run_uploaded_statement(parser=FillParser(), **uploaded_arguments(tmp_path))
    rows = list(csv.DictReader((result.run_dir / "extracted_fills.csv").open()))
    assert [row["source_id"] for row in rows] == ["fill-1"]
    repeated = run_uploaded_statement(parser=FillParser(), **uploaded_arguments(tmp_path))
    repeated_rows = list(csv.DictReader((repeated.run_dir / "extracted_fills.csv").open()))
    assert [row["source_id"] for row in repeated_rows] == ["fill-1"]
```

- [ ] **Step 5: Run the pipeline test and verify RED**

Run: `.venv/bin/pytest tests/test_pipeline.py::test_uploaded_statement_persists_each_fill_once -q`

Expected: FAIL because `extracted_fills.csv` is absent.

- [ ] **Step 6: Persist fill rows in the existing atomic run-directory transaction**

```python
FILL_FIELDNAMES = [
    "source_id", "broker", "market", "symbol", "side",
    "source_order_id", "account_alias", "quantity", "price", "fees",
    "currency", "executed_at",
]

fills_by_key = {
    (fill.broker, fill.source_id): fill
    for fill in [*preserved_fills, *parse_result.fills]
}
write_rows(
    temp_run_dir / "extracted_fills.csv",
    FILL_FIELDNAMES,
    (_fill_to_row(fill) for fill in fills_by_key.values()),
)
```

Extend `_preserved_run_records` to load the file when present and return an empty list for older run directories, preserving backward compatibility.

- [ ] **Step 7: Run focused ingestion tests**

Run: `.venv/bin/pytest tests/test_eastmoney_parser.py tests/test_parsers_text.py tests/test_pipeline.py tests/test_statement_import.py tests/test_tiger_account.py -q`

Expected: PASS.

- [ ] **Step 8: Commit the ingestion slice**

```bash
git add src/open_trader/models.py src/open_trader/parsers/base.py src/open_trader/parsers/eastmoney.py src/open_trader/parsers/phillips.py src/open_trader/pipeline.py src/open_trader/statement_import.py src/open_trader/tiger_account.py tests/test_eastmoney_parser.py tests/test_parsers_text.py tests/test_pipeline.py tests/test_statement_import.py tests/test_tiger_account.py
git commit -m "feat: normalize actual trend fills"
```

### Task 2: Split Immutable Facts and Build Independent Cumulative Reviews

**Files:**
- Modify: `src/open_trader/trend_review.py`
- Modify: `src/open_trader/a_share_trend.py`
- Modify: `tests/test_trend_review.py`
- Modify: `tests/test_a_share_trend.py`

**Interfaces:**
- Consumes: `TradeFill` and extracted fill rows from Task 1.
- Produces: `freeze_discipline_fact(data_dir, market, trading_date, equity, orders, strategy_snapshot)`, `freeze_actual_equity_fact(data_dir, market, trading_date, equity, opening_positions, strategy_snapshot)`, `freeze_actual_fill_batch(data_dir, source_metadata, fills, complete_through)`, and `freeze_benchmark_fact(data_dir, market, trading_date, benchmark)`.
- Produces: projection keys `sample_counts`, `common_cutoff`, `interval`, `strategy_snapshot`, and five three-series `metrics` cells.

- [ ] **Step 1: Replace batch tests with failing 29/30/31 independent-series tests**

```python
@pytest.mark.parametrize(
    ("discipline_count", "actual_count", "discipline_ready", "actual_ready"),
    [(29, 30, False, True), (30, 29, True, False), (31, 31, True, True)],
)
def test_projection_unlocks_series_independently_and_never_batches(
    tmp_path: Path,
    discipline_count: int,
    actual_count: int,
    discipline_ready: bool,
    actual_ready: bool,
) -> None:
    write_separate_review_facts(tmp_path, discipline_count, actual_count)
    projection = build_trend_review_projection(tmp_path, "CN")
    assert projection["sample_counts"] == {
        "discipline": discipline_count,
        "actual": actual_count,
        "required": 30,
    }
    assert (projection["metrics"]["calmar"]["discipline"]["value"] is not None) is discipline_ready
    assert (projection["metrics"]["calmar"]["actual"]["value"] is not None) is actual_ready
    assert "batch" not in projection
    assert "batch_path" not in projection
```

Add tests that partial fills close once, an opening real position closes as the first actual cycle, rebuy creates a second cycle, duplicate source IDs are ignored, different strategy versions never mix, and CN/US/HK effective dates are exactly `2026-07-16`, `2026-07-17`, `2026-07-17`.

- [ ] **Step 2: Run projection tests and verify RED**

Run: `.venv/bin/pytest tests/test_trend_review.py tests/test_a_share_trend.py -q`

Expected: FAIL because projection still exposes batches and one discipline trade count.

- [ ] **Step 3: Implement market effective dates and immutable fact writers**

```python
TREND_V1_EFFECTIVE_FROM = {
    "CN": "2026-07-16",
    "US": "2026-07-17",
    "HK": "2026-07-17",
}

def _fact_path(data_dir: Path, stream: str, market: str, trading_date: str) -> Path:
    return data_dir / "trend_review" / "facts" / stream / market / f"{trading_date}.json"

def freeze_actual_fills(data_dir: Path, market: str, fills: Sequence[TradeFill]) -> list[Path]:
    paths = []
    for fill in fills:
        digest = hashlib.sha256(f"{fill.broker}:{fill.source_id}".encode()).hexdigest()
        paths.append(_write_immutable(
            data_dir / "trend_review" / "facts" / "actual_fills" / market / f"{digest}.json",
            _canonical_json_bytes({"schema_version": "open_trader.trend_review.fill.v1", **asdict(fill)}),
        ))
    return paths
```

Use one file per market/date for discipline, actual equity, and benchmark. Retain the old `daily.v1` loader as a read-only adapter so 2026-07-16 source facts already captured by the current branch remain usable.

- [ ] **Step 4: Implement generic cycle reconstruction and common cutoff**

```python
def _completed_cycles(
    fills: Sequence[Mapping[str, object]],
    opening_positions: Sequence[Mapping[str, object]] = (),
) -> list[dict[str, object]]:
    positions = {
        str(row["symbol"]): {
            "symbol": str(row["symbol"]),
            "entry_date": str(row["opened_at"])[:10],
            "quantity": _required_decimal(row["quantity"], "opening quantity"),
            "fills": [],
        }
        for row in opening_positions
    }
    completed: list[dict[str, object]] = []
    seen: set[tuple[str, str, str]] = set()
    for fill in sorted(
        fills, key=lambda row: (str(row["executed_at"]), str(row["source_id"]))
    ):
        identity = (
            str(fill["broker"]), str(fill["account_alias"]), str(fill["source_id"])
        )
        if identity in seen:
            continue
        seen.add(identity)
        symbol = str(fill["symbol"])
        quantity = _required_decimal(fill["quantity"], "fill quantity")
        current = positions.get(symbol)
        if fill["side"] == "BUY":
            if current is None:
                current = {
                    "symbol": symbol,
                    "entry_date": str(fill["executed_at"])[:10],
                    "quantity": Decimal("0"),
                    "fills": [],
                }
                positions[symbol] = current
            current["quantity"] = (
                _required_decimal(current["quantity"], "open quantity") + quantity
            )
        else:
            if current is None or _required_decimal(
                current["quantity"], "open quantity"
            ) < quantity:
                raise ValueError("sell fill exceeds actual position")
            current["quantity"] = (
                _required_decimal(current["quantity"], "open quantity") - quantity
            )
        current["fills"].append(dict(fill))
        if current["quantity"] == 0:
            completed.append(
                {**current, "exit_date": str(fill["executed_at"])[:10]}
            )
            del positions[symbol]
    return completed

def _common_cutoff(
    effective_from: str,
    discipline_dates: set[str],
    actual_dates: set[str],
    benchmark_dates: set[str],
) -> str | None:
    shared = discipline_dates & actual_dates & benchmark_dates
    ordered = sorted(day for day in shared if day >= effective_from)
    return ordered[-1] if ordered else None
```

Implement the shown logic without a calendar guess: continuity is defined by the benchmark dates present from `effective_from`; the cutoff is the last benchmark date for which both equity streams exist for every benchmark date up to it. Filter fills, cycles, curves, and strategy version to `effective_from <= date <= common_cutoff`.

- [ ] **Step 5: Build the cumulative projection and gate cells per series**

```python
projection = {
    "schema_version": "open_trader.trend_review.projection.v2",
    "available": True,
    "market": market,
    "broker": BROKER_BY_MARKET[market],
    "strategy_snapshot": snapshot,
    "sample_counts": {
        "discipline": len(discipline_cycles),
        "actual": len(actual_cycles),
        "required": 30,
    },
    "common_cutoff": common_cutoff,
    "interval": {"start": effective_from, "end": common_cutoff},
    "metrics": metrics,
}
```

Compute all five benchmark cells whenever the common curve exists. Replace only the not-yet-ready strategy series with `{ "value": None, "reason": "n / 30，数据不足" }`. Preserve finite-number checks and never serialize `NaN` or infinity.

- [ ] **Step 6: Run focused projection tests**

Run: `.venv/bin/pytest tests/test_trend_review.py tests/test_a_share_trend.py -q`

Expected: PASS.

- [ ] **Step 7: Commit the projection slice**

```bash
git add src/open_trader/trend_review.py src/open_trader/a_share_trend.py tests/test_trend_review.py tests/test_a_share_trend.py
git commit -m "feat: calculate independent cumulative trend reviews"
```

### Task 3: Wire Real Close and Statement Workflows

**Files:**
- Modify: `src/open_trader/pipeline.py`
- Modify: `src/open_trader/cli.py`
- Modify: `tests/test_pipeline.py`
- Modify: `tests/test_premarket_cli.py`

**Interfaces:**
- Consumes: Task 1 normalized fills and Task 2 immutable fact writers.
- Produces: daily discipline, actual equity, benchmark, and actual-fill facts before rebuilding the projection.

- [ ] **Step 1: Add failing workflow tests for separate artifacts and idempotency**

```python
def test_trend_review_close_writes_three_separate_daily_facts(monkeypatch, tmp_path: Path) -> None:
    run_trend_review_close(config_for(tmp_path), "CN", "2026-07-17")
    assert (tmp_path / "trend_review/facts/discipline/CN/2026-07-17.json").exists()
    assert (tmp_path / "trend_review/facts/actual_equity/CN/2026-07-17.json").exists()
    assert (tmp_path / "trend_review/facts/benchmark/CN/2026-07-17.json").exists()

def test_tiger_close_freezes_transactions_before_projection(monkeypatch, tmp_path: Path) -> None:
    run_trend_review_close(config_for(tmp_path), "US", "2026-07-17")
    assert list(tmp_path.glob("trend_review/facts/actual_fills/US/*.json"))
```

Add a pipeline test proving Eastmoney maps only to CN, Phillips only to HK, and a repeated statement upload leaves the same fill fact bytes unchanged.

- [ ] **Step 2: Run workflow tests and verify RED**

Run: `.venv/bin/pytest tests/test_pipeline.py tests/test_premarket_cli.py -q`

Expected: FAIL because the workflows still call `capture_trend_review_close` and do not freeze actual fills.

- [ ] **Step 3: Wire statement and Tiger sources to the fact writers**

```python
if parser.broker in {"eastmoney", "phillips"}:
    market = {"eastmoney": "CN", "phillips": "HK"}[parser.broker]
    freeze_actual_fill_batch(
        data_dir,
        {"broker": parser.broker, "source_sha256": sha256_file(statement_path)},
        parse_result.fills,
        statement_date,
    )

freeze_discipline_fact(
    config.data_dir, market, trading_date,
    simulate_snapshot["net_value"], orders, report["strategy_snapshot"],
)
freeze_actual_equity_fact(
    config.data_dir, market, trading_date,
    report["account"]["net_value"], report["account"]["positions"],
    report["strategy_snapshot"],
)
freeze_benchmark_fact(config.data_dir, market, trading_date, benchmark)
if market == "US":
    freeze_actual_fill_batch(
        config.data_dir,
        {"broker": "tiger", "account_alias": "tiger_main"},
        tiger_client.fetch_actual_fills(effective_from, trading_date),
        trading_date,
    )
build_trend_review_projection(config.data_dir, market)
```

Do not freeze an actual-equity fact when the report account is stale or dated differently. A missing source stops the common cutoff but does not delete newer facts from the other streams.

- [ ] **Step 4: Run focused workflow tests and the real read-only close command where configured**

Run: `.venv/bin/pytest tests/test_pipeline.py tests/test_premarket_cli.py tests/test_trend_review.py -q`

Expected: PASS.

Run: `PYTHONPATH=src .venv/bin/python -m open_trader.cli trend-review close --market CN --date 2026-07-17`

Expected: JSON output identifies CN, separate fact paths exist, and projection reports its actual and discipline sample counts. If the configured live source is unavailable, record the exact error and do not substitute fixtures.

- [ ] **Step 5: Commit the workflow slice**

```bash
git add src/open_trader/pipeline.py src/open_trader/cli.py tests/test_pipeline.py tests/test_premarket_cli.py
git commit -m "feat: record separate trend review facts"
```

### Task 4: Project the Approved Dashboard Contract

**Files:**
- Modify: `src/open_trader/dashboard.py`
- Modify: `tests/test_dashboard.py`

**Interfaces:**
- Consumes: projection schema v2 from Task 2.
- Produces: safe API fields `strategy_snapshot`, `sample_counts`, `common_cutoff`, `interval`, and `metrics`; private paths and source artifacts remain hidden.

- [ ] **Step 1: Add failing strict-schema tests**

```python
def test_dashboard_projects_independent_sample_progress_and_cutoff(tmp_path: Path) -> None:
    write_projection(tmp_path, sample_counts={"discipline": 31, "actual": 29, "required": 30})
    review = load_dashboard_state(config_for(tmp_path)).to_dict()["trend_reviews"]["eastmoney"]
    assert review["sample_counts"] == {"discipline": 31, "actual": 29, "required": 30}
    assert review["common_cutoff"] == "2026-07-17"
    assert "batch" not in review

@pytest.mark.parametrize("mutation", [
    lambda value: value["sample_counts"].update(required=29),
    lambda value: value.update(common_cutoff="2026/07/17"),
    lambda value: value["metrics"].pop("sharpe"),
])
def test_dashboard_rejects_invalid_v2_projection(mutation, tmp_path: Path) -> None:
    payload = trend_review_projection("US", "tiger")
    mutation(payload)
    path = tmp_path / "data/latest/trend_review_us.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    reviews = dashboard_module._load_trend_reviews(tmp_path / "data")
    assert reviews["tiger"]["available"] is False
```

- [ ] **Step 2: Run Dashboard backend tests and verify RED**

Run: `.venv/bin/pytest tests/test_dashboard.py -q`

Expected: FAIL because v2 metadata is rejected or dropped.

- [ ] **Step 3: Validate and project only the approved fields**

```python
if (
    payload.get("schema_version") != "open_trader.trend_review.projection.v2"
    or set(payload.get("sample_counts", {})) != {"discipline", "actual", "required"}
    or payload["sample_counts"]["required"] != 30
    or not ISO_DATE.fullmatch(str(payload.get("common_cutoff", "")))
):
    return False

return {
    "available": True,
    "broker": broker,
    "broker_label": broker_label,
    "market": market,
    "market_label": market_label,
    "strategy_snapshot": payload["strategy_snapshot"],
    "sample_counts": payload["sample_counts"],
    "common_cutoff": payload["common_cutoff"],
    "interval": payload["interval"],
    "metrics": payload["metrics"],
}
```

- [ ] **Step 4: Run backend tests and commit**

Run: `.venv/bin/pytest tests/test_dashboard.py -q`

Expected: PASS.

```bash
git add src/open_trader/dashboard.py tests/test_dashboard.py
git commit -m "feat: expose independent trend review progress"
```

### Task 5: Render Mock B and Enforce Its Browser Contract

**Files:**
- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Modify: `src/open_trader/dashboard_static/dashboard.css`
- Modify: `src/open_trader/dashboard_acceptance.py`
- Modify: `tests/test_dashboard_web.py`
- Modify: `tests/test_dashboard_acceptance.py`

**Interfaces:**
- Consumes: Task 4 API shape.
- Produces: one `.trend-review-header-side`, one parameter table, and exactly two `.trend-review-comparison` panels, each with five metrics and exactly two labeled series.

- [ ] **Step 1: Add failing HTML tests for exact structure and copy**

```javascript
const html = renderTrendReviewWorkspace(review);
if ((html.match(/class="trend-review-comparison"/g) || []).length !== 2) throw new Error(html);
if (!html.includes("纪律模拟与市场") || !html.includes("实际执行与市场")) throw new Error(html);
if (!html.includes("纪律模拟 31 笔") || !html.includes("实际执行 29 / 30，数据不足")) throw new Error(html);
if (!html.includes("共同截止日 2026-07-17")) throw new Error(html);
for (const forbidden of ["复盘结论", "运行状态", "创建回测", "Connected", "Backtest", "Alpha", "Beta", "Sortino"]) {
  if (html.includes(forbidden)) throw new Error(forbidden);
}
```

- [ ] **Step 2: Run web tests and verify RED**

Run: `.venv/bin/pytest tests/test_dashboard_web.py -q`

Expected: FAIL because the current page groups metrics and renders three series in each chart.

- [ ] **Step 3: Render two pairwise panels and the compact B header**

```javascript
const TREND_REVIEW_COMPARISONS = [
  {key:"discipline", title:"纪律模拟与市场", label:"纪律模拟"},
  {key:"actual", title:"实际执行与市场", label:"实际执行"},
];

function renderTrendReviewComparison(review, comparison) {
  return `<figure class="trend-review-comparison" data-series="${comparison.key}">
    <figcaption>${comparison.title}</figcaption>
    ${TREND_REVIEW_METRICS.map(metric =>
      renderTrendReviewMetric(review, metric, [
        {key:comparison.key, label:comparison.label},
        {key:"benchmark", label:"同期市场"},
      ])
    ).join("")}
  </figure>`;
}
```

The header right contains, in this order, the existing warm-outline return button, discipline count, actual count, and common cutoff. Do not add wrappers styled as cards; the one wrapper exists only for layout.

- [ ] **Step 4: Implement warm desktop/mobile CSS using existing tokens only**

```css
.trend-review-header-side { display: grid; gap: 8px; justify-items: end; }
.trend-review-comparisons { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.trend-review-comparison { margin: 0; padding: 16px; border: 1px solid var(--line); border-radius: 8px; background: var(--surface-soft); box-shadow: none; }

@media (max-width: 760px) {
  .trend-review-header { grid-template-columns: 1fr; }
  .trend-review-header-side { justify-items: stretch; }
  .trend-review-header-side button { width: 100%; }
  .trend-review-parameter-list > div { grid-template-columns: 1fr; }
  .trend-review-comparisons { grid-template-columns: 1fr; }
}
```

- [ ] **Step 5: Add failing live acceptance assertions for pairwise data, styles, geometry, and screenshots**

```python
assert workspace.locator(".trend-review-comparison").count() == 2
assert workspace.locator(".trend-review-comparison figcaption").all_inner_texts() == [
    "纪律模拟与市场", "实际执行与市场",
]
for panel in workspace.locator(".trend-review-comparison").all():
    assert panel.locator(".trend-review-metric").count() == 5
    assert panel.locator(".trend-review-series").count() == 10
assert discipline_benchmark_values == actual_benchmark_values
assert page.evaluate("document.documentElement.scrollWidth") == 375
```

Read computed `backgroundColor`, `borderColor`, `color`, and `borderRadius` for the workspace, panels, labels, and return button and compare them with `WARM_LEDGER_TOKENS`; reject any extra shadow, gradient, or non-token palette. Add exact fresh screenshot names for desktop and 375px trend review views to `ACCEPTANCE_SCREENSHOT_NAMES`.

- [ ] **Step 6: Run web and acceptance-unit tests**

Run: `.venv/bin/pytest tests/test_dashboard_web.py tests/test_dashboard_acceptance.py -q`

Expected: PASS.

- [ ] **Step 7: Commit the UI slice**

```bash
git add src/open_trader/dashboard_static/dashboard.js src/open_trader/dashboard_static/dashboard.css src/open_trader/dashboard_acceptance.py tests/test_dashboard_web.py tests/test_dashboard_acceptance.py
git commit -m "feat: render pairwise trend review comparisons"
```

### Task 6: Final Live Verification, Acceptance, and Exact-SHA Deployment

**Files:**
- Verify only: all files changed in Tasks 1-5

**Interfaces:**
- Consumes: completed feature branch.
- Produces: one accepted and redeployed Git SHA with a live review URL.

- [ ] **Step 1: Run all focused automated tests**

Run: `.venv/bin/pytest tests/test_eastmoney_parser.py tests/test_phillips_parser.py tests/test_pipeline.py tests/test_tiger_account.py tests/test_trend_review.py tests/test_a_share_trend.py tests/test_premarket_cli.py tests/test_dashboard.py tests/test_dashboard_web.py tests/test_dashboard_acceptance.py -q`

Expected: PASS with zero failures.

- [ ] **Step 2: Exercise real sources and inspect immutable outputs**

Run the configured Eastmoney and Phillips statement imports and Tiger transaction sync without fixtures, then run one close for each configured market. Verify `actual_fills`, `discipline`, `actual_equity`, and `benchmark` files contain the expected broker/market/date and that repeating the sync does not change existing bytes. Verify the projection's two benchmark series are identical and sample counts reflect real completed cycles.

- [ ] **Step 3: Inspect and restart long-running Dashboard processes**

Run: `screen -ls; launchctl list | rg -i 'open_trader|dashboard'; ps -axo pid,lstart,cwd,command | rg 'open_trader.*dashboard'`

Expected: identify every process that could retain pre-change code. Stop/restart only the project Dashboard service through its existing repository command, then verify the new PID, cwd `/Users/ray/projects/open_trader`, current Git SHA, and fresh timestamped logs.

- [ ] **Step 4: Run the final gate once**

Run: `make acceptance`

Expected: final status `PASS`. On `FAIL`, fix and rerun; on `BLOCKED`, report the blocker and do not substitute curl, mocks, fixtures, screenshots, or unit tests.

- [ ] **Step 5: Commit any acceptance-only fix, rerun the affected focused tests, and rerun the gate**

If Step 4 required a source change, commit it with its focused regression test before rerunning `make acceptance`. The final accepted SHA is:

Run: `git rev-parse HEAD`

Expected: one 40-character SHA and a clean status except the user's pre-existing untracked files.

- [ ] **Step 6: Redeploy the exact accepted SHA and verify the review URL**

Restart the Dashboard without changing source or data, then run:

```bash
git rev-parse HEAD
ps -axo pid,lstart,cwd,command | rg 'open_trader.*dashboard'
tail -n 80 /tmp/open_trader_dashboard_8766.log
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8766/
```

Expected: SHA equals Step 5, PID is new, cwd is the project root, logs are newer than the restart and contain no traceback, and HTTP status is `200`.

- [ ] **Step 7: Hand off only the accepted result**

Report the focused test count, `make acceptance: PASS`, accepted/redeployed SHA, PID, cwd, fresh-log timestamp, and clickable review URL. Do not call the feature complete if any live requirement is missing.
