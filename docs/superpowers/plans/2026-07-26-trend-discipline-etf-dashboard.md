# Trend Discipline ETF Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the duplicate strategy-parameter surface from trend reviews and make the Dashboard's current discipline use the configured CN/US/HK stock-and-ETF candidate pools.

**Architecture:** Keep report-frozen parameters immutable and add the current candidate-pool tuples to `DashboardConfig`, populated by the existing env parsers. Current and historical report projections use those configured tuples to build `current_strategy_parameter_rows`; when the tuples are absent, they expose no current rows so the existing frontend falls back honestly to the report-frozen discipline.

**Tech Stack:** Python 3.12, dataclasses, pytest, vanilla JavaScript/CSS, Node VM renderer checks, Playwright Dashboard acceptance.

## Global Constraints

- Do not change trend strategy rules, pool resolution, discipline versions, Kelly identity, or historical report JSON.
- Keep `daily_premarket.env` as the single source for fixed candidate-pool IDs; do not duplicate the IDs in Dashboard implementation code.
- CN current pools combine the configured A-share and ETF IDs; US/HK use their configured ID lists.
- HK `707617` remains a stable root whose child resolution stays in the existing report-generation flow.
- If current pool configuration is absent, show report-frozen discipline and do not label historical pools as current.
- Do not add dependencies or abstractions beyond the three tuple fields and their market lookup.
- Run `make acceptance` only once the implementation, focused checks, direct workflow, full tests, changelog, and commits are complete.
- Only `make acceptance` `PASS` permits review handoff; after `PASS`, redeploy the exact accepted Git SHA and verify PID, working directory, SHA, fresh logs, and HTTP 200.

---

## File Map

- `src/open_trader/dashboard.py`: owns `DashboardConfig`, configured-pool lookup, current and historical trend-report projection.
- `src/open_trader/cli.py`: parses the existing candidate-pool env keys into `DashboardConfig`.
- `src/open_trader/dashboard_web.py`: passes the full Dashboard config into historical-report projection.
- `src/open_trader/dashboard_static/dashboard.js`: renders the trend-review workspace without duplicate parameters.
- `src/open_trader/dashboard_static/dashboard.css`: removes styles that only served the deleted parameter table.
- `src/open_trader/dashboard_acceptance.py`: accepts the smaller review DOM and keeps desktop/mobile metric checks.
- `tests/test_dashboard_cli.py`: proves env configuration reaches the Dashboard config.
- `tests/test_dashboard.py`: proves old report pools do not replace current configured pools and proves fail-closed fallback.
- `tests/test_dashboard_web.py`: locks down the parameter-free trend-review renderer.
- `tests/test_dashboard_acceptance.py`: updates browser fakes and acceptance contracts for the deleted DOM.
- `CHANGELOG.md`: records the operator-visible Dashboard correction.

---

### Task 1: Project configured candidate pools into current discipline

**Files:**

- Modify: `src/open_trader/dashboard.py:172-182, 230-350, 643-675, 776-822, 1693-1755, 1918-1970`
- Modify: `src/open_trader/cli.py:20-35, 2470-2510`
- Modify: `src/open_trader/dashboard_web.py:345-375`
- Test: `tests/test_dashboard_cli.py`
- Test: `tests/test_dashboard.py`
- Test: `tests/test_dashboard_web.py`

**Interfaces:**

- Produces: `DashboardConfig.trend_cn_candidate_pool_ids: tuple[int, ...]`.
- Produces: `DashboardConfig.trend_us_candidate_pool_ids: tuple[int, ...]`.
- Produces: `DashboardConfig.trend_hk_candidate_pool_ids: tuple[int, ...]`.
- Produces: `DashboardConfig.trend_candidate_pool_ids(market: str) -> tuple[int, ...]`.
- Changes: `load_historical_trend_report(config: DashboardConfig, *, broker: str, artifact: str) -> dict[str, Any]`.
- Changes: `_load_trend_reports(..., current_candidate_pool_ids: Mapping[str, tuple[int, ...]] | None = None)`.
- Changes: `_project_broker_trend_report(..., current_candidate_pool_ids: tuple[int, ...] = ())`.
- Consumers: trend report rendering continues to receive `strategy_parameter_rows`, `current_strategy_version`, and `current_strategy_parameter_rows`.

- [ ] **Step 1: Add failing CLI configuration assertions**

Extend `test_dashboard_main_delegates_to_server` so its env file contains:

```python
"TREND_ANIMALS_WARM_TO_HOT_A_SHARE_TM_ID=622466\n"
"TREND_ANIMALS_WARM_TO_HOT_ETF_TM_ID=697199\n"
"TREND_ANIMALS_WARM_TO_HOT_US_TM_IDS=622460,705013\n"
"TREND_ANIMALS_WARM_TO_HOT_HK_TM_IDS=622494,707617\n"
```

Then assert:

```python
assert config.trend_cn_candidate_pool_ids == (622466, 697199)
assert config.trend_us_candidate_pool_ids == (622460, 705013)
assert config.trend_hk_candidate_pool_ids == (622494, 707617)
assert config.trend_candidate_pool_ids("CN") == (622466, 697199)
assert config.trend_candidate_pool_ids("US") == (622460, 705013)
assert config.trend_candidate_pool_ids("HK") == (622494, 707617)
```

- [ ] **Step 2: Add failing projection tests for old US/HK reports**

Extend `_dashboard_frozen_report_payload` to accept `market`, `broker`, and
`candidate_pool_ids`, while preserving its current defaults. Add:

```python
@pytest.mark.parametrize(
    ("market", "broker", "directory", "frozen_ids", "current_ids"),
    [
        ("US", "tiger", "trend_us_tiger", (622460,), (622460, 705013)),
        ("HK", "phillips", "trend_hk_phillips", (622494,), (622494, 707617)),
    ],
)
def test_dashboard_current_discipline_uses_configured_etf_pools_for_old_reports(
    tmp_path: Path,
    market: str,
    broker: str,
    directory: str,
    frozen_ids: tuple[int, ...],
    current_ids: tuple[int, ...],
) -> None:
    config = dashboard_config(tmp_path)
    payload = _dashboard_frozen_report_payload(
        market=market,
        broker=broker,
        candidate_pool_ids=frozen_ids,
    )
    path = config.reports_dir / directory / "2026-07-15.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    report = dashboard_module._load_trend_reports(
        config.data_dir,
        config.reports_dir,
        today=date(2026, 7, 15),
        current_candidate_pool_ids={market: current_ids},
    )[broker]

    source = next(
        row["value"]
        for row in report["current_strategy_parameter_rows"]
        if row["name"] == "趋势动物组合"
    )
    assert source == "、".join(str(pool_id) for pool_id in current_ids)
    frozen_source = next(
        row["value"]
        for row in report["strategy_parameter_rows"]
        if row["name"] == "趋势动物组合"
    )
    assert frozen_source == "、".join(str(pool_id) for pool_id in frozen_ids)
```

Add the fail-closed test:

```python
def test_dashboard_does_not_label_frozen_pools_as_current_without_config(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    payload = _dashboard_frozen_report_payload(
        market="US",
        broker="tiger",
        candidate_pool_ids=(622460,),
    )
    path = config.reports_dir / "trend_us_tiger/2026-07-15.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    report = dashboard_module._load_trend_reports(
        config.data_dir,
        config.reports_dir,
        today=date(2026, 7, 15),
        current_candidate_pool_ids={},
    )["tiger"]

    assert report["current_strategy_version"] == ""
    assert report["current_strategy_parameter_rows"] is None
    assert report["strategy_parameter_rows"]
```

- [ ] **Step 3: Run the new tests and verify red**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_dashboard_cli.py::test_dashboard_main_delegates_to_server \
  tests/test_dashboard.py::test_dashboard_current_discipline_uses_configured_etf_pools_for_old_reports \
  tests/test_dashboard.py::test_dashboard_does_not_label_frozen_pools_as_current_without_config \
  -q
```

Expected: FAIL because `DashboardConfig` has no candidate-pool fields, `_load_trend_reports`
does not accept current configuration, and old report IDs are still reused.

- [ ] **Step 4: Add the minimal Dashboard config surface**

In `DashboardConfig`, add:

```python
trend_cn_candidate_pool_ids: tuple[int, ...] = ()
trend_us_candidate_pool_ids: tuple[int, ...] = ()
trend_hk_candidate_pool_ids: tuple[int, ...] = ()

def trend_candidate_pool_ids(self, market: str) -> tuple[int, ...]:
    return {
        "CN": self.trend_cn_candidate_pool_ids,
        "US": self.trend_us_candidate_pool_ids,
        "HK": self.trend_hk_candidate_pool_ids,
    }.get(market.upper(), ())
```

In the CLI, import the existing `_positive_tm_ids`. Parse the current keys once:

```python
trend_a_share_tm_id = _optional_positive_tm_id(
    config_values, "TREND_ANIMALS_WARM_TO_HOT_A_SHARE_TM_ID"
)
trend_etf_tm_id = _optional_positive_tm_id(
    config_values, "TREND_ANIMALS_WARM_TO_HOT_ETF_TM_ID"
)
trend_cn_candidate_pool_ids = (
    (trend_a_share_tm_id, trend_etf_tm_id)
    if trend_a_share_tm_id and trend_etf_tm_id
    else ()
)
```

Pass these fields into `DashboardConfig`:

```python
trend_cn_candidate_pool_ids=trend_cn_candidate_pool_ids,
trend_us_candidate_pool_ids=_positive_tm_ids(
    config_values.get("TREND_ANIMALS_WARM_TO_HOT_US_TM_IDS", "")
),
trend_hk_candidate_pool_ids=_positive_tm_ids(
    config_values.get("TREND_ANIMALS_WARM_TO_HOT_HK_TM_IDS", "")
),
```

- [ ] **Step 5: Thread the configured pools through current and historical projection**

Have `load_dashboard_state` pass:

```python
current_candidate_pool_ids={
    market: config.trend_candidate_pool_ids(market)
    for market in ("CN", "US", "HK")
},
```

Add the mapping argument to `_load_trend_reports`; pass the matching market tuple through
`_load_broker_trend_report` to `_project_broker_trend_report`.

Replace the old report-derived current pool selection in `_project_broker_trend_report`
with:

```python
current_strategy_snapshot = (
    live_trend_strategy_snapshot(
        market,
        str(strategy_snapshot.get("process_version") or ""),
        current_candidate_pool_ids,
    )
    if isinstance(strategy_snapshot, dict) and current_candidate_pool_ids
    else {}
)
current_parameter_rows = (
    current_strategy_snapshot.get("parameter_rows")
    if current_strategy_snapshot
    else None
)
```

Return `current_parameter_rows` directly. Do not change `strategy_parameter_rows`.

Change `load_historical_trend_report` to accept `DashboardConfig`, use
`config.data_dir`, `config.reports_dir`, and pass
`config.trend_candidate_pool_ids(market)` into `_project_broker_trend_report`.
Update the history endpoint in `dashboard_web.py` to call:

```python
report = load_historical_trend_report(
    config,
    broker=route[0],
    artifact=unquote(route[2]),
)
```

Update existing direct callers in tests to the new signature.

- [ ] **Step 6: Make normal test fixtures explicit about current pools**

Update `tests/test_dashboard.py::dashboard_config` to pass:

```python
trend_cn_candidate_pool_ids=(622466, 697199),
trend_us_candidate_pool_ids=(622460, 705013),
trend_hk_candidate_pool_ids=(622494, 707617),
```

Where a test intentionally exercises missing configuration, construct `DashboardConfig`
with empty tuples or call `_load_trend_reports(..., current_candidate_pool_ids={})`.

- [ ] **Step 7: Run focused projection and endpoint tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_dashboard_cli.py \
  tests/test_dashboard.py \
  tests/test_dashboard_web.py \
  -q -k 'candidate_pool or configured_etf_pools or current_discipline or historical_trend_report or main_delegates_to_server or frozen_cost_contexts'
```

Expected: PASS.

- [ ] **Step 8: Re-run the original real-data ETF feedback loop**

Run the existing local command against `/Users/ray/projects/open_trader/config/daily_premarket.env`,
`data`, and `reports`. Assert CN contains `ETF`, US contains both configured IDs, and HK
contains both configured IDs.

Expected final line: `ok`.

- [ ] **Step 9: Commit the data-flow fix**

```bash
git add src/open_trader/dashboard.py src/open_trader/cli.py \
  src/open_trader/dashboard_web.py tests/test_dashboard_cli.py \
  tests/test_dashboard.py tests/test_dashboard_web.py
git commit -m "fix: project configured ETF pools in dashboard discipline"
```

---

### Task 2: Delete the duplicate trend-review parameter surface

**Files:**

- Modify: `src/open_trader/dashboard_static/dashboard.js:2072-2095`
- Modify: `src/open_trader/dashboard_static/dashboard.css:1400-1475, 4830-4860`
- Modify: `src/open_trader/dashboard_acceptance.py:2914-3160, 3327-3515`
- Test: `tests/test_dashboard_web.py:4620-4685`
- Test: `tests/test_dashboard_acceptance.py:1815-2040, 2164-2180, 2540-2560, 2890-2930, 3260-3310`

**Interfaces:**

- Keeps: `renderTrendReviewWorkspace(review, embedded = false) -> string`.
- Removes: `.trend-review-parameters`, `.trend-review-parameter-list`, and `.trend-review-parameter-table`.
- Keeps: review header, exact sample counts, common cutoff, two comparison panels, ten metrics, desktop two-column layout, 375px stacking, focus return, and no horizontal overflow.

- [ ] **Step 1: Change the renderer contract to require no duplicate parameters**

In `test_dashboard_trend_review_is_compact_exact_and_account_scoped`, remove
`当前策略参数` and the parameter row values from the required text list. Add:

```javascript
for (const forbidden of [
  "当前策略参数",
  "trend-review-parameters",
  "trend-review-parameter-list",
  "trend-review-parameter-table",
]) {
  if (html.includes(forbidden)) throw new Error(forbidden + "\n" + html);
}
```

Keep all header, sample, metric, localization, panel count, and value assertions.

- [ ] **Step 2: Add a CSS deletion assertion**

In the same test module, read `dashboard.css` and assert:

```python
css = (STATIC_DIR / "dashboard.css").read_text(encoding="utf-8")
assert ".trend-review-parameter" not in css
```

- [ ] **Step 3: Run renderer tests and verify red**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_dashboard_web.py::test_dashboard_trend_review_is_compact_exact_and_account_scoped \
  -q
```

Expected: FAIL because the rendered HTML and CSS still contain the duplicate parameter surface.

- [ ] **Step 4: Remove the renderer and its CSS**

In `renderTrendReviewWorkspace`, delete:

```javascript
const rows = Array.isArray(snapshot.parameter_rows) ? snapshot.parameter_rows : [];
```

and delete the complete `<section class="trend-review-parameters">...</section>`.

Remove every CSS selector returned by:

```bash
rg -n 'trend-review-parameter|trend-review-parameters' \
  src/open_trader/dashboard_static/dashboard.css
```

Preserve shared selectors for headers, comparison captions, metric labels, and mobile
comparison stacking.

- [ ] **Step 5: Update live acceptance to the smaller review DOM**

In `_check_trend_review`:

- remove `"当前策略参数"` from required text;
- remove all extraction and comparison of `snapshot["parameter_rows"]`;
- add:

```python
assert workspace.locator(".trend-review-parameters").count() == 0, (
    f"{broker} 趋势复盘仍重复展示当前策略参数"
)
```

- call `_check_trend_review_geometry(page, broker)` without a parameter count.

In `_check_trend_review_geometry`:

- remove the `parameter_count` argument;
- remove parameter selectors, `parameterRows`, and parameter-row geometry assertions;
- remove the parameter selector from `expected_text_counts`;
- keep header, panel, metric, text wrapping, 375px overflow, and target-size checks.

- [ ] **Step 6: Simplify acceptance fakes and obsolete tests**

In `tests/test_dashboard_acceptance.py`:

- remove parameter table branches from fake locator `count()` and `all_inner_texts()`;
- remove parameter selectors and rows from the fake geometry expression contract;
- remove “当前策略参数 …” from `trend_review_workspace_text`;
- remove the three tests that only validate visible raw parameter values:
  `test_acceptance_allows_raw_parameter_facts_with_english_abbreviations`,
  `test_acceptance_rejects_forbidden_text_inside_raw_parameter_values`, and
  `test_acceptance_rejects_english_parameter_group_or_name`;
- retain all metric, forbidden chrome, desktop geometry, mobile overflow, long-text,
  focus, and style-drift tests.

- [ ] **Step 7: Run focused renderer and acceptance tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_dashboard_web.py \
  tests/test_dashboard_acceptance.py \
  -q -k 'trend_review or current_discipline or frozen_trend_disciplines'
```

Expected: PASS.

- [ ] **Step 8: Re-run the original duplicate-surface feedback loop**

Render a trend review through `run_dashboard_js` and fail if the HTML contains
`当前策略参数` or `trend-review-parameter`.

Expected final line: `ok`.

- [ ] **Step 9: Commit the UI deletion**

```bash
git add src/open_trader/dashboard_static/dashboard.js \
  src/open_trader/dashboard_static/dashboard.css \
  src/open_trader/dashboard_acceptance.py \
  tests/test_dashboard_web.py tests/test_dashboard_acceptance.py
git commit -m "fix: remove duplicate trend review discipline table"
```

---

### Task 3: Changelog, full verification, acceptance, and exact-SHA deployment

**Files:**

- Modify: `CHANGELOG.md`

**Interfaces:**

- Consumes: Tasks 1 and 2 completed and committed.
- Produces: an operator-facing changelog entry, a final accepted Git SHA, and a review deployment serving that exact SHA.

- [ ] **Step 1: Add the dated operator-facing changelog entry**

Create this section immediately before `## 2026-07-25`:

```markdown
## 2026-07-26

- Removed the duplicate current-strategy parameter table from trend review pages
  and kept the folded report discipline as the single rule surface. Dashboard
  current discipline now uses the configured CN/US/HK stock-and-ETF candidate
  pools even when the selected report predates ETF integration; frozen historical
  report parameters remain unchanged.
```

- [ ] **Step 2: Run focused automated tests**

```bash
.venv/bin/python -m pytest \
  tests/test_dashboard_cli.py \
  tests/test_dashboard.py \
  tests/test_dashboard_web.py \
  tests/test_dashboard_acceptance.py -q
```

Expected: all selected tests pass with zero failures.

- [ ] **Step 3: Run the full automated suite**

```bash
make test
```

Expected: exit 0 with zero failures.

- [ ] **Step 4: Commit the changelog before acceptance**

```bash
git add CHANGELOG.md
git commit -m "docs: record trend discipline dashboard correction"
git status --short --branch
```

Expected: clean worktree on `fix/trend-discipline-etf-report`.

- [ ] **Step 5: Deploy the committed worktree for direct workflow checks**

Stop the old review screen and start the Dashboard from this exact worktree:

```bash
screen -S open_trader_dashboard_8766 -X quit 2>/dev/null || true
screen -dmS open_trader_dashboard_8766 zsh -lc \
  'cd /Users/ray/projects/open_trader/.worktrees/trend-discipline-etf-report && exec env PYTHONPATH=src .venv/bin/python -u -m open_trader dashboard --portfolio /Users/ray/projects/open_trader/data/latest/portfolio.csv --data-dir /Users/ray/projects/open_trader/data --reports-dir /Users/ray/projects/open_trader/reports --config /Users/ray/projects/open_trader/config/daily_premarket.env --poll-seconds 5 --host 127.0.0.1 --port 8766 >> /tmp/open_trader_dashboard_8766.log 2>&1'
```

Check `screen -ls`, `ps`, listener PID, process working directory, `git rev-parse HEAD`,
fresh log timestamps, and:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8766/
```

Expected: one current process from this worktree and HTTP `200`.

- [ ] **Step 6: Check real API projection**

Fetch `/api/dashboard` and verify:

- review workspaces no longer expose the deleted parameter table when opened;
- `trend_reports.tiger.current_strategy_parameter_rows` source contains `622460` and `705013`;
- `trend_reports.phillips.current_strategy_parameter_rows` source contains `622494` and `707617`;
- CN current source still includes ETF;
- each report still retains its original `strategy_parameter_rows`.

Expected: all assertions pass without rewriting report files.

- [ ] **Step 7: Inspect background services**

Run:

```bash
screen -ls
launchctl list | rg 'com\.open-trader\.(trend|premarket)' || true
ps aux | rg '[o]pen_trader (dashboard|trend-market|run-daily-premarket)'
```

Confirm the Dashboard process uses this worktree. Do not restart trend controllers unless
they are running pre-change code affected by this branch; this change only changes Dashboard
configuration projection and static UI.

- [ ] **Step 8: Run the final Dashboard gate**

```bash
make acceptance
```

Expected terminal status: `PASS`.

On `FAIL`, diagnose and fix, rerun focused checks, recommit, redeploy, and rerun
`make acceptance`. On `BLOCKED`, report the blocker and do not claim completion.

- [ ] **Step 9: Redeploy the exact accepted SHA**

Run and record the exact output:

```bash
git rev-parse HEAD
```

Restart `open_trader_dashboard_8766` with the Step 5 command without modifying source or
data. Verify listener PID, `/proc`/`lsof` working directory where available, worktree
HEAD equals the recorded accepted SHA, fresh log timestamp, and HTTP `200`.

This exact-SHA restart does not require another acceptance run.

- [ ] **Step 10: Provide the review URL**

Report `http://127.0.0.1:8766/`, the accepted SHA, PID, working directory, test counts,
`make acceptance` `PASS`, and the real US/HK configured ETF pool values. Do not describe
the work as complete if any required live or acceptance check is missing.

## Plan Self-Review

- Spec coverage: Task 1 covers config single-source, current-vs-frozen projection,
  historical reports, CN/US/HK pools, and missing-config fallback. Task 2 covers UI
  deletion and accessibility/layout acceptance. Task 3 covers changelog, real data,
  processes, final acceptance, and exact-SHA deployment.
- Scope: no strategy, pool resolution, history rewrite, Kelly, dependency, or unrelated
  refactor work is included.
- Type consistency: all three config fields use `tuple[int, ...]`; projection receives
  the same tuple type; current rows are `list` when available and `None` when unavailable,
  matching the frontend's existing `Array.isArray` fallback.
