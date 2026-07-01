# Holdings Table Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor the dashboard holdings table into the approved compact asset list with US/HK section dividers, the new asset columns, and no main-list broker/action columns.

**Architecture:** Keep the existing static dashboard shell. Update the static HTML header, then update the frontend renderer in `dashboard.js` to group filtered holdings into market sections and render the approved columns from the existing dashboard payload. Add focused static/runtime tests in the existing `tests/test_dashboard_web.py` Node and static-asset checks.

**Tech Stack:** Python pytest, stdlib HTTP dashboard server, static HTML/CSS/JavaScript, Node `vm` runtime checks, local dashboard browser verification with Playwright/local Chrome.

---

## File Structure

- `src/open_trader/dashboard_static/index.html`: owns the static table header and loading row column count. Change only the holdings table header labels and keep the surrounding dashboard shell intact.
- `src/open_trader/dashboard_static/dashboard.js`: owns filtering, quote lookup, holdings row rendering, inline detail expansion, and formatting helpers. Add small helpers for table column count, market grouping, section subtotals, and USD market value display.
- `src/open_trader/dashboard_static/dashboard.css`: owns table width, symbol column sizing, number-cell behavior, and section divider styling. Add stable column classes and market section row styles.
- `tests/test_dashboard_web.py`: already validates dashboard static assets and runs frontend helpers in Node. Extend those tests for the new header contract, US/HK section order, USD market value behavior, removed main-list broker/action columns, and cash view compatibility.

No backend payload change is planned.

---

### Task 1: Lock the Static Table Header Contract

**Files:**
- Modify: `tests/test_dashboard_web.py`

- [ ] **Step 1: Add a helper that extracts the holdings table headers**

Add this helper near the other top-level test helpers in `tests/test_dashboard_web.py`, after `post_text_error`:

```python
def holdings_table_header_labels(html: str) -> list[str]:
    table_prefix = html.split('<tbody id="holdings-body">', 1)[0]
    thead = table_prefix.rsplit("<thead>", 1)[1].split("</thead>", 1)[0]
    labels: list[str] = []
    for segment in thead.split("<th>")[1:]:
        labels.append(segment.split("</th>", 1)[0].strip())
    return labels
```

- [ ] **Step 2: Add the failing static header test**

Add this test after `test_dashboard_static_assets_include_local_shell`:

```python
def test_dashboard_holdings_table_uses_compact_asset_columns() -> None:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    assert holdings_table_header_labels(html) == [
        "明细",
        "市场",
        "标的",
        "数量",
        "成本价",
        "实时价",
        "美元市值",
        "港元市值",
        "持仓占总资产的占比",
        "盈亏",
    ]
    assert "<th>券商</th>" not in html
    assert "<th>动作</th>" not in html
    assert "<th>持仓价</th>" not in html
    assert '<td colspan="10" class="empty-state">加载中</td>' in html
```

- [ ] **Step 3: Run the new static test and verify it fails**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py::test_dashboard_holdings_table_uses_compact_asset_columns -q
```

Expected: the test fails because the current static header still contains `券商`, `持仓价`, and `动作`.

- [ ] **Step 4: Commit the failing test**

Do not commit yet. Keep this failing test in the worktree until Task 2 makes it pass, then commit both test and HTML together.

---

### Task 2: Update the Static Holdings Table Header

**Files:**
- Modify: `src/open_trader/dashboard_static/index.html`
- Test: `tests/test_dashboard_web.py`

- [ ] **Step 1: Replace the holdings table header**

In `src/open_trader/dashboard_static/index.html`, replace the current holdings table `<thead>` row with:

```html
<thead>
  <tr>
    <th>明细</th>
    <th>市场</th>
    <th>标的</th>
    <th>数量</th>
    <th>成本价</th>
    <th>实时价</th>
    <th>美元市值</th>
    <th>港元市值</th>
    <th>持仓占总资产的占比</th>
    <th>盈亏</th>
  </tr>
</thead>
```

Keep the existing loading body row:

```html
<tbody id="holdings-body">
  <tr>
    <td colspan="10" class="empty-state">加载中</td>
  </tr>
</tbody>
```

- [ ] **Step 2: Run the static header test**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py::test_dashboard_holdings_table_uses_compact_asset_columns -q
```

Expected: `1 passed`.

- [ ] **Step 3: Run the existing broad static shell test**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py::test_dashboard_static_assets_include_local_shell -q
```

Expected: `1 passed`.

- [ ] **Step 4: Commit the static contract and header change**

Run:

```bash
git add tests/test_dashboard_web.py src/open_trader/dashboard_static/index.html
git commit -m "test: lock holdings table header"
```

Expected: commit succeeds with only those two files staged.

---

### Task 3: Add Runtime Tests for Sectioned Holdings Rendering

**Files:**
- Modify: `tests/test_dashboard_web.py`
- Modify: `src/open_trader/dashboard_static/dashboard.js`

- [ ] **Step 1: Add failing Node assertions to the existing dashboard runtime test**

In `tests/test_dashboard_web.py`, find the Node script section in `test_dashboard_header_filters_and_cash_view_helpers`. After the existing assertion:

```javascript
if (!elements["holdings-body"].innerHTML.includes("交易决策") || elements["holdings-body"].innerHTML.includes(">详情<")) {
  throw new Error("holdings row should expose trading decision entry: " + elements["holdings-body"].innerHTML);
}
```

insert:

```javascript
const renderedHoldings = elements["holdings-body"].innerHTML;
const usSectionIndex = renderedHoldings.indexOf("US 美股持仓");
const hkSectionIndex = renderedHoldings.indexOf("HK 港股持仓");
if (usSectionIndex === -1 || hkSectionIndex === -1 || usSectionIndex > hkSectionIndex) {
  throw new Error("holdings should render US section before HK section: " + renderedHoldings);
}
for (const required of ["成本价", "美元市值", "港元市值", "持仓占总资产的占比"]) {
  if (renderedHoldings.includes("<th>" + required + "</th>")) {
    throw new Error("body should not render table headers inside market sections: " + renderedHoldings);
  }
}
if (!renderedHoldings.includes("USD 6250.00")) {
  throw new Error("USD holding should show original USD market value: " + renderedHoldings);
}
if (!renderedHoldings.includes("HKD 49062.50")) {
  throw new Error("HKD converted market value should remain visible: " + renderedHoldings);
}
if (!renderedHoldings.includes("<td class=\"number-cell\">-</td>")) {
  throw new Error("non-USD holding should show dash in USD market value column: " + renderedHoldings);
}
if (renderedHoldings.includes("<td>futu</td>") || renderedHoldings.includes("<td>tiger</td>")) {
  throw new Error("main holdings table should not render broker column: " + renderedHoldings);
}
if (renderedHoldings.includes("观察 ·") || renderedHoldings.includes("人工复核 ·")) {
  throw new Error("main holdings table should not render action badges: " + renderedHoldings);
}
```

This relies on the existing fixture data in that test. If the fixture values differ when implementing, set the fixture rows in that Node script to include one US holding with `currency: "USD", market_value: "6250.00", market_value_hkd: "49062.50"` and one HK holding with `currency: "HKD"`.

- [ ] **Step 2: Run the runtime test and verify it fails**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py::test_dashboard_header_filters_and_cash_view_helpers -q
```

Expected: the test fails because the current renderer does not output market section rows and still renders broker/action cells.

- [ ] **Step 3: Add table constants and market section helpers**

In `src/open_trader/dashboard_static/dashboard.js`, add these constants near the other top-level constants:

```javascript
const HOLDINGS_TABLE_COLUMN_COUNT = 10;

const MARKET_SECTION_CONFIGS = [
  { market: "US", label: "US 美股持仓", className: "market-section-us" },
  { market: "HK", label: "HK 港股持仓", className: "market-section-hk" },
  { market: "OTHER", label: "其他市场持仓", className: "market-section-other" },
];
```

Add these helpers near `filteredHoldings()` and `numericValue()`:

```javascript
function holdingsEmptyRow(message) {
  return `<tr><td colspan="${HOLDINGS_TABLE_COLUMN_COUNT}" class="empty-state">${escapeHtml(message)}</td></tr>`;
}

function marketSectionKey(holding) {
  const market = String(holding.market || "").trim().toUpperCase();
  if (market === "US" || market === "HK") {
    return market;
  }
  return "OTHER";
}

function groupedHoldingsByMarketSection(holdings) {
  const groups = new Map(MARKET_SECTION_CONFIGS.map((section) => [section.market, []]));
  for (const holding of holdings) {
    const key = marketSectionKey(holding);
    if (!groups.has(key)) {
      groups.set(key, []);
    }
    groups.get(key).push(holding);
  }
  return MARKET_SECTION_CONFIGS
    .map((section) => ({ ...section, holdings: groups.get(section.market) || [] }))
    .filter((section) => section.holdings.length > 0);
}

function sumNumericField(rows, fieldName) {
  let total = 0;
  for (const row of rows) {
    const value = numericValue(row[fieldName]);
    if (value === null) {
      return null;
    }
    total += value;
  }
  return total;
}

function sumPercentField(rows, fieldName) {
  let total = 0;
  for (const row of rows) {
    const raw = String(row[fieldName] || "").trim();
    if (!raw.endsWith("%")) {
      return null;
    }
    const value = numericValue(raw.slice(0, -1));
    if (value === null) {
      return null;
    }
    total += value;
  }
  return total;
}

function renderMarketSectionRow(section) {
  const marketValue = sumNumericField(section.holdings, "market_value_hkd");
  const weight = sumPercentField(section.holdings, "portfolio_weight_hkd");
  const marketValueText = marketValue === null ? "-" : formatMoney(moneyValue(marketValue), "HKD");
  const weightText = weight === null ? "-" : `${weight.toFixed(2)}%`;
  return `
    <tr class="market-section-row ${escapeHtml(section.className)}">
      <td colspan="${HOLDINGS_TABLE_COLUMN_COUNT}">
        <strong>${escapeHtml(section.label)}</strong>
        <span>${section.holdings.length} 条 · ${escapeHtml(marketValueText)} · ${escapeHtml(weightText)}</span>
      </td>
    </tr>
  `;
}

function renderUsdMarketValue(holding) {
  const currency = String(holding.currency || "").trim().toUpperCase();
  if (currency !== "USD") {
    return "-";
  }
  return formatMoney(holding.market_value, "USD");
}
```

- [ ] **Step 4: Replace the body of `renderHoldings()` with sectioned rendering**

In `src/open_trader/dashboard_static/dashboard.js`, keep the cash-view branch at the top of `renderHoldings()`. Replace the non-cash rendering body with this version:

```javascript
function renderHoldings() {
  if (state.marketFilter === "CASH") {
    const cashRows = filteredCashRows();
    elements["visible-count"].textContent = `${cashRows.length} 条`;
    elements["workspace-grid"].classList.remove("detail-mode");
    elements["holdings-table-wrap"].classList.add("hidden");
    elements["symbol-detail-panel"].classList.add("hidden");
    elements["symbol-detail-panel"].innerHTML = "";
    elements["cash-detail-panel"].classList.remove("hidden");
    renderCashDetailPanel(cashRows);
    return;
  }
  elements["cash-detail-panel"].classList.add("hidden");
  elements["cash-detail-panel"].innerHTML = "";
  const holdings = filteredHoldings();
  elements["visible-count"].textContent = `${holdings.length} 条`;
  const selected = selectedHolding(holdings);
  elements["workspace-grid"].classList.remove("detail-mode");
  elements["holdings-table-wrap"].classList.remove("hidden");
  elements["symbol-detail-panel"].classList.add("hidden");
  elements["symbol-detail-panel"].innerHTML = "";
  if (state.dashboardError) {
    renderDashboardErrorState();
    return;
  }
  if (!state.dashboard) {
    elements["holdings-body"].innerHTML = holdingsEmptyRow("加载中");
    return;
  }
  if (holdings.length === 0) {
    elements["holdings-body"].innerHTML = holdingsEmptyRow("没有匹配的持仓");
    return;
  }

  const rows = [];
  for (const section of groupedHoldingsByMarketSection(holdings)) {
    rows.push(renderMarketSectionRow(section));
    section.holdings.forEach((holding) => {
      const originalIndex = holdings.indexOf(holding);
      const rowKey = holdingKey(holding, originalIndex);
      const selectedClass = selected && rowKey === state.selectedHoldingKey ? "active-row" : "";
      const quote = quoteForHolding(holding);
      rows.push(`
        <tr class="${selectedClass}">
          <td><button class="expand-button" type="button" data-detail-key="${escapeHtml(rowKey)}">交易决策</button></td>
          <td>${escapeHtml(formatPlain(holding.market))}</td>
          <td class="symbol-cell">
            <strong>${escapeHtml(formatPlain(holding.symbol))}</strong>
            <span class="meta-text">${escapeHtml(formatPlain(holding.name))}</span>
          </td>
          <td class="number-cell">${escapeHtml(formatPlain(holding.total_quantity))}</td>
          <td class="number-cell">${escapeHtml(formatPlain(holding.avg_cost_price))}</td>
          <td class="number-cell">${renderQuotePrice(holding, quote)}</td>
          <td class="number-cell">${escapeHtml(renderUsdMarketValue(holding))}</td>
          <td class="number-cell">${escapeHtml(formatMoney(holding.market_value_hkd, "HKD"))}</td>
          <td class="number-cell">${escapeHtml(formatPlain(holding.portfolio_weight_hkd))}</td>
          <td class="number-cell">${escapeHtml(formatPlain(holding.unrealized_pnl_pct))}</td>
        </tr>
      `);
      if (selected && rowKey === state.selectedHoldingKey) {
        rows.push(`
          <tr class="decision-detail-row">
            <td colspan="${HOLDINGS_TABLE_COLUMN_COUNT}">
              <div class="symbol-detail-panel inline-symbol-detail">
                ${renderSymbolDetail(selected.holding, selected.index)}
              </div>
            </td>
          </tr>
        `);
      }
    });
  }
  elements["holdings-body"].innerHTML = rows.join("");
}
```

- [ ] **Step 5: Update error-row rendering to use the shared column count**

Change `renderDashboardErrorState()` to:

```javascript
function renderDashboardErrorState() {
  elements["holdings-body"].innerHTML = holdingsEmptyRow("看板数据加载失败");
}
```

- [ ] **Step 6: Run the runtime test**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py::test_dashboard_header_filters_and_cash_view_helpers -q
```

Expected: `1 passed`.

- [ ] **Step 7: Run focused dashboard web tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py -q
```

Expected: all tests in `tests/test_dashboard_web.py` pass.

- [ ] **Step 8: Commit the renderer change**

Run:

```bash
git add tests/test_dashboard_web.py src/open_trader/dashboard_static/dashboard.js
git commit -m "feat: section holdings table by market"
```

Expected: commit succeeds with only those two files staged.

---

### Task 4: Add Stable Table Styling and Verify in Browser

**Files:**
- Modify: `src/open_trader/dashboard_static/dashboard.css`
- Test: `tests/test_dashboard_web.py`

- [ ] **Step 1: Extend the static shell test for CSS markers**

In `test_dashboard_static_assets_include_local_shell`, add these assertions near the existing table/detail CSS assertions:

```python
    assert ".market-section-row" in css
    assert ".market-section-us" in css
    assert ".market-section-hk" in css
    assert ".symbol-cell" in css
    assert "table-layout: fixed;" in css
```

- [ ] **Step 2: Run the static shell test and verify it fails**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py::test_dashboard_static_assets_include_local_shell -q
```

Expected: the test fails because `.market-section-row` is not styled yet.

- [ ] **Step 3: Update table CSS**

In `src/open_trader/dashboard_static/dashboard.css`, update the `table` rule:

```css
table {
  border-collapse: collapse;
  min-width: 1120px;
  table-layout: fixed;
  width: 100%;
}
```

Add these rules after `tbody tr.active-row`:

```css
.market-section-row td {
  background: #eef3ed;
  border-bottom: 1px solid #b8dac6;
  border-top: 2px solid var(--accent);
  color: var(--accent-strong);
  padding: 9px 12px;
}

.market-section-row strong {
  margin-right: 10px;
}

.market-section-row span {
  color: var(--muted);
  font-size: 12px;
  font-weight: 700;
}

.market-section-hk td {
  background: #fffaf1;
  border-bottom-color: #e1c18e;
  border-top-color: var(--warning);
  color: #7b4208;
}

.market-section-other td {
  background: var(--surface-soft);
  border-top-color: var(--line);
  color: var(--muted);
}
```

Update `.symbol-cell strong` to keep the symbol compact:

```css
.symbol-cell {
  max-width: 170px;
  min-width: 120px;
}

.symbol-cell strong {
  display: block;
  margin-bottom: 4px;
  overflow-wrap: anywhere;
}

.symbol-cell .meta-text {
  display: block;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
```

- [ ] **Step 4: Run focused tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py::test_dashboard_static_assets_include_local_shell tests/test_dashboard_web.py::test_dashboard_header_filters_and_cash_view_helpers tests/test_dashboard_web.py::test_dashboard_holdings_table_uses_compact_asset_columns -q
```

Expected: `3 passed`.

- [ ] **Step 5: Run dashboard and portfolio tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py tests/test_dashboard.py -q
```

Expected: both test files pass.

- [ ] **Step 6: Start the local dashboard for browser verification**

Run:

```bash
.venv/bin/python -m open_trader dashboard --portfolio data/latest/portfolio.csv --data-dir data --reports-dir reports --poll-seconds 5 --host 127.0.0.1 --port 8766
```

Expected: the process prints `dashboard_url: http://127.0.0.1:8766`. Keep it running for the browser checks.

- [ ] **Step 7: Verify the table in a desktop browser viewport**

Use the existing in-app browser or local Chrome Playwright path for `http://127.0.0.1:8766`.

Check these visible conditions:

- Main header labels are `明细`, `市场`, `标的`, `数量`, `成本价`, `实时价`, `美元市值`, `港元市值`, `持仓占总资产的占比`, `盈亏`.
- No main table header for `券商` or `动作`.
- `US 美股持仓` appears before `HK 港股持仓`.
- The section divider rows span the full table and are visually clear.
- A USD holding shows `USD ...` under `美元市值`.
- A HK holding shows `-` under `美元市值`.
- Symbol names do not force the symbol column to occupy a third of the table.

- [ ] **Step 8: Verify the table in a mobile browser viewport**

Use a narrow viewport around `390x844`.

Check these visible conditions:

- The table remains horizontally scrollable.
- Section divider rows remain readable inside the scroll area.
- Text and numeric values do not overlap.
- The `交易决策` button still opens the inline detail row.

- [ ] **Step 9: Stop the local dashboard**

Stop the dashboard process from Step 6 with `Ctrl-C`.

Expected: no dashboard process remains for port `8766`.

- [ ] **Step 10: Commit CSS and verification-driven adjustments**

Run:

```bash
git add tests/test_dashboard_web.py src/open_trader/dashboard_static/dashboard.css
git commit -m "style: tighten holdings table layout"
```

Expected: commit succeeds with only the CSS and any CSS-test updates staged.

---

## Final Verification

- [ ] Run the focused tests:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py tests/test_dashboard.py -q
```

Expected: both test files pass.

- [ ] Run the broader suite if time allows:

```bash
.venv/bin/python -m pytest -q
```

Expected: the full suite passes.

- [ ] Check git status:

```bash
git status --short
```

Expected: no modified tracked files remain. Existing unrelated untracked files may remain if they predated this work.

- [ ] Record browser verification in the final response:

Mention the tested desktop/mobile viewports, the local dashboard URL used, and whether the dashboard process was stopped.

---

## Self-Review

Spec coverage:

- Approved A columns are covered by Tasks 1, 2, and 3.
- US/HK section order and visual dividers are covered by Tasks 3 and 4.
- USD market value display rule is covered by Task 3.
- Broker/action removal from the main table is covered by Tasks 1 and 3.
- Cash view behavior is covered by the existing runtime assertions preserved in Task 3.
- Browser verification is covered by Task 4.

Placeholder scan:

- The plan contains exact file paths, snippets, commands, expected failures, and expected pass conditions.
- The plan does not require new backend payload fields.

Type consistency:

- Helper names introduced in Task 3 are used consistently: `HOLDINGS_TABLE_COLUMN_COUNT`, `holdingsEmptyRow`, `groupedHoldingsByMarketSection`, `renderMarketSectionRow`, and `renderUsdMarketValue`.
