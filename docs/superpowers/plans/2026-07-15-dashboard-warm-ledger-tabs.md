# Dashboard Warm Ledger Tabs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Apply the approved warm-ledger visual system to every Dashboard surface, replace the four expanded account sections with one broker tab at a time, remove the cash view, and format read-only numbers with thousands separators.

**Architecture:** Keep the existing plain HTML/CSS/JavaScript Dashboard and reuse `state.brokerFilter` as the selected broker tab. Centralize workspace visibility and numeric display formatting in `dashboard.js`; use existing payloads and account grouping without backend changes. Update the real acceptance checker to exercise each tab because only one account section will exist in the DOM at a time.

**Tech Stack:** HTML5, CSS custom properties, vanilla JavaScript, Python pytest with Node VM checks, Playwright TypeScript, existing `screen` deployment.

## Global Constraints

- Base colors are background `#FAFAF9`, surface `#FFFFFF`, primary text `#1C1917`, secondary text `#78716C`, accent `#A16207`, and border `#D6D3D1`.
- Broker identity remains visible through small blue/orange/green/red lines, dots, and selected-tab indicators; do not use large tinted backgrounds.
- Profit is red and loss is green; normal/success system state remains green and failed/risk state remains red; text and signs remain visible.
- The largest asset value always represents all accounts and never changes with broker tabs or market filters.
- Broker tabs are exactly `富途 / 老虎 / 辉立 / 东方财富`; there is no all-accounts tab.
- Market filters are exactly `全部市场 / US / HK / A 股`; remove the cash filter and standalone cash detail.
- Read-only money, price, quantity, backtest metrics, and Kelly metrics use thousands separators while preserving source precision; symbols, identifiers, dates, percentages, and editable inputs do not.
- Strategy backtest, Kelly Lab, trend reports, inline decision details, and research chat all use the same warm-ledger visual system.
- Do not add dependencies, fonts, icon packages, APIs, backend models, strategy behavior, trading behavior, or notification behavior.
- Run focused tests during implementation. Run `make acceptance` only once, immediately before asking the user to review the completed Dashboard.
- Preserve unrelated untracked files already present in the workspace.

---

## File Map

- `src/open_trader/dashboard_static/index.html`: static mounts, top strategy-tool buttons, broker-tab mount, removal of cash controls.
- `src/open_trader/dashboard_static/dashboard.js`: selected broker state, one-account rendering, workspace visibility, display-number formatting, P/L classes.
- `src/open_trader/dashboard_static/dashboard.css`: warm-ledger tokens, page layouts, broker accents, responsive tabs, all inner-surface styling.
- `tests/test_dashboard_web.py`: static contracts and Node VM behavior tests for tabs, workspaces, formatting, and style tokens.
- `tests/e2e/dashboard-warm-ledger.spec.ts`: real browser desktop/mobile flows against the existing fixture server.
- `src/open_trader/dashboard_acceptance.py`: final real-data acceptance flow updated from four simultaneous sections to four selected tabs.
- `tests/test_dashboard_acceptance.py`: fake Playwright contracts for the updated acceptance flow.

---

### Task 1: Replace expanded account sections and cash view with broker tabs

**Files:**
- Modify: `src/open_trader/dashboard_static/index.html`
- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Modify: `tests/test_dashboard_web.py`

**Interfaces:**
- Consumes: existing `state.brokerFilter`, `ACCOUNT_STRATEGY_PROFILES`, `accountHoldingGroups()`, `renderAccountSection(group)`, and `renderBrokerSummaryCards()`.
- Produces: `ACCOUNT_BROKERS: string[]`, `renderAccountTabs(groups): string`, and `selectBroker(broker): void`; later tasks rely on `state.brokerFilter` always being a concrete broker.

- [ ] **Step 1: Replace static expectations with a failing broker-tab contract**

Add these assertions near the existing account-holdings static tests in `tests/test_dashboard_web.py`:

```python
def test_dashboard_static_mounts_broker_tabs_and_removes_cash_view() -> None:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    js = (STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")

    assert 'id="account-tabs"' in html
    assert 'data-market="CASH"' not in html
    assert 'id="cash-detail-panel"' not in html
    assert 'id="open-kelly-lab"' in html
    assert 'id="kelly-lab-panel"' in html
    assert 'state.brokerFilter = "futu"' not in js  # state is initialized in the literal
    assert 'brokerFilter: "futu"' in js
    assert "function renderAccountTabs(" in js
    assert "function selectBroker(" in js
```

Replace the old semantic test that expects four account sections and anchor links with this Node VM behavior test:

```python
def test_dashboard_renders_one_selected_broker_tab_and_cards_switch_it() -> None:
    output = run_dashboard_js(r'''
const mount = () => ({innerHTML:"", textContent:"", classList:{add(){},remove(){}}});
for (const id of ["account-tabs","account-holdings","visible-count","workspace-grid","symbol-detail-panel"]) elements[id]=mount();
state.dashboard={
  summary:{portfolio_value_hkd:"4000"}, source_statuses:[], cash_rows:[],
  broker_summaries:[
    {broker:"futu",display_name:"富途",portfolio_value_hkd:"1000",holding_count:"1"},
    {broker:"tiger",display_name:"老虎",portfolio_value_hkd:"1000",holding_count:"1"},
    {broker:"phillips",display_name:"辉立",portfolio_value_hkd:"1000",holding_count:"0"},
    {broker:"eastmoney",display_name:"东方财富",portfolio_value_hkd:"1000",holding_count:"0"},
  ],
  holdings:[
    {market:"US",symbol:"AAPL",brokers:"futu",broker_details:[{broker:"futu",market:"US",symbol:"AAPL",quantity:"1"}]},
    {market:"US",symbol:"QQQ",brokers:"tiger",broker_details:[{broker:"tiger",market:"US",symbol:"QQQ",quantity:"2"}]},
  ],
};
renderAccountHoldings();
const first={broker:state.brokerFilter,tabs:elements["account-tabs"].innerHTML,html:elements["account-holdings"].innerHTML};
selectBroker("tiger");
const second={broker:state.brokerFilter,tabs:elements["account-tabs"].innerHTML,html:elements["account-holdings"].innerHTML};
console.log(JSON.stringify({first,second,cards:renderBrokerSummaryCards()}));
''')
    result = json.loads(output)
    assert result["first"]["broker"] == "futu"
    assert 'aria-selected="true"' in result["first"]["tabs"]
    assert 'id="account-futu"' in result["first"]["html"]
    assert 'id="account-tiger"' not in result["first"]["html"]
    assert result["second"]["broker"] == "tiger"
    assert 'id="account-tiger"' in result["second"]["html"]
    assert 'data-broker="tiger"' in result["cards"]
    assert 'href="#account-tiger"' not in result["cards"]
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_dashboard_web.py \
  -k 'mounts_broker_tabs_and_removes_cash_view or renders_one_selected_broker_tab'
```

Expected: both tests fail because the cash controls and Kelly entry still exist, `brokerFilter` defaults to `ALL`, and broker tabs are not rendered.

- [ ] **Step 3: Make the minimum DOM changes**

In `index.html`:

```html
<div class="strategy-tools" aria-label="策略工具">
  <button id="open-standard-backtest" class="secondary-button" type="button">策略回测</button>
  <button id="open-kelly-lab" class="secondary-button" type="button">凯利实验室</button>
  <button id="return-to-portfolio" class="secondary-button hidden" type="button" hidden>返回持仓</button>
</div>
```

Delete the `data-market="CASH"` button from both visible and compatibility filters. Delete `#cash-detail-panel`. Add this immediately below the holdings heading:

```html
<div id="account-tabs" class="account-tab-list" role="tablist" aria-label="券商账户"></div>
```

Keep `#kelly-lab-panel` as the Kelly workspace mount; do not keep any homepage entry copy inside it.

- [ ] **Step 4: Reuse the existing broker filter as tab state**

In `dashboard.js`, initialize and validate one concrete broker:

```javascript
const ACCOUNT_BROKERS = Object.keys(ACCOUNT_STRATEGY_PROFILES);

const state = {
  // existing fields stay unchanged
  marketFilter: "ALL",
  brokerFilter: "futu",
};

function renderAccountTabs(groups) {
  const counts = new Map(groups.map((group) => [group.broker, group.rows.length]));
  return ACCOUNT_BROKERS.map((broker) => {
    const selected = broker === state.brokerFilter;
    return `<button class="account-tab ${selected ? "active" : ""}" type="button" role="tab"
      data-broker="${escapeHtml(broker)}" aria-selected="${selected}">
      ${escapeHtml(brokerDisplayName(broker))}<span>${escapeHtml(formatPlain(counts.get(broker) || 0))}</span>
    </button>`;
  }).join("");
}

function selectBroker(broker) {
  if (!ACCOUNT_BROKERS.includes(broker)) return;
  state.brokerFilter = broker;
  state.selectedHoldingKey = "";
  state.selectedHoldingDetail = "decision";
  renderAccountHoldings();
}
```

Bind one delegated click handler to `#account-tabs` and one to `#broker-summary-cards`; both call `selectBroker(button.dataset.broker || "")`. Render broker cards as native buttons instead of hash links:

```javascript
return `<button class="broker-summary-card" type="button" data-broker="${escapeHtml(broker)}">
  ${content}
</button>`;
```

Change `renderAccountHoldings()` to render all four tabs but only one section:

```javascript
const groups = accountHoldingGroups();
const active = groups.find((group) => group.broker === state.brokerFilter) || groups[0];
if (active && active.broker !== state.brokerFilter) state.brokerFilter = active.broker;
elements["account-tabs"].innerHTML = renderAccountTabs(groups);
const rows = active ? active.rows.filter(({display}) => state.marketFilter === "ALL"
  || String(display.market || "").toUpperCase() === state.marketFilter) : [];
elements["visible-count"].textContent = `${formatPlain(rows.length)} 条`;
container.innerHTML = active ? renderAccountSection({...active, rows}) : '<div class="empty-state">暂无券商账户</div>';
```

Make the large asset card use the unfiltered payload summary and change its
static label in `index.html` to `全部账户总资产 HKD`:

```javascript
function renderHeaderSummary() {
  const summary = state.dashboard?.summary || {};
  elements["current-view-value"].textContent = formatMoney(summary.portfolio_value_hkd, "HKD");
  elements["current-view-holding-value"].textContent = `持仓资产 ${formatMoney(summary.holding_value_hkd, "HKD")}`;
  elements["current-view-holding-weight"].textContent = formatPlain(summary.holding_weight_hkd);
  elements["current-view-cash-note"].textContent = `现金类资产 ${formatMoney(summary.cash_like_value_hkd, "HKD")} · 持仓 ${formatPlain(summary.holding_count)}`;
  elements["current-view-label"].textContent = currentViewLabel(activeAccountRowCount());
}

function activeAccountRowCount() {
  const group = accountHoldingGroups().find((item) => item.broker === state.brokerFilter);
  if (!group) return 0;
  return group.rows.filter(({display}) => state.marketFilter === "ALL"
    || String(display.market || "").toUpperCase() === state.marketFilter).length;
}
```

`activeAccountRowCount()` counts the selected broker after the current market
filter. It affects only the small current-view copy, never the large asset card.

Remove the `CASH` branch from `renderAccountHoldings()` and delete cash-view-only helpers after `rg` confirms no callers. Keep broker summary cash amounts because they feed each account header.

Update `restoreDecisionDeepLink()` to flatten `{broker, row}` pairs and select the first matching broker in `ACCOUNT_BROKERS` order; never restore `ALL`.

- [ ] **Step 5: Run the focused account tests**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_dashboard_web.py \
  -k 'account or broker or cash_view or deep_link'
```

Expected: PASS. Existing tests that asserted four simultaneous account sections must be rewritten to click/render each broker rather than weakened or deleted.

- [ ] **Step 6: Commit the account-tab behavior**

```bash
git add src/open_trader/dashboard_static/index.html \
  src/open_trader/dashboard_static/dashboard.js tests/test_dashboard_web.py
git commit -m "feat: switch dashboard accounts with broker tabs"
```

---

### Task 2: Centralize portfolio, Kelly, backtest, and trend workspace navigation

**Files:**
- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Modify: `src/open_trader/dashboard_static/index.html`
- Modify: `tests/test_dashboard_web.py`

**Interfaces:**
- Consumes: Task 1 element IDs and `state.brokerFilter`.
- Produces: `setWorkspaceView(view): void`, `returnToPortfolio(): void`, and `.tool-workspace-view`; CSS and browser tests rely on these names.

- [ ] **Step 1: Write a failing shared-workspace state test**

Add:

```python
def test_dashboard_workspace_navigation_uses_one_shared_state_machine() -> None:
    output = run_dashboard_js(r'''
const element=()=>({hidden:false,innerHTML:"",classList:{values:new Set(),add(...n){n.forEach(x=>this.values.add(x))},remove(...n){n.forEach(x=>this.values.delete(x))},toggle(n,f){f?this.add(n):this.remove(n)},contains(n){return this.values.has(n)}}});
for(const id of ["dashboard-shell","workspace-grid","kelly-lab-panel","holdings-panel","standard-backtest-workspace","trend-report-workspace","return-to-portfolio"])elements[id]=element();
for(const view of ["kelly_lab","standard_backtest","trend_report","portfolio"]){
  setWorkspaceView(view);
  console.log(JSON.stringify({view:state.workspaceView,tool:elements["dashboard-shell"].classList.contains("tool-workspace-view"),backHidden:elements["return-to-portfolio"].hidden}));
}
''')
    states = [json.loads(line) for line in output.splitlines()]
    assert states == [
        {"view": "kelly_lab", "tool": True, "backHidden": False},
        {"view": "standard_backtest", "tool": True, "backHidden": False},
        {"view": "trend_report", "tool": True, "backHidden": False},
        {"view": "portfolio", "tool": False, "backHidden": True},
    ]
```

- [ ] **Step 2: Run the test and verify RED**

```bash
.venv/bin/python -m pytest -q tests/test_dashboard_web.py \
  -k shared_workspace_state_machine
```

Expected: FAIL because `setWorkspaceView()` currently accepts only portfolio/Kelly and does not own the other workspaces.

- [ ] **Step 3: Extend the existing workspace function instead of adding another controller**

Add `id="dashboard-shell"` to the existing `.dashboard-shell` root and
`id="holdings-panel"` to the existing `.holdings-panel` section in `index.html`.
Register both IDs in `bindElements()` so the workspace function does not query
or recreate either node. Remove the workspace-local `#close-standard-backtest`
button, the Kelly `返回主页` buttons, and the dynamically rendered
`data-close-trend-report` button; `#return-to-portfolio` is the single visible
return action in tool mode.

Use one allowed set and one chrome renderer:

```javascript
const WORKSPACE_VIEWS = new Set(["portfolio", "kelly_lab", "standard_backtest", "trend_report"]);

function setWorkspaceView(view) {
  state.workspaceView = WORKSPACE_VIEWS.has(view) ? view : "portfolio";
  renderWorkspaceChrome();
  if (state.workspaceView === "kelly_lab") renderKellyLab();
}

function renderWorkspaceChrome() {
  const view = state.workspaceView;
  const toolView = view !== "portfolio";
  elements["dashboard-shell"].classList.toggle("tool-workspace-view", toolView);
  elements["return-to-portfolio"].hidden = !toolView;
  elements["return-to-portfolio"].classList.toggle("hidden", !toolView);
  elements["workspace-grid"].classList.toggle("hidden", view === "standard_backtest" || view === "trend_report");
  elements["holdings-panel"].classList.toggle("hidden", view !== "portfolio");
  elements["kelly-lab-panel"].classList.toggle("hidden", view !== "kelly_lab");
  elements["standard-backtest-workspace"].hidden = view !== "standard_backtest";
  elements["standard-backtest-workspace"].classList.toggle("hidden", view !== "standard_backtest");
  elements["trend-report-workspace"].hidden = view !== "trend_report";
  elements["trend-report-workspace"].classList.toggle("hidden", view !== "trend_report");
}
```

Make `renderKellyLabPanel()` return an empty string outside `kelly_lab` and delete `renderKellyLabEntry()`.

`openStandardBacktest()` must call `setWorkspaceView("standard_backtest")` before loading options. `openTrendReport()` renders report HTML and then calls `setWorkspaceView("trend_report")`. Both close functions call `returnToPortfolio()`.

```javascript
function returnToPortfolio() {
  const trendBroker = state.selectedTrendBroker;
  if (state.workspaceView === "standard_backtest") syncStandardBacktestInputs();
  state.selectedTrendBroker = "";
  setWorkspaceView("portfolio");
  renderAccountHoldings();
  if (trendBroker) {
    document.querySelector(`#account-${trendBroker} [data-trend-report]`)?.focus();
  }
}
```

Bind `#open-kelly-lab` to `setWorkspaceView("kelly_lab")` and
`#return-to-portfolio` to `returnToPortfolio()`. After opening a trend report,
focus `#return-to-portfolio` instead of a workspace-local close button.

- [ ] **Step 4: Run workspace, Kelly, backtest, and trend tests**

```bash
.venv/bin/python -m pytest -q tests/test_dashboard_web.py \
  -k 'workspace or kelly or backtest or trend_report'
```

Expected: PASS, including focus restoration through the shared return button
and existing backtest request behavior. Update Kelly E2E copy from `返回主页`
to `返回持仓` when Task 4 touches that file.

- [ ] **Step 5: Commit the shared workspace state**

```bash
git add src/open_trader/dashboard_static/index.html \
  src/open_trader/dashboard_static/dashboard.js tests/test_dashboard_web.py
git commit -m "refactor: unify dashboard workspace navigation"
```

---

### Task 3: Format read-only numbers and add Chinese-market P/L colors

**Files:**
- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Modify: `tests/test_dashboard_web.py`

**Interfaces:**
- Consumes: existing `formatPlain(value)`, `numericValue(value)`, and render helpers.
- Produces: `formatDisplayNumber(value): string` and `pnlClass(value): string`; CSS uses `pnl-profit` and `pnl-loss`.

- [ ] **Step 1: Write failing exact-format tests**

```python
def test_dashboard_display_number_preserves_precision_and_identifiers() -> None:
    output = run_dashboard_js(r'''
console.log(JSON.stringify({
  money: formatDisplayNumber("3064187.62"),
  integer: formatDisplayNumber("10000"),
  trailing: formatDisplayNumber("2932.00"),
  signed: formatDisplayNumber("+1234567.50"),
  symbol: formatPlain("02840"),
  percent: formatPlain("21.13%"),
  input: "100000",
  profit: pnlClass("12.50%"),
  loss: pnlClass("-12.50%"),
}));
''')
    assert json.loads(output) == {
        "money": "3,064,187.62",
        "integer": "10,000",
        "trailing": "2,932.00",
        "signed": "+1,234,567.50",
        "symbol": "02840",
        "percent": "21.13%",
        "input": "100000",
        "profit": "pnl-profit",
        "loss": "pnl-loss",
    }


def test_dashboard_account_table_formats_values_but_not_symbol() -> None:
    output = run_dashboard_js(r'''
console.log(renderAccountTable([{key:"futu:HK:02840:0",holding:{},display:{
  market:"HK",symbol:"02840",name:"SPDR 金",total_quantity:"10000",
  avg_cost_price:"2932.00",market_value_hkd:"31845000.00",
  account_weight:"3.28%",portfolio_weight:"1.04%",unrealized_pnl_pct:"-1.26%"
}}]));
''')
    assert "10,000" in output
    assert "2,932.00" in output
    assert "HKD 31,845,000.00" in output
    assert ">02840<" in output
    assert 'class="number-cell account-holding-pnl pnl-loss"' in output
```

- [ ] **Step 2: Run the tests and verify RED**

```bash
.venv/bin/python -m pytest -q tests/test_dashboard_web.py \
  -k 'display_number or account_table_formats_values'
```

Expected: FAIL because `formatDisplayNumber()` and `pnlClass()` do not exist and table numbers are raw strings.

- [ ] **Step 3: Add a string-safe formatter that never converts through float**

```javascript
function formatDisplayNumber(value) {
  const raw = formatPlain(value).trim();
  const match = raw.match(/^([+-]?)(\d+)(\.\d+)?$/);
  if (!match) return raw;
  const [, sign, integer, fraction = ""] = match;
  return `${sign}${integer.replace(/\B(?=(\d{3})+(?!\d))/g, ",")}${fraction}`;
}

function formatMoney(value, currency) {
  if (!hasValue(value)) return "-";
  return `${currency} ${formatDisplayNumber(value)}`;
}

function pnlClass(value) {
  const number = numericValue(String(value || "").replace("%", ""));
  return number > 0 ? "pnl-profit" : number < 0 ? "pnl-loss" : "";
}
```

Use `formatDisplayNumber()` only at known numeric display sites. Update these functions in this task:

- `renderAccountTable()` for quantity, costs, prices, USD/HKD values, and P/L class;
- `renderQuotePrice()` and `renderUsdMarketValue()`;
- `formatCapitalMoney()` and numeric cells in `renderKellyStrategyCapital()`, order-sync, and execution rows;
- `renderStandardBacktestResult()`, `renderBacktestComparisonMetrics()`, `renderBacktestTradeTable()`, and `renderBacktestRunAssumptions()`;
- numeric-only values in trend metrics and decision metric cards.

Do not call `formatDisplayNumber()` for symbols, account aliases, dates, times, percentages, status strings, or form values. Keep `#backtest-initial-cash` and `state.standardBacktest.initialCash` as `100000`.

- [ ] **Step 4: Run all Dashboard JavaScript render tests**

```bash
.venv/bin/python -m pytest -q tests/test_dashboard_web.py
```

Expected: PASS. Update exact string assertions such as `USD 30000` to `USD 30,000` only where the output is a read-only numeric display.

- [ ] **Step 5: Commit number formatting**

```bash
git add src/open_trader/dashboard_static/dashboard.js tests/test_dashboard_web.py
git commit -m "feat: format dashboard display numbers"
```

---

### Task 4: Apply the warm-ledger visual system across all Dashboard surfaces

**Files:**
- Modify: `src/open_trader/dashboard_static/dashboard.css`
- Modify: `tests/test_dashboard_web.py`
- Create: `tests/e2e/dashboard-warm-ledger.spec.ts`
- Modify: `tests/e2e/kelly-lab.spec.ts`

**Interfaces:**
- Consumes: Task 1 `.account-tab`, Task 2 `.tool-workspace-view`, and Task 3 P/L classes.
- Produces: one token-driven style system used by portfolio, Kelly, backtest, trend, inline detail, and research-chat surfaces.

- [ ] **Step 1: Change static CSS assertions to the approved visual contract**

Replace old command-center token/tint assertions in `tests/test_dashboard_web.py` with:

```python
def test_dashboard_warm_ledger_theme_and_broker_accents() -> None:
    css = (STATIC_DIR / "dashboard.css").read_text(encoding="utf-8")
    for token in (
        "--bg: #fafaf9;", "--surface: #ffffff;", "--text: #1c1917;",
        "--muted: #78716c;", "--accent: #a16207;", "--line: #d6d3d1;",
    ):
        assert token in css
    for broker, color in {
        "futu": "#2563eb", "tiger": "#d97706",
        "phillips": "#15803d", "eastmoney": "#dc2626",
    }.items():
        assert f'.account-tab[data-broker="{broker}"] {{ --broker-accent: {color}; }}' in css
    assert ".account-tab.active" in css
    assert "border-bottom-color: var(--broker-accent);" in css
    assert ".pnl-profit { color: #b91c1c;" in css
    assert ".pnl-loss { color: #15803d;" in css
    assert ".tool-workspace-view .header-assets-panel" in css
    assert (
        ".backtest-workspace,\n.kelly-lab-panel,\n.trend-report-workspace,\n"
        ".symbol-detail-panel,\n.research-chat-modal"
    ) in css
    assert "linear-gradient" not in css
```

Extend the mobile CSS test:

```python
mobile = css.split("@media (max-width: 760px) {", 1)[1]
assert ".account-tab-list" in mobile
assert "grid-template-columns: repeat(4, minmax(0, 1fr));" in mobile
assert "overflow-x: hidden;" in mobile
```

- [ ] **Step 2: Run style tests and verify RED**

```bash
.venv/bin/python -m pytest -q tests/test_dashboard_web.py \
  -k 'warm_ledger or accessible_responsive or mobile_layout_css'
```

Expected: FAIL on the old cool-blue tokens, large broker tint backgrounds, missing tab styles, and missing tool-workspace chrome.

- [ ] **Step 3: Replace the root tokens and style existing components through them**

Start `dashboard.css` with:

```css
:root {
  --bg: #fafaf9;
  --surface: #ffffff;
  --surface-soft: #f5f2eb;
  --text: #1c1917;
  --muted: #78716c;
  --accent: #a16207;
  --line: #d6d3d1;
  --primary: #1c1917;
  --success: #15803d;
  --danger: #b91c1c;
  --shadow: 0 8px 30px rgba(68, 55, 38, 0.06);
}
```

Use a full-width brand/tool row, a stable asset ledger below it, and restrained square-to-8px radii. Keep the existing DOM rather than adding layout wrappers. Apply the same tokens to `.backtest-workspace`, `.kelly-lab-panel`, `.trend-report-workspace`, `.symbol-detail-panel`, `.research-chat-modal`, form fields, tables, tabs, and status cards.

Own those major surfaces with one shared declaration so later inner pages cannot
drift back to the old blue theme:

```css
.backtest-workspace,
.kelly-lab-panel,
.trend-report-workspace,
.symbol-detail-panel,
.research-chat-modal {
  background: var(--surface);
  border: 1px solid var(--line);
  box-shadow: var(--shadow);
}
```

Use small broker accents:

```css
.account-tab[data-broker="futu"] { --broker-accent: #2563eb; }
.account-tab[data-broker="tiger"] { --broker-accent: #d97706; }
.account-tab[data-broker="phillips"] { --broker-accent: #15803d; }
.account-tab[data-broker="eastmoney"] { --broker-accent: #dc2626; }
.account-tab.active { color: var(--text); border-bottom-color: var(--broker-accent); }
#account-futu { --broker-accent: #2563eb; }
#account-tiger { --broker-accent: #d97706; }
#account-phillips { --broker-accent: #15803d; }
#account-eastmoney { --broker-accent: #dc2626; }
.account-section-header { background: var(--surface); border-left: 3px solid var(--broker-accent); }
.pnl-profit { color: #b91c1c; font-weight: 800; }
.pnl-loss { color: #15803d; font-weight: 800; }
```

Tool mode reuses the brand row and hides irrelevant overview content:

```css
.tool-workspace-view .header-assets-panel,
.tool-workspace-view .header-source-panel,
.tool-workspace-view .header-filter-block,
.tool-workspace-view .current-view-label { display: none; }
.tool-workspace-view .dashboard-header { grid-template-columns: minmax(0, 1fr); }
```

On mobile, use four equal tabs with no tab strip scrolling; keep the existing holdings-card grid and 44px targets.

- [ ] **Step 4: Add real browser coverage for desktop and mobile**

Create `tests/e2e/dashboard-warm-ledger.spec.ts` with route-local fixture enrichment so the shared JSON fixture remains useful to Kelly tests:

```typescript
import { expect, test, type Page } from '@playwright/test';

async function installLedgerFixture(page: Page) {
  await page.route('**/api/dashboard', async (route) => {
    const response = await route.fetch();
    const fixture = await response.json();
    fixture.summary = { portfolio_value_hkd: '3064187.62', holding_value_hkd: '647547.98', cash_like_value_hkd: '2416639.64', holding_count: 4 };
    fixture.broker_summaries = [
      { broker: 'futu', display_name: '富途', portfolio_value_hkd: '971244.73', holding_value_hkd: '960926.44', cash_like_value_hkd: '10318.30', holding_count: 1 },
      { broker: 'tiger', display_name: '老虎', portfolio_value_hkd: '726091.55', holding_value_hkd: '700000.00', cash_like_value_hkd: '26091.55', holding_count: 1 },
      { broker: 'phillips', display_name: '辉立', portfolio_value_hkd: '628554.06', holding_value_hkd: '600000.00', cash_like_value_hkd: '28554.06', holding_count: 1 },
      { broker: 'eastmoney', display_name: '东方财富', portfolio_value_hkd: '730673.51', holding_value_hkd: '700000.00', cash_like_value_hkd: '30673.51', holding_count: 1 },
    ];
    fixture.holdings = [
      { market:'US', symbol:'AAPL', name:'Apple', currency:'USD', total_quantity:'10000', avg_cost_price:'180.00', market_value_hkd:'16380000.00', unrealized_pnl_pct:'16.67%', brokers:'futu', broker_details:[{broker:'futu',market:'US',symbol:'AAPL',name:'Apple',quantity:'10000',avg_cost_price:'180.00',market_value_hkd:'16380000.00',unrealized_pnl:'300000.00'}] },
      { market:'US', symbol:'QQQ', name:'Nasdaq 100', currency:'USD', total_quantity:'2', avg_cost_price:'500.00', market_value_hkd:'7800.00', unrealized_pnl_pct:'-2.00%', brokers:'tiger', broker_details:[{broker:'tiger',market:'US',symbol:'QQQ',name:'Nasdaq 100',quantity:'2',avg_cost_price:'500.00',market_value_hkd:'7800.00',unrealized_pnl:'-20.00'}] },
      { market:'HK', symbol:'02840', name:'SPDR 金', currency:'HKD', total_quantity:'11', avg_cost_price:'2932.00', market_value_hkd:'31845.00', unrealized_pnl_pct:'-1.26%', brokers:'phillips', broker_details:[{broker:'phillips',market:'HK',symbol:'02840',name:'SPDR 金',quantity:'11',avg_cost_price:'2932.00',market_value_hkd:'31845.00',unrealized_pnl:'-407.00'}] },
      { market:'CN', symbol:'600519', name:'贵州茅台', currency:'CNY', total_quantity:'100', avg_cost_price:'1500.00', market_value_hkd:'165000.00', unrealized_pnl_pct:'10.00%', brokers:'eastmoney', broker_details:[{broker:'eastmoney',market:'CN',symbol:'600519',name:'贵州茅台',quantity:'100',avg_cost_price:'1500.00',market_value_hkd:'165000.00',unrealized_pnl:'15000.00'}] },
    ];
    await route.fulfill({ response, json: fixture });
  });
}

test('switches broker tabs while keeping global assets and market filter', async ({ page }) => {
  await installLedgerFixture(page);
  await page.goto('/');
  await expect(page.getByRole('tab')).toHaveCount(4);
  await expect(page.getByRole('tab', { name: /富途/ })).toHaveAttribute('aria-selected', 'true');
  await expect(page.locator('#current-view-value')).toHaveText('HKD 3,064,187.62');
  await expect(page.locator('#account-futu')).toBeVisible();
  await expect(page.locator('.account-section')).toHaveCount(1);
  await expect(page.getByText('10,000', { exact: true })).toBeVisible();
  await expect(page.getByText('02840', { exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: 'US', exact: true }).click();
  await page.getByRole('tab', { name: /老虎/ }).click();
  await expect(page.locator('#account-tiger')).toBeVisible();
  await expect(page.locator('#current-view-value')).toHaveText('HKD 3,064,187.62');
  await page.locator('.broker-summary-card[data-broker="phillips"]').click();
  await expect(page.locator('#account-phillips')).toBeVisible();
  await expect(page.getByRole('button', { name: '现金' })).toHaveCount(0);
});

test('keeps four tabs and workspaces usable on mobile', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await installLedgerFixture(page);
  await page.goto('/');
  const tabs = page.locator('#account-tabs [role="tab"]');
  await expect(tabs).toHaveCount(4);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.getByRole('tab', { name: /老虎/ }).click();
  await page.getByRole('button', { name: '凯利实验室' }).click();
  await expect(page.locator('.dashboard-shell')).toHaveClass(/tool-workspace-view/);
  await expect(page.locator('.header-assets-panel')).toBeHidden();
  await page.getByRole('button', { name: '返回持仓' }).click();
  await expect(page.getByRole('tab', { name: /老虎/ })).toHaveAttribute('aria-selected', 'true');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});
```

Update Kelly E2E exact money assertions from ungrouped to grouped display,
change the initial portfolio heading assertion from `持仓列表` to `持仓与策略`,
and use the shared `返回持仓` button. Do not change input or identifier assertions.

- [ ] **Step 5: Run focused Python and browser tests**

```bash
.venv/bin/python -m pytest -q tests/test_dashboard_web.py
npx playwright test tests/e2e/dashboard-warm-ledger.spec.ts tests/e2e/kelly-lab.spec.ts
```

Expected: both commands PASS. Do not run `make acceptance` yet.

- [ ] **Step 6: Commit the complete visual system and browser fixture flow**

```bash
git add src/open_trader/dashboard_static/dashboard.css tests/test_dashboard_web.py \
  tests/e2e/dashboard-warm-ledger.spec.ts tests/e2e/kelly-lab.spec.ts
git commit -m "style: apply warm ledger dashboard theme"
```

---

### Task 5: Teach the real acceptance gate to verify one broker tab at a time

**Files:**
- Modify: `src/open_trader/dashboard_acceptance.py`
- Modify: `tests/test_dashboard_acceptance.py`

**Interfaces:**
- Consumes: `#account-tabs [data-broker]`, one visible `.account-section`, existing trend-report entries, and existing Playwright page API.
- Produces: `_select_account_tab(page, broker): Locator`, an updated `_check_account_holdings()`, and an updated `_check_cn_filter()` used by the final gate.

- [ ] **Step 1: Write failing acceptance-checker tests for tab iteration**

Replace the fake locator expectations that return four `.account-section` nodes with a stateful fake that records tab clicks. The key assertions are:

```python
def test_check_account_holdings_visits_every_broker_tab() -> None:
    page = tabbed_account_page(valid_payload())
    dashboard_acceptance._check_account_holdings(page, valid_payload())
    assert page.selected_brokers == ["futu", "tiger", "phillips", "eastmoney"]
    assert page.max_visible_account_sections == 1


def test_cn_filter_checks_each_broker_tab_without_all_accounts_view() -> None:
    page = tabbed_cn_page()
    dashboard_acceptance._check_cn_filter(page, expected_cn=2)
    assert page.selected_brokers == ["futu", "tiger", "phillips", "eastmoney"]
```

Keep existing trend projection, focus restoration, internal-code leakage, and no-horizontal-scroll assertions in these fakes.

- [ ] **Step 2: Run the acceptance unit tests and verify RED**

```bash
.venv/bin/python -m pytest -q tests/test_dashboard_acceptance.py \
  -k 'account_holdings or cn_filter or browser_check'
```

Expected: FAIL because the checker still requires four simultaneous account sections and the Tiger hash anchor.

- [ ] **Step 3: Replace simultaneous-section assumptions with tab selection**

Add:

```python
ACCOUNT_BROKERS = ("futu", "tiger", "phillips", "eastmoney")

def _select_account_tab(page: Any, broker: str) -> Any:
    tab = page.locator(f'#account-tabs [data-broker="{broker}"]')
    assert tab.count() == 1, f"缺少 {broker} 券商 Tab"
    tab.click()
    assert tab.get_attribute("aria-selected") == "true", f"{broker} Tab 未选中"
    section = page.locator(f"#account-{broker}:visible")
    assert section.count() == 1, f"{broker} 账户区块未显示"
    assert page.locator(".account-section:visible").count() == 1, "同时显示多个账户区块"
    return section
```

Rewrite `_check_account_holdings()` to:

1. assert four tabs and no cash filter/detail mount;
2. iterate `ACCOUNT_BROKERS` through `_select_account_tab()`;
3. validate that broker's identity, horizon, account metrics, strategy summary, holdings or empty state;
4. validate/open only that broker's trend report, assert focus moves to
   `#return-to-portfolio:visible`, click that shared button, and assert focus
   returns to the report trigger before moving to the next tab;
5. retain the no-internal-code, focus, projection, and no-horizontal-scroll checks.

Rewrite `_check_cn_filter()` to click `CN`, then select each broker and verify that the single visible section contains only CN rows or the exact empty-state copy. Replace `_check_tiger_anchor()` with `_check_tiger_tab()` and assert the selected state, not `window.location.hash`.

Update the browser orchestrator and fake Page/Locator classes in `tests/test_dashboard_acceptance.py` for `get_attribute("aria-selected")`, one visible section, and tab clicks.

- [ ] **Step 4: Run the complete acceptance-checker unit suite**

```bash
.venv/bin/python -m pytest -q tests/test_dashboard_acceptance.py
```

Expected: PASS. No real browser or external environment is used in this step.

- [ ] **Step 5: Commit the acceptance flow**

```bash
git add src/open_trader/dashboard_acceptance.py tests/test_dashboard_acceptance.py
git commit -m "test: verify dashboard broker tabs in acceptance"
```

---

### Task 6: Focused verification, final acceptance, and exact-SHA redeployment

**Files:**
- Verify: all files modified in Tasks 1-5
- Verify runtime: `screen` session `open_trader_dashboard_8766`
- Verify log: `/tmp/open_trader_dashboard_8766.log`

**Interfaces:**
- Consumes: committed implementation and updated acceptance checker.
- Produces: focused test evidence, one final `make acceptance` result, and a review process running the exact accepted SHA.

- [ ] **Step 1: Run focused suites before the expensive gate**

```bash
.venv/bin/python -m pytest -q tests/test_dashboard_web.py tests/test_dashboard_acceptance.py
npx playwright test tests/e2e/dashboard-warm-ledger.spec.ts tests/e2e/kelly-lab.spec.ts
git diff --check
```

Expected: all pytest and Playwright tests PASS; `git diff --check` prints nothing.

- [ ] **Step 2: Inspect the existing live process before replacing it**

```bash
screen -ls | rg 'open_trader_dashboard_8766' || true
lsof -nP -iTCP:8766 -sTCP:LISTEN || true
ps -axo pid,lstart,command | rg 'open_trader dashboard .*--port 8766' || true
```

Expected: old process state is recorded. Stop every stale port-8766 Dashboard before starting the candidate.

- [ ] **Step 3: Start the committed candidate on the review port**

```bash
screen -S open_trader_dashboard_8766 -X quit 2>/dev/null || true
listener=$(lsof -tiTCP:8766 -sTCP:LISTEN 2>/dev/null || true)
if test -n "$listener"; then kill $listener; fi
rm -f /tmp/open_trader_dashboard_8766.log
screen -dmS open_trader_dashboard_8766 zsh -lc \
  'cd /Users/ray/projects/open_trader && exec env PYTHONPATH=src .venv/bin/python -u -m open_trader dashboard --portfolio data/latest/portfolio.csv --data-dir data --reports-dir reports --poll-seconds 5 --host 127.0.0.1 --port 8766 >> /tmp/open_trader_dashboard_8766.log 2>&1'
```

Expected: one new listener starts from `/Users/ray/projects/open_trader`.

- [ ] **Step 4: Run the single final acceptance gate**

```bash
make acceptance
```

Expected: exact final status `PASS`. On `FAIL`, fix the defect, rerun focused tests, recommit, restart the candidate, and rerun this gate. On `BLOCKED`, report the external/browser blocker and do not offer the task for review.

- [ ] **Step 5: Record the accepted commit and redeploy that exact SHA**

```bash
ACCEPTED_SHA=$(git rev-parse HEAD)
LOG_SIZE=$(stat -f '%z' /tmp/open_trader_dashboard_8766.log 2>/dev/null || echo 0)
screen -S open_trader_dashboard_8766 -X quit 2>/dev/null || true
screen -dmS open_trader_dashboard_8766 zsh -lc \
  'cd /Users/ray/projects/open_trader && exec env PYTHONPATH=src .venv/bin/python -u -m open_trader dashboard --portfolio data/latest/portfolio.csv --data-dir data --reports-dir reports --poll-seconds 5 --host 127.0.0.1 --port 8766 >> /tmp/open_trader_dashboard_8766.log 2>&1'
```

This restart uses the exact accepted checkout and makes no source or data changes, so it does not require another acceptance run.

- [ ] **Step 6: Verify the post-acceptance review process**

```bash
NEW_PID=$(lsof -tiTCP:8766 -sTCP:LISTEN)
RUNNING_CWD=$(lsof -a -p "$NEW_PID" -d cwd -Fn | sed -n 's/^n//p')
RUNNING_SHA=$(git -C "$RUNNING_CWD" rev-parse HEAD)
test "$RUNNING_SHA" = "$ACCEPTED_SHA"
tail -c +$((LOG_SIZE + 1)) /tmp/open_trader_dashboard_8766.log
curl -fsS -o /dev/null -w 'HTTP %{http_code}\n' http://127.0.0.1:8766/
```

Expected: a new PID, cwd `/Users/ray/projects/open_trader`, `RUNNING_SHA` equal to `ACCEPTED_SHA`, fresh logs without traceback, and `HTTP 200`.

- [ ] **Step 7: Hand off for review**

Provide the user with:

- `make acceptance: PASS`;
- accepted Git SHA;
- new Dashboard PID and verified cwd;
- fresh log path `/tmp/open_trader_dashboard_8766.log`;
- direct review URL `http://127.0.0.1:8766/`.

Do not describe the task as complete or accepted before all six preceding steps succeed.
