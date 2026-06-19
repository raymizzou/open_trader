# Agent Report Readability Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Redesign the dashboard symbol detail report area so the first viewport answers what to do now, what to watch today, who said what, and what final conclusion was reached.

**Architecture:** Keep the Python dashboard API and existing `data/latest/*` artifact contract unchanged. Replace the current detail-page first-view composition of separate `TradingAgents 报告`, `交易策略`, and `当前交易动作` sections with one combined frontend projection named `分析与交易策略`, backed by small display-only JavaScript helpers and CSS. Continue to suppress raw English prose from the Chinese primary UI and keep English source text collapsed.

**Tech Stack:** Python 3.12, pytest, static JavaScript/CSS in `src/open_trader/dashboard_static`, existing dashboard server, Node-based helper checks when available, Playwright or in-app browser for visual verification.

---

## Approved Spec

Implement:

```text
docs/superpowers/specs/2026-06-19-agent-report-readability-redesign-design.md
```

## File Structure

- Modify: `tests/test_dashboard_web.py`
  - Update static asset assertions to cover the new `分析与交易策略` combined report area.
  - Add runtime helper assertions for decision text, watch points, final conclusion, dialogue extraction, and raw-English suppression.
- Modify: `src/open_trader/dashboard_static/dashboard.js`
  - Add display-only helpers for selecting the active action, building operation rows, watch points, metrics, final conclusion items, and report source text.
  - Add `renderAnalysisStrategySection(holding)` as the single first-class report area.
  - Update `renderSymbolDetail()` to render `renderAnalysisStrategySection(holding)` before broker details.
  - Keep existing raw source toggle and disabled `重新分析 · 未启用`.
  - Leave right-rail behavior and CSV contracts unchanged.
- Modify: `src/open_trader/dashboard_static/dashboard.css`
  - Add the combined report layout: decision dashboard, primary decision card, compact metrics, analyst dialogue grid, final conclusion panel, and lower-priority broker section.
  - Preserve existing palette, 8px radius, dense operational layout, and mobile stacking.
- Verify only: `src/open_trader/dashboard.py`
  - No backend change is expected. Only touch it if `/api/dashboard` lacks an existing CSV field required by the UI.

---

### Task 1: Add Static Asset Expectations

**Files:**
- Modify: `tests/test_dashboard_web.py`
- Test: `tests/test_dashboard_web.py`

- [ ] **Step 1: Update the static asset test**

In `tests/test_dashboard_web.py`, inside `test_dashboard_static_assets_include_local_shell()`, add these assertions after the existing detail-view assertions and keep the existing right-rail assertions:

```python
    assert "renderAnalysisStrategySection" in js
    assert "currentDecisionAction" in js
    assert "desiredActionText" in js
    assert "operationRows" in js
    assert "watchPointText" in js
    assert "decisionMetricCells" in js
    assert "finalConclusionItems" in js
    assert "renderAnalystDialogue" in js
    assert "sourceReviewText" in js
    assert "分析与交易策略" in js
    assert "当前希望你做什么" in js
    assert "操作指令" in js
    assert "今天重点关注" in js
    assert "分析师对话" in js
    assert "最终结论" in js
    assert "失败条件" in js
    assert "只读 · 需要人工确认" in js
    assert "今天暂无触发中的交易动作" in js
    assert "查看英文原文" in js
    assert ".analysis-strategy-section" in css
    assert ".decision-dashboard" in css
    assert ".decision-card.primary" in css
    assert ".decision-metric-strip" in css
    assert ".analyst-dialogue" in css
    assert ".final-conclusion-list" in css
    assert ".broker-detail-section" in css
```

Also add these assertions near the existing `detail-grid` checks to prevent the old report text wall from returning to the first viewport:

```python
    assert "renderAgentReportSection(holding.agent_report, holding)" not in js
    assert "renderStrategySection(holding.strategy, holding)" not in js
    assert "renderTradeActionSection(holding)" not in js
```

- [ ] **Step 2: Run the focused test and confirm it fails**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py::test_dashboard_static_assets_include_local_shell -v
```

Expected: FAIL on a missing string such as `renderAnalysisStrategySection`, because the combined report renderer does not exist yet.

- [ ] **Step 3: Commit the failing test**

Run:

```bash
git add tests/test_dashboard_web.py
git commit -m "test: cover agent report readability redesign"
```

Expected: commit succeeds with only `tests/test_dashboard_web.py` staged.

---

### Task 2: Add Runtime Helper Expectations

**Files:**
- Modify: `tests/test_dashboard_web.py`
- Test: `tests/test_dashboard_web.py`

- [ ] **Step 1: Add a Node runtime test for the new report helpers**

Append this test after `test_dashboard_display_helpers_keep_raw_english_out_of_chinese_ui()`:

```python
def test_dashboard_report_readability_helpers_build_decision_first_sections() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for dashboard helper runtime checks")
    js_path = STATIC_DIR / "dashboard.js"
    script = r"""
const fs = require("fs");
const vm = require("vm");
const code = fs.readFileSync(process.argv[1], "utf8");
const sandbox = { document: { addEventListener() {} } };
vm.createContext(sandbox);
vm.runInContext(code, sandbox);
vm.runInContext(`
state.detailLanguage = "zh";
const holding = {
  market: "US",
  symbol: "DRAM",
  name: "DRAM Test",
  total_quantity: "100",
  strategy: {
    available: true,
    rating: "Underweight",
    target_1: "51",
    target_2: "53",
    stop_loss: "60",
    catalyst: "6 月 24 日财报后复评",
    time_horizon: "1-3 个月",
    plan_text_zh: "财报前先锁定收益，财报后重新评估。",
    agent_reason_zh: "MACD 背离，仓位风险上升。财报是下一判断点。因此先减半而非清仓。",
  },
  agent_report: {
    available: true,
    rating: "Underweight",
    status: "ok",
    run_date: "2026-06-19",
    summary_zh: "评级低配。趋势派认为 MACD 背离。风控派建议锁定部分收益。组合结论是减仓而非清仓。",
    raw_decision: "The bull case remains possible, but risk is elevated.",
  },
  premarket_action: {
    available: true,
    suggested_action: "reduce",
    watch_trigger_zh: "跌回 50 日均线需要复核。",
  },
  trade_action: {
    available: true,
    action: "TRIM",
    status: "ready",
    trigger_status: "target_1_hit",
    limit_price: "51",
    suggested_quantity: "50",
    suggested_notional: "2550",
    notional_currency: "USD",
    stop_price: "60",
    trigger_reason_zh: "达到第一目标价，先锁定部分收益。",
    agent_reason_zh: "MACD 背离，仓位风险上升。财报是下一判断点。因此先减半而非清仓。",
  },
};
const action = currentDecisionAction(holding);
if (action.action !== "TRIM") {
  throw new Error("trade_action should lead the decision row");
}
const desired = desiredActionText(holding);
if (!desired.includes("减仓") || !desired.includes("DRAM")) {
  throw new Error("desired action should be Chinese and symbol-specific: " + desired);
}
const watch = watchPointText(holding);
if (!watch.includes("达到第一目标价") && !watch.includes("财报")) {
  throw new Error("watch point should use trigger or catalyst: " + watch);
}
const metrics = decisionMetricCells(holding).map((cell) => cell[0]).join(",");
if (!metrics.includes("观点") || !metrics.includes("下次复评")) {
  throw new Error("metrics missing required labels: " + metrics);
}
const conclusion = finalConclusionItems(holding).map((item) => item.label).join(",");
if (!conclusion.includes("结论") || !conclusion.includes("失败条件")) {
  throw new Error("conclusion missing required labels: " + conclusion);
}
const html = renderAnalysisStrategySection(holding);
for (const required of ["分析与交易策略", "当前希望你做什么", "操作指令", "今天重点关注", "分析师对话", "最终结论", "查看英文原文"]) {
  if (!html.includes(required)) {
    throw new Error("missing rendered label " + required + " in " + html);
  }
}
if (html.includes("risk is elevated") || html.includes("The bull case")) {
  throw new Error("raw English leaked into primary Chinese UI: " + html);
}
const noActionHtml = renderAnalysisStrategySection({
  market: "US",
  symbol: "CASH",
  strategy: { available: false },
  agent_report: { available: false },
  trade_action: { available: false },
  premarket_action: { available: false },
});
if (!noActionHtml.includes("今天暂无触发中的交易动作")) {
  throw new Error("missing explicit no-action state: " + noActionHtml);
}
`, sandbox);
"""
    subprocess.run([node, "-e", script, str(js_path)], check=True)
```

- [ ] **Step 2: Run the new runtime helper test and confirm it fails**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py::test_dashboard_report_readability_helpers_build_decision_first_sections -v
```

Expected: FAIL with `ReferenceError: currentDecisionAction is not defined`.

- [ ] **Step 3: Commit the failing runtime test**

Run:

```bash
git add tests/test_dashboard_web.py
git commit -m "test: cover decision-first report helpers"
```

Expected: commit succeeds with only `tests/test_dashboard_web.py` staged.

---

### Task 3: Add Decision-First JavaScript Helpers

**Files:**
- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Test: `tests/test_dashboard_web.py`

- [ ] **Step 1: Add helper functions**

In `src/open_trader/dashboard_static/dashboard.js`, insert this block after `nextTriggerText(action, holding)` and before `suggestedNotionalText(action)`:

```javascript
function currentDecisionAction(holding) {
  const tradeAction = holding.trade_action || {};
  if (sectionAvailable(tradeAction)) {
    return tradeAction;
  }
  const premarketAction = holding.premarket_action || {};
  if (sectionAvailable(premarketAction)) {
    return premarketAction;
  }
  return {};
}

function desiredActionText(holding) {
  const action = currentDecisionAction(holding);
  const symbol = detailSymbol(holding);
  const actionText = formatAction(action.action || action.suggested_action);
  if (actionText === "-") {
    return `今天暂无触发中的交易动作`;
  }
  const quantity = firstPresent(action.suggested_quantity, action.target_quantity, action.quantity);
  const quantityText = hasValue(quantity) ? `，数量 ${formatPlain(quantity)}` : "";
  return `${actionText} ${symbol}${quantityText}`;
}

function detailSymbol(holding) {
  const market = formatPlain(holding.market);
  const symbol = formatPlain(holding.symbol);
  if (market === "-" && symbol === "-") {
    return "-";
  }
  if (market === "-") {
    return symbol;
  }
  if (symbol === "-") {
    return market;
  }
  return `${market}.${symbol}`;
}

function decisionSubline(holding) {
  const action = currentDecisionAction(holding);
  if (!sectionAvailable(action)) {
    const view = analystViewText(holding);
    return view === "-" ? "暂无触发动作，继续观察。" : `${view}，暂无触发动作，继续观察。`;
  }
  const trigger = formatTriggerStatus(action.trigger_status || action.watch_trigger);
  const reason = shortActionReason(action);
  const parts = [trigger, reason].filter((part) => part && part !== "-");
  if (!parts.length) {
    return "执行前保持人工确认。";
  }
  return `${parts.join("；")} 执行前保持人工确认。`;
}

function operationRows(holding) {
  const action = currentDecisionAction(holding);
  const strategy = holding.strategy || {};
  return [
    ["动作", actionCardStatusLabel(action)],
    ["价格", firstPresent(action.limit_price, action.last_price, strategy.target_1, strategy.target_range)],
    ["仓位", firstPresent(action.suggested_quantity, action.suggested_notional, strategy.max_weight, strategy.target_weight)],
    ["止损", firstPresent(action.stop_price, strategy.stop_loss)],
  ];
}

function watchPointText(holding) {
  const action = currentDecisionAction(holding);
  const strategy = holding.strategy || {};
  const direct = firstChineseText(
    action.trigger_reason_zh,
    action.watch_trigger_zh,
    strategy.catalyst_zh,
    strategy.plan_text_zh,
    strategy.rationale_zh,
  );
  if (direct) {
    return compactSentence(direct, 92);
  }
  const mappedTrigger = firstMappedLabel(TRIGGER_STATUS_LABELS, action.trigger_status, action.watch_trigger);
  if (mappedTrigger && mappedTrigger !== "未触发") {
    return compactSentence(`${mappedTrigger}；继续观察 ${nextReviewText(holding)}。`, 92);
  }
  const catalyst = safeChineseDisplayText(firstAvailableText(strategy.catalyst, strategy.time_horizon, strategy.plan_text));
  if (catalyst) {
    return compactSentence(catalyst, 92);
  }
  return "暂无新的触发条件，继续观察。";
}

function decisionMetricCells(holding) {
  const action = currentDecisionAction(holding);
  const strategy = holding.strategy || {};
  return [
    ["观点", analystViewText(holding)],
    ["目标价", joinRange(strategy.target_1, strategy.target_2) || strategy.target_range],
    ["触发状态", formatTriggerStatus(action.trigger_status || action.watch_trigger)],
    ["动作状态", formatActionStatus(action.status)],
    ["下次复评", nextReviewText(holding)],
  ];
}

function analystViewText(holding) {
  const strategy = holding.strategy || {};
  const report = holding.agent_report || {};
  return formatAction(strategy.view || strategy.stance || strategy.signal || strategy.rating || report.rating || report.advice_action);
}

function nextReviewText(holding) {
  const strategy = holding.strategy || {};
  const action = currentDecisionAction(holding);
  const direct = firstChineseText(strategy.catalyst_zh, strategy.time_horizon_zh, action.watch_trigger_zh);
  if (direct) {
    return compactSentence(direct, 32);
  }
  const text = safeChineseDisplayText(firstAvailableText(strategy.catalyst, strategy.time_horizon, action.watch_trigger));
  return text ? compactSentence(text, 32) : "-";
}

function finalConclusionItems(holding) {
  const action = currentDecisionAction(holding);
  const strategy = holding.strategy || {};
  return [
    ["结论", finalConclusionText(holding)],
    ["理由", finalReasonText(holding)],
    ["条件", finalConditionText(holding)],
    ["失败条件", firstPresent(action.stop_price, strategy.stop_loss) ? `跌破 ${firstPresent(action.stop_price, strategy.stop_loss)} 后进入防守复核。` : "触发风险条件后进入人工复核。"],
  ].map(([label, text]) => ({ label, text: formatPlain(text) }));
}

function finalConclusionText(holding) {
  const action = currentDecisionAction(holding);
  const view = analystViewText(holding);
  const actionText = formatAction(action.action || action.suggested_action);
  if (actionText === "-" && view === "-") {
    return "暂无明确结论。";
  }
  if (actionText === "-") {
    return `${view}，但今天暂无触发动作。`;
  }
  if (view === "-") {
    return `${actionText}，执行前保持人工确认。`;
  }
  return `${view}，当前动作是${actionText}。`;
}

function finalReasonText(holding) {
  const action = currentDecisionAction(holding);
  const reason = firstChineseText(
    action.trigger_reason_zh,
    action.reason_zh,
    action.agent_reason_zh,
    holding.strategy && holding.strategy.agent_reason_zh,
    holding.agent_report && holding.agent_report.summary_zh,
  );
  if (reason) {
    return compactSentence(reason, 82);
  }
  const mapped = firstMappedLabel(REASON_LABELS, action.trigger_reason, action.reason);
  return mapped || "理由见分析师对话。";
}

function finalConditionText(holding) {
  const strategy = holding.strategy || {};
  const action = currentDecisionAction(holding);
  const text = firstChineseText(strategy.plan_text_zh, strategy.catalyst_zh, action.watch_trigger_zh);
  if (text) {
    return compactSentence(text, 82);
  }
  const trigger = firstMappedLabel(TRIGGER_STATUS_LABELS, action.watch_trigger, action.trigger_status);
  return trigger ? `${trigger} 后复核。` : "出现新的价格或事件触发后复核。";
}

function sourceReviewText(holding) {
  const report = holding.agent_report || {};
  const strategy = holding.strategy || {};
  const action = currentDecisionAction(holding);
  return firstAvailableText(
    report.raw_decision,
    report.raw_report,
    report.full_report,
    report.summary,
    strategy.agent_excerpt,
    action.agent_excerpt,
  );
}
```

- [ ] **Step 2: Run the runtime helper test and confirm it still fails on renderer**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py::test_dashboard_report_readability_helpers_build_decision_first_sections -v
```

Expected: FAIL with `ReferenceError: renderAnalysisStrategySection is not defined`.

- [ ] **Step 3: Commit helper functions**

Run:

```bash
git add src/open_trader/dashboard_static/dashboard.js
git commit -m "feat: add decision report display helpers"
```

Expected: commit succeeds with only `dashboard.js` staged.

---

### Task 4: Add Combined Report Renderer

**Files:**
- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Test: `tests/test_dashboard_web.py`

- [ ] **Step 1: Add `renderAnalysisStrategySection()` and supporting renderers**

In `src/open_trader/dashboard_static/dashboard.js`, insert this block before `renderAgentReportSection(report, holding)`:

```javascript
function renderAnalysisStrategySection(holding) {
  const action = currentDecisionAction(holding);
  const sourceText = sourceReviewText(holding);
  const body = `
    ${renderReportStatusLine(holding)}
    <div class="decision-dashboard">
      <article class="decision-card primary">
        <h4>当前希望你做什么</h4>
        <strong>${escapeHtml(desiredActionText(holding))}</strong>
        <p>${escapeHtml(decisionSubline(holding))}</p>
      </article>
      <article class="decision-card">
        <h4>操作指令</h4>
        <dl class="compact-kv">
          ${operationRows(holding).map(([label, value]) => renderCompactKv(label, value)).join("")}
        </dl>
      </article>
      <article class="decision-card">
        <h4>今天重点关注</h4>
        <p>${escapeHtml(watchPointText(holding))}</p>
      </article>
    </div>
    <div class="decision-metric-strip">
      ${decisionMetricCells(holding).map(([label, value]) => `
        <article>
          <span>${escapeHtml(label)}</span>
          <strong>${escapeHtml(formatPlain(value))}</strong>
        </article>
      `).join("")}
    </div>
    <div class="analysis-main-grid">
      ${renderAnalystDialogue(holding)}
      ${renderFinalConclusion(holding)}
    </div>
    ${renderSourceReview(sourceText)}
  `;
  return renderDetailSection("分析与交易策略", body, "analysis-strategy-section");
}

function renderReportStatusLine(holding) {
  const report = holding.agent_report || {};
  const statusParts = [
    report.run_date || report.generated_at,
    analystViewText(holding),
    report.source_status === "fallback" ? "使用历史报告回退" : formatActionStatus(report.status),
    "只读 · 需要人工确认",
  ].filter((part) => hasValue(part) && part !== "-");
  return `<div class="analysis-status-line">${statusParts.map((part) => `<span>${escapeHtml(part)}</span>`).join("")}</div>`;
}

function renderAnalystDialogue(holding) {
  const rows = rationaleRows(rationaleSource(holding))
    .map((row) => ({
      label: row.label,
      text: chineseDisplayText(row.text),
    }))
    .filter((row) => hasValue(row.text) && row.text !== "-" && !hasRawEnglishProse(row.text));
  if (!rows.length) {
    return `
      <section class="analyst-dialogue">
        <h4>分析师对话</h4>
        ${renderStatusMessage("暂无可展示的中文分析师对话")}
      </section>
    `;
  }
  return `
    <section class="analyst-dialogue">
      <h4>分析师对话</h4>
      <div class="dialogue-list">
        ${rows.map((row) => `
          <div class="dialogue-row">
            <strong>${escapeHtml(row.label)}</strong>
            <span>${escapeHtml(row.text)}</span>
          </div>
        `).join("")}
      </div>
    </section>
  `;
}

function renderFinalConclusion(holding) {
  return `
    <section class="final-conclusion-panel">
      <h4>最终结论</h4>
      <div class="final-conclusion-list">
        ${finalConclusionItems(holding).map((item) => `
          <div>
            <strong>${escapeHtml(item.label)}</strong>
            <span>${escapeHtml(formatPlain(item.text))}</span>
          </div>
        `).join("")}
      </div>
    </section>
  `;
}

function renderSourceReview(sourceText) {
  if (!hasValue(sourceText)) {
    return "";
  }
  return `
    <div class="source-review">
      <button class="raw-toggle english-source-toggle" type="button" data-toggle-raw-report>查看英文原文</button>
      ${renderSplitSourceRows(sourceText)}
    </div>
  `;
}
```

- [ ] **Step 2: Update `renderDetailSection()` to accept an optional CSS class**

Replace the existing `renderDetailSection(title, body)` function with:

```javascript
function renderDetailSection(title, body, extraClass = "") {
  const className = ["detail-section", extraClass].filter(Boolean).join(" ");
  return `
    <section class="${escapeHtml(className)}">
      <h3>${escapeHtml(title)}</h3>
      ${body}
    </section>
  `;
}
```

- [ ] **Step 3: Run the runtime helper test and confirm it passes**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py::test_dashboard_report_readability_helpers_build_decision_first_sections -v
```

Expected: PASS.

- [ ] **Step 4: Commit the combined renderer**

Run:

```bash
git add src/open_trader/dashboard_static/dashboard.js
git commit -m "feat: render decision-first agent report"
```

Expected: commit succeeds with only `dashboard.js` staged.

---

### Task 5: Replace the Detail Page Composition

**Files:**
- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Test: `tests/test_dashboard_web.py`

- [ ] **Step 1: Replace the detail grid contents**

In `renderSymbolDetail(holding, index)`, replace this block:

```javascript
    <div class="detail-grid">
      ${renderAgentReportSection(holding.agent_report, holding)}
      ${renderStrategySection(holding.strategy, holding)}
      ${renderTradeActionSection(holding)}
      ${renderBrokerDetailSection(holding.broker_details)}
    </div>
```

with:

```javascript
    <div class="detail-grid report-readability-grid">
      ${renderAnalysisStrategySection(holding)}
      ${renderBrokerDetailSection(holding.broker_details)}
    </div>
```

- [ ] **Step 2: Mark broker details as lower priority**

In `renderBrokerDetailSection(details)`, replace both calls to `renderDetailSection("券商账户明细", ...)` with:

```javascript
return renderDetailSection("券商账户明细", renderStatusMessage("暂无券商账户明细"), "broker-detail-section");
```

for the empty state, and:

```javascript
return renderDetailSection("券商账户明细", `
  <div class="compact-detail-table">
    <table>
      <thead>
        <tr>
          <th>券商</th>
          <th>账户</th>
          <th>数量</th>
          <th>成本价</th>
          <th>持仓价</th>
          <th>市值</th>
          <th>盈亏</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
  </div>
`, "broker-detail-section");
```

for the non-empty state.

- [ ] **Step 3: Run the static asset test and confirm only CSS assertions fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py::test_dashboard_static_assets_include_local_shell -v
```

Expected: FAIL on missing CSS selectors such as `.analysis-strategy-section`, while JavaScript string assertions pass.

- [ ] **Step 4: Commit the detail composition change**

Run:

```bash
git add src/open_trader/dashboard_static/dashboard.js
git commit -m "feat: combine agent report detail sections"
```

Expected: commit succeeds with only `dashboard.js` staged.

---

### Task 6: Add Combined Report CSS

**Files:**
- Modify: `src/open_trader/dashboard_static/dashboard.css`
- Test: `tests/test_dashboard_web.py`

- [ ] **Step 1: Add desktop styles**

In `src/open_trader/dashboard_static/dashboard.css`, insert this block after the existing `.detail-section h3` rule and before `.decision-band`:

```css
.report-readability-grid {
  grid-template-columns: minmax(0, 1fr);
}

.analysis-strategy-section {
  background: var(--surface);
}

.analysis-status-line {
  align-items: center;
  color: var(--muted);
  display: flex;
  flex-wrap: wrap;
  font-size: 12px;
  font-weight: 700;
  gap: 8px;
  margin-bottom: 12px;
}

.analysis-status-line span {
  background: var(--surface-soft);
  border: 1px solid var(--line);
  border-radius: 999px;
  padding: 4px 8px;
}

.decision-dashboard {
  display: grid;
  gap: 10px;
  grid-template-columns: 1.35fr 1fr 1fr;
}

.decision-card {
  background: var(--surface-soft);
  border: 1px solid var(--line);
  border-radius: 8px;
  min-width: 0;
  padding: 12px;
}

.decision-card.primary {
  background: #eaf3ee;
  border-color: #bfd6ca;
}

.decision-card h4,
.analyst-dialogue h4,
.final-conclusion-panel h4 {
  color: var(--muted);
  font-size: 12px;
  font-weight: 700;
  margin: 0 0 8px;
}

.decision-card strong {
  display: block;
  font-size: 19px;
  line-height: 1.25;
  margin-bottom: 7px;
  overflow-wrap: anywhere;
}

.decision-card p {
  line-height: 1.55;
  margin: 0;
  overflow-wrap: anywhere;
}

.decision-metric-strip {
  display: grid;
  gap: 10px;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  margin-top: 10px;
}

.decision-metric-strip article {
  background: var(--surface-soft);
  border: 1px solid var(--line);
  border-radius: 8px;
  min-height: 62px;
  padding: 9px;
}

.decision-metric-strip span {
  color: var(--muted);
  display: block;
  font-size: 12px;
  font-weight: 700;
}

.decision-metric-strip strong {
  display: block;
  margin-top: 4px;
  overflow-wrap: anywhere;
}

.analysis-main-grid {
  display: grid;
  gap: 12px;
  grid-template-columns: minmax(0, 1.35fr) minmax(280px, 0.85fr);
  margin-top: 12px;
}

.analyst-dialogue,
.final-conclusion-panel {
  background: var(--surface-soft);
  border: 1px solid var(--line);
  border-radius: 8px;
  min-width: 0;
  padding: 12px;
}

.final-conclusion-list {
  display: grid;
  gap: 10px;
}

.final-conclusion-list div {
  border-left: 3px solid var(--accent);
  display: grid;
  gap: 3px;
  padding-left: 9px;
}

.final-conclusion-list strong {
  color: var(--accent-strong);
}

.final-conclusion-list span {
  line-height: 1.5;
  overflow-wrap: anywhere;
}

.source-review {
  border-top: 1px solid var(--line);
  margin-top: 12px;
  padding-top: 12px;
}

.broker-detail-section {
  background: var(--surface-soft);
}
```

- [ ] **Step 2: Update responsive styles**

Inside the existing `@media (max-width: 1180px)` block, replace:

```css
  .decision-band,
  .impact-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
```

with:

```css
  .decision-band,
  .impact-grid,
  .decision-dashboard,
  .decision-metric-strip,
  .analysis-main-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
```

Inside the existing `@media (max-width: 760px)` block, replace:

```css
  .action-card-metrics,
  .action-summary-grid,
  .decision-band,
  .impact-grid {
    grid-template-columns: 1fr;
  }
```

with:

```css
  .action-card-metrics,
  .action-summary-grid,
  .decision-band,
  .impact-grid,
  .decision-dashboard,
  .decision-metric-strip,
  .analysis-main-grid {
    grid-template-columns: 1fr;
  }
```

- [ ] **Step 3: Run the static asset test and confirm it passes**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py::test_dashboard_static_assets_include_local_shell -v
```

Expected: PASS.

- [ ] **Step 4: Commit the CSS**

Run:

```bash
git add src/open_trader/dashboard_static/dashboard.css
git commit -m "style: add decision-first report layout"
```

Expected: commit succeeds with only `dashboard.css` staged.

---

### Task 7: Run Focused Test Suite

**Files:**
- Verify: `tests/test_dashboard_web.py`
- Verify: `src/open_trader/dashboard_static/dashboard.js`
- Verify: `src/open_trader/dashboard_static/dashboard.css`

- [ ] **Step 1: Run dashboard web tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py -v
```

Expected: PASS. If Node is unavailable, the two Node helper tests may be skipped; record that in the final implementation summary.

- [ ] **Step 2: Run dashboard backend tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard.py -v
```

Expected: PASS. This confirms the frontend redesign did not require or break the dashboard payload.

- [ ] **Step 3: Run lint-style diff check**

Run:

```bash
git diff --check
```

Expected: no output and exit code 0.

- [ ] **Step 4: Commit any test-only fixes if needed**

If Steps 1-3 forced small test or formatting corrections, run:

```bash
git add tests/test_dashboard_web.py src/open_trader/dashboard_static/dashboard.js src/open_trader/dashboard_static/dashboard.css
git commit -m "fix: verify agent report readability tests"
```

Expected: create a commit only if there are actual corrections. If there are no changes, do not create an empty commit.

---

### Task 8: Browser Verification

**Files:**
- Verify: local dashboard static UI
- Verify: `src/open_trader/dashboard_static/dashboard.js`
- Verify: `src/open_trader/dashboard_static/dashboard.css`

- [ ] **Step 1: Start the local dashboard server**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m open_trader dashboard --portfolio data/latest/portfolio.csv --data-dir data --reports-dir reports --poll-seconds 5 --host 127.0.0.1 --port 8766
```

Expected: server starts and listens on `http://127.0.0.1:8766`. Keep this session running during browser verification.

- [ ] **Step 2: Open the dashboard in Playwright or the in-app browser**

Open:

```text
http://127.0.0.1:8766
```

Expected: dashboard loads without a top-level error.

- [ ] **Step 3: Select a holding with report/action data**

Use the UI to open a symbol detail row that has TradingAgents and trade action data. If the right rail has `今日交易动作`, click `查看完整策略` on an action card.

Expected:

- The detail view opens.
- The first report section title is `分析与交易策略`.
- The first viewport contains `当前希望你做什么`, `操作指令`, and `今天重点关注`.
- `分析师对话` and `最终结论` are visible without expanding English source.
- The disabled `重新分析 · 未启用` button is still visible.
- Raw English source is hidden behind `查看英文原文`.

- [ ] **Step 4: Verify mobile layout**

Resize the browser to a mobile width around `390x844`.

Expected:

- Decision cards stack in one column.
- Compact metrics stack without overlap.
- Dialogue rows and final conclusion remain readable.
- Buttons and labels do not overflow their containers.

- [ ] **Step 5: Stop the dashboard server**

Stop the server process with Ctrl-C in the server terminal, or kill the specific process if it is running in the background.

Expected: no dashboard server session remains running.

---

### Task 9: Final Verification And Handoff

**Files:**
- Verify: full relevant working tree

- [ ] **Step 1: Check git status**

Run:

```bash
git status --short
```

Expected: only intentional files are modified. Ignore pre-existing unrelated untracked plan files and `.superpowers/` mockup files unless the user asks to clean them.

- [ ] **Step 2: Review final diff**

Run:

```bash
git diff -- src/open_trader/dashboard_static/dashboard.js src/open_trader/dashboard_static/dashboard.css tests/test_dashboard_web.py
```

Expected: diff shows only the decision-first report UI, helper tests, and CSS.

- [ ] **Step 3: Commit final implementation if there are uncommitted implementation changes**

If `git status --short` shows modified implementation or test files from this plan, run:

```bash
git add src/open_trader/dashboard_static/dashboard.js src/open_trader/dashboard_static/dashboard.css tests/test_dashboard_web.py
git commit -m "feat: redesign agent report readability"
```

Expected: commit succeeds. Do not stage `.superpowers/`, runtime `data/`, runtime `reports/`, logs, or unrelated plan files.

- [ ] **Step 4: Summarize verification**

Final response should report:

- The combined report area now shows `当前希望你做什么`, `操作指令`, `今天重点关注`, `分析师对话`, and `最终结论`.
- Raw English source remains collapsed.
- Tests run and their results.
- Browser verification viewports checked.
- Any skipped test reason, such as Node unavailable, if applicable.
