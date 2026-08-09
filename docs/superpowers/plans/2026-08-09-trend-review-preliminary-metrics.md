# Trend Review Preliminary Metrics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep Kelly sample progress independent while restoring each market's existing continuous daily NAV and benchmark metrics before 30 closed rounds.

**Architecture:** `trend_api_stats.json` remains the sole source for Kelly sample counts and eligibility. `build_trend_review_projection` will derive performance cutoffs from validated daily NAV fact maps, while continuing to use the existing strategy-identity selection only for the displayed snapshot and Kelly disposition.

**Tech Stack:** Python 3.12, pytest, immutable JSON fact streams, existing Dashboard acceptance workflow.

## Global Constraints

- The Kelly minimum remains exactly 30 eligible simulation rounds.
- Do not recalculate Kelly statistics as part of report or projection generation.
- Do not synthesize actual NAV from simulated NAV, fills, or closed-round returns.
- Do not add API fields, dependencies, services, launchd labels, schedules, or UI layout changes.
- Preserve immutable daily facts; only the rebuildable latest projection may be replaced.
- Run `make acceptance` only once as the final Dashboard gate; only `PASS` permits completion.

## File Map

- Modify `src/open_trader/trend_review.py`: decouple daily performance date selection from Kelly strategy identity selection.
- Modify `tests/test_trend_review.py`: add one shared-path regression that reproduces a continuous NAV curve crossing a strategy identity boundary.
- Modify `CHANGELOG.md`: add the dated operator-facing fix entry before merge.
- Update `docs/superpowers/specs/2026-08-09-trend-review-preliminary-metrics-design.md`: record user approval.

---

### Task 1: Restore Continuous Daily Performance Curves

**Files:**
- Modify: `tests/test_trend_review.py` near the projection metric-cutoff tests
- Modify: `src/open_trader/trend_review.py:7690-7713`

**Interfaces:**
- Consumes: `build_trend_review_projection(data_dir: Path, market: str) -> dict[str, object]`, existing `discipline_by_date`, `actual_by_date`, and `benchmark_by_date` validated fact maps.
- Produces: unchanged projection v3 schema with independent `sample_counts`, `metric_cutoffs`, and `metrics`.

- [ ] **Step 1: Establish the focused baseline**

Run:

```bash
.venv/bin/pytest tests/test_trend_review.py -q
```

Expected: existing suite passes before the regression is added. If the linked worktree does not contain `.venv`, use `/Users/ray/projects/open_trader/.venv/bin/pytest`.

- [ ] **Step 2: Write the failing regression test**

Add one test after `test_projection_metric_cutoffs_are_source_specific`:

```python
def test_projection_daily_metrics_ignore_kelly_identity_boundary(
    tmp_path: Path,
) -> None:
    write_review_history(tmp_path, completed_trades=0, days=3)
    path = tmp_path / "trend_review/daily/CN/2026-07-18.json"
    fact = json.loads(path.read_text(encoding="utf-8"))
    fact["strategy_snapshot"] = live_trend_strategy_snapshot(
        "CN", "test-sha", (), strategy_version="v4"
    )
    path.write_text(json.dumps(fact), encoding="utf-8")

    projection = trend_review.build_trend_review_projection(tmp_path, "CN")

    assert projection["sample_counts"]["discipline"] is None
    assert projection["metric_cutoffs"]["discipline"] == "2026-07-18"
    assert Decimal(
        projection["metrics"]["period_net_return"]["discipline"]["value"]
    ) == Decimal("0.2")
    assert (
        projection["metrics"]["period_net_return"]
        ["discipline_benchmark"]["value"]
        is not None
    )
```

The production mutation caught by this test is reusing the Kelly-filtered `discipline_facts` list as the daily NAV date source. The literal `0.2` is hand-derived from `100000 -> 100200`.

- [ ] **Step 3: Run the regression and verify RED**

Run:

```bash
/Users/ray/projects/open_trader/.venv/bin/pytest tests/test_trend_review.py::test_projection_daily_metrics_ignore_kelly_identity_boundary -q
```

Expected: FAIL because `metric_cutoffs.discipline` is `None` and the metric remains unavailable.

- [ ] **Step 4: Implement the minimal shared-path fix**

In `build_trend_review_projection`, leave `discipline_facts`, `actual_facts`, `target_identity`, and both dispositions unchanged. Change only the two metric cutoff date inputs:

```python
    discipline_metric_cutoff = _series_cutoff(
        effective_from,
        {
            trading_date
            for trading_date, fact in discipline_by_date.items()
            if "discipline_equity_after_fees" in fact
        },
        benchmark_dates,
    )
```

For the US actual curve, make the equivalent change from filtered `actual_facts` to validated `actual_by_date`:

```python
        equity_cutoff = _series_cutoff(
            effective_from,
            {
                trading_date
                for trading_date, fact in actual_by_date.items()
                if "actual_equity" in fact
            },
            benchmark_dates,
        )
```

Do not add a helper: the two comprehensions are the existing interface and keep the change local.

- [ ] **Step 5: Verify GREEN and compatibility**

Run:

```bash
/Users/ray/projects/open_trader/.venv/bin/pytest tests/test_trend_review.py::test_projection_daily_metrics_ignore_kelly_identity_boundary -q
/Users/ray/projects/open_trader/.venv/bin/pytest tests/test_trend_review.py -q
/Users/ray/projects/open_trader/.venv/bin/pytest tests/test_dashboard.py tests/test_dashboard_web.py -q
git diff --check
```

Expected: all commands exit 0. Existing tests must continue proving that actual metrics stay unavailable without actual NAV and that Kelly sample counts remain independent.

- [ ] **Step 6: Commit the behavior fix**

```bash
git add src/open_trader/trend_review.py tests/test_trend_review.py
git commit -m "fix: restore preliminary trend review metrics"
```

---

### Task 2: Verify Real Data and Prepare the Release

**Files:**
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: the unchanged `build_trend_review_projection` public function and production data copied to a temporary directory.
- Produces: operator-visible changelog entry and verified projection values without mutating live data during development.

- [ ] **Step 1: Run the direct workflow against a temporary production-data copy**

Create a narrow temporary data directory, preserving only the inputs needed by the projection:

```bash
review_tmp=$(mktemp -d)
mkdir -p "$review_tmp/latest"
cp -R /Users/ray/projects/open_trader/data/trend_review "$review_tmp/"
cp -R /Users/ray/projects/open_trader/data/rates "$review_tmp/"
cp /Users/ray/projects/open_trader/data/latest/trend_api_stats.json "$review_tmp/latest/"
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python - "$review_tmp" <<'PY'
import json
import sys
from decimal import Decimal
from pathlib import Path
from open_trader.trend_review import build_trend_review_projection

root = Path(sys.argv[1])
for market in ("CN", "HK", "US"):
    projection = build_trend_review_projection(root, market)
    discipline = {
        name: values["discipline"]
        for name, values in projection["metrics"].items()
    }
    benchmark = {
        name: values["discipline_benchmark"]
        for name, values in projection["metrics"].items()
    }
    assert projection["metric_cutoffs"]["discipline"] is not None
    assert discipline["period_net_return"]["value"] is not None
    assert benchmark["period_net_return"]["value"] is not None
    print(json.dumps({
        "market": market,
        "sample_count": projection["sample_counts"]["discipline"],
        "cutoff": projection["metric_cutoffs"]["discipline"],
        "discipline": discipline,
        "benchmark": benchmark,
    }, ensure_ascii=False))
PY
```

Expected: CN/HK/US each print a non-null discipline cutoff and non-null period return for both simulation and benchmark. After inspection, remove only the validated temporary directory:

```bash
case "$review_tmp" in
  /var/folders/*|/tmp/*) rm -rf -- "$review_tmp" ;;
  *) echo "refusing unsafe cleanup: $review_tmp" >&2; exit 1 ;;
esac
```

- [ ] **Step 2: Add the dated changelog entry**

Under the current `2026-08-09` section, add one operator-facing bullet:

```markdown
- 修复趋势复盘把 Kelly 策略版本边界误用于连续日终净值的问题：未满 30 笔仍显示已有模拟盘与同期市场绩效，30 笔门槛只控制 Kelly 启用；实盘缺少日终净值时继续明确不可用。
```

- [ ] **Step 3: Verify and commit the release record**

```bash
git diff --check
git status --short
git add CHANGELOG.md docs/superpowers/specs/2026-08-09-trend-review-preliminary-metrics-design.md
git add -f docs/superpowers/plans/2026-08-09-trend-review-preliminary-metrics.md
git commit -m "docs: record trend review metrics fix"
```

---

### Task 3: Review, Merge, Accept, and Deploy

**Files:**
- No new source files.

**Interfaces:**
- Consumes: the complete branch diff from `main`.
- Produces: reviewed local `main`, final acceptance status, and exact-SHA live Dashboard processes.

- [ ] **Step 1: Review the branch diff**

Run a standards-and-spec review over:

```bash
git diff main...HEAD -- src/open_trader/trend_review.py tests/test_trend_review.py CHANGELOG.md docs/superpowers/specs/2026-08-09-trend-review-preliminary-metrics-design.md docs/superpowers/plans/2026-08-09-trend-review-preliminary-metrics.md
```

Resolve every P1/P2 finding with a fresh failing regression where behavior changes. Re-run Task 1 Step 5 after any source edit.

- [ ] **Step 2: Merge into local main**

Verify the main checkout remains clean, then merge the reviewed branch non-interactively. The changelog commit must already be present before this step.

- [ ] **Step 3: Run the final Dashboard gate once**

From `/Users/ray/projects/open_trader` on merged `main`:

```bash
make acceptance
```

Expected terminal status: `PASS`. A `FAIL` must be diagnosed and fixed; a `BLOCKED` must be reported as blocked.

- [ ] **Step 4: Redeploy the exact accepted SHA and rebuild projections**

Use the repository's existing frontend/controller installers. Do not add or start a separate statistics service. After the exact accepted SHA is live, rebuild each market's projection once through the existing projection workflow; do not force or rerun `trend-api-stats`.

- [ ] **Step 5: Verify live state**

Record the accepted Git SHA and verify:

```bash
curl -fsS http://127.0.0.1:8766/api/dashboard
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8766/
```

The API must show existing Kelly counts independently, a non-null simulation and benchmark period return for CN/HK/US, truthful actual-NAV reasons where unavailable, exact-SHA process metadata, fresh logs, and HTTP 200. Also inspect controller/frontend PIDs, working directories, and Git SHA per `AGENTS.md`.
