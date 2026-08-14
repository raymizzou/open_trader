# Historical Trend Holding Sections Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split real CN, HK, and US broker positions into vertically stacked `趋势持仓` and `非趋势持仓` sections on both the Account and Trend Report surfaces, using permanent historical formal-buy-plan membership.

**Architecture:** Project one deterministic historical membership contract per trend broker from existing report JSON files and attach it to every broker trend-report payload. Reuse one browser classification helper to partition Account rows and Trend Report real-position rows while retaining each surface's existing table renderer, interactions, and current row-state styling.

**Tech Stack:** Python 3.12 stdlib, existing `open_trader.dashboard` report projection, vanilla JavaScript/CSS, pytest, and the repository Dashboard acceptance gate.

## Global Constraints

- The headings are exactly `趋势持仓` and `非趋势持仓` on both surfaces; never use `被趋势持仓`.
- A current real position is trend-owned when its normalized market and symbol appeared in a formal `BUY` action in any historical report artifact for that broker.
- Membership is permanent across later reports and sell/reopen cycles.
- A mixed strategy/personal broker row stays whole in `趋势持仓`; never split quantity.
- Apply the same rule to Eastmoney/CN, Phillips/HK, and Tiger/US.
- Change `持仓 → 真实持仓` and current/historical `趋势报告 → 盘中持续 · 已有持仓 → 真实持仓`.
- Do not change `模拟盘持仓`, Account API contracts, totals, statement reconciliation, order behavior, current Trend Report row colors, columns, filters, or detail actions.
- On unavailable membership, keep the old single table and show `历史买入计划归属暂不可用，未执行分组`; never guess that all rows are non-trend.
- Add no dependency, database, schema migration, persisted artifact, cache, secondary tab, side-by-side table, or manual override.
- Run focused tests during development. Run `make acceptance` only as the final Dashboard gate.
- Before any merge, commit a dated operator-facing `CHANGELOG.md` entry.
- Only `make acceptance` `PASS` permits completion language. Redeploy the exact accepted SHA and verify PID, working directory, SHA, fresh logs, and HTTP 200 before user review.

---

### Task 1: Publish historical buy-plan membership

**Files:**
- Modify: `src/open_trader/dashboard.py:110-124,2365-2539,2710-2785`
- Modify: `tests/test_dashboard.py:300-370`

**Interfaces:**
- Produces: `_historical_buy_plan_membership(reports_dir: Path, *, market: str) -> dict[str, object]`.
- Produces on each broker report: `historical_buy_plan_membership = {available: bool, symbols: list[str], reason: str}`.
- Symbol keys are exactly `MARKET.SYMBOL`, using `normalize_backtest_symbol` for the symbol component.
- Preserves: all current report-selection, revision, execution-batch, action, and historical-report route behavior.

- [ ] **Step 1: Write failing projection tests for permanent three-market membership**

  Add a small test helper that writes only the history fields required by the membership projection:

  ```python
  def write_buy_plan_history(
      root: Path,
      directory: str,
      artifact: str,
      *,
      market: str,
      actions: list[dict[str, object]],
  ) -> None:
      path = root / directory / artifact
      path.parent.mkdir(parents=True, exist_ok=True)
      path.write_text(json.dumps({
          "metadata": {"market": market},
          "strategy_judgments": {
              "formal_actions": actions,
          },
      }), encoding="utf-8")
  ```

  Parameterize Eastmoney/CN, Phillips/HK, and Tiger/US. Write two artifacts per market: an older formal buy and a newer empty plan. Add a revision containing the same buy plus one distinct buy. Assert the exact sorted contract:

  ```python
  assert _historical_buy_plan_membership(directory, market=market) == {
      "available": True,
      "symbols": expected_symbols,
      "reason": "",
  }
  ```

  Use expected normalized examples `CN.511190`, `HK.00622`, and `US.ADP`. Include a `SELL_ALL`, `HOLD`, and `MANUAL_REVIEW` action and prove none enters the set. Do not write execution or fill data; the test must prove plan membership is sufficient.

- [ ] **Step 2: Run the projection test and verify RED**

  Run:

  ```bash
  PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
    tests/test_dashboard.py -k 'historical_buy_plan_membership'
  ```

  Expected: FAIL because `_historical_buy_plan_membership` and the report contract do not exist.

- [ ] **Step 3: Write failing unavailable-versus-empty tests**

  Cover these literal cases:

  ```python
  assert missing["available"] is False
  assert malformed["available"] is False
  assert invalid_formal_actions["available"] is False
  assert invalid_buy_symbol["available"] is False
  assert valid_empty == {"available": True, "symbols": [], "reason": ""}
  ```

  Assert every unavailable result has `symbols == []` and a non-empty `reason`. A directory containing one valid file and one malformed file is unavailable, proving the code does not silently misclassify an unknown historical buy as non-trend.

- [ ] **Step 4: Implement the smallest backend projection**

  Keep the implementation in `dashboard.py`; add no class or artifact. The helper should follow this shape:

  ```python
  def _historical_buy_plan_membership(
      reports_dir: Path, *, market: str
  ) -> dict[str, object]:
      paths = sorted(reports_dir.glob("*.json"))
      if not paths:
          return {
              "available": False,
              "symbols": [],
              "reason": "历史趋势报告不存在",
          }
      symbols: set[str] = set()
      for path in paths:
          try:
              payload = json.loads(path.read_text(encoding="utf-8"))
          except (OSError, UnicodeError, json.JSONDecodeError):
              return {
                  "available": False,
                  "symbols": [],
                  "reason": "历史趋势报告不可读取",
              }
          judgments = payload.get("strategy_judgments") if isinstance(payload, dict) else None
          actions = judgments.get("formal_actions") if isinstance(judgments, dict) else None
          if not isinstance(actions, list) or not all(isinstance(item, dict) for item in actions):
              return {
                  "available": False,
                  "symbols": [],
                  "reason": "历史买入计划格式无效",
              }
          for item in actions:
              if item.get("action") != "BUY" or _trend_action_needs_review(item):
                  continue
              try:
                  symbol = normalize_backtest_symbol(market, str(item.get("symbol") or ""))
              except ValueError:
                  return {
                      "available": False,
                      "symbols": [],
                      "reason": "历史买入计划标的无效",
                  }
              symbols.add(f"{market}.{symbol}")
      return {"available": True, "symbols": sorted(symbols), "reason": ""}
  ```

  Compute the contract at the top of `_project_broker_trend_report` and add it to both the execution-batch-error return and normal return. In `_load_broker_trend_report`, also add it to the no-current-report return. This covers current reports, historical report routes, and the Account surface even if today's report is unavailable.

- [ ] **Step 5: Run Task 1 GREEN and commit**

  Run:

  ```bash
  PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
    tests/test_dashboard.py -k 'historical_buy_plan_membership or trend_report_history or historical_trend_report'
  git diff --check
  ```

  Expected: all selected tests PASS.

  Commit only Task 1 files:

  ```bash
  git add src/open_trader/dashboard.py tests/test_dashboard.py
  git commit -m "feat: project historical trend holding membership"
  ```

### Task 2: Reuse one split across both real-holding surfaces

**Files:**
- Modify: `src/open_trader/dashboard_static/dashboard.js:4536-4589,5987-6000,6252-6260,9568-9595`
- Modify: `src/open_trader/dashboard_static/dashboard.css:1300-1360,5560-5635`
- Modify: `tests/test_dashboard_web.py:5310-5375,5600-5695,13510-13545`

**Interfaces:**
- Consumes: `report.historical_buy_plan_membership` from Task 1.
- Produces: `splitHistoricalTrendHoldings(items: object[], report: object) -> {trend: object[], nonTrend: object[]} | null`.
- Uses either an Account row's `item.holding` or a Trend Report row directly and matches with existing `normalizeActionKey`.
- Preserves: `renderAccountTable`, `renderTrendHoldingTable`, simulated view rendering, selected detail rows, and current `trend_report_state` classes.

- [ ] **Step 1: Write the failing shared-classifier JavaScript test**

  Use `run_dashboard_js` with one report contract and both row shapes:

  ```javascript
  const report = {
    market: "US",
    historical_buy_plan_membership: {
      available: true,
      symbols: ["US.ADP"],
      reason: "",
    },
  };
  const accountRows = [
    {holding:{market:"US",symbol:"ADP"},display:{market:"US",symbol:"ADP",market_value_hkd:"10"}},
    {holding:{market:"US",symbol:"AMZN"},display:{market:"US",symbol:"AMZN",market_value_hkd:"20"}},
  ];
  const reportRows = [{symbol:"ADP"},{symbol:"AMZN"}];
  console.log(JSON.stringify({
    account: splitHistoricalTrendHoldings(accountRows, report),
    report: splitHistoricalTrendHoldings(reportRows, report),
  }));
  ```

  Assert both outputs put ADP in `trend` and AMZN in `nonTrend`, preserve input order, preserve the complete original objects, and conserve two rows. Add HK `622`/`00622` and CN six-digit cases to prove existing normalization matches Task 1 keys.

- [ ] **Step 2: Run the classifier test and verify RED**

  Run:

  ```bash
  PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
    tests/test_dashboard_web.py -k 'shared_historical_trend_holding_split'
  ```

  Expected: FAIL because `splitHistoricalTrendHoldings` is undefined.

- [ ] **Step 3: Implement the shared classifier**

  Add one function next to `normalizeActionKey`; do not add a client-side cache or copy the rule into two renderers:

  ```javascript
  function splitHistoricalTrendHoldings(items, report) {
    const membership = report?.historical_buy_plan_membership;
    if (membership?.available !== true || !Array.isArray(membership.symbols)) return null;
    const planned = new Set(membership.symbols.map((key) => normalizeActionKey("", key)).filter(Boolean));
    const trend = [];
    const nonTrend = [];
    for (const item of items) {
      const position = item?.holding || item;
      const key = normalizeActionKey(position?.market || report?.market, position?.symbol);
      (key && planned.has(key) ? trend : nonTrend).push(item);
    }
    return {trend, nonTrend};
  }
  ```

  This is the only membership decision in the browser.

- [ ] **Step 4: Write failing rendering tests for Account, Trend Report, and fallback**

  Add one Account test that seeds `state.dashboard.trend_reports.tiger` with the membership contract, renders `renderAccountViewPanel`, and asserts:

  ```python
  assert html.index("趋势持仓") < html.index("非趋势持仓")
  assert html.count("account-holding-row") == 2
  assert html.count("ADP") == 1
  assert html.count("AMZN") == 1
  assert "HKD 10" in html and "HKD 20" in html
  ```

  Render one row with a selected detail key and assert the existing `decision-detail-row` remains adjacent to that row inside its assigned section.

  Add one Trend Report test that calls `renderTrendHoldingPanel(report, "real", rows)` and asserts both headings, each symbol once, and existing `trend-holding-included` / `trend-holding-excluded` classes unchanged. Then call `renderTrendHoldingPanel(report, "simulate", rows)` and assert it contains neither section heading.

  For both real surfaces, set `available: false` and assert the exact warning plus one old-style table. Assert it does not render either section heading.

- [ ] **Step 5: Render the approved stacked sections with existing tables**

  Add one tiny wrapper that renders a heading/count above supplied table HTML:

  ```javascript
  function renderHoldingOriginSection(title, rows, tableHtml) {
    return `<section class="holding-origin-section">
      <div class="holding-origin-heading"><h3>${escapeHtml(title)}</h3><span>${escapeHtml(formatDisplayNumber(rows.length))} 条</span></div>
      ${rows.length ? tableHtml : '<p class="account-empty">无</p>'}
    </section>`;
  }
  ```

  In `renderAccountViewPanel`, read `state.dashboard?.trend_reports?.[group.broker]`, call the shared classifier, and render two calls to `renderAccountTable`. Keep the existing no-position behavior. When classification is null, prepend the exact warning and return the existing single table.

  In `renderTrendHoldingPanel`, change only the `view === "real"` available branch. Keep `renderTrendHoldingSource(report)` first, then render the two sections with `renderTrendHoldingTable`. Leave the simulated branch untouched. Existing empty table behavior supplies `无` for an empty section.

  Add minimal CSS using current variables:

  ```css
  .holding-origin-section {
    padding: 14px 0 0;
  }

  .holding-origin-section + .holding-origin-section {
    border-top: 8px solid var(--surface-soft);
    margin-top: 14px;
  }

  .holding-origin-heading {
    align-items: baseline;
    display: flex;
    justify-content: space-between;
    padding: 0 12px 8px;
  }

  .holding-origin-heading h3 {
    font-size: 1rem;
    margin: 0;
  }

  .holding-origin-heading span {
    color: var(--muted);
  }
  ```

  Use the existing `--surface-soft` token shown above; do not introduce a new theme token for one separator.

- [ ] **Step 6: Run Task 2 GREEN and commit**

  Run:

  ```bash
  PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
    tests/test_dashboard_web.py -k 'historical_trend_holding or account_table or trend_holding'
  PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
    tests/test_dashboard.py tests/test_dashboard_web.py
  git diff --check
  ```

  Expected: all selected tests PASS.

  Commit only Task 2 files:

  ```bash
  git add src/open_trader/dashboard_static/dashboard.js \
    src/open_trader/dashboard_static/dashboard.css tests/test_dashboard_web.py
  git commit -m "feat: split real holdings by historical trend origin"
  ```

### Task 3: Operator log, final gate, and exact-SHA review deployment

**Files:**
- Modify: `CHANGELOG.md`
- Modify only if a focused or acceptance check fails: Task 1 or Task 2 files
- Write ignored runtime evidence only under: `logs/acceptance/`, `logs/frontend_gateway/`, `logs/legacy_dashboard/`

**Interfaces:**
- Produces: one dated operator-facing changelog entry describing both changed real-position surfaces, all three markets, and unchanged simulated holdings/trading behavior.
- Produces: final `make acceptance` result.
- Produces after `PASS`: a review deployment whose Dashboard processes run the exact accepted SHA.

- [ ] **Step 1: Add and commit the Merge Log Gate entry**

  Add a `2026-08-12` entry using the existing changelog format. It must say that real holdings are split into `趋势持仓` and `非趋势持仓` from historical formal buy plans on both Account and Trend Report views for CN/HK/US, with simulated holdings and trading behavior unchanged.

  Run and commit:

  ```bash
  git diff --check
  git add CHANGELOG.md
  git commit -m "docs: log historical trend holding sections"
  ```

- [ ] **Step 2: Run the complete focused Dashboard suite**

  Run:

  ```bash
  PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
    tests/test_dashboard.py tests/test_dashboard_web.py \
    tests/test_dashboard_cli.py tests/test_dashboard_quotes.py
  git status --short
  ```

  Expected: all tests PASS and the worktree is clean. If any test fails, fix the smallest owning function, rerun the failing test, then rerun this exact suite before continuing.

- [ ] **Step 3: Run the final Dashboard acceptance gate once**

  From the feature worktree run:

  ```bash
  make acceptance
  ```

  Expected final line: `PASS`. `FAIL` requires diagnosis and a fix followed by another full gate. `BLOCKED` must be reported as blocked; do not replace it with curl, fixtures, mocks, screenshots, or unit tests.

- [ ] **Step 4: Record and redeploy the exact accepted SHA**

  Only after `PASS`:

  ```bash
  ACCEPTED_SHA="$(git rev-parse HEAD)"
  REPO_ROOT="$PWD"
  export ACCEPTED_SHA REPO_ROOT
  test -n "$ACCEPTED_SHA"
  test -z "$(git status --short)"

  scripts/install_daily_premarket_launchd.sh \
    --config /Users/ray/projects/open_trader/config/daily_premarket.env \
    --trend-only --market all

  scripts/install_dashboard_launchd.sh --mode stack \
    --repo-root "$REPO_ROOT" \
    --runtime-root /Users/ray/projects/open_trader \
    --python /Users/ray/projects/open_trader/.venv/bin/python
  ```

  Do not make a source or data change between acceptance and this restart.

- [ ] **Step 5: Verify fresh exact-SHA runtime evidence and hand off**

  Verify each CN/HK/US `data/trend_controller/<MARKET>/status.json` has a live new PID, `working_directory == REPO_ROOT`, `git_sha == ACCEPTED_SHA`, and an advancing heartbeat. Then run:

  ```bash
  launchctl print gui/$(id -u)/com.open-trader.frontend-gateway
  launchctl print gui/$(id -u)/com.open-trader.legacy-dashboard
  lsof -nP -iTCP:8766 -sTCP:LISTEN
  tail -n 80 "$REPO_ROOT"/logs/frontend_gateway/launchd.out.log
  tail -n 80 "$REPO_ROOT"/logs/legacy_dashboard/launchd.out.log
  curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8766/
  curl -fsS http://127.0.0.1:8766/api/dashboard | \
    /Users/ray/projects/open_trader/.venv/bin/python -m json.tool >/dev/null
  ```

  Expected: both launchd jobs are running from `REPO_ROOT`, the deployed Git SHA is `ACCEPTED_SHA`, logs have post-restart timestamps without startup errors, the review URL returns `200`, and `/api/dashboard` is valid JSON. Provide `http://127.0.0.1:8766/` to the user only after all evidence passes.
