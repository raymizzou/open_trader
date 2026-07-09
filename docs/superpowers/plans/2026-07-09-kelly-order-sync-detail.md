# Kelly Order Sync Detail Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a compact per-order detail table inside each Kelly experiment `订单同步` block.

**Architecture:** Keep `order_sync` as part of each experiment payload. `dashboard.js` renders a read-only compact order table from `order_sync.orders[]`, falling back to `暂无同步订单明细。` when no orders exist. Existing tests and Playwright fixture verify that each strategy tab only shows its own order rows.

**Tech Stack:** Existing dashboard static JavaScript/CSS, pytest Node-based JS tests, Playwright Chromium fixture tests.

---

## Files

- Modify: `src/open_trader/dashboard_static/dashboard.js`
  - Add `renderKellyOrderSyncOrders()`, `kellyOrderSideLabel()`, and `kellyOrderStatusLabel()`.
  - Call the order detail renderer from `renderKellyOrderSync()`.

- Modify: `src/open_trader/dashboard_static/dashboard.css`
  - Add compact responsive table styles for `.kelly-order-table`.
  - Keep text wrapping stable for long order ids.

- Modify: `tests/test_dashboard_web.py`
  - Add `order_sync.orders[]` fixture data to both strategies.
  - Assert order rows, fallback text, labels, and tab isolation.

- Modify: `tests/e2e/fixtures/kelly-dashboard.json`
  - Add first-strategy successful orders and second-strategy failed/rejected orders.

- Modify: `tests/e2e/kelly-lab.spec.ts`
  - Verify order table details for both strategy tabs.

## Task 1: JS Test Coverage For Compact Order Details

**Files:**
- Modify: `tests/test_dashboard_web.py`

- [ ] **Step 1: Add first-strategy order detail fixture**

In `test_dashboard_js_renders_kelly_lab_panel`, update the first experiment `order_sync` block:

```javascript
order_sync: {
  status: "success",
  environment: "SIMULATE",
  last_synced_at: "2026-07-08 10:08",
  order_count: 7,
  fill_count: 5,
  message: "富途模拟盘订单已同步。",
  next_action: "可以继续扫描入场与退出信号。",
  orders: [
    {
      market: "US",
      symbol: "RAM",
      side: "buy",
      submitted_at: "2026-07-08 10:01",
      order_price: "12.34",
      order_qty: "800",
      filled_qty: "800",
      avg_fill_price: "12.34",
      status: "filled",
      order_id: "SIM-10001"
    },
    {
      market: "HK",
      symbol: "02840",
      side: "sell",
      submitted_at: "2026-07-08 10:03",
      order_price: "218.80",
      order_qty: "100",
      filled_qty: "0",
      avg_fill_price: "-",
      status: "submitted",
      order_id: "SIM-10002"
    }
  ]
}
```

- [ ] **Step 2: Add second-strategy failed order fixture**

Update the second experiment `order_sync` block:

```javascript
order_sync: {
  status: "failed",
  environment: "SIMULATE",
  last_synced_at: "2026-07-08 10:09",
  order_count: 3,
  fill_count: 2,
  message: "模拟盘订单同步失败：OpenD 不可用。",
  next_action: "本轮不下单，保留现有订单状态。",
  orders: [
    {
      market: "US",
      symbol: "MSFT",
      side: "buy",
      submitted_at: "2026-07-08 10:05",
      order_price: "505.10",
      order_qty: "20",
      filled_qty: "0",
      avg_fill_price: "-",
      status: "rejected",
      order_id: "SIM-20001"
    }
  ]
}
```

- [ ] **Step 3: Add first-tab assertions**

Add these strings to the existing `required` list for first-tab HTML:

```javascript
"标的",
"方向",
"下单时间",
"订单价",
"订单数量",
"成交数量",
"成交均价",
"状态",
"US.RAM",
"SIM-10001",
"买入",
"2026-07-08 10:01",
"12.34",
"800",
"已成交",
"HK.02840",
"SIM-10002",
"卖出",
"218.80",
"100",
"0",
"待成交"
```

- [ ] **Step 4: Add fallback assertion**

Update the existing `fallbackHtml` test object with:

```javascript
order_sync: {
  status: "success",
  environment: "SIMULATE",
  last_synced_at: "2026-07-08 10:10",
  order_count: 0,
  fill_count: 0,
  message: "富途模拟盘订单已同步。",
  next_action: "等待下一次信号。"
}
```

Then assert:

```javascript
if (!fallbackHtml.includes("暂无同步订单明细。")) {
  throw new Error("kelly order sync empty detail missing: " + fallbackHtml);
}
```

- [ ] **Step 5: Add second-tab assertions**

After `secondHtml` is rendered, extend the existing order sync required list:

```javascript
for (const required of ["订单同步", "同步失败", "模拟盘订单同步失败：OpenD 不可用。", "本轮不下单，保留现有订单状态。", "US.MSFT", "SIM-20001", "买入", "505.10", "20", "拒单"]) {
  if (!secondHtml.includes(required)) {
    throw new Error("kelly second tab order sync missing " + required + ": " + secondHtml);
  }
}
```

- [ ] **Step 6: Run JS test and verify it fails**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py::test_dashboard_js_renders_kelly_lab_panel -q
```

Expected: FAIL because `dashboard.js` does not yet render order rows or fallback text.

## Task 2: Render Compact Order Details

**Files:**
- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Modify: `src/open_trader/dashboard_static/dashboard.css`

- [ ] **Step 1: Add JS renderer functions**

Add below `renderKellyOrderSync()`:

```javascript
function renderKellyOrderSyncOrders(sync) {
  const orders = sync && Array.isArray(sync.orders)
    ? sync.orders.filter((order) => order && typeof order === "object")
    : [];
  if (!orders.length) {
    return `<p class="kelly-order-empty">暂无同步订单明细。</p>`;
  }
  const headers = ["标的", "方向", "下单时间", "订单价", "订单数量", "成交数量", "成交均价", "状态"];
  return `
    <div class="kelly-order-table" role="table" aria-label="Kelly 同步订单明细">
      <div class="kelly-order-row header" role="row">
        ${headers.map((header) => `<span role="columnheader">${escapeHtml(header)}</span>`).join("")}
      </div>
      ${orders.map(renderKellyOrderSyncOrder).join("")}
    </div>
  `;
}

function renderKellyOrderSyncOrder(order) {
  const item = order && typeof order === "object" ? order : {};
  const symbol = [item.market, item.symbol]
    .filter(hasValue)
    .map(formatPlain)
    .join(".");
  const symbolCell = `
    <strong>${escapeHtml(firstPresent(symbol, item.symbol, "-"))}</strong>
    ${hasValue(item.order_id) ? `<small>${escapeHtml(formatPlain(item.order_id))}</small>` : ""}
  `;
  const cells = [
    symbolCell,
    escapeHtml(kellyOrderSideLabel(item.side)),
    escapeHtml(formatPlain(item.submitted_at || "-")),
    escapeHtml(formatPlain(item.order_price || "-")),
    escapeHtml(formatPlain(item.order_qty || "-")),
    escapeHtml(formatPlain(item.filled_qty || "-")),
    escapeHtml(formatPlain(item.avg_fill_price || "-")),
    escapeHtml(kellyOrderStatusLabel(item.status)),
  ];
  return `
    <div class="kelly-order-row" role="row">
      ${cells.map((cell) => `<span role="cell">${cell}</span>`).join("")}
    </div>
  `;
}

function kellyOrderSideLabel(side) {
  const labels = {
    buy: "买入",
    sell: "卖出",
  };
  const key = formatPlain(side).toLowerCase();
  return labels[key] || formatPlain(side);
}

function kellyOrderStatusLabel(status) {
  const labels = {
    cancelled: "已撤单",
    failed: "失败",
    filled: "已成交",
    partial_filled: "部分成交",
    pending: "待成交",
    rejected: "拒单",
    submitted: "待成交",
  };
  const key = formatPlain(status).toLowerCase();
  return labels[key] || formatPlain(status);
}
```

- [ ] **Step 2: Call order detail renderer**

In `renderKellyOrderSync()`, add this before the closing `</section>`:

```javascript
${renderKellyOrderSyncOrders(sync)}
```

- [ ] **Step 3: Add CSS**

Add below existing `.kelly-order-sync small`:

```css
.kelly-order-table {
  border: 1px solid var(--line);
  border-radius: 8px;
  display: grid;
  overflow-x: auto;
}

.kelly-order-row {
  display: grid;
  gap: 8px;
  grid-template-columns: minmax(110px, 1.1fr) repeat(7, minmax(72px, .8fr));
  min-width: 760px;
  padding: 8px;
}

.kelly-order-row + .kelly-order-row {
  border-top: 1px solid var(--line);
}

.kelly-order-row.header {
  background: var(--surface);
  color: var(--muted);
  font-size: 11px;
  font-weight: 700;
}

.kelly-order-row span {
  min-width: 0;
  overflow-wrap: anywhere;
}

.kelly-order-row strong,
.kelly-order-row small {
  display: block;
}

.kelly-order-row small,
.kelly-order-empty {
  color: var(--muted);
  font-size: 11px;
}

.kelly-order-empty {
  margin: 0;
}
```

- [ ] **Step 4: Run JS test and verify it passes**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py::test_dashboard_js_renders_kelly_lab_panel -q
```

Expected: PASS.

## Task 3: Playwright Fixture And Browser Verification

**Files:**
- Modify: `tests/e2e/fixtures/kelly-dashboard.json`
- Modify: `tests/e2e/kelly-lab.spec.ts`

- [ ] **Step 1: Add order detail fixture data**

Mirror the `orders` arrays from Task 1 into `tests/e2e/fixtures/kelly-dashboard.json`.

- [ ] **Step 2: Add first-tab Playwright checks**

After the first-tab `orderSync` assertions, add:

```typescript
await expect(orderSync.getByText('US.RAM')).toBeVisible();
await expect(orderSync.getByText('SIM-10001')).toBeVisible();
await expect(orderSync.getByText('买入')).toBeVisible();
await expect(orderSync.getByText('12.34')).toBeVisible();
await expect(orderSync.getByText('800')).toBeVisible();
await expect(orderSync.getByText('已成交')).toBeVisible();
await expect(orderSync.getByText('US.MSFT')).toHaveCount(0);
```

- [ ] **Step 3: Add second-tab Playwright checks**

After the second-tab `failedOrderSync` assertions, add:

```typescript
await expect(failedOrderSync.getByText('US.MSFT')).toBeVisible();
await expect(failedOrderSync.getByText('SIM-20001')).toBeVisible();
await expect(failedOrderSync.getByText('拒单')).toBeVisible();
await expect(failedOrderSync.getByText('505.10')).toBeVisible();
await expect(failedOrderSync.getByText('US.RAM')).toHaveCount(0);
```

- [ ] **Step 4: Run Playwright**

Run:

```bash
npm run test:e2e:kelly
```

Expected: PASS.

## Task 4: Verification, Commit, And Deployment

**Files:**
- Commit all modified source/test/fixture files.

- [ ] **Step 1: Run focused tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py::test_dashboard_js_renders_kelly_lab_panel -q
npm run test:e2e:kelly
```

Expected: both pass.

- [ ] **Step 2: Run relevant regression tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_kelly_lifecycle.py tests/test_kelly_rules.py tests/test_kelly_lab.py tests/test_dashboard.py tests/test_dashboard_web.py -q
npm run test:e2e:kelly
git diff --check
```

Expected: Python tests pass, Playwright passes, and `git diff --check` has no output.

- [ ] **Step 3: Commit implementation**

Run:

```bash
git add src/open_trader/dashboard_static/dashboard.js src/open_trader/dashboard_static/dashboard.css tests/test_dashboard_web.py tests/e2e/fixtures/kelly-dashboard.json tests/e2e/kelly-lab.spec.ts
git commit -m "feat: show kelly order sync details"
```

- [ ] **Step 4: Restart local dashboard**

Run:

```bash
kill $(lsof -tiTCP:8766 -sTCP:LISTEN) 2>/dev/null || true
sleep 1
screen -dmS open_trader_dashboard_8766 zsh -lc 'cd /Users/ray/projects/open_trader && export PYTHONPATH=src && exec .venv/bin/python -m open_trader dashboard --portfolio data/latest/portfolio.csv --data-dir data --reports-dir reports --poll-seconds 5 --host 127.0.0.1 --port 8766'
```

- [ ] **Step 5: Browser verify local dashboard**

Run:

```bash
node - <<'NODE'
const { chromium, expect } = require('@playwright/test');
(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1800, height: 900 } });
  await page.goto('http://127.0.0.1:8766/', { waitUntil: 'networkidle' });
  await page.getByRole('button', { name: '凯利实验室' }).click();
  const orderSync = page.getByLabel('Kelly 订单同步');
  await expect(orderSync.getByText('US.RAM')).toBeVisible();
  await expect(orderSync.getByText('SIM-10001')).toBeVisible();
  await page.getByRole('tab', { name: /突破 10D Mock 第一批/ }).click();
  const failedOrderSync = page.getByLabel('Kelly 订单同步');
  await expect(failedOrderSync.getByText('US.MSFT')).toBeVisible();
  await expect(failedOrderSync.getByText('SIM-20001')).toBeVisible();
  await page.screenshot({ path: '/tmp/open-trader-kelly-order-sync-details.png', fullPage: true });
  await browser.close();
})();
NODE
```

Expected: script exits 0 and writes `/tmp/open-trader-kelly-order-sync-details.png`.
