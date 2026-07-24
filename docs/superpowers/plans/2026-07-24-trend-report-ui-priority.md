# Trend Report UI Priority Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Apply the approved compact, semantic-color, list-based trend-report UI consistently to CN, US, and HK reports, with Playwright acceptance covering every market entry point.

**Architecture:** Keep the existing vanilla JavaScript renderer and CSS sheet. Change the shared renderTrendReportWorkspace path so every market receives the same hierarchy, density, list semantics, and disclosure defaults; keep market-specific facts inside the current CN and market-neutral branches. Extend the existing Python Playwright acceptance flow.

**Tech Stack:** Vanilla JavaScript templates, existing CSS custom properties, Python pytest, Python Playwright, existing Dashboard server.

## Global Constraints

- Apply the same hierarchy, density, semantic accents, list treatment, and disclosure defaults to every CN, US, and HK trend report.
- Preserve all report data, trading rules, risk calculations, execution behavior, and audit semantics.
- Keep sell actions and buy plans visible; omit manual review only when it has no rows.
- Keep 纪律, portfolio risk, strategy controller, and audit details collapsed by default.
- Keep interactive buttons and disclosure summaries at least 44px high.
- Add no frontend framework, dependency, generic component system, or animation.
- Verify desktop and 375px layouts with Playwright; the final gate is make acceptance.

---

### Task 1: Define the cross-market renderer contract with failing tests

**Files**

- Modify: tests/test_dashboard_web.py around the existing action-first, frozen-lifecycle, and market-neutral discipline tests.

**Interfaces**

- Consumes: the existing run_dashboard_js helper and renderTrendReportWorkspace.
- Produces: executable assertions for CN, US, and HK order, labels, lists, and default disclosures.

- [ ] **Step 1: Write the failing assertions**

Add these assertions to the existing JavaScript fixture:

~~~javascript
const reportOrder = (html) => [
  '<header class="trend-report-header">',
  '优先处理 · 卖出触发',
  '正式买入计划',
  '盘中持续 · 已有持仓',
  '行业上下文',
  '<summary>纪律',
  '<summary>组合计划风险',
  '<summary>策略控制器',
  '<summary>审计详情',
].map((needle) => html.indexOf(needle));

for (const market of ["CN", "US", "HK"]) {
  const html = renderTrendReportWorkspace(report(market));
  const order = reportOrder(html);
  if (order.some((index) => index < 0) ||
      !order.every((index, i) => i === 0 || order[i - 1] < index)) {
    throw new Error(market + ": report order\n" + html);
  }
  for (const text of ["卖出 0", "买入 0", "持有 0", "复核 0",
    "纪律", "组合计划风险", "策略控制器", "审计详情"]) {
    if (!html.includes(text)) throw new Error(market + ": missing " + text);
  }
  if (html.includes("策略参数快照") || html.includes("冻结策略纪律")) {
    throw new Error(market + ": retired title");
  }
  if (html.includes('<details class="trend-discipline-card"')) {
    throw new Error(market + ": legacy discipline cards");
  }
  for (const selector of ["trend-risk-summary", "trend-controller-status", "trend-audit"]) {
    if (html.includes('class="' + selector + '" open')) {
      throw new Error(market + ": " + selector + " open by default");
    }
  }
}

const withReview = renderTrendReportWorkspace({
  ...report("US"),
  counts: {sell:0, buy:0, hold:0, review:1},
  review_actions: [{symbol:"BOTZ", name:"Global X Robotics ETF",
    reason:"holding_signal_unknown"}],
});
if (!withReview.includes("需要确认 · 人工复核")) throw new Error("review rows missing");

const withoutReview = renderTrendReportWorkspace({
  ...report("US"),
  counts: {sell:0, buy:0, hold:0, review:0},
  review_actions: [],
});
if (withoutReview.includes("需要确认 · 人工复核")) throw new Error("empty review stage");
~~~

- [ ] **Step 2: Verify red**

Run:

~~~bash
.venv/bin/python -m pytest tests/test_dashboard_web.py -q -k 'action_first_trend_report or frozen_lifecycle_cards_and_industry_context or market_neutral_empty_discipline_state'
~~~

Expected: FAIL because the current renderer puts the controller before actions, opens desktop discipline cards, uses retired titles, and renders industry cards.

- [ ] **Step 3: Commit the red tests**

~~~bash
git add tests/test_dashboard_web.py
git commit -m "test: define compact cross-market trend report layout"
~~~

### Task 2: Implement shared report HTML order and disclosure behavior

**Files**

- Modify: src/open_trader/dashboard_static/dashboard.js lines 2382-2825 and 3021-3136.
- Test: tests/test_dashboard_web.py.

**Interfaces**

- Consumes: existing report payloads, renderCnTrendTable, renderCnTrendCell, trendFrozenStrategyRows, risk helpers, and audit helpers.
- Produces: the approved shared classes and DOM order for all markets.

- [ ] **Step 1: Add the failing grouped-disclosure test**

Add a fixture with strategy_parameter_rows containing rows from 候选来源,
入场过滤, 候选排序, 仓位执行, and 退出保护. Assert the HTML contains one
outer details element with class trend-discipline-workspace, a summary titled
纪律, and nested summaries named 入场门槛, 候选排序, 仓位执行, 持有管理,
退出规则, and 其他设置. Assert it contains neither 策略参数快照 nor 冻结策略纪律.

- [ ] **Step 2: Verify red**

Run the focused command from Task 1. Expected: FAIL on the old title and
missing outer disclosure.

- [ ] **Step 3: Implement the renderer changes**

Update renderCnTrendReportWorkspace so that, after the header and blocking
notices, it appends the sell stage, buy stage, optional review stage, hold
stage, industry section, discipline disclosure, risk disclosure, controller
disclosure, and audit disclosure in that exact order. Remove the wrapper that
places industry beside sell. Render review only when review_actions contains
rows.

Update the action renderers so each instrument is one table row. Keep existing
secondary facts and execution rows; the compact primary buy columns are
instrument, action, reference price, trend, industry, target weight, target
amount, quantity, protection line, and planned risk. Existing filter price,
phase, strength, context, market cap, turnover, and execution facts remain in
secondary cell text or subordinate rows.

Replace renderTrendIndustryContext article cards with a full-width table and
one tr element with class trend-industry-context-row per context. Preserve all
fallback, invalid-reason, historical-share, and percentage-point text.

Replace renderTrendDisciplineCards with an outer native details element titled
纪律, collapsed by default. Its summary reports six categories and the
frozen-row count. Render six nested native details titled 入场门槛, 候选排序,
仓位执行, 持有管理, 退出规则, and 其他设置. Each nested body renders every
frozen group/name/value with escaping. Remove repeated visible phrases 冻结,
影响 N 条纪律, and source-group prefixes.

Wrap the current risk body in a collapsed details element with class
trend-risk-summary whose summary retains budget status, used percentage, and
remaining risk. Wrap the current controller facts in a collapsed details
element with class trend-controller-status whose summary retains health,
executor host, and latest success. Preserve blocking, data-health, risk
attributes, and every existing body fact.

Shorten header labels to 报告, 数据, 生成, 账户, 卖出, 买入, 持有, and 复核
while preserving values and API-cost semantics.

- [ ] **Step 4: Verify green**

~~~bash
.venv/bin/python -m pytest tests/test_dashboard_web.py -q -k 'action_first_trend_report or frozen_lifecycle_cards_and_industry_context or market_neutral_empty_discipline_state or frozen_risk_summary'
~~~

Expected: PASS.

- [ ] **Step 5: Commit**

~~~bash
git add src/open_trader/dashboard_static/dashboard.js tests/test_dashboard_web.py
git commit -m "feat: reorder and compact cross-market trend reports"
~~~

### Task 3: Match layout-v4 density and semantic accents

**Files**

- Modify: src/open_trader/dashboard_static/dashboard.css lines 1-20, 1840-2068, 2238-2420, and 4680-4930.
- Test: tests/test_dashboard_web.py.

**Interfaces**

- Consumes: Task 2 classes.
- Produces: the approved full-width layout, density, focus behavior, and 375px behavior.

- [ ] **Step 1: Add failing CSS contract checks**

Assert the CSS contains the header, sell, buy, hold, industry, discipline,
risk, controller, and audit selectors and no longer uses the industry/sell
two-column grid. Assert the mobile rule keeps scrollWidth less than or equal
to innerWidth.

- [ ] **Step 2: Verify red**

~~~bash
.venv/bin/python -m pytest tests/test_dashboard_web.py -q -k 'trend_report_mobile_layout'
~~~

Expected: FAIL against the old two-column industry/action layout and
five-column discipline grid.

- [ ] **Step 3: Implement CSS**

Add semantic tokens --trend-hold: #b7791f and --trend-info: #39738d. Set the
report header to the existing brand accent, sell to --danger, buy to --ok,
hold to --trend-hold, industry to --trend-info, and folded evidence to a
subdued brand accent. Keep text on existing --text and --muted tokens.

Set report/section gap to 10px, section padding to 10px 12px 11px, table cell
padding to 6px 8px, compact fact padding to 5px 9px, and disclosure summary
min-height to 44px. Make industry full width; remove desktop two-column
placement and mobile reordering. Style nested discipline summaries as a
two-column desktop grid and one-column 760px grid. Preserve the existing
desktop buy scroller and mobile table-to-card transformation.

- [ ] **Step 4: Verify green**

~~~bash
.venv/bin/python -m pytest tests/test_dashboard_web.py -q -k 'trend_report_mobile_layout or action_first_trend_report or frozen_lifecycle_cards_and_industry_context'
~~~

Expected: PASS.

- [ ] **Step 5: Commit**

~~~bash
git add src/open_trader/dashboard_static/dashboard.css tests/test_dashboard_web.py
git commit -m "feat: compact trend report sections with semantic accents"
~~~

### Task 4: Extend Playwright acceptance to all markets

**Files**

- Modify: src/open_trader/dashboard_acceptance.py helpers around _check_frozen_trend_disciplines, _check_action_trend_stages, and the live report flow.
- Modify: tests/test_dashboard_acceptance.py expectations for titles, order, and defaults.

**Interfaces**

- Consumes: Task 2/3 DOM and CSS.
- Produces: live Playwright checks for every available CN, US, and HK report entry at desktop and 375px.

- [ ] **Step 1: Add failing browser assertions**

In the live report checker, collect these selectors in DOM order:

~~~python
ordered = report_root.locator(
    ".trend-report-header, .trend-execution-batch-error, .trend-revision-anomaly, "
    ".cn-trend-sell, .cn-trend-buy, .cn-trend-review, .cn-trend-hold, "
    ".trend-industry-context, .trend-discipline-workspace, .trend-risk-summary, "
    ".trend-controller-status, .trend-audit"
)
assert ordered.count() >= 7
boxes = ordered.evaluate_all(
    "nodes => nodes.map(node => ({className: node.className, y: node.getBoundingClientRect().top}))"
)
assert all(current["y"] <= following["y"] for current, following in zip(boxes, boxes[1:]))
for selector in (".trend-discipline-workspace", ".trend-risk-summary",
                 ".trend-controller-status", ".trend-audit"):
    node = report_root.locator(selector)
    assert node.count() == 1
    assert node.get_attribute("open") is None
~~~

At 375px assert document.documentElement.scrollWidth <= window.innerWidth.
For every summary, click, verify it receives focus and opens, then click again
and verify it closes.

- [ ] **Step 2: Verify red**

~~~bash
.venv/bin/python -m pytest tests/test_dashboard_acceptance.py -q -k 'trend_report'
~~~

Expected: FAIL on old order and default-open state.

- [ ] **Step 3: Update the live browser loop**

Replace old CN-only titles/counts with compact labels but retain each
market-specific buy-window and fact assertion. Iterate the existing live report
entry map for eastmoney (CN), tiger (US), phillips (HK), and every other broker
entry exposing a trend report. Expand 纪律 and its six nested categories, risk,
controller, and audit once; verify keyboard focus and close each. Click the
report trigger to close the report and assert focus returns.

- [ ] **Step 4: Verify green**

~~~bash
.venv/bin/python -m pytest tests/test_dashboard_acceptance.py -q -k 'trend_report'
~~~

Expected: PASS for all available markets and both viewports.

- [ ] **Step 5: Commit**

~~~bash
git add src/open_trader/dashboard_acceptance.py tests/test_dashboard_acceptance.py
git commit -m "test: accept compact trend reports across markets"
~~~

### Task 5: Changelog and final verification

**Files**

- Modify: CHANGELOG.md.

- [ ] **Step 1: Add the dated operator-facing entry**

Add a 2026-07-24 entry describing the shared CN/US/HK hierarchy, compact
instrument/industry rows, semantic accents, default disclosures, and
Playwright coverage.

- [ ] **Step 2: Run the full automated suite**

~~~bash
make test
~~~

Expected: exit 0 with 0 failures.

- [ ] **Step 3: Run the live Dashboard workflow**

Start the Dashboard from this worktree, use Playwright to open one current CN,
US, and HK report, inspect PID/working directory/Git SHA, stop or restart any
old process using pre-change code, confirm fresh logs contain the new SHA, and
confirm HTTP 200.

- [ ] **Step 4: Run the final gate**

~~~bash
make acceptance
~~~

Expected: PASS. Fix FAIL and rerun; report BLOCKED without claiming completion
when the required browser or external environment is unavailable.

- [ ] **Step 5: Commit changelog and verify the worktree**

~~~bash
git add CHANGELOG.md
git commit -m "docs: record compact cross-market trend report UI"
git status --short --branch
~~~

After PASS, redeploy the exact accepted SHA and verify PID, working directory,
SHA, fresh logs, and HTTP 200 before handing over the review URL.

## Plan Self-Review

- Spec coverage: summary, ordered actions, optional review, full-width holdings
  and industry lists, semantic accents, 纪律/risk/controller/audit disclosures,
  responsive density, all CN/US/HK markets, and Playwright are covered.
- Placeholder scan: no TBD, TODO, or deferred implementation step remains.
- Selector consistency: Task 2 emits the selectors consumed by Tasks 3 and 4,
  and every market uses the shared workspace renderer.
