# Dashboard Simulation and Real Account Comparison Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show Futu simulation/report reconciliation beside the existing Tiger real-account comparison, with every visible report number limited to two decimal places.

**Architecture:** Reuse the existing on-demand `/api/trend-simulate-positions/<broker>` endpoint and its browser cache. The report renderer joins report actions to that payload in JavaScript; the existing backend `actual_overlay` remains unchanged and independent.

**Tech Stack:** Python 3.12, pytest, vanilla JavaScript, Playwright, existing Dashboard HTTP APIs.

## Global Constraints

- Do not add an API, background poller, account connection, dependency, or strategy version.
- Simulation data must never fall back to real-account data.
- Real-account comparison remains read-only and cannot affect reports or orders.
- Integers remain integers; displayed decimals use at most two places.
- Run `make acceptance` only once as the final gate; only `PASS` permits deployment.

---

### Task 1: Bound numeric display precision

**Files:**
- Modify: `src/open_trader/dashboard_static/dashboard.js:7095`
- Test: `tests/test_dashboard_web.py`

**Interfaces:**
- Consumes: existing `formatDisplayNumber(value)` callers.
- Produces: the same string-returning function, now using native locale formatting with `maximumFractionDigits: 2`.

- [ ] **Step 1: Write the failing formatter test**

Add a `run_dashboard_js` test that exercises the real formatter:

```python
def test_dashboard_numbers_never_show_more_than_two_decimal_places() -> None:
    output = run_dashboard_js(r'''
console.log(JSON.stringify([
  formatDisplayNumber("485.0"),
  formatDisplayNumber("1296"),
  formatDisplayNumber("30.594999999999995"),
  formatDisplayNumber("23.428857142857142857"),
]));
''')
    assert json.loads(output) == ["485", "1,296", "30.59", "23.43"]
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_dashboard_web.py::test_dashboard_numbers_never_show_more_than_two_decimal_places
```

Expected: `FAIL`; the current function returns the original fractional tails.

- [ ] **Step 3: Apply the native formatter**

Replace only `formatDisplayNumber`:

```javascript
function formatDisplayNumber(value) {
  const raw = formatPlain(value).trim();
  if (!/^([+-]?)(\d+)(\.\d+)?$/.test(raw)) return raw;
  const number = Number(raw);
  return Number.isFinite(number)
    ? number.toLocaleString("zh-CN", {maximumFractionDigits: 2})
    : raw;
}
```

Also reduce `trendKellyPercent` from four displayed decimal places to two, retaining its existing trimming behavior.

- [ ] **Step 4: Verify GREEN and nearby formatting**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_dashboard_web.py::test_dashboard_numbers_never_show_more_than_two_decimal_places \
  tests/test_dashboard_web.py::test_dashboard_renders_read_only_actual_execution_overlay \
  tests/test_dashboard_web.py::test_dashboard_simulate_positions_load_once_and_render_all_states
```

Expected: all selected tests `PASS`.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/dashboard_static/dashboard.js tests/test_dashboard_web.py
git commit -m "fix: bound dashboard number precision"
```

### Task 2: Render simulation and real-account comparisons independently

**Files:**
- Modify: `src/open_trader/dashboard_static/dashboard.js:2800-2880,2170-2260,2579`
- Test: `tests/test_dashboard_web.py`

**Interfaces:**
- Consumes: `state.trendSimulatePositions[broker]`, `report.buy_actions`, `report.sell_actions`, `report.hold_actions`, `report.review_actions`, `report.risk_skips`, and the unchanged `report.actual_overlay`.
- Produces: `renderTrendSimulationOverlay(report, payload) -> string` and DOM rows with `data-simulation-symbol` and `data-deviation` attributes.

- [ ] **Step 1: Write the failing report-view loading test**

Add a JS harness that selects the report tab directly, returns GPN and TOST from the existing endpoint, and asserts one request plus both panels:

```javascript
state.dashboard = {trend_reports:{tiger:{
  available:true,broker:"tiger",broker_label:"老虎",market:"US",
  risk_summary:{},drawdown_summary:{},actual_overlay:{available:true,
    broker_label:"老虎",status_text:"账户实时同步",notice:"只读对照，不影响模拟建议与自动执行",
    items:[],outside_positions:[]},
  sell_actions:[],buy_actions:[{action:"BUY",symbol:"HST",name:"HOST酒店及度假村",
    estimated_shares:"1635",close:"24.44",estimated_initial_line:"23.428857142857"}],
  hold_actions:[{action:"HOLD",symbol:"GPN",name:"环汇有限公司",close:"80.07",active_line:"74.3550"},
    {action:"HOLD",symbol:"TOST",name:"Toast",close:"30.37",active_line:"28.305071428571"}],
  review_actions:[],risk_skips:[],counts:{},audit:{},
}}};
globalThis.fetch = async () => ({ok:true,json:async()=>({available:true,broker:"tiger",positions:[
  {symbol:"GPN",name:"环汇有限公司",quantity:"485.0",cost_price:"80.99",last_price:"80.07"},
  {symbol:"TOST",name:"Toast",quantity:"1296.0",cost_price:"30.594999999999995",last_price:"30.37"},
]})});
await setAccountView("tiger", "report");
```

Assert the resulting HTML contains `模拟盘执行状态`, `富途`, `实盘执行辅助`, `老虎`, `GPN`, `模拟持仓 485`, `TOST`, `模拟持仓 1,296`, two `一致` statuses, and no simulated-row `未持有`.

- [ ] **Step 2: Run the test and verify RED**

Run the exact new test with pytest. Expected: `FAIL` because report selection neither loads nor renders simulation positions.

- [ ] **Step 3: Reuse the existing loader for report view**

Change only the existing conditions:

```javascript
const needsSimulation = view === "simulate" || view === "report";
if (needsSimulation && !Object.hasOwn(state.trendSimulatePositions, broker)) {
  await loadTrendSimulatePositions(broker);
} else {
  renderAccountViewPanelOnly(broker);
}
```

After the fetch, re-render when the selected view is either `simulate` or `report`.

- [ ] **Step 4: Add the minimal reconciliation renderer**

Implement three local helpers in `dashboard.js`:

```javascript
function trendSimulationActions(report) {
  const ordered = ["sell_actions", "buy_actions", "hold_actions", "review_actions", "risk_skips"]
    .flatMap((key) => Array.isArray(report?.[key]) ? report[key] : []);
  const seen = new Set();
  return ordered.filter((item) => {
    const symbol = String(item?.symbol || "").trim().toUpperCase();
    if (!symbol || seen.has(symbol)) return false;
    seen.add(symbol);
    return true;
  });
}

function trendSimulationDeviation(action, quantity) {
  if (action.action === "BUY" && !action.execution) return ["pending", "待执行"];
  if (action.action === "HOLD") return quantity > 0 ? ["followed", "一致"] : ["not_held", "未持有"];
  if (action.action === "SELL_ALL") return quantity === 0 ? ["followed", "一致"] : ["missed_sell", "待卖出"];
  return ["review", "待核对"];
}
```

`renderTrendSimulationOverlay(report, payload)` must:

- render a loading/unavailable message without synthesizing zero positions;
- map positions by uppercase symbol;
- render report actions first and report-external simulation positions afterward;
- show report quantities, simulation quantities, prices, and protection lines through `formatDisplayNumber`;
- use `data-simulation-symbol="<symbol>"` on each row;
- title the primary details element `模拟盘执行状态 · 富途` and keep the unchanged real overlay below it.

Extend the existing function compatibly as
`renderTrendRiskSummary(summary, drawdown, actualOverlay, reportDate, simulationOverlay = "")`
and insert the returned simulation HTML immediately before
`${renderTrendActualOverlay(actualOverlay)}`. Existing four-argument callers keep working;
do not change `actual_overlay` data or labels.

- [ ] **Step 5: Verify GREEN and responsive DOM**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_dashboard_web.py::test_dashboard_report_loads_simulation_and_keeps_real_comparison \
  tests/test_dashboard_web.py::test_dashboard_simulate_positions_load_once_and_render_all_states \
  tests/test_dashboard_web.py::test_dashboard_renders_read_only_actual_execution_overlay \
  tests/test_dashboard_web.py::test_dashboard_risk_summary_and_candidate_cards_fit_375px
```

Expected: all selected tests `PASS`; the existing simulate-tab request remains cached.

- [ ] **Step 6: Commit**

```bash
git add src/open_trader/dashboard_static/dashboard.js tests/test_dashboard_web.py
git commit -m "fix: separate simulation and real account checks"
```

### Task 3: Make acceptance enforce the same contract

**Files:**
- Modify: `src/open_trader/dashboard_acceptance.py:1058-1240,1517-1650`
- Test: `tests/test_dashboard_acceptance.py`

**Interfaces:**
- Consumes: the already-validated `simulate_payloads[broker]` and current report payload.
- Produces: `_check_report_simulation_overlay(report_root, report, simulated, broker) -> None` and `_check_visible_decimal_precision(text, label) -> None`.

- [ ] **Step 1: Write failing acceptance-unit tests**

Add tests proving precision accepts `485`, `1,296`, `30.59`, `23.43` and rejects `30.594999`:

```python
def test_acceptance_rejects_visible_numbers_over_two_decimal_places() -> None:
    dashboard_acceptance._check_visible_decimal_precision(
        "模拟持仓 485 成本 30.59 保护线 23.43", "模拟盘"
    )
    with pytest.raises(AssertionError, match="超过两位小数"):
        dashboard_acceptance._check_visible_decimal_precision(
            "成本 30.594999", "模拟盘"
        )
```

Add a lightweight fake-locator test that requires a GPN row with formatted quantity `485` and deviation `followed`; verify it fails before the helper exists.

- [ ] **Step 2: Run the two new tests and verify RED**

Run both exact pytest node IDs. Expected: `FAIL` because the acceptance helpers are absent.

- [ ] **Step 3: Implement live cross-validation**

Add `_check_report_simulation_overlay` and call it from `_check_trend_account_views` after the report tab opens, passing the already-fetched `simulated` payload.

The helper must assert:

```python
simulation = report_root.locator(".trend-simulation-overlay")
assert simulation.count() == 1
assert "模拟盘执行状态" in simulation.inner_text()
assert "富途" in simulation.inner_text()
```

For every report `HOLD` whose symbol exists in `simulated["positions"]`, locate `[data-simulation-symbol="<symbol>"]`, assert its text contains `_display_number(position["quantity"])`, and assert its status has `data-deviation="followed"`. Never hard-code GPN or TOST in live acceptance.

Implement the precision check with a token regex and run it against visible text in `.trend-risk-summary` and visible report stages:

```python
def _check_visible_decimal_precision(text: str, label: str) -> None:
    offenders = re.findall(r"(?<![\w.-])[+-]?\d[\d,]*\.\d{3,}(?![\w.-])", text)
    assert not offenders, f"{label} 数值超过两位小数：{offenders[:3]}"
```

- [ ] **Step 4: Verify acceptance helpers and focused browser tests**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_dashboard_acceptance.py::test_acceptance_rejects_visible_numbers_over_two_decimal_places \
  tests/test_dashboard_acceptance.py \
  tests/test_dashboard_web.py
```

Expected: all tests `PASS`.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/dashboard_acceptance.py tests/test_dashboard_acceptance.py
git commit -m "test: enforce dashboard account comparison contract"
```

### Task 4: Verify, accept, and redeploy the exact SHA

**Files:**
- No source changes.

**Interfaces:**
- Consumes: completed commits from Tasks 1-3.
- Produces: a live Dashboard and controllers running the exact accepted Git SHA.

- [ ] **Step 1: Run focused and full automated tests**

```bash
.venv/bin/python -m pytest -q tests/test_dashboard_web.py tests/test_dashboard_acceptance.py
.venv/bin/python -m pytest -q
```

Expected: zero failures.

- [ ] **Step 2: Run the candidate Dashboard directly**

Restart the Dashboard from this worktree, verify its fresh log records the candidate SHA and clean source state, then check HTTP 200 and both account panels with the real APIs. Confirm GPN/TOST use the simulation quantities when currently held and real-account rows remain independently labeled.

- [ ] **Step 3: Run the final gate once**

```bash
make acceptance
```

Expected: `{"status": "PASS", ...}`. On `FAIL`, fix and rerun; on `BLOCKED`, report the external blocker.

- [ ] **Step 4: Commit only if verification changed tracked artifacts**

Do not create an empty commit. Confirm `git status --short` is empty.

- [ ] **Step 5: Redeploy the exact accepted SHA**

Reinstall all three trend controllers and restart the Dashboard without changing source or data. Verify new PIDs, worktree, Git SHA, fresh logs, controller heartbeats with `blocker: null`, stable Futu connections, and HTTP 200 at `http://127.0.0.1:8766/`.
