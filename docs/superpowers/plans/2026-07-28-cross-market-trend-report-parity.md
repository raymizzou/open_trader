# Cross-Market Trend Report Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make CN, US, and HK trend reports render identical action-table columns and expose temperature/phase facts for every action stage without rewriting frozen reports.

**Architecture:** Replace the market-selected Dashboard renderers with one shared buy renderer and one shared sell/review/hold renderer. Add `phase` to generated holding decisions, and enrich only the in-memory Dashboard projection from frozen holding snapshots for old artifacts that predate the field.

**Tech Stack:** Python `pytest`, JavaScript renderer evaluated by the existing Node harness, frozen JSON report artifacts, local Dashboard acceptance workflow.

## Global Constraints

- Use the existing local `main` baseline and the isolated worktree `fix/unify-trend-report-tables`.
- Do not rewrite frozen report JSON files or alter strategy, entry, exit, risk, execution, or market-data rules.
- Same action stage across CN, US, and HK uses the same column names, order, missing-value label, and responsive behavior.
- Preserve truthful market-specific prices, currencies, broker sources, trade windows, and field values.
- Run `make acceptance` only as the final Dashboard gate; only `PASS` is review-ready.

## Task 1: Lock the shared table contract with failing web tests

**Files:**
- Modify: `tests/test_dashboard_web.py` near `test_dashboard_renders_action_first_trend_report_for_every_market`
- Test: `tests/test_dashboard_web.py`

**Interfaces:**
- Consumes: `renderTrendReportWorkspace`, `renderCnTrendTable`, and the existing `run_dashboard_js` helper.
- Produces: regression assertions that extract each stage's `<th>` labels and require identical contracts for CN, US, and HK.

- [ ] **Step 1: Write the failing test**

Add one test that renders the same minimal action payload for `CN`, `US`, and `HK`, then asserts:

```javascript
const expectedBuy = ["标的", "动作", "筛选价（Trend Animals）", "执行参考价", "温度变化", "节气", "强度", "行业", "行业温度", "行业确认", "市值（亿元）", "日成交额（亿元）", "目标仓位（占净值）", "目标金额", "预计数量", "预计保护线"];
const expectedHold = ["标的", "动作", "执行参考价", "温度变化", "节气", "强度", "当前判断", "活动保护线", "持仓提示"];
const headers = (html, title) => {
  const section = html.slice(html.indexOf(`<h2>${title}</h2>`));
  return [...section.matchAll(/<th scope="col">([^<]+)<\/th>/g)].map((match) => match[1]);
};
for (const market of ["CN", "US", "HK"]) {
  const html = renderTrendReportWorkspace(report(market));
  if (JSON.stringify(headers(html, "09:30–10:00 · 正式买入计划")) !== JSON.stringify(expectedBuy)) throw new Error(market + " buy columns");
  if (JSON.stringify(headers(html, "盘中持续 · 已有持仓")) !== JSON.stringify(expectedHold)) throw new Error(market + " hold columns");
  for (const text of ["温 → 热", "立夏"]) if (!html.includes(text)) throw new Error(market + " missing " + text);
}
```

Use distinct sell and review rows in the same payload and assert their headers use the same expected hold contract with only the reason heading changed to `触发原因` or `复核原因`.

- [ ] **Step 2: Run the test to verify it fails for the current renderer**

Run:

```bash
PYTHONPATH=.:src .venv/bin/pytest -q tests/test_dashboard_web.py -k cross_market_trend_report_parity
```

Expected: `FAIL`, because current US/HK renderers omit temperature, phase, and the complete buy fields.

- [ ] **Step 3: Update existing direct-renderer tests to call the shared function names**

Replace direct test calls to `renderCnSellOrHoldStage`, `renderMarketSellOrHoldStage`, `renderCnBuyStage`, and `renderMarketBuyStage` with `renderTrendSellOrHoldStage` and `renderTrendBuyStage`, retaining their existing assertions.

- [ ] **Step 4: Run the focused web tests again**

Run:

```bash
PYTHONPATH=.:src .venv/bin/pytest -q tests/test_dashboard_web.py -k 'trend_stages or cross_market_trend_report_parity or action_first_trend_report'
```

Expected: the new parity test remains red while the test collection succeeds.

## Task 2: Lock phase projection and generation with failing Python tests

**Files:**
- Modify: `tests/test_dashboard.py` near the existing `_project_trend_actions` tests
- Modify: `tests/test_a_share_trend.py` near `HoldingDecision`/`build_report` serialization tests

**Interfaces:**
- Consumes: `_project_trend_actions(payload, {})`, `build_report`, and `HoldingDecision` serialization.
- Produces: tests proving old frozen payloads project `phase` from `signal_snapshots.holdings`, and new generated holding decisions serialize `phase` directly.

- [ ] **Step 1: Write the failing Dashboard projection test**

Use a payload with a `HOLD` holding decision containing `symbol: "00939"` but no phase and a matching `signal_snapshots.holdings["00939"].phase: "立夏"`. Assert the returned hold item has `phase == "立夏"` and leaves a missing snapshot phase as `None`.

- [ ] **Step 2: Run the projection test to verify it fails**

Run:

```bash
PYTHONPATH=.:src .venv/bin/pytest -q tests/test_dashboard.py -k holding_phase_projection
```

Expected: `FAIL` because `_project_trend_actions` currently copies holding decisions without snapshot enrichment.

- [ ] **Step 3: Write the failing report-generation test**

Build a minimal report with a holding snapshot containing `phase="立夏"`, serialize it, and assert `strategy_judgments.holding_decisions[0]["phase"] == "立夏"`.

- [ ] **Step 4: Run the generation test to verify it fails**

Run:

```bash
PYTHONPATH=.:src .venv/bin/pytest -q tests/test_a_share_trend.py -k holding_decision_phase
```

Expected: `FAIL` because `HoldingDecision` has no phase field.

## Task 3: Implement the minimum shared projection and renderer

**Files:**
- Modify: `src/open_trader/dashboard.py:_project_trend_actions`
- Modify: `src/open_trader/a_share_trend.py:HoldingDecision` and the holding decision construction in `build_report`
- Modify: `src/open_trader/dashboard_static/dashboard.js` around the current market-specific renderers

**Interfaces:**
- Consumes: the tests from Tasks 1 and 2.
- Produces: `renderTrendBuyStage(report)`, `renderTrendSellOrHoldStage(title, items, kind, report)`, and projected/generated holding items carrying `phase`.

- [ ] **Step 1: Add the generated `phase` field**

Add `phase: str | None = None` to `HoldingDecision`, pass `snapshot.phase if snapshot else None` when building a holding decision, and let existing dataclass serialization include it.

- [ ] **Step 2: Add read-only legacy phase enrichment**

In `_project_trend_actions`, build a symbol-to-snapshot map from `payload.get("signal_snapshots", {}).get("holdings", {})`. For each holding decision, copy the item and set `phase` only when the item does not already have a value and the matching snapshot has one. Do not write the payload back to disk.

- [ ] **Step 3: Replace the market-specific JavaScript renderers**

Create one complete `renderTrendSellOrHoldStage` using the CN field contract plus `节气`; use `数据未提供` for absent values. Create one complete `renderTrendBuyStage` with the 16-column contract and neutral `执行参考价`. Preserve action labels, reason labels, audit content, execution detail rows, and mobile horizontal scrolling.

- [ ] **Step 4: Remove market-based renderer selection**

Make `renderCnTrendReportWorkspace` call the shared renderers for every market. Remove the `isCn` renderer branch and the old market-specific function definitions. Leave market-specific discipline/audit content untouched.

- [ ] **Step 5: Run the focused tests to verify green**

Run:

```bash
PYTHONPATH=.:src .venv/bin/pytest -q tests/test_dashboard_web.py -k 'trend_stages or cross_market_trend_report_parity or action_first_trend_report'
PYTHONPATH=.:src .venv/bin/pytest -q tests/test_dashboard.py -k holding_phase_projection
PYTHONPATH=.:src .venv/bin/pytest -q tests/test_a_share_trend.py -k holding_decision_phase
```

Expected: all focused tests pass.

## Task 4: Regression sweep and live report proof

**Files:**
- Modify: `CHANGELOG.md` with a dated operator-facing entry for 2026-07-28

- [ ] **Step 1: Run the broader affected test suites**

Run:

```bash
PYTHONPATH=.:src .venv/bin/pytest -q tests/test_dashboard.py tests/test_dashboard_web.py tests/test_a_share_trend.py
```

- [ ] **Step 2: Render live API reports through the shared workspace**

Fetch `http://127.0.0.1:8766/api/dashboard`, render current CN/US/HK payloads with the same JavaScript workspace, and assert each market's buy/hold/review/sell `<th>` arrays match the contract. Confirm live HK `00939` renders its phase from the frozen snapshot projection.

- [ ] **Step 3: Add the changelog entry and commit source/tests/log**

Document that cross-market trend report action tables now share one complete schema and that old frozen holdings receive phase through read-only projection enrichment.

- [ ] **Step 4: Run `make acceptance` once as the final gate**

Expected: `PASS`; if `FAIL`, continue fixing and rerun; if `BLOCKED`, report the external blocker without substituting local tests.

- [ ] **Step 5: Commit the accepted source SHA**

Commit the implementation, tests, and changelog only after the acceptance result is captured.

## Task 5: Redeploy the exact accepted SHA and verify the live surface

- [ ] **Step 1: Restart the Dashboard from the accepted worktree/SHA**

Stop the process serving port 8766, start it with the accepted worktree, and preserve the existing runtime configuration.

- [ ] **Step 2: Verify runtime identity and fresh output**

Check PID, process cwd, `git_sha`, fresh startup log timestamp, and HTTP 200 from `http://127.0.0.1:8766`.

- [ ] **Step 3: Re-fetch the live API and confirm table parity**

Run the same header comparison against the restarted process and confirm the current HK/US pages include `温度变化` and `节气`.
