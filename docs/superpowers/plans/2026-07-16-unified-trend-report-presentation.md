# Unified Trend Report Presentation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render A-share, US, and HK trend reports with the same action-first table and mobile-card presentation while preserving each market's existing facts and rules.

**Architecture:** Keep the existing vanilla-JavaScript A-share renderer and CSS hooks as the single visual path. Add only two market-neutral row builders for non-CN facts, route every market through the existing action-first workspace, and generalize the real browser acceptance checks from CN-only to all available trend reports.

**Tech Stack:** Vanilla JavaScript, existing Dashboard CSS, Python 3.12, pytest, Node VM render checks, Playwright-backed real acceptance.

## Global Constraints

- Do not change trade rules, report generation, report JSON/Markdown, positions, broker sync, or notifications.
- Reuse the existing `.cn-trend-*` table/card CSS hooks; do not add a new component system, dependency, or theme.
- A-share-only temperature, phase, and discipline content must remain A-share-only.
- US/HK missing facts render as `—`; no A-share fact may be inferred.
- Section order is sell, manual review, buy, hold for every market.
- Counts remain formal buy, sell all, continue holding, manual review.
- `make acceptance` returning `PASS` is the only completion status.
- After `PASS`, redeploy the exact accepted Git SHA and verify PID, working directory, SHA, fresh logs, and HTTP 200.

---

### Task 1: Route every market through the existing action-first renderer

**Files:**
- Modify: `tests/test_dashboard_web.py:2781-3075`
- Modify: `src/open_trader/dashboard_static/dashboard.js:1915-2205`

**Interfaces:**
- Consumes: existing `renderCnTrendTable(title, kind, headings, rows, note)`, `renderCnTrendCell(label, value, ariaLabel)`, `cnTrendRows(items)`, and `renderTrendAudit(audit)`.
- Produces: `renderMarketSellOrHoldStage(title, items, kind)` and `renderMarketBuyStage(report)` returning table section HTML; `renderTrendReportWorkspace(report)` always returning the action-first workspace.

- [ ] **Step 1: Replace the US legacy-renderer assertion with a failing unified-renderer contract**

In `test_dashboard_renders_action_first_cn_trend_report_only_for_cn`, rename the test to `test_dashboard_renders_action_first_trend_report_for_every_market`. Replace its final US assertion with a real US action payload and these checks:

```javascript
const us = renderTrendReportWorkspace({
  market:"US",broker_label:"富途",market_label:"美股",
  report_date:"2026-07-16",data_date:"2026-07-15",generated_at:"now",
  account_status:"已更新",buy_window:"美股常规交易时段",
  counts:{sell:0,buy:1,hold:0,review:1},sell_actions:[],hold_actions:[],
  buy_actions:[{symbol:"EA",name:"艺电",close:"207.27",strength:"99.8",
    industry:"通讯服务",target_weight:"0.04",target_amount:"4941.49",
    estimated_shares:23,estimated_initial_line:"205.46930"}],
  review_actions:[{symbol:"BOTZ",name:"Global X Robotics ETF",
    reason:"holding_signal_unknown",close:null,strength:null,active_line:null}],
  audit:{account_exceptions:["现金类资产不参与趋势判断"]},
});
for (const text of ["优先处理 · 卖出触发","需要确认 · 人工复核",
  "美股常规交易时段 · 正式买入计划","盘中持续 · 已有持仓",
  "正式买入 1","全部卖出 0","继续持有 0","人工复核 1",
  "EA 艺电","207.27","99.8","通讯服务","4%","4941.49","23 股",
  "205.46930","BOTZ Global X Robotics ETF","趋势信号不完整",
  "账户不参与项","现金类资产不参与趋势判断","审计详情"]) {
  if (!us.includes(text)) throw new Error(text + "\n" + us);
}
const usOrder=["优先处理 · 卖出触发","需要确认 · 人工复核",
  "美股常规交易时段 · 正式买入计划","盘中持续 · 已有持仓"]
  .map((text)=>us.indexOf(`<h2>${text}</h2>`));
if (usOrder.some((index)=>index<0) ||
    !usOrder.every((index,i)=>i===0||usOrder[i-1]<index)) throw new Error(us);
if (!us.includes('class="cn-trend-report"') ||
    (us.match(/class="cn-trend-table"/g) || []).length !== 4 ||
    !us.includes('class="cn-trend-card"') ||
    us.includes("今日执行检查") || us.includes("筛选价（Trend Animals）") ||
    us.includes('class="trend-discipline"')) throw new Error(us);
```

Also update `test_dashboard_trend_report_entries_and_workspace_interactions` to expect the same four action-first titles, the four formal count labels, four `.cn-trend-table` elements, and no `今日执行检查` copy for Futu.

- [ ] **Step 2: Run the render tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_dashboard_web.py::test_dashboard_renders_action_first_trend_report_for_every_market \
  tests/test_dashboard_web.py::test_dashboard_trend_report_entries_and_workspace_interactions
```

Expected: FAIL because US still renders `trend-report-body`, legacy stage titles, and the checklist.

- [ ] **Step 3: Add the two minimal non-CN table row builders**

In `dashboard.js`, keep the current table and cell functions. Add these functions immediately after `renderCnSellOrHoldStage`:

```javascript
function renderMarketSellOrHoldStage(title, items, kind) {
  const action = { sell: "全部卖出", review: "人工复核" }[kind] || "继续持有";
  const reasonHeading = kind === "sell" ? "触发原因" : kind === "review" ? "复核原因" : "当前判断";
  const headings = ["标的", "动作", "执行参考价", "强度", reasonHeading, "活动保护线", "持仓提示"];
  const rows = cnTrendRows(items).map((item) => `<tr class="cn-trend-card">
    ${renderCnTrendCell("标的", cnTrendIdentity(item))}
    ${renderCnTrendCell("动作", action)}
    ${renderCnTrendCell("执行参考价", item.close)}
    ${renderCnTrendCell("强度", item.strength)}
    ${renderCnTrendCell(reasonHeading, TREND_REASON_LABELS[item.reason] || "未知动作或原因，需人工确认")}
    ${renderCnTrendCell("活动保护线", item.active_line)}
    ${renderCnTrendCell("持仓提示", Array.isArray(item.entry_hints) && item.entry_hints.length ? item.entry_hints.map(formatPlain).join("；") : "—")}
  </tr>`);
  return renderCnTrendTable(title, kind, headings, rows);
}

function renderMarketBuyStage(report) {
  const headings = ["标的", "动作", "执行参考价", "强度", "行业", "目标仓位", "金额上限", "预计数量", "预计保护线"];
  const rows = cnTrendRows(report.buy_actions).map((item) => {
    const targetWeight = decimalAsPercent(item.target_weight, "—");
    return `<tr class="cn-trend-card">
      ${renderCnTrendCell("标的", cnTrendIdentity(item))}
      ${renderCnTrendCell("动作", "正式买入")}
      ${renderCnTrendCell("执行参考价", item.close)}
      ${renderCnTrendCell("强度", item.strength)}
      ${renderCnTrendCell("行业", item.industry)}
      ${renderCnTrendCell("目标仓位", targetWeight, `目标仓位 ${targetWeight}`)}
      ${renderCnTrendCell("金额上限", item.target_amount)}
      ${renderCnTrendCell("预计数量", hasValue(item.estimated_shares) ? `${formatPlain(item.estimated_shares)} 股` : "—")}
      ${renderCnTrendCell("预计保护线", item.estimated_initial_line)}
    </tr>`;
  });
  return renderCnTrendTable(`${formatPlain(report.buy_window)} · 正式买入计划`, "buy", headings, rows);
}
```

Update `renderCnTrendCell` so a missing value uses the accepted glyph without changing global formatting:

```javascript
function renderCnTrendCell(label, value, ariaLabel = "") {
  const display = hasValue(value) ? formatPlain(value) : "—";
  return `<td data-label="${escapeHtml(label)}"${ariaLabel ? ` aria-label="${escapeHtml(ariaLabel)}"` : ""}>${escapeHtml(display)}</td>`;
}
```

- [ ] **Step 4: Use one workspace shell and choose only market-specific row builders**

Replace `renderCnTrendReportWorkspace` and `renderTrendReportWorkspace` with one action-first path. Preserve the existing header markup and A-share discipline/audit functions:

```javascript
function renderCnTrendReportWorkspace(report) {
  const counts = report.counts || {};
  const audit = report.audit || {};
  const isCn = String(report.market || "").toUpperCase() === "CN";
  const sellOrHold = isCn ? renderCnSellOrHoldStage : renderMarketSellOrHoldStage;
  const buyStage = isCn ? renderCnBuyStage(report) : renderMarketBuyStage(report);
  return `<main class="cn-trend-report">
    <header class="trend-report-header">
      <div><p>${escapeHtml(`${formatPlain(report.broker_label)}｜${formatPlain(report.market_label)}`)}</p><h1>当天趋势报告</h1></div>
      <button type="button" data-close-trend-report>返回持仓看板</button>
      <dl>
        <div><dt>报告日期</dt><dd>${escapeHtml(formatPlain(report.report_date))}</dd></div>
        <div><dt>数据截至</dt><dd>${escapeHtml(formatPlain(report.data_date))}</dd></div>
        <div><dt>生成时间</dt><dd>${escapeHtml(formatPlain(report.generated_at))}</dd></div>
        <div><dt>账户状态</dt><dd>${escapeHtml(formatPlain(report.account_status))}</dd></div>
      </dl>
      <div class="trend-report-metrics cn-trend-counts">
        <span>正式买入 ${escapeHtml(formatPlain(counts.buy || 0))}</span>
        <span>全部卖出 ${escapeHtml(formatPlain(counts.sell || 0))}</span>
        <span>继续持有 ${escapeHtml(formatPlain(counts.hold || 0))}</span>
        <span>人工复核 ${escapeHtml(formatPlain(counts.review || 0))}</span>
      </div>
    </header>
    <div class="cn-trend-actions">
      ${sellOrHold("优先处理 · 卖出触发", report.sell_actions, "sell")}
      ${sellOrHold("需要确认 · 人工复核", report.review_actions, "review")}
      ${buyStage}
      ${sellOrHold("盘中持续 · 已有持仓", report.hold_actions, "hold")}
    </div>
    ${isCn ? renderCnTrendDisciplines() : ""}
    ${isCn ? renderCnTrendAudit(audit) : renderTrendAudit(audit)}
  </main>`;
}

function renderTrendReportWorkspace(report) {
  return renderCnTrendReportWorkspace(report || {});
}
```

Delete the now-unused `renderTrendAction`, `renderTrendStage`, and `renderDefaultTrendReportWorkspace`. Keep `renderTrendAudit`, because US/HK audit data includes `account_exceptions`.

- [ ] **Step 5: Run focused Dashboard render tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_dashboard_web.py -k 'trend_report'
```

Expected: all selected tests PASS, including malformed-array and HTML-escaping checks.

- [ ] **Step 6: Commit the unified renderer**

```bash
git add src/open_trader/dashboard_static/dashboard.js tests/test_dashboard_web.py
git commit -m "fix: unify trend report presentation"
```

---

### Task 2: Make real browser acceptance enforce the shared presentation

**Files:**
- Modify: `tests/test_dashboard_acceptance.py:1030-1260,2200-2370`
- Modify: `src/open_trader/dashboard_acceptance.py:690-1070`

**Interfaces:**
- Consumes: the shared `.cn-trend-report`, `.cn-trend-stage`, `.cn-trend-table`, `.cn-trend-card`, and `.cn-trend-buy` DOM hooks from Task 1.
- Produces: `_check_action_trend_stages(stage_texts, report, broker)` validating every market's table text and `_check_account_holdings(...)` enforcing the same desktop/mobile structure for all available reports.

- [ ] **Step 1: Update the fake browser contract first**

In `tests/test_dashboard_acceptance.py`, change fake locator counts so `.cn-trend-report` and four `.cn-trend-table` elements exist for every active trend broker, `.trend-discipline` exists only for `eastmoney`, and `[data-close-trend-report]` exists for every active trend broker. Make `.cn-trend-card:visible` return the total number of sell, review, buy, and hold actions in the active report.

Update the final selector assertions to require these selectors for Futu, Phillips, and Eastmoney on wide desktop, desktop, and mobile. Keep the three A-share report screenshot names unchanged; real DOM assertions are authoritative for US/HK.

- [ ] **Step 2: Run the acceptance unit test and verify RED**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_dashboard_acceptance.py -k 'browser_check'
```

Expected: FAIL because `_check_account_holdings` still expects legacy Futu/Phillips titles, counts, and list stages.

- [ ] **Step 3: Generalize stage validation without weakening market facts**

Rename `_check_cn_trend_stages` to `_check_action_trend_stages` and add `broker`. Keep the common title/action/symbol/name validation. For `eastmoney`, retain the existing complete CN facts. For US/HK buy rows validate:

```python
facts = (
    item.get("close"), item.get("strength"), item.get("industry"),
    f"{format((Decimal(str(item.get('target_weight'))) * 100).normalize(), 'f')}%",
    item.get("target_amount"), f"{_plain(item.get('estimated_shares'))} 股",
    item.get("estimated_initial_line"),
)
```

For US/HK sell/review/hold rows validate close, strength, translated reason, active line, and every non-empty `entry_hints` item. Remove `_check_trend_stage` after its final caller is deleted.

- [ ] **Step 4: Require the shared shell, counts, order, tables, and mobile cards for every broker**

In `_check_account_holdings`, remove the non-Eastmoney checklist assertions and legacy branch. For every available broker assert:

```python
for label, key in (
    ("正式买入", "buy"), ("全部卖出", "sell"),
    ("继续持有", "hold"), ("人工复核", "review"),
):
    assert f"{label} {_display_number(counts.get(key) or 0)}" in workspace_text

assert workspace.locator(".cn-trend-report").count() == 1
stage_texts = workspace.locator(".cn-trend-stage").all_inner_texts()
_check_action_trend_stages(stage_texts, report, broker)
assert workspace.locator(".cn-trend-table").count() == 4
```

In `_check_open_report_layout`, remove the `broker != "eastmoney"` early return so every
market verifies the same buy-table keyboard label, focus outline, desktop overflow behavior,
mobile card count, and zero-buy empty state. Change only the assertion messages from
`A 股趋势报告` to `${broker} 趋势报告`; keep the existing geometry and 760px breakpoint.

Keep `_check_cn_buy_rows` and the two A-share discipline assertions only inside `broker == "eastmoney"`. For every mobile report, assert the document has no horizontal overflow and every visible `.cn-trend-card` ends within the 376px boundary. Require `[data-close-trend-report]` for every broker and click it to return.

- [ ] **Step 5: Run acceptance unit tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_dashboard_acceptance.py tests/test_dashboard_web.py
```

Expected: both files PASS.

- [ ] **Step 6: Commit the strengthened acceptance contract**

```bash
git add src/open_trader/dashboard_acceptance.py tests/test_dashboard_acceptance.py
git commit -m "test: enforce unified trend report layout"
```

---

### Task 3: Run the mandatory live gate and deploy the accepted SHA

**Files:**
- Verify: `src/open_trader/dashboard_static/dashboard.js`
- Verify: `src/open_trader/dashboard_acceptance.py`
- Verify: `/tmp/open_trader_dashboard_8766.log`

**Interfaces:**
- Consumes: committed renderer and browser acceptance changes from Tasks 1-2.
- Produces: one `PASS` acceptance result and a review process running the exact accepted SHA.

- [ ] **Step 1: Confirm the worktree is clean and record the candidate SHA**

```bash
git status --short
git rev-parse HEAD
```

Expected: no status output and one full SHA.

- [ ] **Step 2: Restart the candidate from this worktree with real project data**

```bash
screen -S open_trader_dashboard_8766 -X quit 2>/dev/null || true
listener=$(lsof -tiTCP:8766 -sTCP:LISTEN 2>/dev/null || true)
if [ -n "$listener" ]; then kill "$listener"; fi
rm -f /tmp/open_trader_dashboard_8766.log
screen -dmS open_trader_dashboard_8766 zsh -lc \
  'cd /Users/ray/projects/open_trader/.worktrees/dashboard-futu-report-acceptance && exec env PYTHONPATH=src .venv/bin/python -u -m open_trader dashboard --portfolio /Users/ray/projects/open_trader/data/latest/portfolio.csv --data-dir /Users/ray/projects/open_trader/data --reports-dir /Users/ray/projects/open_trader/reports --poll-seconds 5 --host 127.0.0.1 --port 8766 >> /tmp/open_trader_dashboard_8766.log 2>&1'
```

- [ ] **Step 3: Run the only completion gate**

```bash
make acceptance
```

Expected: `2199` or more tests pass and the final JSON line is `{"status": "PASS", ...}`. On `FAIL`, diagnose and fix the reported source, process, data, log, desktop, or mobile failure, then rerun this exact gate. On `BLOCKED`, report the blocker and stop.

- [ ] **Step 4: Redeploy the exact accepted SHA**

Record `ACCEPTED_SHA=$(git rev-parse HEAD)`, restart the same `screen` command from Step 2 without changing source or data, and wait for the new listener.

- [ ] **Step 5: Verify the review deployment**

```bash
NEW_PID=$(lsof -tiTCP:8766 -sTCP:LISTEN)
lsof -a -p "$NEW_PID" -d cwd -Fn
git -C /Users/ray/projects/open_trader/.worktrees/dashboard-futu-report-acceptance rev-parse HEAD
tail -20 /tmp/open_trader_dashboard_8766.log
curl -fsS -o /dev/null -w 'HTTP %{http_code}\n' http://127.0.0.1:8766/
```

Expected: a new PID, the implementation worktree as cwd, SHA exactly equal to `ACCEPTED_SHA`, fresh startup logs without traceback, and `HTTP 200`.
