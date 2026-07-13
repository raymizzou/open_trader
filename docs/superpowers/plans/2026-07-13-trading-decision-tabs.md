# Trading Decision Tabs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the trading-decision plugin grid with four conclusion-first tabs that keep unavailable modules visible and red.

**Architecture:** Keep the static dashboard and its existing module renderers. Add one `selectedDecisionTab` frontend state value, one tab definition/rendering function, and one delegated click path; render only the selected module panel while deriving unavailable status from existing data. CSS supplies the horizontal scroll row, selected state, failure state, and panel sizing.

**Tech Stack:** Plain JavaScript, CSS, Python `pytest`, existing Node VM frontend checks, project Dashboard acceptance runner.

## Global Constraints

- Fixed order: `最终决策`, `趋势 / K 线`, `新闻 / 舆论`, `富途异动`.
- Opening or switching holdings always selects `最终决策`.
- Keep all four tabs visible; unavailable tabs are red and remain clickable.
- An unavailable panel shows its existing error, otherwise `数据未生成`.
- Keep the existing plugin section width and natural content height.
- On narrow screens tabs remain one horizontal scrollable row.
- No backend changes, dependencies, framework, build step, retry UI, or second status model.
- `make acceptance` is the final completion gate; only `PASS` is complete.

---

### Task 1: Render And Switch The Four Decision Tabs

**Files:**
- Modify: `tests/test_dashboard_web.py`
- Modify: `src/open_trader/dashboard_static/dashboard.js`

**Interfaces:**
- Consumes: existing `renderLLMDecisionTemplate(holding)`, `renderTradingAgentsSummaryCard(holding)`, `klineDecisionFactsPlugin(holding)`, `newsSentimentPlugin(holding)`, `futuAnomalySignalsPlugin(holding)`, and `handleSymbolDetailClick(event)`.
- Produces: `DECISION_TABS`, `decisionTabViews(holding)`, `renderTradingDecisionTabs(holding)`, and state property `selectedDecisionTab: "final"`.

- [ ] **Step 1: Add a failing Node runtime test for order, one visible panel, and unavailable status**

Append a focused test to `tests/test_dashboard_web.py`. Load `dashboard.js` using the same Node VM prelude already used by neighboring dashboard renderer tests, then evaluate:

```javascript
const holding = {
  market: "US",
  symbol: "NVDA",
  name: "英伟达",
  total_quantity: "10",
  tradingagents_summary: {
    ta_view: "偏多",
    current_action: "持有",
    core_reason: "趋势仍在",
  },
  decision_facts: {
    kline: { available: true, fields: { trend: "上涨" } },
    news_sentiment: { available: false, error: "新闻任务失败" },
  },
  futu_skill_facts: {},
};
state.selectedDecisionTab = "final";
const html = renderTradingDecisionTabs(holding);
assertOrdered(html, ["最终决策", "趋势 / K 线", "新闻 / 舆论", "富途异动"]);
if ((html.match(/role="tabpanel"/g) || []).length !== 1) throw new Error(html);
if (!html.includes('data-decision-tab="news"') || !html.includes("decision-tab-failed")) throw new Error(html);
if (!html.includes("大模型决策模板") || !html.includes("TradingAgents")) throw new Error(html);
```

In the same test, set `state.selectedDecisionTab = "news"` and assert the sole panel contains `新闻任务失败`, then set it to `"futu"` and assert it contains `数据未生成`.

Also verify delegated switching and holding reset with the same VM globals:

```javascript
let renders = 0;
renderHoldings = () => { renders += 1; };
handleSymbolDetailClick({ target: { closest: (selector) => selector === "[data-decision-tab]" ? { dataset: { decisionTab: "kline" } } : null } });
if (state.selectedDecisionTab !== "kline" || renders !== 1) throw new Error("tab click did not render");
state.selectedDecisionTab = "news";
showSymbolDetail("US|NVDA", "decision");
if (state.selectedDecisionTab !== "final") throw new Error("new holding did not reset tab");
```

- [ ] **Step 2: Run the focused test and verify failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py -k trading_decision_tabs -v
```

Expected: `FAIL` because `renderTradingDecisionTabs` and `selectedDecisionTab` do not exist.

- [ ] **Step 3: Add the minimal tab state, definitions, availability mapping, and renderer**

Add to the top-level `state` object:

```javascript
selectedDecisionTab: "final",
```

Add one fixed definition list near the other frontend constants:

```javascript
const DECISION_TABS = [
  { key: "final", label: "最终决策" },
  { key: "kline", label: "趋势 / K 线" },
  { key: "news", label: "新闻 / 舆论" },
  { key: "futu", label: "富途异动" },
];
```

Replace `renderTradingDecisionPlugins(holding)` with a tab renderer. Keep the existing module renderer functions unchanged. The mapping should return `{key, label, available, error, html}` records:

```javascript
function decisionTabViews(holding) {
  const facts = holding && holding.decision_facts && typeof holding.decision_facts === "object"
    ? holding.decision_facts : {};
  const futuFacts = holding && holding.futu_skill_facts && typeof holding.futu_skill_facts === "object"
    ? holding.futu_skill_facts : {};
  const summary = holding && holding.tradingagents_summary && typeof holding.tradingagents_summary === "object"
    ? holding.tradingagents_summary : {};
  const futuModules = ["technical_anomaly", "capital_anomaly", "derivatives_anomaly"]
    .map((key) => futuFacts[key]);
  const definitions = {
    final: {
      available: [summary.ta_view, summary.current_action, summary.core_reason].some(hasValue),
      error: summary.error,
      html: `${renderLLMDecisionTemplate(holding)}${renderTradingAgentsSummaryCard(holding)}`,
    },
    kline: {
      available: facts.kline && facts.kline.available === true,
      error: facts.kline && facts.kline.error,
      html: renderDecisionPluginCard(klineDecisionFactsPlugin(holding)),
    },
    news: {
      available: facts.news_sentiment && facts.news_sentiment.available === true,
      error: facts.news_sentiment && facts.news_sentiment.error,
      html: renderDecisionPluginCard(newsSentimentPlugin(holding)),
    },
    futu: {
      available: futuModules.some((module) => module && module.available === true),
      error: futuModules.map((module) => module && module.error).find(hasValue),
      html: futuAnomalySignalsPlugin(holding),
    },
  };
  return DECISION_TABS.map((tab) => ({ ...tab, ...definitions[tab.key] }));
}
```

Render native buttons with `role="tab"`, `aria-selected`, `aria-controls`, stable IDs, and `data-decision-tab`. Render only the selected view's panel. When unavailable, replace the module body with its escaped error or `数据未生成`:

```javascript
function renderTradingDecisionTabs(holding) {
  const views = decisionTabViews(holding);
  const selected = views.find((view) => view.key === state.selectedDecisionTab) || views[0];
  const panel = selected.available
    ? selected.html
    : `<div class="decision-tab-empty status-failed">${escapeHtml(selected.error || "数据未生成")}</div>`;
  return `
    <section class="detail-section trading-decision-section">
      <div class="trading-decision-section-header"><div><h3>交易决策</h3><p>结论先行，按证据模块逐项复核。</p></div></div>
      <div class="decision-tab-list" role="tablist" aria-label="交易决策模块">
        ${views.map((view) => `<button id="decision-tab-${view.key}" class="decision-tab${view.key === selected.key ? " active" : ""}${view.available ? "" : " decision-tab-failed"}" type="button" role="tab" aria-selected="${view.key === selected.key}" aria-controls="decision-panel-${view.key}" data-decision-tab="${view.key}">${escapeHtml(view.label)}</button>`).join("")}
      </div>
      <div id="decision-panel-${selected.key}" class="decision-tab-panel" role="tabpanel" aria-labelledby="decision-tab-${selected.key}">${panel}</div>
    </section>`;
}
```

Update `renderSymbolDetail(holding, index)` to call only `renderTradingDecisionTabs(holding)` inside `.trading-decision-layout`; remove the separate `renderLLMDecisionTemplate(holding)` call.

- [ ] **Step 4: Add delegated click handling and reset on holding changes**

At the start of `handleSymbolDetailClick(event)`, handle the tab button:

```javascript
const decisionTab = event.target.closest("[data-decision-tab]");
if (decisionTab) {
  state.selectedDecisionTab = decisionTab.dataset.decisionTab || "final";
  renderHoldings();
  return;
}
```

Set `state.selectedDecisionTab = "final"` in `showSymbolDetail()`, `openTradeActionDetail()`, the back-to-holdings branch, and the market/broker filter branches wherever `selectedHoldingKey` changes or is cleared. Do not preserve tab state across holdings.

- [ ] **Step 5: Run the focused test and the existing renderer tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py -k 'trading_decision_tabs or decision_fact or futu_anomaly or tradingagents' -v
```

Expected: all selected tests `PASS`. If an old test asserts placeholder cards or the old grid, update that assertion to the fixed four-tab contract; do not keep compatibility markup solely for the test.

- [ ] **Step 6: Commit the functional tab behavior**

```bash
git add tests/test_dashboard_web.py src/open_trader/dashboard_static/dashboard.js
git commit -m "feat: tab trading decision modules"
```

---

### Task 2: Style A Single-Row Responsive Tab Surface

**Files:**
- Modify: `tests/test_dashboard_web.py`
- Modify: `src/open_trader/dashboard_static/dashboard.css`

**Interfaces:**
- Consumes: `.decision-tab-list`, `.decision-tab`, `.decision-tab-failed`, `.decision-tab-panel`, and `.decision-tab-empty` emitted by Task 1.
- Produces: the selected visual state, red unavailable state, horizontal narrow-screen overflow, and full-width natural-height panel.

- [ ] **Step 1: Add failing CSS contract assertions**

Add a focused test that reads `dashboard.css` and asserts:

```python
assert ".decision-tab-list" in css
assert "overflow-x: auto" in css
assert "flex-wrap: nowrap" in css
assert ".decision-tab.active" in css
assert ".decision-tab-failed" in css
assert ".decision-tab-panel" in css
```

- [ ] **Step 2: Run the CSS test and verify failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py -k trading_decision_tab_css -v
```

Expected: `FAIL` because the tab classes are not styled.

- [ ] **Step 3: Add the minimal tab CSS and remove obsolete grid-only rules**

Add:

```css
.decision-tab-list {
  border-bottom: 1px solid var(--line);
  display: flex;
  flex-wrap: nowrap;
  gap: 6px;
  overflow-x: auto;
  scrollbar-width: thin;
}

.decision-tab {
  background: transparent;
  border: 0;
  border-bottom: 3px solid transparent;
  color: var(--muted);
  cursor: pointer;
  flex: 0 0 auto;
  font: inherit;
  font-weight: 800;
  padding: 10px 12px;
}

.decision-tab.active {
  border-bottom-color: var(--accent);
  color: var(--text);
}

.decision-tab-failed {
  color: var(--danger);
}

.decision-tab-failed.active {
  border-bottom-color: var(--danger);
}

.decision-tab:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}

.decision-tab-panel {
  min-width: 0;
  padding-top: 12px;
  width: 100%;
}

.decision-tab-empty {
  background: #fff1f1;
  border: 1px solid var(--danger);
  border-radius: 8px;
  color: var(--danger);
  padding: 16px;
}
```

Remove `.decision-plugin-grid` from responsive grid selector groups when it no longer renders. Keep card styles still used inside selected panels. Do not add fixed heights or mobile-only JavaScript.

- [ ] **Step 4: Run the CSS test and Dashboard frontend suite**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py -v
```

Expected: all tests `PASS`.

- [ ] **Step 5: Commit the responsive styling**

```bash
git add tests/test_dashboard_web.py src/open_trader/dashboard_static/dashboard.css
git commit -m "style: focus trading decision tabs"
```

---

### Task 3: Verify The Real Dashboard And Acceptance Gate

**Files:**
- Verify only: `src/open_trader/dashboard_static/dashboard.js`
- Verify only: `src/open_trader/dashboard_static/dashboard.css`
- Verify only: running Dashboard process, logs, desktop browser, and mobile browser.

**Interfaces:**
- Consumes: Tasks 1 and 2.
- Produces: project-required acceptance evidence; no new product interface.

- [ ] **Step 1: Run focused automated tests**

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py -v
```

Expected: all tests `PASS`, with the exact count recorded in the handoff.

- [ ] **Step 2: Run the full project test command**

```bash
make test
```

Expected: exit code `0` and all tests `PASS`.

- [ ] **Step 3: Inspect live process state before acceptance**

```bash
screen -ls
launchctl list | rg 'open-trader|open_trader' || true
ps aux | rg '[o]pen_trader.*dashboard'
```

Expected: identify any Dashboard process that could still hold pre-change JavaScript. Stop or restart only the affected old Dashboard process using its existing project workflow; do not disturb unrelated services.

- [ ] **Step 4: Run the mandatory final Dashboard gate**

```bash
make acceptance
```

Expected: final line/status `PASS`. The gate itself checks automated tests, real API/data, two refresh cycles, process version, fresh logs, and desktop/mobile browser flows. `FAIL` must be diagnosed and fixed before rerunning; `BLOCKED` must be reported as blocked.

- [ ] **Step 5: Inspect fresh process/log evidence produced by acceptance**

Confirm the configured port listener and default acceptance log are fresh:

```bash
lsof -nP -iTCP:8766 -sTCP:LISTEN
tail -n 80 /tmp/open_trader_dashboard_8766.log
```

Expected: the PID is running the current checkout and the log contains fresh timestamps from the acceptance run without tab-rendering errors.

- [ ] **Step 6: Record only verified completion**

If and only if `make acceptance` reports `PASS`, hand off the deployed URL printed by the gate and the exact focused/full test results. On `FAIL`, continue fixing. On `BLOCKED`, report the blocker without substituting curl, fixtures, mocks, screenshots, or unit tests.
