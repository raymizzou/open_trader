# Account-Grouped Holdings Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the separate Tiger strategy panel and merged holdings table with four always-visible broker account sections that combine each account's strategy summary and holdings.

**Architecture:** Keep the Dashboard API unchanged. Build account groups in the existing browser renderer from `broker_summaries`, `source_statuses`, `holdings[].broker_details`, and `tiger_long_term_strategy`; render one semantic account section and table per broker. Preserve the existing holding-detail renderer by carrying the original merged holding beside each broker-specific display row and adding the broker to the row key.

**Tech Stack:** Python 3.12, vanilla JavaScript, HTML/CSS, pytest, Node VM helper tests, Playwright acceptance.

## Global Constraints

- Reuse the existing Dashboard payload; add no backend endpoint, dependency, configuration file, or account strategy engine.
- Show accounts in this order: 富途、老虎、辉立、东方财富.
- Labels are fixed: 富途 `中短线 / 股票与期权`; 老虎 `长线 / SMA200 组合策略`; 辉立 `中线 / 中线策略`; 东方财富 `偏短线 / 趋势交易`.
- All account sections are always expanded; add no collapse state or controls.
- Only Tiger displays real account-strategy metrics. Every other account displays `策略指标待接入`.
- Do not turn single-symbol plans into account-level metrics.
- Preserve market filters, cash view, holding details, research, deep links, and read-only/no-order behavior.
- Do not show English internal status codes.
- Mobile must have no horizontal page or holdings scroll.
- Preserve unrelated user changes already present in the worktree.
- After every modification, run `make acceptance`; only `PASS` is complete.
- After final `PASS`, redeploy the exact accepted SHA and verify PID, working directory, SHA, fresh logs, and HTTP 200.

---

## File Map

- Modify `src/open_trader/dashboard_static/index.html`: remove the broker filter mount and standalone Tiger panel; replace the static holdings table with an account-section mount.
- Modify `src/open_trader/dashboard_static/dashboard.js`: define account profiles, derive broker-specific rows, render account cards/sections/strategy cells, and preserve detail selection.
- Modify `src/open_trader/dashboard_static/dashboard.css`: style account sections and convert tables to mobile cards.
- Modify `src/open_trader/dashboard_acceptance.py`: replace standalone Tiger-panel assertions with account-section assertions.
- Modify `tests/test_dashboard_web.py`: cover grouping, row identity, navigation, strategy joins, empty/error states, and mobile CSS.
- Modify `tests/test_dashboard_acceptance.py`: cover the new browser acceptance helper.
- Do not modify `src/open_trader/dashboard.py`, Tiger strategy modules, or strategy artifacts.

---

### Task 1: Derive Broker Account Groups Without Changing the API

**Files:**
- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Test: `tests/test_dashboard_web.py`

**Interfaces:**
- Consumes: `getHoldings()`, `brokerSummaries()`, `sourceStatuses()`, `rowBrokers(holding)`, and `holding.broker_details`.
- Produces: `ACCOUNT_STRATEGY_PROFILES`, `accountHoldingGroups()`, `accountDisplayRow(holding, detail, summary, portfolioTotal)`, and `accountHoldingKey(broker, holding, index)`.

- [ ] **Step 1: Write a failing broker-specific grouping test**

Add a Node-VM test beside the existing holding-render tests. Give `QQQ` both Futu and Tiger details, then assert the two quantities and keys remain distinct:

```python
def test_dashboard_derives_account_groups_from_existing_broker_details() -> None:
    output = run_dashboard_js(r'''
state.dashboard = {
  summary: {portfolio_value_hkd: "3000"}, broker_summaries: [
    {broker: "futu", portfolio_value_hkd: "1000"},
    {broker: "tiger", portfolio_value_hkd: "2000"},
    {broker: "phillips", portfolio_value_hkd: "0"},
    {broker: "eastmoney", portfolio_value_hkd: "0"},
  ], source_statuses: [], cash_rows: [],
  holdings: [{market: "US", symbol: "QQQ", brokers: "futu;tiger", broker_details: [
    {broker: "futu", account_alias: "futu_1", market: "US", symbol: "QQQ", quantity: "1", market_value_hkd: "700", cost_value: "600", unrealized_pnl: "100"},
    {broker: "tiger", account_alias: "tiger_1", market: "US", symbol: "QQQ", quantity: "2", market_value_hkd: "1600", cost_value: "1100", unrealized_pnl: "500"},
  ]}],
};
console.log(JSON.stringify(accountHoldingGroups().map((group) => ({
  broker: group.broker, horizon: group.profile.horizon,
  rows: group.rows.map((row) => ({key: row.key, quantity: row.display.total_quantity, accountWeight: row.display.account_weight})),
}))));
''')
    groups = json.loads(output)
    assert [group["broker"] for group in groups] == ["futu", "tiger", "phillips", "eastmoney"]
    assert groups[0]["rows"] == [{"key": "futu:US:QQQ:0", "quantity": "1", "accountWeight": "70.00%"}]
    assert groups[1]["rows"] == [{"key": "tiger:US:QQQ:0", "quantity": "2", "accountWeight": "80.00%"}]
```

- [ ] **Step 2: Run the test and verify the missing-helper failure**

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py::test_dashboard_derives_account_groups_from_existing_broker_details -q
```

Expected: `FAIL` because `accountHoldingGroups is not defined`.

- [ ] **Step 3: Add fixed profiles and minimal grouping helpers**

```javascript
const ACCOUNT_STRATEGY_PROFILES = {
  futu: {horizon: "中短线", strategy: "股票与期权"},
  tiger: {horizon: "长线", strategy: "SMA200 组合策略"},
  phillips: {horizon: "中线", strategy: "中线策略"},
  eastmoney: {horizon: "偏短线", strategy: "趋势交易"},
};

function accountHoldingGroups() {
  const portfolioTotal = state.dashboard?.summary?.portfolio_value_hkd;
  return Object.entries(ACCOUNT_STRATEGY_PROFILES).map(([broker, profile]) => {
    const summary = brokerSummaries().find((item) => brokerKey(item) === broker) || {broker};
    const rows = [];
    getHoldings().forEach((holding, index) => {
      const details = (Array.isArray(holding.broker_details) ? holding.broker_details : [])
        .filter((detail) => brokerKey(detail) === broker);
      details.forEach((detail) => rows.push({
        key: accountHoldingKey(broker, holding, index), broker, holding,
        display: accountDisplayRow(holding, detail, summary, portfolioTotal), index,
      }));
      if (!details.length && rowBrokers(holding).length === 1 && rowBrokers(holding)[0] === broker) {
        rows.push({key: accountHoldingKey(broker, holding, index), broker, holding,
          display: accountDisplayRow(holding, null, summary, portfolioTotal), index});
      }
    });
    return {broker, profile, summary, rows};
  });
}

function accountHoldingKey(broker, holding, index) {
  return [broker, holding.market || "", holding.symbol || "", index]
    .map((part) => String(part)).join(":");
}
```

`accountDisplayRow()` maps detail `quantity → total_quantity` and `cost_price → avg_cost_price`, preserves parent analysis fields, calculates `account_weight` from detail value/account total, calculates `portfolio_weight` from detail value/portfolio total, and calculates P&L percentage from `unrealized_pnl / cost_value` only when cost is positive. Missing numbers return `-`, never zero.

- [ ] **Step 4: Add the missing-number test and run both tests**

```bash
.venv/bin/python -m pytest \
  tests/test_dashboard_web.py::test_dashboard_derives_account_groups_from_existing_broker_details \
  tests/test_dashboard_web.py::test_dashboard_account_rows_do_not_turn_unknown_values_into_zero -q
```

Expected: `2 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/dashboard_static/dashboard.js tests/test_dashboard_web.py
git commit -m "refactor: derive holdings by broker account"
```

---

### Task 2: Render Strategy and Holdings Inside Each Account Section

**Files:**
- Modify: `src/open_trader/dashboard_static/index.html`
- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Test: `tests/test_dashboard_web.py`

**Interfaces:**
- Consumes: Task 1 rows shaped as `{key, broker, holding, display, index}` and existing `tiger_long_term_strategy`.
- Produces: `renderAccountHoldings()`, `renderAccountSection(group)`, `renderAccountStrategy(group)`, and `renderAccountStrategyCell(group, row)`.

- [ ] **Step 1: Write failing mount and render tests**

```python
def test_dashboard_static_mounts_account_holdings_without_standalone_tiger_panel() -> None:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert 'id="account-holdings"' in html
    assert 'id="tiger-long-term-panel"' not in html
    assert 'id="header-broker-filters"' not in html
```

The render test must assert all four `id="account-<broker>"` sections, four horizon/strategy pairs, Tiger's four metrics, `多头`, `目标 10%`, `漂移`, and `策略指标待接入`; it must reject `calibration_required` and the standalone panel ID.

- [ ] **Step 2: Run the new tests and verify failure**

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py -k 'account_holdings or account_strategy_sections' -q
```

Expected: failures against the old static table and Tiger panel.

- [ ] **Step 3: Replace static mounts with the account container**

```html
<section class="holdings-panel" aria-labelledby="account-holdings-title">
  <div class="section-heading">
    <div><h1 id="account-holdings-title">持仓与策略</h1><p>按券商账户查看持仓、交易周期与策略状态。</p></div>
    <span id="visible-count" class="count-pill">-</span>
  </div>
  <div id="account-holdings" class="account-holdings" aria-live="polite"></div>
  <div id="cash-detail-panel" class="cash-detail-panel hidden" aria-live="polite"></div>
  <div id="symbol-detail-panel" class="symbol-detail-panel hidden" aria-live="polite"></div>
</section>
```

Delete the static Tiger mount and `header-broker-filters`. Bind the old `holdings-body` element key to `account-holdings` only if needed to keep existing detail tests small.

- [ ] **Step 4: Render all four semantic account sections**

```javascript
function renderAccountSection(group) {
  const headingId = `account-${group.broker}-title`;
  const rows = group.rows.filter(({display}) => state.marketFilter === "ALL"
    || String(display.market || "").toUpperCase() === state.marketFilter);
  return `<section id="account-${escapeHtml(group.broker)}" class="account-section" aria-labelledby="${headingId}">
    <header class="account-section-header">
      <div><h2 id="${headingId}">${escapeHtml(brokerDisplayName(group.summary))}</h2>
      <span>${escapeHtml(group.profile.horizon)} · ${escapeHtml(group.profile.strategy)}</span></div>
      <strong>${escapeHtml(formatMoney(group.summary.portfolio_value_hkd, "HKD"))}</strong>
    </header>
    ${renderAccountStrategy(group)}
    ${rows.length ? renderAccountTable(group, rows) : '<p class="account-empty">当前筛选下没有持仓</p>'}
  </section>`;
}
```

The header also renders existing holding/cash values, count, source text, and the first account alias. Unknown values display `-`.

- [ ] **Step 5: Join Tiger members only inside Tiger**

```javascript
function renderAccountStrategyCell(group, row) {
  if (group.broker !== "tiger") {
    return `<strong>${escapeHtml(group.profile.strategy)}</strong><span>策略指标待接入</span>`;
  }
  const member = tigerMemberBySymbol(row.display.symbol);
  if (!member) return "策略数据缺失";
  const trend = TIGER_TREND_LABELS[member.trend] || "未知";
  const reason = member.eligibility_reason
    ? TIGER_ELIGIBILITY_LABELS[member.eligibility_reason] || "资格条件未满足" : "";
  return `<strong>${escapeHtml(trend)}</strong><span>${escapeHtml(
    `目标 ${decisionPlanWeight(member.target_weight)} · 漂移 ${decisionPlanWeight(member.drift)}${reason ? ` · ${reason}` : ""}`
  )}</span>`;
}
```

Reuse the current Tiger metric-card markup in `renderAccountStrategy(group)`, then delete `renderTigerLongTermStrategy()`.

- [ ] **Step 6: Preserve detail behavior with broker-aware keys**

Flatten `accountHoldingGroups().flatMap(group => group.rows)` when resolving selection. Render `data-detail-key="${row.key}"` with exact market/symbol, but pass `row.holding` to the existing detail renderer. Add a two-broker `QQQ` test proving only the selected account row becomes active and only one inline detail appears. Update deep-link and trade-action restoration to select the first matching account row.

- [ ] **Step 7: Make account cards native page links**

```javascript
return summaries.map((summary) => {
  const broker = brokerKey(summary);
  const profile = ACCOUNT_STRATEGY_PROFILES[broker];
  return `<a class="broker-summary-card" data-broker="${escapeHtml(broker)}" href="#account-${escapeHtml(broker)}">
    <span class="summary-label">${escapeHtml(brokerDisplayName(summary))}</span>
    <span class="account-horizon-label">${escapeHtml(profile.horizon)} · ${escapeHtml(profile.strategy)}</span>
    <strong>${escapeHtml(formatMoney(summary.portfolio_value_hkd, "HKD"))}</strong>
    <span class="summary-note">持仓 ${escapeHtml(formatPlain(summary.holding_count))} · ${escapeHtml(brokerSummarySourceText(summary))}</span>
  </a>`;
}).join("");
```

Remove the broker-filter listener. Keep `state.brokerFilter = "ALL"` for compatibility; do not refactor unrelated money/filter helpers.

- [ ] **Step 8: Run all Dashboard JS tests**

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py -q
```

Expected: all tests pass. Update obsolete market-section/standalone-panel assertions without deleting quote, cash, `交易决策`, `做T`, inline-detail, or deep-link coverage.

- [ ] **Step 9: Commit**

```bash
git add src/open_trader/dashboard_static/index.html src/open_trader/dashboard_static/dashboard.js tests/test_dashboard_web.py
git commit -m "feat: group holdings and strategies by account"
```

---

### Task 3: Make Account Holdings Responsive and Accessible

**Files:**
- Modify: `src/open_trader/dashboard_static/dashboard.css`
- Test: `tests/test_dashboard_web.py`

**Interfaces:**
- Consumes: `.account-holdings`, `.account-section`, `.account-section-header`, `.account-strategy-summary`, `.account-holdings-table`, `.account-holding-row`, `.account-strategy-cell`, and `.account-mobile-label` from Task 2.
- Produces: desktop account cards/tables and a no-horizontal-scroll mobile card layout.

- [ ] **Step 1: Add failing responsive and semantic tests**

Extract the existing `@media (max-width: 760px)` block and assert:

```python
assert ".account-holdings-table thead" in mobile
assert ".account-holding-row" in mobile
assert "grid-template-columns: 1fr;" in mobile
assert ".account-mobile-label" in mobile
assert "overflow-x: hidden;" in mobile
```

Add HTML assertions for four native `href="#account-..."` links, `aria-labelledby`, visible period text, and strategy text so color is not the only state indicator.

- [ ] **Step 2: Run the tests and verify failure**

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py -k 'account_holdings_mobile or account_sections_and_links' -q
```

Expected: failures because the new account classes are unstyled or missing.

- [ ] **Step 3: Add minimal desktop styles using existing tokens**

```css
.account-holdings { display: grid; gap: 14px; padding: 0 14px 14px; }
.account-section { border: 1px solid var(--line); border-radius: 10px; min-width: 0; overflow: hidden; }
.account-section-header { align-items: flex-start; background: var(--surface-soft); display: flex; gap: 16px; justify-content: space-between; padding: 12px; }
.account-strategy-summary { border-top: 1px solid var(--line); padding: 12px; }
.account-holdings-table { border-collapse: collapse; table-layout: fixed; width: 100%; }
.account-strategy-cell { display: grid; gap: 3px; overflow-wrap: anywhere; }
.account-mobile-label { display: none; }
.broker-summary-card { color: inherit; text-decoration: none; }
.broker-summary-card:focus-visible { outline: 3px solid rgba(37, 99, 235, 0.32); outline-offset: 2px; }
```

Reuse current surface, line, shadow, muted, status-pill, metric-card, and numeric styles. Add no color system, accordion selector, chevron, or animation.

- [ ] **Step 4: Convert account tables to mobile cards at 760px**

```css
.account-holdings { padding: 0 10px 10px; }
.account-section-header { display: grid; grid-template-columns: 1fr; }
.account-holdings-table { display: block; overflow-x: hidden; }
.account-holdings-table thead { display: none; }
.account-holdings-table tbody { display: grid; gap: 8px; padding: 8px; }
.account-holding-row { border: 1px solid var(--line); border-radius: 8px; display: grid; grid-template-columns: 1fr; padding: 10px; }
.account-holding-row td { border: 0; display: grid; gap: 8px; grid-template-columns: 88px minmax(0, 1fr); padding: 4px 0; }
.account-mobile-label { color: var(--muted); display: inline; font-size: 11px; font-weight: 700; }
```

Order mobile cells as symbol, HKD value, account/global weight, P&L, market, quantity, price, strategy, then detail actions. Keep action targets at least 44px high.

- [ ] **Step 5: Run Dashboard state and UI tests**

```bash
.venv/bin/python -m pytest tests/test_dashboard.py tests/test_dashboard_web.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/open_trader/dashboard_static/dashboard.css tests/test_dashboard_web.py
git commit -m "style: make account holdings responsive"
```

---

### Task 4: Move the Browser Acceptance Gate to the Account View

**Files:**
- Modify: `src/open_trader/dashboard_acceptance.py`
- Test: `tests/test_dashboard_acceptance.py`

**Interfaces:**
- Consumes: Task 2 account IDs/text and Task 3 responsive layout.
- Produces: `_check_account_holdings(page)` called for desktop and mobile by `_browser_check()`.

- [ ] **Step 1: Replace the Tiger helper test with a failing account-view test**

```python
def test_check_account_holdings_requires_all_profiles_and_tiger_metrics() -> None:
    class Locator:
        def inner_text(self) -> str:
            return (
                "富途 中短线 股票与期权 策略指标待接入 "
                "老虎 长线 SMA200 组合策略 夏普比率 卡玛比率 多头 目标 10% 漂移 "
                "辉立 中线 中线策略 策略指标待接入 "
                "东方财富 偏短线 趋势交易 策略指标待接入"
            )
        def count(self) -> int:
            return 4

    class Page:
        def locator(self, selector: str) -> Locator:
            assert selector in {"#account-holdings", ".account-section"}
            return Locator()
        def evaluate(self, expression: str) -> bool:
            return True

    dashboard_acceptance._check_account_holdings(Page())
```

Add parameterized negative cases that remove one broker, `策略指标待接入`, `夏普比率`, or `卡玛比率` and expect `AssertionError`.

- [ ] **Step 2: Run the helper tests and verify failure**

```bash
.venv/bin/python -m pytest tests/test_dashboard_acceptance.py -k account_holdings -q
```

Expected: failure because `_check_account_holdings` does not exist.

- [ ] **Step 3: Implement the acceptance helper**

```python
def _check_account_holdings(page: Any) -> None:
    text = page.locator("#account-holdings").inner_text()
    for required in (
        "富途", "中短线", "股票与期权", "老虎", "长线", "SMA200 组合策略",
        "辉立", "中线策略", "东方财富", "偏短线", "趋势交易",
        "策略指标待接入", "夏普比率", "卡玛比率", "目标", "漂移",
    ):
        assert required in text, f"账户持仓视图缺少 {required}"
    assert page.locator(".account-section").count() == 4, "账户区块数量不是 4"
    for forbidden in ("tiger-long-term-panel", "calibration_required", "provenance_incomplete"):
        assert forbidden not in text, f"账户持仓视图泄漏内部代码 {forbidden}"
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "页面出现横向滚动"
```

Replace `_check_tiger_panel(page)` with this helper. Click `a[href="#account-tiger"]`, assert Tiger is visible, and assert four account sections still exist.

- [ ] **Step 4: Update fake Playwright objects and run acceptance tests**

Add only the `count()` and `evaluate()` methods required by the helper. Keep the test proving a desktop failure does not skip mobile.

```bash
.venv/bin/python -m pytest tests/test_dashboard_acceptance.py tests/test_dashboard_web.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/dashboard_acceptance.py tests/test_dashboard_acceptance.py tests/test_dashboard_web.py
git commit -m "test: accept account-grouped holdings dashboard"
```

---

### Task 5: Full Live Verification and Exact-SHA Deployment

**Files:**
- Verify only; do not modify source after the accepted commit.

**Interfaces:**
- Consumes: Tasks 1–4 and the real Dashboard data directory.
- Produces: one accepted and deployed Git SHA with live process evidence.

- [ ] **Step 1: Run focused regression tests**

```bash
.venv/bin/python -m pytest tests/test_dashboard.py tests/test_dashboard_web.py tests/test_dashboard_acceptance.py -q
```

Expected: all tests pass.

- [ ] **Step 2: Restart Dashboard on the candidate SHA**

Stop the port-8766 listener, clear `/tmp/open_trader_dashboard_8766.log`, and launch from the implementation worktree with `PYTHONPATH=src`. Verify one PID, the expected worktree directory, candidate SHA, fresh logs, and HTTP 200:

```bash
lsof -nP -iTCP:8766 -sTCP:LISTEN
lsof -a -p "$PID" -d cwd -Fn
git rev-parse HEAD
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8766/
```

- [ ] **Step 3: Run the required gate**

```bash
make acceptance
```

Expected final result:

```json
{"status": "PASS", "errors": [], "blocker": null}
```

On `FAIL`, diagnose, fix, commit, restart, and repeat. On `BLOCKED`, report the external blocker; do not substitute curl, fixtures, screenshots, or unit tests.

- [ ] **Step 4: Redeploy the exact accepted SHA from main**

Fast-forward main only after `PASS`, preserving unrelated dirty files. Restart port 8766 from `/Users/ray/projects/open_trader` without changing source or data.

- [ ] **Step 5: Verify the post-acceptance process**

```bash
lsof -nP -iTCP:8766 -sTCP:LISTEN
lsof -a -p "$PID" -d cwd -Fn
git -C /Users/ray/projects/open_trader rev-parse HEAD
stat -f '%Sm' -t '%Y-%m-%dT%H:%M:%S%z' /tmp/open_trader_dashboard_8766.log
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8766/
rg -n 'Traceback|看板数据加载失败' /tmp/open_trader_dashboard_8766.log
```

Expected: one fresh PID, main directory, exact accepted SHA, fresh log timestamp, HTTP 200, and no error markers.

- [ ] **Step 6: Deliver the review URL**

Provide `http://127.0.0.1:8766`, the accepted SHA, exact `make acceptance` result, and new PID. State that non-Tiger strategy metrics remain intentionally unimplemented.
