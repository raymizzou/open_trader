# Trend Report Position Separation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep the trend report focused on strategy output and risk summaries while removing embedded position and execution facts.

**Architecture:** Change only the dashboard rendering boundary. The report view stops requesting simulation positions, stops rendering execution/detail rows and account overlays, and continues rendering the existing strategy table plus plan-risk and drawdown summaries. The standalone simulated-account view remains unchanged.

**Tech Stack:** Vanilla JavaScript, CSS, pytest-driven dashboard rendering checks, Playwright-backed acceptance.

## Global Constraints

- Preserve the main report table, including target weight, target amount, estimated quantity, and protection line.
- Preserve “组合计划风险” and “策略累计回撤”, including Kelly and trade-stat facts supplied by the report.
- Remove candidate detail rows, execution-status rows, simulation-position overlays, and actual-position overlays from the report view.
- Do not add a replacement position page or change report/position APIs or backend data structures.
- Run `make acceptance` only as the final dashboard gate; only `PASS` is review-ready.

---

### Task 1: Add the report-separation regression test

**Files:**
- Modify: `tests/test_dashboard_web.py` near the existing frozen-risk-summary rendering test

**Interfaces:**
- Consumes: `run_dashboard_js`, `renderTrendReportWorkspace`
- Produces: a failing browser-render regression test for the report boundary

- [ ] **Step 1: Write the failing test**

Add `test_dashboard_report_keeps_strategy_and_risk_but_excludes_positions` with a report containing one buy action, one skipped candidate, a pending execution, `actual_overlay`, and a loaded simulation-position payload. Assert the rendered report contains the main table, `组合计划风险`, and `策略累计回撤`, and does not contain these user-visible position/execution facts:

```python
forbidden = [
    "允许 · 建议", "跳过 · 建议", "计划止损风险", "正常成本", "决定性约束",
    "待执行", "模拟盘执行状态", "模拟持仓", "实盘执行辅助", "真实持仓",
]
for text in forbidden:
    if text in html:
        throw new Error(text + "\\n" + html)
for text in ["正式买入计划", "组合计划风险", "策略累计回撤"]:
    if text not in html:
        throw new Error(text + "\\n" + html)
```

Use literal fixture values for the action and overlay data; do not derive expected strings from dashboard helpers.

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py -k report_keeps_strategy_and_risk_but_excludes_positions -q
```

Expected: `FAIL`, because the current renderer includes the candidate detail/execution rows and the simulation/actual overlays.

- [ ] **Step 3: Commit the failing test**

```bash
git add tests/test_dashboard_web.py
git commit -m "test: separate trend report from positions"
```

### Task 2: Remove position and execution layers from the report renderer

**Files:**
- Modify: `src/open_trader/dashboard_static/dashboard.js:2184-2379, 2500-2615, 3038-3068, 3345-3387`
- Modify: `src/open_trader/dashboard_static/dashboard.css:2405-2464, 2539-2555, 4781-4786, 5180-5188`
- Modify: `tests/test_dashboard_web.py` tests that assert report overlays or candidate detail rows

**Interfaces:**
- Consumes: existing report JSON and the standalone simulated-account view
- Produces: report HTML with strategy rows and risk summaries only

- [ ] **Step 1: Remove detail and execution-row composition**

Delete the calls to `renderTrendRiskRow` and `renderTrendExecutionRow` from CN and non-CN sell, buy, hold, and review row builders. Delete those two now-unused functions after the calls are gone.

- [ ] **Step 2: Remove report overlay composition and fetch coupling**

Change `renderTrendRiskSummary` to accept only `(summary, drawdown, reportDate)`, remove overlay-only availability/status branches, and stop rendering `renderTrendActualOverlay`. In `renderCnTrendReportWorkspace`, remove the `simulationOverlay` variable and its HTML insertion, and pass only the risk and drawdown data to `renderTrendRiskSummary`.

Change `setAccountView` and `loadTrendSimulatePositions` so the simulation-position endpoint is fetched for `view === "simulate"` only; selecting `view === "report"` renders the report without requesting positions.

- [ ] **Step 3: Delete dead overlay helpers and styles**

Delete `trendSimulationActions`, `trendSimulationDeviation`, `renderTrendSimulationOverlay`, and `renderTrendActualOverlay`, then remove their `.trend-actual-*` and `.cn-trend-execution` CSS blocks and combined selectors. Keep styles used by the standalone simulated-account table and the risk summary.

- [ ] **Step 4: Update old assertions to the new contract**

Replace direct report-overlay expectations with absence assertions. Keep standalone simulated-account tests focused on `renderSimulatedAccountView`; remove tests whose only contract is the deleted report overlay helper. Update the existing frozen-risk-summary test to expect zero `cn-trend-risk-detail` rows while retaining its risk-summary assertions.

- [ ] **Step 5: Run the focused dashboard tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py -q
```

Expected: all dashboard web tests pass with no deleted position/execution markup in the report.

- [ ] **Step 6: Commit the implementation**

```bash
git add src/open_trader/dashboard_static/dashboard.js src/open_trader/dashboard_static/dashboard.css tests/test_dashboard_web.py
git commit -m "fix: separate trend reports from positions"
```

### Task 3: Verify the accepted dashboard behavior

**Files:**
- Verify: `src/open_trader/dashboard_static/dashboard.js`
- Verify: `src/open_trader/dashboard_static/dashboard.css`
- Verify: `tests/test_dashboard_web.py`

**Interfaces:**
- Consumes: the committed implementation SHA
- Produces: fresh automated, live workflow, process, log, and browser evidence

- [ ] **Step 1: Run the complete test suite from the repository root**

Run:

```bash
cd /Users/ray/projects/open_trader
PYTHONSAFEPATH=1 PYTHONPATH=/Users/ray/projects/open_trader/.worktrees/report-position-separation:/Users/ray/projects/open_trader/.worktrees/report-position-separation/src /Users/ray/projects/open_trader/.worktrees/report-position-separation/.venv/bin/python -m pytest /Users/ray/projects/open_trader/.worktrees/report-position-separation/tests -q
```

Expected: zero failures; running from the repository root supplies the ignored historical snapshot fixtures.

- [ ] **Step 2: Run the final dashboard acceptance gate**

Run `make acceptance` from the worktree only after all source and test changes are complete. Record `PASS`, `FAIL`, or `BLOCKED` exactly; do not substitute unit tests or curl for a blocked browser/external check.

- [ ] **Step 3: Redeploy the exact accepted SHA and verify the live process**

After `PASS`, restart the dashboard using that exact Git SHA, inspect the process PID and working directory, inspect fresh logs, and verify HTTP 200 from the review URL before reporting the URL.

- [ ] **Step 4: Commit any required dated changelog entry before merging**

Do not merge this branch into `main` until its dated operator-facing `CHANGELOG.md` entry is committed first.
