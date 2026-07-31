# Dashboard Disclosure State Preservation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve manual `<details>` open/closed state across account/trend-report refresh renders while leaving every unrelated dashboard surface unchanged.

**Architecture:** Add DOM-local capture/restore helpers in the existing dashboard renderer. The helpers snapshot all account-surface disclosures immediately before `#account-holdings` or the active account panel is replaced, then restore matching states after the new HTML is installed. A broker/view/report scope guard prevents state leaking across different content; prediction-market rendering keeps its existing independent expansion logic.

**Tech Stack:** Vanilla JavaScript, native `<details>`, existing Python pytest and Playwright browser checks, existing dashboard fixture harness.

## Global Constraints

- Keep the initial default collapsed state unchanged.
- Keep `state.accountViews` as the authority for `真实持仓` / `模拟盘持仓` / `趋势报告`.
- Do not modify prediction-market, Kelly lab, standard backtest, API payload, report-selection, strategy, or execution behavior.
- Do not add localStorage, URL state, backend state, or a dependency.
- Preserve existing focus and scroll restoration behavior.
- Use `PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" /Users/ray/projects/open_trader/.venv/bin/pytest` from the linked worktree.
- Do not run `make acceptance` until the final verification step.

---

## File Map

- Modify: `src/open_trader/dashboard_static/dashboard.js`
  - Add the account-surface disclosure snapshot/restore helpers.
  - Wrap the two existing account DOM replacement paths.
- Modify: `tests/test_dashboard_web.py`
  - Extend the existing real-browser dashboard account-view test with the
    regression assertions so the test exercises the actual renderer and DOM.
- Create: no new runtime files or dependencies.

### Task 1: Add the failing browser regression

**Files:**
- Modify: `tests/test_dashboard_web.py` near `test_dashboard_account_view_dom_at_375px`
- Test: the modified real-browser dashboard test

**Interfaces:**
- Consumes: existing `page`, `section`, `dashboard`, report fixture, and
  `renderAccountHoldings()` / `renderAccountViewPanelOnly()` globals already
  used by the test.
- Produces: a red test proving that account disclosures must preserve their
  explicit `open` value across full and panel renders and must not leak across
  view/broker switches.

- [ ] **Step 1: Add the minimal state assertions and render trigger after the report view is visible.**

  Use the existing report section and assert the current default is closed,
  then open the top-level discipline disclosure, one discipline category, and
  the report audit disclosure. Capture the exact selectors from the existing
  markup:

  ```python
  discipline = section.locator("details.trend-discipline-workspace")
  assert discipline.count() == 1
  assert discipline.evaluate("node => !node.open")
  discipline.locator(":scope > summary").click()

  category = discipline.locator("details.trend-discipline-category")
  assert category.count() == 6
  category.nth(0).locator(":scope > summary").click()

  audit = section.locator(
      ".cn-trend-report > details.trend-audit:not(.trend-review-disclosure)"
  )
  assert audit.count() == 1
  audit.locator(":scope > summary").click()

  page.evaluate("renderAccountHoldings()")
  section = page.locator("#account-tiger")
  assert section.locator("details.trend-discipline-workspace").evaluate("node => node.open")
  assert section.locator("details.trend-discipline-category").nth(0).evaluate("node => node.open")
  assert section.locator(
      ".cn-trend-report > details.trend-audit:not(.trend-review-disclosure)"
  ).evaluate("node => node.open")

  page.evaluate("renderAccountViewPanelOnly('tiger')")
  section = page.locator("#account-tiger")
  assert section.locator("details.trend-discipline-workspace").evaluate("node => node.open")
  assert section.locator("details.trend-discipline-category").nth(0).evaluate("node => node.open")
  ```

- [ ] **Step 2: Run the full test function and verify the new assertion fails for the reported reason.**

  Run:

  ```bash
  PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" /Users/ray/projects/open_trader/.venv/bin/pytest -q tests/test_dashboard_web.py::test_dashboard_account_view_dom_at_375px
  ```

  Expected: the test reaches the new assertions, then fails after a render
  because the current implementation recreates the disclosures with
  `open === false`.

- [ ] **Step 3: Add the no-leak checks.**

  Close the discipline disclosure, switch to another account view, and return
  to the report; then switch broker and return. The new report content must
  start from its normal collapsed state rather than inheriting the prior
  content's snapshot:

  ```python
  section.locator("details.trend-discipline-workspace > summary").click()
  section.locator('[data-account-view="real"]').click()
  section.locator('[data-account-view="report"]').click()
  assert section.locator("details.trend-discipline-workspace").evaluate("node => !node.open")
  page.locator("#account-tab-futu").click()
  page.locator("#account-tab-tiger").click()
  section = page.locator("#account-tiger")
  assert section.locator("details.trend-discipline-workspace").evaluate("node => !node.open")
  ```

  Keep the existing prediction-market tests unchanged; they remain the
  regression guard for its separate expansion-state implementation.

- [ ] **Step 4: Commit the failing test only.**

  ```bash
  git add tests/test_dashboard_web.py
  git commit -m "test: reproduce dashboard disclosure reset"
  ```

### Task 2: Implement scoped disclosure capture and restore

**Files:**
- Modify: `src/open_trader/dashboard_static/dashboard.js` near the account
  rendering helpers and the two account DOM replacement paths

**Interfaces:**
- Consumes: an existing account DOM root, broker, and target account view.
- Produces: `accountDisclosureKey(root, details)`,
  `captureAccountDisclosureState(root, broker, view)`, and
  `restoreAccountDisclosureState(root, broker, view, snapshot)`, all used only
  by account rendering.

- [ ] **Step 1: Add `accountDisclosureKey(root, details)`.**

  Build each key from the disclosure's nested position relative to the render
  root plus its class and existing semantic `data-*` attributes. Include the
  parent path so repeated nested disclosures cannot overwrite each other.
  Store both `true` and `false` values; a manual collapse must be preserved as
  deliberately as a manual expansion.

- [ ] **Step 2: Add `captureAccountDisclosureState(root, broker, view)`.**

  Capture only when the current DOM contains the requested broker section and
  the current panel `aria-labelledby` matches the requested account view. Add
  the current embedded report identity (`data-report-artifact`, SHA, and
  strategy version when present) to the snapshot scope. Return `null` when the
  old DOM represents another broker/view or no longer has a matching panel.

- [ ] **Step 3: Add `restoreAccountDisclosureState(root, broker, view, snapshot)`.**

  Compare the snapshot scope with the new DOM's broker/view/report scope. If it
  differs, do nothing. Otherwise assign the saved boolean to matching
  `<details>` nodes; leave new or unmatched nodes at their renderer defaults.

- [ ] **Step 4: Wrap `renderAccountViewPanelOnly()`.**

  Capture before `panel.innerHTML = renderAccountViewPanel(...)`, keep the
  existing `aria-labelledby` and tab updates unchanged, then restore after the
  new panel HTML is installed. A view switch must fail the old-scope check;
  refreshing the same view must pass it.

- [ ] **Step 5: Wrap `renderAccountHoldings()`.**

  Once the active group is known and before replacing `container.innerHTML`,
  capture the current active account section. Restore against the newly
  rendered active section after the replacement and existing focus restoration.
  Keep loading/error branches unchanged and do not restore a previous broker's
  disclosure state.

- [ ] **Step 6: Run the regression test and verify it passes.**

  ```bash
  PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" /Users/ray/projects/open_trader/.venv/bin/pytest -q tests/test_dashboard_web.py::test_dashboard_account_view_dom_at_375px
  ```

  Expected: PASS, including full-render persistence, panel-render persistence,
  and no-leak assertions.

- [ ] **Step 7: Commit the minimal implementation.**

  ```bash
  git add src/open_trader/dashboard_static/dashboard.js
  git commit -m "fix: preserve dashboard disclosure state on refresh"
  ```

### Task 3: Run focused regression coverage

**Files:**
- Modify: none

- [ ] **Step 1: Run the dashboard web module.**

  ```bash
  PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" /Users/ray/projects/open_trader/.venv/bin/pytest -q tests/test_dashboard_web.py
  ```

  Expected: all dashboard web tests pass with no new warnings or page errors.

- [ ] **Step 2: Run the dashboard acceptance module.**

  ```bash
  PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" /Users/ray/projects/open_trader/.venv/bin/pytest -q tests/test_dashboard_acceptance.py
  ```

  Expected: all acceptance helper/scenario tests pass, including existing
  default-collapsed and prediction-market expansion checks.

- [ ] **Step 3: Check the diff.**

  ```bash
  git diff --check
  rg -n "DEBUG-dashboard-disclosure" src/open_trader/dashboard_static/dashboard.js tests/test_dashboard_web.py || true
  ```

  Expected: no whitespace errors and no temporary debug instrumentation added
  by this change.

### Task 4: Final live verification and Dashboard handoff

**Files:**
- Modify: `CHANGELOG.md` only before a later merge, per `AGENTS.md`.

- [ ] **Step 1: Inspect the worktree and runtime ownership before live testing.**

  ```bash
  git status --short --branch
  lsof -nP -iTCP:8766 -sTCP:LISTEN
  curl -fsS http://127.0.0.1:8766/api/dashboard | jq '{poll_seconds, trend_reports: (.trend_reports | keys)}'
  ```

  Expected: identify the actual serving PID/CWD/SHA before attributing browser
  behavior to this worktree.

- [ ] **Step 2: Run the repository final acceptance gate only after the code is stable.**

  ```bash
  make acceptance
  ```

  Expected: `PASS`. `FAIL` requires another diagnosis/fix cycle; `BLOCKED`
  remains blocked and cannot be relabeled as complete.

- [ ] **Step 3: Redeploy the exact accepted SHA and verify the review runtime.**

  Confirm the deployed process PID, working directory, Git SHA, fresh log
  timestamp, and HTTP 200 from `http://127.0.0.1:8766/` after the restart.

- [ ] **Step 4: Reproduce the original five-second browser flow.**

  On the deployed exact SHA, open `趋势报告` for an account, open `纪律` and
  one nested category, wait through at least one automatic quote poll, and
  confirm both remain open. Close them, wait through another poll, and confirm
  both remain closed. Confirm switching broker/view starts the new content
  collapsed.

- [ ] **Step 5: Capture the affected live view for UI handoff.**

  Capture the deployed account/trend-report view after the behavioral checks;
  include the screenshot in the final response because visible UI behavior
  changed.
