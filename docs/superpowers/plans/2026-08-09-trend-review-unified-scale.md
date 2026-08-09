# Trend Review Unified Scale Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the duplicated Trend Review comparison cards with one truthful shared-scale matrix and add independently refreshed 1-year and 5-year benchmarks for CSI 500, Hang Seng Index, and SPY.

**Architecture:** Keep all market calculations in the existing `trend_review` domain and reuse `FutuQuoteClient.get_daily_kline`, `_portfolio_metrics`, `_annualized_sharpe`, DGS3MO, and atomic JSON writes. Store one validated long-term snapshot plus one monthly attempt state per market; the Controller and explicit CLI call the same refresh function, while the Dashboard only validates and renders projection v4. Report generation, Kelly statistics, and benchmark refresh remain sibling workflows with independent failures.

**Tech Stack:** Python 3 standard library, existing Futu OpenD client, pytest, server-rendered Dashboard JSON, vanilla JavaScript and CSS.

## Global Constraints

- Use `CN = SH.000905 / CSI_500_PRICE`, `HK = HK.800000 / HSI_PRICE`, and `US = US.SPY / SPY_QFQ` for same-period, 1-year, and 5-year market data.
- Do not add a launchd job, scheduler, database, data provider, frontend library, or Python dependency.
- Refresh each market at most once per calendar month during an ordinary Controller cycle; preserve the last successful snapshot on failure.
- Report generation, Kelly/statistics refresh, and long-term benchmark refresh must never call or block one another.
- Regenerating a report must not recompute statistics or long-term benchmarks.
- Only an explicit force command with actor and reason may bypass the monthly benchmark idempotence check.
- Kelly's 30-round threshold remains independent from continuous portfolio metrics.
- Never substitute simulation values for missing actual daily equity.
- Dashboard is read-only and must not fetch prices, write snapshots, or calculate ratios.
- Preserve the existing paper surface, brown rules, typography, and Chinese copy; do not rely on color alone and do not introduce mobile horizontal scrolling.

---

### Task 1: Current benchmark identity and durable monthly snapshot

**Files:**
- Modify: `src/open_trader/trend_review.py:30-118,289-307,6711-6730,7340-7465`
- Test: `tests/test_trend_review.py`

**Interfaces:**
- Consumes: `FutuQuoteClient.get_daily_kline(futu_symbol, start, end)`, `_load_dgs3mo_csv`, `_portfolio_metrics`, `_write_json_atomic`, `_canonical_json_bytes`.
- Produces: `BENCHMARK_IDENTITIES`, `long_term_benchmark_snapshot_path(data_dir, market)`, `long_term_benchmark_cycle_path(data_dir, market, month)`, `read_long_term_benchmark_snapshot(data_dir, market)`, and `refresh_long_term_benchmark(data_dir, market, quote, *, now, process_git_sha, force=False, actor="", reason="") -> dict[str, object]`.

- [ ] **Step 1: Write failing identity and calculation tests**

```python
class FiveYearQuote:
    def __init__(self, symbol: str = "US.SPY") -> None:
        self.symbol = symbol

    def get_daily_kline(self, symbol: str, *, start: str, end: str) -> list[object]:
        assert symbol == self.symbol
        closes = ("100", "108", "102", "121", "117", "150")
        years = range(2021, 2027)
        return [
            SimpleNamespace(date=f"{year}-08-08", close=close)
            for year, close in zip(years, closes, strict=True)
        ]


class ExplodingQuote:
    def get_daily_kline(self, *_args: object, **_kwargs: object) -> list[object]:
        raise ValueError("quote failed")


def write_rates(root: Path) -> None:
    path = root / "rates/DGS3MO.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("DATE,DGS3MO\n2021-08-08,4.0\n", encoding="utf-8")


@pytest.mark.parametrize(
    ("market", "source_id", "symbol"),
    [
        ("CN", "CSI_500_PRICE", "SH.000905"),
        ("HK", "HSI_PRICE", "HK.800000"),
        ("US", "SPY_QFQ", "US.SPY"),
    ],
)
def test_long_term_benchmark_uses_approved_market_identity(
    tmp_path: Path, market: str, source_id: str, symbol: str
) -> None:
    write_rates(tmp_path)
    quote = FiveYearQuote(symbol=symbol)
    result = trend_review.refresh_long_term_benchmark(
        tmp_path,
        market,
        quote,
        now=datetime(2026, 8, 9, 12, tzinfo=UTC),
        process_git_sha="abc123",
    )
    snapshot = json.loads(
        trend_review.long_term_benchmark_snapshot_path(tmp_path, market)
        .read_text(encoding="utf-8")
    )
    assert result["status"] == "completed"
    assert snapshot["benchmark"] == {
        "name": trend_review.BENCHMARK_IDENTITIES[market]["name"],
        "source_id": source_id,
        "futu_symbol": symbol,
    }
    assert set(snapshot["windows"]) == {"1Y", "5Y"}
    assert Decimal(snapshot["windows"]["5Y"]["metrics"]["annualized_return_pct"]).is_finite()
```

Also assert deterministic maximum drawdown, Calmar, Sharpe, cutoff, observation count, and DGS3MO-adjusted daily returns from a hand-built QFQ series.

- [ ] **Step 2: Run the tests and confirm the current mapping fails**

Run: `pytest tests/test_trend_review.py -k 'long_term_benchmark or approved_market_identity' -v`

Expected: FAIL because CN/HK still use `SH.000985` and `HK.800701`, and no long-term snapshot API exists.

- [ ] **Step 3: Add the minimum benchmark identity and snapshot implementation**

Use one current mapping as the source of truth:

```python
BENCHMARK_IDENTITIES = {
    "CN": {"name": "中证 500", "source_id": "CSI_500_PRICE", "futu_symbol": "SH.000905"},
    "HK": {"name": "恒生指数", "source_id": "HSI_PRICE", "futu_symbol": "HK.800000"},
    "US": {"name": "S&P 500 ETF", "source_id": "SPY_QFQ", "futu_symbol": "US.SPY"},
}
```

Retain a separate exact legacy mapping only for reading already frozen `CSI_ALL_SHARE_PRICE / SH.000985` and `HSCI_PRICE / HK.800701` daily facts. New `benchmark_fact` and `freeze_benchmark_fact` calls accept only the current mapping; projection v4 never uses legacy closes for market metrics. Add a regression proving old immutable facts remain audit-readable while all new and projected market values use the approved identity.

Fetch one five-year-plus daily QFQ series through the existing paginated quote method. Validate strictly increasing unique dates, positive finite closes, exact identity, aware timestamps, and nonempty 1Y/5Y windows. Store the validated daily closes in the snapshot so the same identity can also supply same-period projections without rewriting immutable legacy facts. Calculate window metrics once and atomically replace the snapshot only after the entire payload validates.

- [ ] **Step 4: Add monthly idempotence and failure preservation tests**

```python
def test_long_term_refresh_is_monthly_and_failure_preserves_snapshot(tmp_path: Path) -> None:
    write_rates(tmp_path)
    first = trend_review.refresh_long_term_benchmark(
        tmp_path, "US", FiveYearQuote(),
        now=datetime(2026, 8, 9, tzinfo=UTC), process_git_sha="first",
    )
    body = trend_review.long_term_benchmark_snapshot_path(tmp_path, "US").read_bytes()
    skipped = trend_review.refresh_long_term_benchmark(
        tmp_path, "US", ExplodingQuote(),
        now=datetime(2026, 8, 20, tzinfo=UTC), process_git_sha="second",
    )
    failed = trend_review.refresh_long_term_benchmark(
        tmp_path, "US", ExplodingQuote(),
        now=datetime(2026, 9, 1, tzinfo=UTC), process_git_sha="third",
    )
    assert first["status"] == "completed"
    assert skipped["status"] == "already_completed"
    assert failed["status"] == "failed"
    assert trend_review.long_term_benchmark_snapshot_path(tmp_path, "US").read_bytes() == body
```

Add a forced-run test requiring nonempty actor and reason and proving a failed force does not erase the completed monthly state or successful snapshot.

- [ ] **Step 5: Run focused tests and commit**

Run: `pytest tests/test_trend_review.py -k 'benchmark or portfolio_metrics' -v`

Expected: PASS.

```bash
git add src/open_trader/trend_review.py tests/test_trend_review.py
git commit -m "feat: add durable long-term trend benchmarks"
```

### Task 2: Projection v4 and credible metric boundaries

**Files:**
- Modify: `src/open_trader/trend_review.py:7340-7897`
- Modify: `src/open_trader/dashboard.py:157-168,548-813`
- Test: `tests/test_trend_review.py`
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Consumes: `read_long_term_benchmark_snapshot`, existing discipline and actual daily-equity curves, existing statistics overlay.
- Produces: `open_trader.trend_review.projection.v4` with metric series `discipline`, `actual`, `same_period_benchmark`, `market_1y`, and `market_5y`, plus `benchmark_context` and `benchmark_refresh` metadata.

- [ ] **Step 1: Write failing projection tests**

```python
def test_projection_v4_uses_one_benchmark_identity_for_all_windows(tmp_path: Path) -> None:
    write_projection_metric_history(
        tmp_path, "CN", discipline_days=4, actual_days=0, benchmark_days=4
    )
    write_rates(tmp_path)
    trend_review.refresh_long_term_benchmark(
        tmp_path, "CN", FiveYearQuote(symbol="SH.000905"),
        now=datetime(2026, 8, 9, tzinfo=UTC), process_git_sha="abc123",
    )
    projection = trend_review.build_trend_review_projection(tmp_path, "CN")
    assert projection["schema_version"] == "open_trader.trend_review.projection.v4"
    assert projection["benchmark_context"]["futu_symbol"] == "SH.000905"
    assert set(projection["metrics"]["period_net_return"]) == {
        "discipline", "actual", "same_period_benchmark", "market_1y", "market_5y",
    }
    assert projection["metrics"]["market_excess_return"]["market_1y"] == {
        "value": None, "reason": "基准自身",
    }
```

Add cases for HK/US identity, actual equity missing, corrupt/missing long-term snapshot, and retained simulation metrics when only the benchmark is unavailable.

- [ ] **Step 2: Write the short-window ratio regression**

```python
def test_projection_does_not_annualize_ratios_before_one_full_year(tmp_path: Path) -> None:
    write_projection_metric_history(
        tmp_path, "US", discipline_days=21, actual_days=0, benchmark_days=21
    )
    write_rates(tmp_path)
    trend_review.refresh_long_term_benchmark(
        tmp_path, "US", FiveYearQuote(symbol="US.SPY"),
        now=datetime(2026, 8, 9, tzinfo=UTC), process_git_sha="abc123",
    )
    projection = trend_review.build_trend_review_projection(tmp_path, "US")
    for metric in ("calmar", "sharpe"):
        assert projection["metrics"][metric]["discipline"] == {
            "value": None, "reason": "观察期不足",
        }
        assert projection["metrics"][metric]["same_period_benchmark"] == {
            "value": None, "reason": "观察期不足",
        }
```

- [ ] **Step 3: Run the projection tests and confirm they fail**

Run: `pytest tests/test_trend_review.py tests/test_dashboard.py -k 'projection_v4 or annualize_ratios or long_term_benchmark' -v`

Expected: FAIL because projection v3 has four series and accepts short-window annualized ratios.

- [ ] **Step 4: Implement projection v4 without Dashboard-side calculation**

Build same-period benchmark metrics from the long-term snapshot's validated daily closes over each strategy window. Keep the actual-specific benchmark curve internal for actual excess-return calculation, but expose the discipline-window benchmark as the single `same_period_benchmark` display series with its dates in `benchmark_context`. Expose precomputed 1Y/5Y cells from the snapshot; use `5Y.metrics.annualized_return_pct` for the return row and label its return basis as `CAGR` in metadata.

Apply the one-full-year gate before returning Calmar or Sharpe cells:

```python
if (dates[-1] - dates[0]).days < 365:
    values["calmar_ratio"] = None
    values["sharpe_ratio"] = None
    ratio_reason = "观察期不足"
```

Do not gate period return, excess return, or maximum drawdown. Update Dashboard validation to require exact v4 keys and reject malformed identities, windows, values, dates, refresh states, and extra controls.

- [ ] **Step 5: Run focused and compatibility tests, then commit**

Run: `pytest tests/test_trend_review.py tests/test_dashboard.py -k 'trend_review or benchmark or portfolio_metrics' -v`

Expected: PASS.

```bash
git add src/open_trader/trend_review.py src/open_trader/dashboard.py tests/test_trend_review.py tests/test_dashboard.py
git commit -m "feat: project unified trend benchmark metrics"
```

### Task 3: Independent Controller and explicit force command

**Files:**
- Modify: `src/open_trader/trend_market_controller.py:65-91,733-781,3270-3360`
- Modify: `src/open_trader/cli.py:453-482,1848-1908`
- Test: `tests/test_trend_market_controller.py`
- Test: `tests/test_trend_api_stats_cli.py`

**Interfaces:**
- Consumes: `refresh_long_term_benchmark`, the Controller's existing shared `FutuQuoteClient`, current process SHA, and market-local current time.
- Produces: `_run_cycle_long_term_benchmark`, `_record_long_term_benchmark_exception`, ordinary monthly refresh in the Controller, and `open-trader trend-review refresh-benchmark --market ... [--force --actor ... --reason ...]`.

- [ ] **Step 1: Write failing Controller independence tests**

```python
def test_controller_report_statistics_and_benchmark_fail_independently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = controller_config(tmp_path)
    patch_cycle(monkeypatch, active_cn_cycle())
    calls = []
    monkeypatch.setattr(controller, "_run_cycle_statistics", lambda *args: calls.append("statistics") or {"status": "completed"})
    monkeypatch.setattr(controller, "_run_cycle_long_term_benchmark", lambda *args: (_ for _ in ()).throw(RuntimeError("quote failed")))
    monkeypatch.setattr(controller, "_generate_report", lambda *args: calls.append("report") or write_report(config))
    monkeypatch.setattr(controller, "_execution_due", lambda *_args: False)
    run_trend_market_controller(
        config, "CN", once=True, now_fn=lambda: NOW
    )
    assert calls == ["statistics", "report"]
    state = json.loads(
        trend_review.long_term_benchmark_cycle_path(
            config.data_dir, "CN", "2026-07"
        ).read_text(encoding="utf-8")
    )
    assert state["status"] == "failed"
```

Add inverse cases where report generation fails but benchmark completes, statistics fails but benchmark/report complete, revisions never trigger statistics or benchmark refresh, and an already-completed month skips quote client construction.

- [ ] **Step 2: Write failing CLI force-audit tests**

```python
def test_refresh_benchmark_force_requires_actor_and_reason(capsys) -> None:
    result = main(["trend-review", "refresh-benchmark", "--market", "HK", "--force"])
    assert result == 1
    assert "--force requires --actor and --reason" in capsys.readouterr().err
```

Also assert an ordinary command cannot redo a completed month and a forced attempt records actor, reason, attempted time, and process SHA without invoking statistics or report functions.

- [ ] **Step 3: Run the new tests and confirm they fail**

Run: `pytest tests/test_trend_market_controller.py tests/test_trend_api_stats_cli.py -k 'long_term_benchmark or refresh_benchmark or independently' -v`

Expected: FAIL because no Controller or CLI path exists.

- [ ] **Step 4: Add one sibling Controller step and one CLI subcommand**

Create `_run_cycle_long_term_benchmark(config, cycle, now, process_version, quote_client)` as a thin adapter. In an ordinary, non-revision, current market cycle, call it in its own `try/except` independently from `_run_cycle_statistics` and report generation. `_record_long_term_benchmark_exception` atomically writes the failed monthly attempt state without touching the successful snapshot. The refresh function itself owns monthly idempotence, so the Controller does not duplicate date logic. Reuse `shared_quote()`; do not create a new client, thread, timer, or retry service.

Add the CLI parser and call the same domain function with a normal `FutuQuoteClient`. Reject `--force` unless actor and reason are nonempty; close the quote client in `finally`.

- [ ] **Step 5: Run Controller and CLI suites, then commit**

Run: `pytest tests/test_trend_market_controller.py tests/test_trend_api_stats_cli.py -v`

Expected: PASS.

```bash
git add src/open_trader/trend_market_controller.py src/open_trader/cli.py tests/test_trend_market_controller.py tests/test_trend_api_stats_cli.py
git commit -m "feat: refresh trend benchmarks monthly"
```

### Task 4: Single shared-scale Dashboard matrix

**Files:**
- Modify: `src/open_trader/dashboard_static/dashboard.js:3940-4055`
- Modify: `src/open_trader/dashboard_static/dashboard.css:1438-1565,5168-5188`
- Test: `tests/test_dashboard_web.py`
- Test: `tests/test_dashboard_acceptance.py`

**Interfaces:**
- Consumes: projection v4 metric cells and `benchmark_context`/`benchmark_refresh` already validated by `dashboard.py`.
- Produces: one `.trend-review-matrix` containing five metric rows, one shared numeric axis per row, five directly labelled series, and responsive text fallback.

- [ ] **Step 1: Replace the old two-card assertions with failing matrix assertions**

```javascript
const html = renderTrendReviewWorkspace(review);
if ((html.match(/class="trend-review-matrix"/g) || []).length !== 1) throw new Error(html);
if ((html.match(/class="trend-review-metric"/g) || []).length !== 5) throw new Error(html);
for (const label of ["纪律模拟", "实际执行", "同期市场", "市场 1 年", "市场 5 年"]) {
  if (!html.includes(label)) throw new Error(label);
}
if (!html.includes("观察期不足") || !html.includes("基准自身")) throw new Error(html);
```

Add assertions that each metric has one axis, all numeric points use one row domain, exact values are visible, shape classes differ, refresh cutoff is visible, and no value is encoded by color alone.

- [ ] **Step 2: Add failing 1440 px and 375 px geometry checks**

Require one matrix on desktop, no two-column comparison cards, no horizontal overflow at 375 px, readable direct labels, and at least 4.5:1 text contrast / 3:1 chart-marker contrast using the existing acceptance style probes.

Run: `pytest tests/test_dashboard_web.py tests/test_dashboard_acceptance.py -k 'trend_review' -v`

Expected: FAIL on the old two-card structure and geometry contract.

- [ ] **Step 3: Implement the minimum HTML and CSS redesign**

Replace `TREND_REVIEW_COMPARISONS` and `renderTrendReviewComparison` with one five-series definition and one row renderer. For each row, compute a numeric domain from all available cells, always include zero for signed metrics, and render CSS-positioned point markers plus a visible five-column value list. Missing cells render their reason and no marker. Use the approved shapes and existing `--accent`, `--primary`, `--surface-soft`, `--line`, and `--muted` tokens; add no chart library.

At the mobile breakpoint, keep the axis full width and stack the labelled value list below it. Use semantic headings/list markup and `aria-label` text containing series, metric, value, and window.

- [ ] **Step 4: Run Dashboard tests and commit**

Run: `pytest tests/test_dashboard_web.py tests/test_dashboard_acceptance.py -k 'trend_review' -v`

Expected: PASS.

```bash
git add src/open_trader/dashboard_static/dashboard.js src/open_trader/dashboard_static/dashboard.css tests/test_dashboard_web.py tests/test_dashboard_acceptance.py
git commit -m "feat: unify trend review metric scale"
```

### Task 5: Real-data verification, release log, and final Dashboard gate

**Files:**
- Modify: `CHANGELOG.md`
- Verify: `data/trend_review/`, `data/latest/trend_review_cn.json`, `data/latest/trend_review_hk.json`, `data/latest/trend_review_us.json`

**Interfaces:**
- Consumes: Tasks 1-4 and existing installer/runtime scripts.
- Produces: three validated live benchmark snapshots, final accepted Git SHA, and exact-SHA Dashboard deployment.

- [ ] **Step 1: Run all focused suites**

Run:

```bash
pytest tests/test_trend_review.py tests/test_trend_market_controller.py tests/test_trend_api_stats_cli.py tests/test_dashboard.py tests/test_dashboard_web.py tests/test_dashboard_acceptance.py -v
```

Expected: all tests PASS with no new warnings.

- [ ] **Step 2: Run one direct real-data refresh per market**

Run:

```bash
open-trader trend-review refresh-benchmark --market CN --config config/daily_premarket.env
open-trader trend-review refresh-benchmark --market HK --config config/daily_premarket.env
open-trader trend-review refresh-benchmark --market US --config config/daily_premarket.env
```

Check exact source identity, QFQ cutoff, 1Y/5Y start/end, observation counts, finite metrics, atomic snapshot paths, and monthly completed states. Rebuild the three projections and verify short-window ratios say “观察期不足” while 1Y/5Y metrics remain numeric.

- [ ] **Step 3: Prove the three workflows are independent in a direct Controller run**

Run the normal Controller command once for CN/HK/US. Confirm fresh logs show separate report, statistics, and long-term benchmark outcomes; a deliberately pre-completed monthly benchmark must skip without changing its snapshot hash. Inspect process PID, working directory, Git SHA, and timestamps before making a live claim.

- [ ] **Step 4: Update the merge log and commit**

Add a dated operator-facing `CHANGELOG.md` entry naming the unified matrix, approved benchmark identities, monthly refresh, short-window ratio suppression, and independent failure behavior.

```bash
git add CHANGELOG.md
git commit -m "docs: log unified trend review benchmarks"
```

- [ ] **Step 5: Run the final acceptance gate once**

Run: `make acceptance`

Expected: terminal `PASS`. `FAIL` must be fixed and rerun; `BLOCKED` must be reported as blocked without substituting mocks or curl.

- [ ] **Step 6: Deploy the exact accepted SHA and verify runtime**

Use the repository's existing Dashboard installer for the accepted commit. Verify the new PID, working directory, exact Git SHA, fresh log timestamp, and HTTP 200 from `http://127.0.0.1:8766/`. Do not rerun acceptance when only restarting the exact accepted SHA with no source or data changes.
