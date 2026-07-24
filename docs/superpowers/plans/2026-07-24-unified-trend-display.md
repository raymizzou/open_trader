# Unified Trend Display Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render CN, US, and HK trend-report buy tables with the same field order and missing-value behavior.

**Architecture:** Keep the existing report projection and market-specific discipline/audit sections. Rename the current CN buy renderer to the market-neutral `renderTrendBuyStage` and call it for every non-options trend report. The existing numeric formatters, execution rows, risk rows, and horizontal-scroll behavior remain shared.

**Tech Stack:** Vanilla JavaScript in `dashboard_static/dashboard.js`, Python tests executing the JavaScript through the existing Node harness, Playwright-backed Dashboard acceptance.

## Global Constraints

- Dashboard renderer/projection, acceptance comparison, and focused tests may
  change; frozen reports and strategy rules remain untouched.
- Prices remain in each instrument's market currency.
- Market cap and daily amount remain normalized CNY billions.
- Legacy local-currency actions receive projection-only normalized CNY fields
  using the existing fixed market rates when no frozen normalized field exists;
  persisted reports and exchange-rate definitions are unchanged.
- Missing business values render as `数据未提供`; do not infer them.

---

### Task 1: Add the failing cross-market rendering assertion

**Files:**
- Modify: `tests/test_dashboard_web.py` near `test_dashboard_renders_action_first_trend_report_for_every_market`

**Interfaces:**
- Consumes: existing `run_dashboard_js()` harness and `renderTrendReportWorkspace()`.
- Produces: a regression assertion that a non-CN trend report has the same buy-table headings as CN and renders null values explicitly.

- [ ] **Step 1: Extend the existing US fixture with unified fields**

Add `filter_price`, `temperature_prev`, `temperature_curr`, `phase`,
`industry_temperature: null`, `market_cap`, and `amount` to its buy action.

- [ ] **Step 2: Assert the unified headings and missing-value copy**

Add this JavaScript assertion after the existing US assertions:

```javascript
for (const text of [
  "筛选价（Trend Animals）", "执行参考价（Futu 前复权）", "温度变化", "节气",
  "行业温度", "市值（亿元）", "日成交额（亿元）", "数据未提供",
]) {
  if (!us.includes(text)) throw new Error(text + "\\n" + us);
}
```

- [ ] **Step 3: Run the focused test and confirm it fails**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_dashboard_web.py::test_dashboard_renders_action_first_trend_report_for_every_market -q
```

Expected: FAIL because `renderTrendReportWorkspace()` currently calls the
market-specific renderer for US/HK.

### Task 2: Use one buy-table renderer for all markets

**Files:**
- Modify: `src/open_trader/dashboard_static/dashboard.js:2494-2577,2818`

**Interfaces:**
- Consumes: the existing projected `report.buy_actions` and `report.risk_skips` fields.
- Produces: `renderTrendBuyStage(report)` with the existing CN column order and row helpers.

- [ ] **Step 1: Rename the current CN renderer without changing its body**

Change the declaration from:

```javascript
function renderCnBuyStage(report) {
```

to:

```javascript
function renderTrendBuyStage(report) {
```

Keep its headings, `cnTrendRows`, `renderTrendRiskRow`,
`renderTrendExecutionRow`, `renderCnTrendTable`, and price-source note unchanged.

- [ ] **Step 2: Route every trend market through the shared renderer**

Change the workspace selection from:

```javascript
const buyStage = isCn ? renderCnBuyStage(report) : renderMarketBuyStage(report);
```

to:

```javascript
const buyStage = renderTrendBuyStage(report);
```

Leave the `isCn` branches for A-share discipline and audit sections intact.

- [ ] **Step 3: Remove the now-unused market buy renderer**

Delete `renderMarketBuyStage`; no callers should remain:

```bash
rg -n "renderMarketBuyStage|renderCnBuyStage" src/open_trader/dashboard_static/dashboard.js tests/test_dashboard_web.py
```

Expected: only the new `renderTrendBuyStage` declaration and its test calls
remain; update direct test calls to use `renderTrendBuyStage`.

- [ ] **Step 4: Run focused frontend tests**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_dashboard_web.py -q
```

Expected: PASS, including the new US/HK heading and `数据未提供` assertions.

### Task 3: Verify and deploy the unified display

**Files:**
- Modify: none beyond Tasks 1–2.

**Interfaces:**
- Consumes: the shared renderer and existing Dashboard API payload.
- Produces: a deployed Dashboard whose CN/US/HK buy tables have identical columns.

- [ ] **Step 1: Run the affected automated suites**

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_dashboard_web.py tests/test_dashboard_acceptance.py -q
```

- [ ] **Step 2: Restart the Dashboard from the current worktree**

Run the existing process replacement sequence:

```bash
old_pid=$(lsof -tiTCP:8766 -sTCP:LISTEN | head -1)
screen -S open_trader_dashboard_8766 -X quit 2>/dev/null || true
if [ -n "$old_pid" ]; then kill -TERM "$old_pid" 2>/dev/null || true; fi
sleep 2
if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null; then kill -KILL "$old_pid"; fi
screen -dmS open_trader_dashboard_8766 zsh -lc 'cd /Users/ray/projects/open_trader/.worktrees/unify-trend-discipline && exec env PYTHONPATH=src .venv/bin/python -u -m open_trader dashboard --portfolio /Users/ray/projects/open_trader/data/latest/portfolio.csv --data-dir /Users/ray/projects/open_trader/data --reports-dir /Users/ray/projects/open_trader/reports --config /Users/ray/projects/open_trader/config/daily_premarket.env --poll-seconds 5 --host 127.0.0.1 --port 8766 >> /tmp/open_trader_dashboard_8766.log 2>&1'
```

Verify the new `dashboard_runtime` line reports the current Git SHA and
worktree path.

- [ ] **Step 3: Run the final acceptance gate**

```bash
make acceptance
```

Expected: `PASS`; then redeploy the exact accepted SHA and verify the new PID,
worktree, SHA, fresh log, and HTTP 200 response at `http://127.0.0.1:8766/`.
