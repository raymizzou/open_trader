# Collapsible Trend Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render the current CN/HK/US trend review as a default-closed disclosure inside its market's Trend Report tab.

**Architecture:** Reuse the browser-native `<details>` disclosure already used by discipline, controller, and audit sections. The current-report renderer wraps the existing review markup; historical-report branches return before the wrapper, preserving frozen-report boundaries. Dashboard acceptance asserts the disclosure's existence, closed state, and expanded content.

**Tech Stack:** Static JavaScript, CSS, pytest, Playwright Dashboard acceptance.

## Global Constraints

- Do not add dependencies or custom disclosure state.
- Keep exactly three account tabs: `real`, `simulate`, and `report`.
- The review disclosure is closed by default and only belongs to current reports.
- Preserve the existing review metric markup and mobile no-horizontal-overflow contract.

---

### Task 1: Render the current review as a native disclosure

**Files:**

- Modify: `tests/test_dashboard_web.py:4615`
- Modify: `src/open_trader/dashboard_static/dashboard.js:3487`
- Modify: `src/open_trader/dashboard_static/dashboard.css:2040`

**Interfaces:**

- Consumes: `renderTrendReviewWorkspace(review, true) -> string`.
- Produces: `renderEmbeddedTrendReport(broker) -> string` containing one closed `<details class="trend-review-disclosure">` for a current report.

- [ ] **Step 1: Write the failing renderer contract**

  In `test_dashboard_trend_review_is_compact_exact_and_account_scoped`, render each broker's report view and assert:

  ```javascript
  const disclosure = report.match(/<details class="trend-review-disclosure"[\\s\\S]*?<\\/details>/)?.[0] || "";
  if (!disclosure.includes("<summary>趋势复盘") || disclosure.includes(" open")) throw new Error(report);
  if (!disclosure.includes("纪律模拟 31 笔") || !disclosure.includes("trend-review")) throw new Error(disclosure);
  ```

- [ ] **Step 2: Run the renderer test and verify it fails**

  Run:

  ```bash
  PYTHONPATH=.:src .venv/bin/pytest -q tests/test_dashboard_web.py::test_dashboard_trend_review_is_compact_exact_and_account_scoped
  ```

  Expected: FAIL because the current report directly appends `.trend-review` without a `trend-review-disclosure` wrapper.

- [ ] **Step 3: Wrap the existing review markup in the minimal disclosure**

  In `renderEmbeddedTrendReport`, derive the summary from existing sample-count helpers and use:

  ```javascript
  const reviewPanel = !review ? "" : review.available
    ? `<details class="trend-review-disclosure"><summary>趋势复盘 <span>${escapeHtml(formatTrendReviewSampleCount(review, "discipline", "纪律模拟"))}</span><span>${escapeHtml(formatTrendReviewSampleCount(review, "actual", "实际执行"))}</span><small>已折叠</small></summary>${renderTrendReviewWorkspace(review, true)}</details>`
    : `<details class="trend-review-disclosure"><summary>趋势复盘 <small>不可用</small></summary><p class="account-empty">${escapeHtml(formatPlain(review.status_text || "暂无复盘数据"))}</p></details>`;
  ```

  Add `.trend-review-disclosure` selectors to the existing disclosure CSS group so its summary has the same focus, spacing, and mobile behavior as discipline and controller disclosures. Do not alter the existing review metric markup.

- [ ] **Step 4: Run the renderer test and verify it passes**

  Run:

  ```bash
  PYTHONPATH=.:src .venv/bin/pytest -q tests/test_dashboard_web.py::test_dashboard_trend_review_is_compact_exact_and_account_scoped
  ```

  Expected: PASS.

- [ ] **Step 5: Commit the renderer change**

  ```bash
  git add src/open_trader/dashboard_static/dashboard.js src/open_trader/dashboard_static/dashboard.css tests/test_dashboard_web.py
  git commit -m "fix: collapse trend review in report"
  ```

### Task 2: Enforce the disclosure in Dashboard acceptance

**Files:**

- Modify: `src/open_trader/dashboard_acceptance.py:1321`
- Modify: `src/open_trader/dashboard_acceptance.py:1494`
- Test: `tests/test_dashboard_web.py:4299`

**Interfaces:**

- Consumes: the `details.trend-review-disclosure` emitted by Task 1.
- Produces: browser acceptance failures when the review is absent, opened by default, detached from the Trend Report tab, or missing metrics after expansion.

- [ ] **Step 1: Write the failing browser contract**

  In `test_dashboard_account_view_dom_at_375px`, after selecting the `report` tab, assert:

  ```python
  disclosure = section.locator("details.trend-review-disclosure")
  assert disclosure.count() == 1
  assert disclosure.get_attribute("open") is None
  disclosure.locator(":scope > summary").click()
  assert "卡玛比率" in disclosure.inner_text()
  assert "夏普比率" in disclosure.inner_text()
  ```

- [ ] **Step 2: Run the browser contract and verify it fails**

  Run:

  ```bash
  PYTHONPATH=.:src .venv/bin/pytest -q tests/test_dashboard_web.py::test_dashboard_account_view_dom_at_375px
  ```

  Expected: FAIL because the current review root is not inside a disclosure.

- [ ] **Step 3: Update the acceptance checks**

  In `_check_trend_account_views` and `_check_separated_trend_report_views`, require one `details.trend-review-disclosure` under the report panel, assert `open` is absent, click its summary, then run the existing `.trend-review` text and overflow checks. Historical-report checks must continue to assert no current review disclosure.

- [ ] **Step 4: Run focused browser and acceptance tests**

  Run:

  ```bash
  PYTHONPATH=.:src .venv/bin/pytest -q tests/test_dashboard_web.py tests/test_dashboard_acceptance.py tests/test_dashboard.py tests/test_dashboard_cli.py
  ```

  Expected: PASS.

- [ ] **Step 5: Commit the acceptance contract**

  ```bash
  git add src/open_trader/dashboard_acceptance.py tests/test_dashboard_web.py
  git commit -m "test: enforce collapsed trend review"
  ```

### Task 3: Record and verify the operator-facing result

**Files:**

- Modify: `CHANGELOG.md`
- Test: `Makefile` target `acceptance`

**Interfaces:**

- Consumes: the committed source and the local Dashboard process.
- Produces: an accepted Dashboard running the candidate Git SHA.

- [ ] **Step 1: Add the dated changelog entry**

  Add one 2026-07-26 bullet stating that current trend reviews are default-closed disclosures inside Trend Report, while historical reports remain frozen without current review content.

- [ ] **Step 2: Commit the changelog**

  ```bash
  git add CHANGELOG.md
  git commit -m "docs: log collapsible trend review"
  ```

- [ ] **Step 3: Run the complete test suite**

  Run:

  ```bash
  make test
  ```

  Expected: all tests pass.

- [ ] **Step 4: Restart the candidate Dashboard and controllers**

  Run:

  ```bash
  scripts/install_daily_premarket_launchd.sh --config /Users/ray/projects/open_trader/config/daily_premarket.env --trend-only --market all
  ```

  Restart the Dashboard from `/Users/ray/projects/open_trader/.worktrees/acceptance-skip-missing-baseline`, rotate `/tmp/open_trader_dashboard_8766.log`, then verify its first runtime line has the candidate SHA and clean source state.

- [ ] **Step 5: Run final Dashboard acceptance**

  Run:

  ```bash
  make acceptance
  ```

  Expected: JSON status `PASS`.

- [ ] **Step 6: Redeploy the exact accepted SHA and check live behavior**

  Restart the same controller and Dashboard commands without source changes. Verify the new Dashboard PID, all controller heartbeat/status SHAs, fresh logs, API JSON, HTTP 200, and a Playwright flow that opens the Trend Report, finds one closed `details.trend-review-disclosure`, expands it, and reads the metrics.
