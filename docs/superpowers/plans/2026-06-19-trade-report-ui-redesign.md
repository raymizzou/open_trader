# Trade Report UI Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Redesign the local dashboard trade report UI so `今日交易动作` is a compact review queue and the symbol detail page presents a clear decision-first trade report with split rationale rows.

**Architecture:** Keep the existing Python-served static dashboard. Do not change the trade-action CSV schema or backend action-generation logic; all changes are a frontend projection of the existing `/api/dashboard` payload. Add small JavaScript display helpers, update the right rail and symbol detail renderers, then add CSS classes and static/browser verification.

**Tech Stack:** Python `pytest`, static HTML/CSS/JavaScript in `src/open_trader/dashboard_static`, existing local dashboard server, Playwright for final browser verification.

---

## File Structure

- Modify: `tests/test_dashboard_web.py`
  - Add static asset assertions for the redesigned UI names, classes, and helper functions. This repo currently tests dashboard frontend behavior by reading static assets from Python; follow that pattern instead of adding Node tooling.
- Modify: `src/open_trader/dashboard_static/dashboard.js`
  - Add display-only helpers for sorting actions, counting action statuses, building short trade reasons, selecting matching holdings, building decision/impact values, and splitting rationale text into rows.
  - Update `renderTradeActions()` to render compact review cards.
  - Update the right-rail click handler to open the matching symbol detail.
  - Replace the existing `renderTradeActionSection()` definition-list output with a decision band, impact metrics, and rationale dialogue.
  - Keep English source collapsed by default and split long text before display.
- Modify: `src/open_trader/dashboard_static/dashboard.css`
  - Add styles for action cards, right-rail count summaries, decision band, impact metrics, and dialogue rows.
  - Preserve current dashboard palette and responsive behavior.
- No backend file changes are planned. If implementation reveals a missing field in `/api/dashboard`, add the minimal direct CSV passthrough in `src/open_trader/dashboard.py` and corresponding tests in `tests/test_dashboard.py`.

---

### Task 1: Add Static Asset Expectations

**Files:**
- Modify: `tests/test_dashboard_web.py`
- Verify only: `src/open_trader/dashboard_static/dashboard.js`
- Verify only: `src/open_trader/dashboard_static/dashboard.css`

- [ ] **Step 1: Add failing static assertions**

In `tests/test_dashboard_web.py`, inside `test_dashboard_static_assets_include_local_shell()`, append these assertions near the existing dashboard static assertions:

```python
    assert "renderActionQueueSummary" in js
    assert "sortedTradeActions" in js
    assert "tradeActionCounts" in js
    assert "openTradeActionDetail" in js
    assert "renderTradeDecisionBand" in js
    assert "renderTradeImpactGrid" in js
    assert "renderRationaleDialogue" in js
    assert "rationaleRows" in js
    assert "短触发理由" in js
    assert "清晰交易策略" in js
    assert "操作方向与价位" in js
    assert "理由对话" in js
    assert "查看完整策略" in js
    assert "需复核" in js
    assert "待处理" in js
    assert "未知值显示 -" not in js
    assert ".action-summary-grid" in css
    assert ".action-card" in css
    assert ".decision-band" in css
    assert ".impact-grid" in css
    assert ".dialogue-row" in css
```

- [ ] **Step 2: Run the focused static test and confirm failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py::test_dashboard_static_assets_include_local_shell -v
```

Expected: FAIL on the first new missing string, such as `renderActionQueueSummary`.

- [ ] **Step 3: Commit the failing test**

```bash
git add tests/test_dashboard_web.py
git commit -m "test: cover trade report ui redesign assets"
```

---

### Task 2: Add JavaScript Display Helpers

**Files:**
- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Test: `tests/test_dashboard_web.py`

- [ ] **Step 1: Add action and rationale helper functions**

In `src/open_trader/dashboard_static/dashboard.js`, insert these helpers after `firstAvailableText()` and before `renderTradeActions()`:

```javascript
function sortedTradeActions(actions) {
  return [...actions].sort((left, right) => {
    const statusDelta = actionStatusRank(left.status) - actionStatusRank(right.status);
    if (statusDelta !== 0) {
      return statusDelta;
    }
    const priorityDelta = priorityRank(left.priority) - priorityRank(right.priority);
    if (priorityDelta !== 0) {
      return priorityDelta;
    }
    return `${left.market || ""}.${left.symbol || ""}`.localeCompare(`${right.market || ""}.${right.symbol || ""}`);
  });
}

function actionStatusRank(status) {
  const normalized = String(status || "").trim().toLowerCase();
  if (normalized === "ready") {
    return 0;
  }
  if (normalized === "review") {
    return 1;
  }
  if (normalized === "watch") {
    return 2;
  }
  return 3;
}

function priorityRank(priority) {
  const normalized = String(priority || "").trim().toLowerCase();
  const ranks = { critical: 0, high: 1, medium: 2, low: 3 };
  return Object.prototype.hasOwnProperty.call(ranks, normalized) ? ranks[normalized] : 4;
}

function tradeActionCounts(actions) {
  const counts = { ready: 0, review: 0, watch: 0 };
  for (const action of actions) {
    const status = String(action.status || "").trim().toLowerCase();
    if (status === "ready") {
      counts.ready += 1;
    } else if (status === "review") {
      counts.review += 1;
    } else if (status === "watch") {
      counts.watch += 1;
    }
  }
  return counts;
}

function actionSymbol(action) {
  const futu = formatPlain(action.futu_symbol);
  if (futu !== "-") {
    return futu;
  }
  const market = formatPlain(action.market);
  const symbol = formatPlain(action.symbol);
  if (market === "-" && symbol === "-") {
    return "-";
  }
  return `${market}.${symbol}`;
}

function actionSourceContext(action) {
  const parts = [
    formatTriggerStatus(action.trigger_status),
    formatPriority(action.priority),
  ].filter((part) => part && part !== "-");
  return parts.join(" · ") || "交易计划触发";
}

function shortActionReason(action) {
  const reason = formatActionReason(
    firstAvailableText(
      action.trigger_reason,
      action.reason,
      action.agent_reason,
      action.rationale,
      action.watch_trigger,
    ),
  );
  if (reason === "-") {
    return "暂无简短理由。";
  }
  return compactSentence(reason, 96);
}

function compactSentence(text, maxLength) {
  const normalized = String(text || "").replace(/\s+/g, " ").trim();
  if (!normalized) {
    return "";
  }
  if (normalized.length <= maxLength) {
    return normalized;
  }
  return `${normalized.slice(0, Math.max(0, maxLength - 1)).trim()}…`;
}

function actionNotionalText(action) {
  if (hasValue(action.suggested_notional)) {
    const currency = formatPlain(action.notional_currency);
    return currency === "-" ? action.suggested_notional : `${action.suggested_notional} ${currency}`;
  }
  return "-";
}

function actionCardStatusLabel(action) {
  const actionText = formatAction(action.action || action.suggested_action);
  const statusText = formatActionStatus(action.status);
  if (actionText === "-" && statusText === "-") {
    return "-";
  }
  if (actionText === "-") {
    return statusText;
  }
  if (statusText === "-") {
    return actionText;
  }
  return `${actionText} · ${statusText}`;
}

function rationaleSource(holding) {
  const action = holding.trade_action || holding.premarket_action || {};
  const strategy = holding.strategy || {};
  const report = holding.agent_report || {};
  return firstAvailableText(
    action.agent_reason,
    strategy.agent_reason,
    report.summary_zh,
    action.agent_excerpt,
    strategy.agent_excerpt,
    report.raw_decision,
  );
}

function rationaleRows(text) {
  const sentences = splitRationaleText(text);
  const rows = sentences.map((sentence, index) => ({
    label: rationaleLabel(sentence, index, sentences.length),
    text: sentence,
  }));
  if (rows.length === 0) {
    return [];
  }
  return rows.slice(0, 8);
}

function splitRationaleText(text) {
  const raw = String(text || "").trim();
  if (!raw) {
    return [];
  }
  const lineParts = raw
    .split(/\r?\n+/)
    .map((part) => part.trim())
    .filter(Boolean);
  const sourceParts = lineParts.length > 1 ? lineParts : raw.split(/(?<=[。！？!?])\s+|(?<=[。！？!?])/);
  const parts = sourceParts
    .map((part) => part.trim())
    .filter(Boolean)
    .map((part) => part.replace(/^[-*•\d.\s]+/, "").trim())
    .filter(Boolean);
  const rows = [];
  let buffer = "";
  for (const part of parts) {
    const candidate = buffer ? `${buffer} ${part}` : part;
    if (candidate.length <= 120) {
      buffer = candidate;
    } else {
      if (buffer) {
        rows.push(buffer);
      }
      buffer = part;
    }
  }
  if (buffer) {
    rows.push(buffer);
  }
  return rows;
}

function rationaleLabel(text, index, total) {
  const lower = String(text || "").toLowerCase();
  if (/(macd|rsi|趋势|反弹|突破|阻力|支撑|均线|technical|trend|momentum)/i.test(lower)) {
    return "趋势派";
  }
  if (/(风险|止损|回撤|衰减|升水|仓位|风控|risk|stop|drawdown|decay|contango|position)/i.test(lower)) {
    return "风控派";
  }
  if (/(宏观|政策|财报|油|利率|伊朗|地缘|事件|macro|policy|earnings|oil|rate|geopolitical)/i.test(lower)) {
    return "事件派";
  }
  if (index === total - 1 || /(结论|因此|所以|减仓|买入|卖出|持有|配置|action|trim|buy|sell|hold|allocation)/i.test(lower)) {
    return "组合结论";
  }
  return `依据${index + 1}`;
}
```

- [ ] **Step 2: Run the focused static test and confirm partial failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py::test_dashboard_static_assets_include_local_shell -v
```

Expected: still FAIL because render functions/classes such as `renderActionQueueSummary`, `decision-band`, and `action-card` are not implemented yet.

- [ ] **Step 3: Commit helper functions**

```bash
git add src/open_trader/dashboard_static/dashboard.js
git commit -m "feat: add trade report display helpers"
```

---

### Task 3: Redesign The Right-Rail Action Queue

**Files:**
- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Modify: `src/open_trader/dashboard_static/dashboard.css`
- Test: `tests/test_dashboard_web.py`

- [ ] **Step 1: Add click handling for action-card detail links**

In `bindEvents()` in `src/open_trader/dashboard_static/dashboard.js`, after the existing `elements["holdings-body"].addEventListener(...)` block, add:

```javascript
  elements["trade-actions"].addEventListener("click", (event) => {
    const button = event.target.closest("[data-action-detail]");
    if (!button) {
      return;
    }
    openTradeActionDetail(button.dataset.actionDetail || "");
  });
```

Then add this function after `showSymbolDetail()`:

```javascript
function openTradeActionDetail(actionKey) {
  if (!actionKey) {
    return;
  }
  const holdings = filteredHoldings();
  for (let index = 0; index < holdings.length; index += 1) {
    const holding = holdings[index];
    const tradeAction = holding.trade_action || {};
    const premarketAction = holding.premarket_action || {};
    const candidates = [
      `${String(tradeAction.market || "").toUpperCase()}.${String(tradeAction.symbol || "").toUpperCase()}`,
      `${String(premarketAction.market || "").toUpperCase()}.${String(premarketAction.symbol || "").toUpperCase()}`,
      `${String(holding.market || "").toUpperCase()}.${String(holding.symbol || "").toUpperCase()}`,
    ];
    if (candidates.includes(actionKey)) {
      state.selectedHoldingKey = holdingKey(holding, index);
      renderHoldings();
      return;
    }
  }
}
```

- [ ] **Step 2: Replace `renderTradeActions()`**

Replace the existing `renderTradeActions()` function with:

```javascript
function renderTradeActions() {
  const actions = sortedTradeActions((state.dashboard && state.dashboard.trade_actions) || []);
  const counts = tradeActionCounts(actions);
  const pendingCount = counts.ready + counts.review;
  elements["action-count"].textContent = `${pendingCount} 待处理`;
  if (!actions.length) {
    elements["trade-actions"].innerHTML = `<div class="empty-state">暂无交易动作</div>`;
    return;
  }
  elements["trade-actions"].innerHTML = `
    ${renderActionQueueSummary(counts)}
    <div class="action-card-list">
      ${actions.map(renderActionCard).join("")}
    </div>
  `;
}

function renderActionQueueSummary(counts) {
  return `
    <div class="action-summary-grid" aria-label="交易动作摘要">
      <div><span>待确认</span><strong>${escapeHtml(String(counts.ready))}</strong></div>
      <div><span>复核</span><strong>${escapeHtml(String(counts.review))}</strong></div>
      <div><span>观察</span><strong>${escapeHtml(String(counts.watch))}</strong></div>
    </div>
  `;
}

function renderActionCard(action) {
  const key = `${String(action.market || "").toUpperCase()}.${String(action.symbol || "").toUpperCase()}`;
  const status = String(action.status || "").toLowerCase();
  const statusClass = status === "review" ? "review" : status === "ready" ? "ready" : "watch";
  return `
    <article class="action-card ${statusClass}">
      <div class="action-card-header">
        <div>
          <strong>${escapeHtml(actionSymbol(action))}</strong>
          <span>${escapeHtml(actionSourceContext(action))}</span>
        </div>
        <span class="badge">${escapeHtml(actionCardStatusLabel(action))}</span>
      </div>
      <div class="action-card-metrics">
        <div><span>限价</span><strong>${escapeHtml(formatPlain(action.limit_price || action.last_price))}</strong></div>
        <div><span>数量</span><strong>${escapeHtml(formatPlain(action.suggested_quantity))}</strong></div>
        <div><span>金额</span><strong>${escapeHtml(actionNotionalText(action))}</strong></div>
      </div>
      <div class="action-card-reason">
        <span>短触发理由</span>
        <p>${escapeHtml(shortActionReason(action))}</p>
      </div>
      <button class="raw-toggle action-detail-button" type="button" data-action-detail="${escapeHtml(key)}">查看完整策略</button>
    </article>
  `;
}
```

- [ ] **Step 3: Add right-rail CSS**

Append this block before `.connection-list` in `src/open_trader/dashboard_static/dashboard.css`:

```css
.action-summary-grid {
  display: grid;
  gap: 8px;
  grid-template-columns: repeat(3, minmax(0, 1fr));
}

.action-summary-grid div,
.action-card-metrics div {
  background: var(--surface-soft);
  border: 1px solid var(--line);
  border-radius: 7px;
  min-width: 0;
  padding: 8px;
}

.action-summary-grid span,
.action-card-metrics span,
.action-card-reason span {
  color: var(--muted);
  display: block;
  font-size: 12px;
  font-weight: 700;
  margin-bottom: 4px;
}

.action-card-list {
  display: grid;
  gap: 10px;
}

.action-card {
  background: var(--surface-soft);
  border: 1px solid var(--line);
  border-radius: 8px;
  display: grid;
  gap: 10px;
  padding: 10px;
}

.action-card.ready {
  background: #fffaf1;
  border-color: #efc47e;
}

.action-card.review {
  background: #fae8e6;
  border-color: #e3aaa3;
}

.action-card-header {
  align-items: start;
  display: flex;
  gap: 10px;
  justify-content: space-between;
}

.action-card-header strong {
  display: block;
  font-size: 15px;
  margin-bottom: 4px;
  overflow-wrap: anywhere;
}

.action-card-header span:not(.badge) {
  color: var(--muted);
  font-size: 12px;
}

.action-card-metrics {
  display: grid;
  gap: 7px;
  grid-template-columns: repeat(3, minmax(0, 1fr));
}

.action-card-metrics strong {
  display: block;
  font-variant-numeric: tabular-nums;
  overflow-wrap: anywhere;
}

.action-card-reason {
  border-left: 3px solid var(--accent);
  background: var(--surface);
  border-radius: 6px;
  padding: 8px 10px;
}

.action-card-reason p {
  line-height: 1.5;
  margin: 0;
}

.action-detail-button {
  width: max-content;
}
```

- [ ] **Step 4: Run focused static test**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py::test_dashboard_static_assets_include_local_shell -v
```

Expected: still FAIL only on detail-page strings/classes such as `renderTradeDecisionBand` or `.decision-band`.

- [ ] **Step 5: Commit right-rail redesign**

```bash
git add src/open_trader/dashboard_static/dashboard.js src/open_trader/dashboard_static/dashboard.css
git commit -m "feat: redesign dashboard trade action queue"
```

---

### Task 4: Add Decision Band And Impact Metrics To Detail View

**Files:**
- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Modify: `src/open_trader/dashboard_static/dashboard.css`
- Test: `tests/test_dashboard_web.py`

- [ ] **Step 1: Replace `renderTradeActionSection()`**

Replace the current `renderTradeActionSection(holding)` with:

```javascript
function renderTradeActionSection(holding) {
  const premarketAction = holding.premarket_action || {};
  const tradeAction = holding.trade_action || {};
  if (!sectionAvailable(tradeAction) && !sectionAvailable(premarketAction)) {
    return renderDetailSection("当前交易动作", renderStatusMessage("暂无触发中的交易动作", tradeAction));
  }
  const action = sectionAvailable(tradeAction) ? tradeAction : premarketAction;
  const body = `
    ${renderStatusWarning(action)}
    ${renderTradeDecisionBand(action, holding)}
    ${renderTradeImpactGrid(action, holding)}
    ${renderRationaleDialogue(holding)}
  `;
  return renderDetailSection("当前交易动作", body);
}
```

- [ ] **Step 2: Add decision and impact renderers**

Add these functions after `renderTradeActionSection()`:

```javascript
function renderTradeDecisionBand(action, holding) {
  return `
    <div class="decision-band">
      <article class="decision-block">
        <h4>清晰交易策略</h4>
        <strong>${escapeHtml(strategyHeadline(action, holding))}</strong>
        <p>${escapeHtml(strategySubline(action, holding))}</p>
      </article>
      <article class="decision-block">
        <h4>操作方向与价位</h4>
        <dl class="compact-kv">
          ${renderCompactKv("动作", actionCardStatusLabel(action))}
          ${renderCompactKv("限价", action.limit_price || action.last_price)}
          ${renderCompactKv("数量", action.suggested_quantity || action.target_quantity || action.quantity)}
          ${renderCompactKv("金额", actionNotionalText(action))}
          ${renderCompactKv("止损", action.stop_price)}
        </dl>
      </article>
      <article class="decision-block">
        <h4>简短触发理由</h4>
        <p class="decision-reason">${escapeHtml(shortActionReason(action))}</p>
      </article>
    </div>
  `;
}

function renderTradeImpactGrid(action, holding) {
  const cells = [
    ["当前数量", action.current_quantity || holding.total_quantity],
    ["交易后数量", action.post_trade_quantity],
    ["建议金额", actionNotionalText(action)],
    ["交易后权重", action.post_trade_weight],
    ["下一触发", nextTriggerText(action, holding)],
  ];
  return `
    <div class="impact-grid" aria-label="交易影响">
      ${cells.map(([label, value]) => `
        <article class="impact-cell">
          <span>${escapeHtml(label)}</span>
          <strong>${escapeHtml(formatPlain(value))}</strong>
        </article>
      `).join("")}
    </div>
  `;
}

function renderCompactKv(label, value) {
  return `
    <div>
      <dt>${escapeHtml(label)}</dt>
      <dd>${escapeHtml(formatPlain(value))}</dd>
    </div>
  `;
}

function strategyHeadline(action, holding) {
  const symbol = actionSymbol(action) !== "-" ? actionSymbol(action) : `${formatPlain(holding.market)}.${formatPlain(holding.symbol)}`;
  const actionText = formatAction(action.action || action.suggested_action);
  if (actionText === "-") {
    return `${symbol} 交易策略`;
  }
  return `${actionText} ${symbol}`;
}

function strategySubline(action, holding) {
  const strategy = holding.strategy || {};
  const view = formatAction(strategy.view || strategy.stance || strategy.signal || strategy.rating);
  const status = formatActionStatus(action.status);
  const parts = [view, status].filter((part) => part && part !== "-");
  if (parts.length) {
    return `${parts.join(" · ")}；执行前保持人工确认。`;
  }
  return "执行前保持人工确认。";
}

function nextTriggerText(action, holding) {
  if (hasValue(action.watch_trigger)) {
    return formatActionReason(action.watch_trigger);
  }
  const strategy = holding.strategy || {};
  const targetText = joinRange(strategy.target_1, strategy.target_2) || strategy.target_range;
  if (hasValue(targetText)) {
    return `目标价 ${targetText}`;
  }
  if (hasValue(strategy.plan_text)) {
    return compactSentence(strategy.plan_text, 48);
  }
  return "";
}
```

- [ ] **Step 3: Add decision/impact CSS**

Append this block after `.detail-section h3` in `src/open_trader/dashboard_static/dashboard.css`:

```css
.decision-band {
  display: grid;
  gap: 10px;
  grid-template-columns: 1.05fr 1.15fr 1fr;
}

.decision-block {
  background: var(--surface-soft);
  border: 1px solid var(--line);
  border-radius: 8px;
  min-width: 0;
  padding: 10px;
}

.decision-block h4,
.impact-cell span,
.compact-kv dt {
  color: var(--muted);
  font-size: 12px;
  font-weight: 700;
  margin: 0 0 7px;
}

.decision-block strong {
  display: block;
  font-size: 18px;
  margin-bottom: 6px;
  overflow-wrap: anywhere;
}

.decision-block p,
.decision-reason {
  line-height: 1.55;
  margin: 0;
}

.compact-kv {
  display: grid;
  gap: 7px;
  margin: 0;
}

.compact-kv div {
  border-bottom: 1px solid var(--line);
  display: flex;
  gap: 10px;
  justify-content: space-between;
  padding-bottom: 6px;
}

.compact-kv div:last-child {
  border-bottom: 0;
  padding-bottom: 0;
}

.compact-kv dd {
  font-variant-numeric: tabular-nums;
  margin: 0;
  overflow-wrap: anywhere;
  text-align: right;
}

.impact-grid {
  display: grid;
  gap: 10px;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  margin-top: 10px;
}

.impact-cell {
  background: var(--surface-soft);
  border: 1px solid var(--line);
  border-radius: 8px;
  min-height: 66px;
  padding: 9px;
}

.impact-cell strong {
  display: block;
  overflow-wrap: anywhere;
}
```

- [ ] **Step 4: Run focused static test**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py::test_dashboard_static_assets_include_local_shell -v
```

Expected: still FAIL only on rationale dialogue helpers/classes if Task 5 is not done.

- [ ] **Step 5: Commit detail decision band**

```bash
git add src/open_trader/dashboard_static/dashboard.js src/open_trader/dashboard_static/dashboard.css
git commit -m "feat: add decision-first trade detail"
```

---

### Task 5: Render Split Rationale Dialogue And English Source Rows

**Files:**
- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Modify: `src/open_trader/dashboard_static/dashboard.css`
- Test: `tests/test_dashboard_web.py`

- [ ] **Step 1: Add rationale dialogue renderers**

Add these functions after `renderTradeImpactGrid()`:

```javascript
function renderRationaleDialogue(holding) {
  const rows = rationaleRows(rationaleSource(holding));
  if (!rows.length) {
    return "";
  }
  return `
    <div class="rationale-dialogue">
      <h4>理由对话</h4>
      <div class="dialogue-list">
        ${rows.map((row) => `
          <div class="dialogue-row">
            <strong>${escapeHtml(row.label)}</strong>
            <span>${escapeHtml(chineseDisplayText(row.text) || row.text)}</span>
          </div>
        `).join("")}
      </div>
    </div>
  `;
}

function renderSplitSourceRows(text) {
  const rows = rationaleRows(text);
  if (!rows.length) {
    return `<pre class="raw-report english-source hidden">${escapeHtml(text)}</pre>`;
  }
  return `
    <div class="raw-report english-source split-source hidden">
      ${rows.map((row) => `
        <div class="dialogue-row">
          <strong>${escapeHtml(row.label)}</strong>
          <span>${escapeHtml(row.text)}</span>
        </div>
      `).join("")}
    </div>
  `;
}
```

- [ ] **Step 2: Update English source block**

Replace the existing `renderEnglishSourceBlock(text, rawText, buttonText)` with:

```javascript
function renderEnglishSourceBlock(text, rawText, buttonText) {
  const sourceText = firstAvailableText(text, rawText);
  if (!hasValue(sourceText)) {
    return "";
  }
  return `
    <button class="raw-toggle english-source-toggle" type="button" data-toggle-raw-report>${escapeHtml(buttonText)}</button>
    ${renderSplitSourceRows(sourceText)}
  `;
}
```

- [ ] **Step 3: Add rationale CSS**

Append this CSS after `.raw-report`:

```css
.rationale-dialogue {
  border-top: 1px solid var(--line);
  margin-top: 12px;
  padding-top: 12px;
}

.rationale-dialogue h4 {
  color: var(--muted);
  font-size: 12px;
  margin: 0 0 8px;
}

.dialogue-list,
.split-source {
  display: grid;
  gap: 8px;
}

.dialogue-row {
  background: var(--surface-soft);
  border-left: 3px solid var(--line);
  border-radius: 7px;
  line-height: 1.55;
  padding: 8px 10px;
}

.dialogue-row strong {
  color: var(--accent-strong);
  margin-right: 8px;
}

.dialogue-row span {
  overflow-wrap: anywhere;
}
```

- [ ] **Step 4: Run focused static test and confirm it passes**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py::test_dashboard_static_assets_include_local_shell -v
```

Expected: PASS.

- [ ] **Step 5: Commit rationale dialogue**

```bash
git add src/open_trader/dashboard_static/dashboard.js src/open_trader/dashboard_static/dashboard.css
git commit -m "feat: split trade rationale dialogue"
```

---

### Task 6: Responsive Polish And Regression Tests

**Files:**
- Modify: `src/open_trader/dashboard_static/dashboard.css`
- Test: `tests/test_dashboard_web.py`
- Test: `tests/test_dashboard.py`

- [ ] **Step 1: Add responsive CSS for new components**

In the existing `@media (max-width: 1180px)` block, add:

```css
  .decision-band,
  .impact-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
```

In the existing `@media (max-width: 760px)` block, add:

```css
  .action-card-metrics,
  .action-summary-grid,
  .decision-band,
  .impact-grid {
    grid-template-columns: 1fr;
  }

  .compact-kv div {
    display: grid;
    gap: 3px;
  }

  .compact-kv dd {
    text-align: left;
  }
```

- [ ] **Step 2: Strengthen static responsive assertions**

In `tests/test_dashboard_web.py`, inside `test_dashboard_static_assets_include_local_shell()`, append:

```python
    assert "@media (max-width: 1180px)" in css
    assert "@media (max-width: 760px)" in css
    assert ".compact-kv dd" in css
    assert "grid-template-columns: 1fr" in css
```

- [ ] **Step 3: Run dashboard-focused tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard.py tests/test_dashboard_web.py -v
```

Expected: PASS.

- [ ] **Step 4: Commit responsive polish**

```bash
git add tests/test_dashboard_web.py src/open_trader/dashboard_static/dashboard.css
git commit -m "fix: polish trade report responsive layout"
```

---

### Task 7: Browser Verification

**Files:**
- No planned source edits.
- Uses local dashboard server and browser verification.

- [ ] **Step 1: Start the dashboard server**

Run:

```bash
.venv/bin/python -m open_trader dashboard --host 127.0.0.1 --port 8765 --poll-seconds 5
```

Expected: server prints `dashboard_url: http://127.0.0.1:8765` and keeps running.

- [ ] **Step 2: Open dashboard in browser**

Use Playwright or the in-app browser to open:

```text
http://127.0.0.1:8765
```

Expected: topbar, summary cards, holdings table, and `今日交易动作` are visible.

- [ ] **Step 3: Verify right rail**

Check visible page text.

Expected:

- `今日交易动作` remains visible.
- The right rail shows `待确认`, `复核`, and `观察` counts.
- At least one action card shows symbol, action/status pill, limit price, quantity, amount, `短触发理由`, and `查看完整策略`.
- If no action data exists in the local environment, use a temporary test data directory with `portfolio.csv` and `trade_actions.csv` fixtures rather than changing `data/latest`.

- [ ] **Step 4: Verify symbol detail**

Click `查看完整策略` on an action card or `详情` in the holdings table.

Expected:

- The detail view opens without page navigation.
- `清晰交易策略`, `操作方向与价位`, `简短触发理由`, and `理由对话` are visible.
- English source is collapsed behind `查看英文原文`.
- Unknown values show `-`, not fabricated `0`.

- [ ] **Step 5: Verify mobile layout**

Set browser viewport to approximately `390x844`.

Expected:

- Right rail action card metrics stack without text overlap.
- Detail decision band and impact metrics stack cleanly.
- Buttons and labels fit inside their containers.

- [ ] **Step 6: Stop dashboard server**

Stop the server process with Ctrl-C or by terminating its shell session.

Expected: no dashboard server remains running on port `8765`.

---

### Task 8: Final Test Sweep And Status

**Files:**
- No planned source edits unless verification exposes a defect.

- [ ] **Step 1: Run focused tests**

```bash
.venv/bin/python -m pytest tests/test_dashboard.py tests/test_dashboard_web.py -v
```

Expected: PASS.

- [ ] **Step 2: Run the broader relevant suite**

```bash
.venv/bin/python -m pytest tests/test_dashboard.py tests/test_dashboard_web.py tests/test_dashboard_cli.py tests/test_dashboard_quotes.py -v
```

Expected: PASS.

- [ ] **Step 3: Inspect git diff**

```bash
git status --short
git diff --stat HEAD
git diff --check
```

Expected:

- Only intended files are modified.
- `git diff --check` prints no whitespace errors.

- [ ] **Step 4: Commit final verification fixes if any**

If Task 7 or Task 8 required fixes, commit them:

```bash
git add tests/test_dashboard_web.py src/open_trader/dashboard_static/dashboard.js src/open_trader/dashboard_static/dashboard.css
git commit -m "fix: verify trade report ui redesign"
```

If no fixes were required, do not create an empty commit.

