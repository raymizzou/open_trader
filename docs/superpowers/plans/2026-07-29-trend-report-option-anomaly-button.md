# Trend Report Futu Option Anomaly Button Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show existing Futu `derivatives_anomaly` details from US/HK trend-report buy and hold rows, fail closed when the dated fact is unavailable, and remove the legacy synthetic option-attention surfaces from Dashboard and Feishu.

**Architecture:** `dashboard.py` joins each projected trend report to the already-written Futu fact file for that report date and attaches one small `option_anomaly` view to eligible buy/hold rows. `dashboard.js` renders the button and a native `<dialog>` directly inside the existing symbol cell, so no route, dependency, or extra table column is needed. The legacy aggregate projection and Feishu renderer are retired without changing frozen report JSON or replay behavior.

**Tech Stack:** Python 3.12, pytest, plain JavaScript, native HTML `<dialog>`, existing Dashboard CSS, Playwright acceptance.

## Global Constraints

- Work only in `/Users/ray/projects/open_trader/.worktrees/trend-report-option-anomaly-button` on `feat/trend-report-option-anomaly-button`.
- Reuse `futu_skill_facts` and its existing `derivatives_anomaly`; add no API, dependency, collection job, or external request.
- Show buttons only for US/HK formal buy and hold rows; omit them from sells, reviews, risk skips, and CN.
- Missing, unsupported, error, stale, and report-date mismatch states must render a disabled button.
- Current reports may use `data/latest/{market}/futu_skill_facts.json`; historical reports must use `data/runs/{report_date}/{market}/futu_skill_facts.json`.
- Do not rewrite frozen trend reports or remove `option_attention` from historical audit/replay schemas.
- Remove the Dashboard aggregate “期权关注” entry and the Feishu “期权关注” section.
- Run focused checks during development; run `make acceptance` only as the final Dashboard gate.
- Before any merge to `main`, commit the dated operator-facing `CHANGELOG.md` entry.

---

### Task 1: Project dated Futu derivatives data onto trend rows

**Files:**
- Modify: `src/open_trader/dashboard.py:36-44,658-871,794-832,1791-2065,3784-3860`
- Test: `tests/test_dashboard.py:780-930,3939-4055,5666-5785`

**Interfaces:**
- Consumes: `futu_skill_facts_latest_path(data_dir, market)`, `futu_skill_facts_run_path(data_dir, run_date, market)`, `index_futu_skill_facts_by_market_symbol(payload)`, `_futu_skill_signal_detail(module, run_date, advice_row)`.
- Produces: `item["option_anomaly"]`, a dictionary with the existing signal-detail keys plus `run_date` and `reason`.
- Produces: `_project_broker_trend_report(..., historical: bool = False)`; `load_historical_trend_report` passes `historical=True`.

- [ ] **Step 1: Read the repository test-writing guidance**

Run:

```bash
sed -n '1,320p' /Users/ray/.codex/plugins/cache/openai-curated-remote/superpowers/6.2.0/skills/test-driven-development/writing-good-tests.md
```

Expected: guidance is read before changing tests.

- [ ] **Step 2: Add failing current and historical projection tests**

Add focused tests that build a US trend report containing one `BUY` row for `VIXY` and one `HOLD` row without a Futu record:

```python
def test_trend_report_projects_only_same_day_futu_derivatives(tmp_path: Path) -> None:
    config = dashboard_config(tmp_path)
    payload = write_trend_history_report(
        config.reports_dir,
        "2026-07-15.json",
        execution_date="2026-07-15",
        generated_at="2026-07-15T09:00:00+08:00",
    )
    payload["strategy_judgments"]["holding_decisions"] = [
        {"action": "HOLD", "symbol": "SPY"},
    ]
    (
        config.reports_dir / "trend_us_tiger/2026-07-15.json"
    ).write_text(json.dumps(payload), encoding="utf-8")
    write_futu_skill_facts(
        config.data_dir / "latest/US/futu_skill_facts.json",
        run_date="2026-07-15",
    )

    report = dashboard_module._load_trend_reports(
        config.data_dir,
        config.reports_dir,
        today=date(2026, 7, 15),
    )["tiger"]

    assert report["buy_actions"][0]["option_anomaly"]["available"] is True
    assert report["buy_actions"][0]["option_anomaly"]["summary"] == "期权波动率偏高。"
    assert report["hold_actions"][0]["option_anomaly"]["available"] is False
    assert report["hold_actions"][0]["option_anomaly"]["reason"] == "富途未返回该标的期权异动"
```

Add one mismatch case asserting `status == "stale_run_date"` and a disabled reason, plus one historical case that writes only:

```text
data/runs/2026-07-15/US/futu_skill_facts.json
```

and asserts `load_historical_trend_report(...)` uses it even when `data/latest/US/futu_skill_facts.json` contains a different run date.

- [ ] **Step 3: Run the new tests and confirm RED**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_dashboard.py::test_trend_report_projects_only_same_day_futu_derivatives \
  tests/test_dashboard.py::test_trend_report_disables_mismatched_futu_derivatives \
  tests/test_dashboard.py::test_historical_trend_report_uses_archived_futu_derivatives
```

Expected: failures because trend action rows do not yet contain `option_anomaly`.

- [ ] **Step 4: Implement the smallest dated fact projection**

Import the existing path helpers:

```python
from .futu_skill_facts import (
    futu_skill_facts_latest_path,
    futu_skill_facts_run_path,
    index_futu_skill_facts_by_market_symbol,
    load_futu_skill_facts_cache,
)
```

Add one loader/projector:

```python
def _trend_option_anomalies(
    data_dir: Path,
    *,
    market: str,
    report_date: str,
    historical: bool,
) -> dict[tuple[str, str], dict[str, Any]]:
    path = (
        futu_skill_facts_run_path(data_dir, report_date, market)
        if historical
        else futu_skill_facts_latest_path(data_dir, market)
    )
    records = index_futu_skill_facts_by_market_symbol(
        load_futu_skill_facts_cache(path)
    )
    projected: dict[tuple[str, str], dict[str, Any]] = {}
    for key, record in records.items():
        run_date = str(record.get("run_date") or "")
        detail = _futu_skill_signal_detail(
            record.get("derivatives_anomaly"),
            run_date,
            {"run_date": report_date},
        )
        if detail["available"]:
            reason = ""
        elif detail["status"] == "stale_run_date":
            reason = "富途期权异动日期与趋势报告不一致"
        elif detail["unsupported"]:
            reason = "富途不支持该标的期权异动"
        else:
            reason = str(detail["error"] or "富途未返回该标的期权异动")
        projected[key] = {**detail, "run_date": run_date, "reason": reason}
    return projected
```

In `_project_broker_trend_report`, after `_project_trend_actions`, attach only to `buy_actions + hold_actions` when `market in {"US", "HK"}`. For a missing symbol, attach `_missing_futu_skill_signal()` plus:

```python
{"run_date": "", "reason": "富途未返回该标的期权异动"}
```

Use the existing symbol-normalization helper used by Dashboard holdings instead of adding a second normalizer.

- [ ] **Step 5: Run the projection tests and confirm GREEN**

Run the Step 3 command.

Expected: `3 passed`.

- [ ] **Step 6: Run neighboring Dashboard projection tests**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_dashboard.py -k 'trend_report or futu_skill_facts'
```

Expected: all selected tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/open_trader/dashboard.py tests/test_dashboard.py
git commit -m "feat: project Futu option anomalies onto trend reports"
```

---

### Task 2: Render the row button and native detail dialog

**Files:**
- Modify: `src/open_trader/dashboard_static/dashboard.js:270-405,3041-3051,3232-3315,3780-3865`
- Modify: `src/open_trader/dashboard_static/dashboard.css:2433-2510,5088-5140`
- Test: `tests/test_dashboard_web.py:4620-4710,6720-6905`

**Interfaces:**
- Consumes: `item.option_anomaly` from Task 1.
- Produces: `renderTrendOptionIdentityCell(item)` and native `<dialog class="trend-option-dialog">`.
- Produces: `data-option-anomaly-open` and `data-option-anomaly-close` click targets.

- [ ] **Step 1: Add failing render tests**

Add a JS-render test with:

```javascript
const available = {
  symbol: "VIXY", name: "波动率ETF",
  option_anomaly: {
    available: true, status: "partial", run_date: "2026-07-15",
    window_days: 7, signal: "risk_up", confidence: "low",
    suggested_constraint: "no_add", summary: "<b>不得执行</b>",
    categories: [{
      name: "期权波动率", state: "anomaly", direction: "risk_up",
      detail: "<img src=x onerror=alert(1)>", evidence_date: "2026-07-15",
    }],
  },
};
const missing = {
  symbol: "SPY", name: "标普ETF",
  option_anomaly: {
    available: false, status: "missing",
    reason: "富途未返回该标的期权异动", categories: [],
  },
};
```

Assert:

- buy and hold rows contain `期权异动`;
- available rows contain a native `<dialog>` and escaped hostile strings;
- missing rows contain a disabled button and the reason in `title`;
- sell, review, risk-skip, and CN rows do not contain the button;
- the heading count is unchanged, proving no new column was added.

- [ ] **Step 2: Run the render test and confirm RED**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_dashboard_web.py::test_dashboard_renders_option_anomaly_button_and_native_dialog
```

Expected: failure because no option button/dialog renderer exists.

- [ ] **Step 3: Implement one native-dialog renderer**

Add a symbol-cell renderer that escapes all values and uses existing
`translateFutuSignalValue`:

```javascript
function renderTrendOptionIdentityCell(item) {
  const anomaly = item?.option_anomaly && typeof item.option_anomaly === "object"
    ? item.option_anomaly : {};
  const identity = escapeHtml(trendIdentity(item) || "数据未提供");
  const reason = formatPlain(anomaly.reason || "富途未返回该标的期权异动");
  const button = anomaly.available === true
    ? `<button class="trend-option-button" type="button" data-option-anomaly-open>期权异动</button>`
    : `<button class="trend-option-button" type="button" disabled title="${escapeHtml(reason)}" aria-label="期权异动不可用：${escapeHtml(reason)}">期权异动</button>`;
  return `<td data-label="标的"><strong>${identity}</strong>${button}${
    anomaly.available === true ? renderTrendOptionDialog(item, anomaly) : ""
  }</td>`;
}
```

Render one dialog next to each enabled button. Its fixed order is:

1. source, symbol/name, run date, and window;
2. summary;
3. signal and confidence;
4. suggested constraint;
5. every category with state, direction, detail, and evidence date;
6. `关闭` button.

Use delegated events on both existing report containers:

```javascript
function handleTrendOptionDialog(event) {
  const close = event.target.closest("[data-option-anomaly-close]");
  if (close) {
    close.closest("dialog")?.close();
    return true;
  }
  const open = event.target.closest("[data-option-anomaly-open]");
  if (!open) return false;
  open.parentElement?.querySelector("dialog")?.showModal();
  return true;
}
```

Call this before the existing report-navigation branches in
`#account-holdings` and `#trend-report-workspace` click handlers. Native dialog
supplies Escape-to-close and focus containment; do not add a custom modal manager.

- [ ] **Step 4: Add minimal CSS**

Style `.trend-option-button`, `.trend-option-dialog`,
`.trend-option-dialog::backdrop`, its header/summary/category rows, and the
mobile single-column arrangement. Keep the visual button at 32–34px but give it
a 44px minimum touch target through padding/min-height at `max-width: 760px`.

- [ ] **Step 5: Run the render and CSS tests**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_dashboard_web.py::test_dashboard_renders_option_anomaly_button_and_native_dialog \
  tests/test_dashboard_web.py::test_dashboard_trend_report_mobile_layout_css
```

Expected: `2 passed`.

- [ ] **Step 6: Commit**

```bash
git add \
  src/open_trader/dashboard_static/dashboard.js \
  src/open_trader/dashboard_static/dashboard.css \
  tests/test_dashboard_web.py
git commit -m "feat: show Futu option anomaly dialog in trend rows"
```

---

### Task 3: Retire the synthetic Dashboard option-attention entry

**Files:**
- Modify: `src/open_trader/dashboard.py:658-871`
- Modify: `src/open_trader/dashboard_static/dashboard.js:2845-2885,3915-3995`
- Modify: `src/open_trader/dashboard_static/dashboard.css:2505-2585` and option-attention mobile rules
- Test: `tests/test_dashboard.py:780-3505`
- Test: `tests/test_dashboard_web.py:4620-4710,6720-6905`

**Interfaces:**
- Removes: `trend_reports["futu"]` and `_project_futu_attention`.
- Preserves: real Futu holdings, Futu fact tabs, and US/HK trend reports.

- [ ] **Step 1: Add failing retirement assertions**

Assert:

```python
state = load_dashboard_state(config).to_dict()
assert "futu" not in state["trend_reports"]
```

and in the JS harness:

```javascript
if (renderTrendReportEntry("futu") !== "") throw new Error("legacy Futu entry remains");
```

- [ ] **Step 2: Run the assertions and confirm RED**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_dashboard.py -k 'does_not_project_futu_option_attention' \
  tests/test_dashboard_web.py -k 'does_not_render_legacy_futu_option_attention'
```

Expected: failures because the aggregate is still projected and rendered.

- [ ] **Step 3: Remove the aggregate projection and entry**

Delete:

```python
reports["futu"] = _project_futu_attention(reports["tiger"], reports["phillips"])
```

and `_project_futu_attention`. Make `renderTrendReportEntry("futu")` return an
empty string. Remove now-unreachable option-attention workspace renderers and
their CSS, while leaving the Futu holdings-detail anomaly UI untouched.

- [ ] **Step 4: Replace legacy tests with the retirement assertions**

Delete only tests dedicated to the aggregate `option-attention-table`. Keep
strict trend-report payload validation tests because frozen historical reports
still carry `option_attention`.

- [ ] **Step 5: Run focused Dashboard tests**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_dashboard.py -k 'trend_report or option_attention' \
  tests/test_dashboard_web.py -k 'trend_report or option_attention'
```

Expected: all selected tests pass; remaining `option_attention` tests cover
artifact compatibility, not the retired UI.

- [ ] **Step 6: Commit**

```bash
git add \
  src/open_trader/dashboard.py \
  src/open_trader/dashboard_static/dashboard.js \
  src/open_trader/dashboard_static/dashboard.css \
  tests/test_dashboard.py \
  tests/test_dashboard_web.py
git commit -m "refactor: retire synthetic option attention dashboard"
```

---

### Task 4: Remove option attention from Feishu text

**Files:**
- Modify: `src/open_trader/a_share_trend.py:3520-3565,3650-3680`
- Test: `tests/test_a_share_trend.py:3850-3930`
- Test: `tests/test_market_trend.py:1030-1075`

**Interfaces:**
- Preserves: `payload["option_attention"]` for frozen audit and replay compatibility.
- Removes: `_append_feishu_attention(...)` from `render_trend_feishu_text`.

- [ ] **Step 1: Change the Feishu test to the new required behavior**

For both US and HK payloads containing a non-empty `option_attention`, assert:

```python
_, message = render_trend_feishu_text(
    payload,
    broker_label="老虎" if market == "US" else "辉立",
    market_label=market,
)

assert "期权关注" not in message
assert "QQQ｜右侧" not in message
```

Keep the market-run test assertion that the JSON payload still contains the
frozen attention rows, but change its message assertion to:

```python
assert "\n期权关注\n" not in message
```

- [ ] **Step 2: Run the tests and confirm RED**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_a_share_trend.py -k 'feishu_text and option_attention' \
  tests/test_market_trend.py -k 'recovery and revision'
```

Expected: the US/HK Feishu test fails because the section is still appended.

- [ ] **Step 3: Remove only the presentation path**

Delete the `_append_feishu_attention(...)` call and remove its now-unused helper,
value formatter, and Feishu-only constants. Do not change `build_option_attention`,
report JSON, frozen evidence, replay, or API cost accounting.

- [ ] **Step 4: Run focused notification and market-flow tests**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_a_share_trend.py -k 'feishu_text' \
  tests/test_market_trend.py -k 'notification or recovery or revision'
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/a_share_trend.py tests/test_a_share_trend.py tests/test_market_trend.py
git commit -m "refactor: remove option attention from Feishu reports"
```

---

### Task 5: Update acceptance coverage, changelog, and live review deployment

**Files:**
- Modify: `src/open_trader/dashboard_acceptance.py:2660-2895`
- Modify: `tests/test_dashboard_acceptance.py:1190-1260,2540-3265,4760-5105`
- Modify: `tests/e2e/fixtures/kelly-dashboard.json`
- Modify: `tests/e2e/dashboard-warm-ledger.spec.ts:318-470`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Acceptance checks the visible Dashboard contract: no Futu aggregate entry, US/HK eligible rows have buttons, enabled buttons open a native dialog, disabled buttons remain disabled, and mobile geometry fits.

- [ ] **Step 1: Replace old aggregate acceptance with row-button acceptance**

In `_check_account_holdings`:

- for `broker == "futu"`, assert `.trend-report-entry` count is zero and continue;
- for Tiger/Phillips opened reports, assert every formal buy and hold row has one
  `.trend-option-button`;
- compare each button’s disabled state with `option_anomaly.available`;
- click the first enabled button when present and assert one visible
  `.trend-option-dialog`, source `富途`, expected symbol, and a working `关闭`;
- assert the trend table heading count is unchanged.

Delete aggregate-only fake-page state and tests for the ten-column
`.option-attention-table`.

- [ ] **Step 2: Update the Playwright fixture and warm-ledger paths**

Add one available `option_anomaly` object to a Tiger/Phillips buy or hold action
in `kelly-dashboard.json`. Replace the two flows that click the Futu
`期权关注` entry with:

```typescript
await page.getByRole('tab', { name: /老虎/ }).click();
await page.getByRole('button', { name: '当天趋势报告' }).click();
await page.getByRole('button', { name: '期权异动' }).first().click();
await expect(page.locator('.trend-option-dialog')).toBeVisible();
await expectMobileTargetsAtLeast44(
  page,
  '.trend-option-dialog',
  'button:visible',
);
await page.getByRole('button', { name: '关闭期权异动' }).click();
```

Also assert the Futu account contains no button named `期权关注`.

- [ ] **Step 3: Run acceptance-unit and browser E2E tests**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_dashboard_acceptance.py
npx playwright test tests/e2e/dashboard-warm-ledger.spec.ts
```

Expected: both commands pass.

- [ ] **Step 4: Run the affected workflow directly**

Generate or load the current US/HK trend-report projections against the main
workspace data context and inspect:

```bash
PYTHONPATH=src .venv/bin/python -c 'from pathlib import Path; from open_trader.dashboard import DashboardConfig, load_dashboard_state; c=DashboardConfig(Path("data/latest/portfolio.csv"),Path("data"),Path("reports"),5.0,"127.0.0.1",11111); s=load_dashboard_state(c).to_dict(); print(sorted(s["trend_reports"])); print([(b,[(r.get("symbol"),r.get("option_anomaly",{}).get("status")) for r in s["trend_reports"][b].get("buy_actions",[])+s["trend_reports"][b].get("hold_actions",[])]) for b in ("tiger","phillips")])'
```

Expected: Tiger/Phillips buy and hold actions contain truthful
`option_anomaly` states; `trend_reports` has no `futu` key.

- [ ] **Step 5: Add the operator-facing changelog entry**

Under `2026-07-29`, record:

```markdown
- Dashboard 美股/港股趋势报告在正式买入和继续持有标的下增加富途“期权异动”按钮；同日数据可查看只读详情，缺失或过期时置灰。移除旧跨市场“期权关注”入口，并从飞书趋势报告删除该段落。
```

- [ ] **Step 6: Run focused full-suite verification**

Run the worktree tests from the repository root so the ignored historical
snapshots are read from the same data context as the baseline:

```bash
cd /Users/ray/projects/open_trader && \
  PYTHONSAFEPATH=1 \
  PYTHONPATH=/Users/ray/projects/open_trader/.worktrees/trend-report-option-anomaly-button:/Users/ray/projects/open_trader/.worktrees/trend-report-option-anomaly-button/src \
  /Users/ray/projects/open_trader/.worktrees/trend-report-option-anomaly-button/.venv/bin/python \
  -m pytest /Users/ray/projects/open_trader/.worktrees/trend-report-option-anomaly-button/tests -q
```

Expected: no regression; the prior six missing-snapshot failures do not recur.

- [ ] **Step 7: Commit the review-ready source state**

```bash
git add \
  src/open_trader/dashboard_acceptance.py \
  tests/test_dashboard_acceptance.py \
  tests/e2e/fixtures/kelly-dashboard.json \
  tests/e2e/dashboard-warm-ledger.spec.ts \
  CHANGELOG.md
git commit -m "test: cover trend report option anomaly buttons"
```

- [ ] **Step 8: Run the final Dashboard gate once**

Run:

```bash
make acceptance
```

Expected: `PASS`. On `FAIL`, fix, commit the fix, then rerun. On `BLOCKED`,
report the blocker and do not substitute curl, fixtures, screenshots, or unit
tests. Record the accepted Git SHA.

- [ ] **Step 9: Redeploy the exact accepted SHA**

Restart the Dashboard using the repository’s existing deployment command. Verify:

```text
new PID
working directory = this worktree
Git SHA = accepted SHA
fresh startup log timestamp
HTTP 200 from http://127.0.0.1:8766/
```

Do not ask the user to review until all five checks pass. Provide
`http://127.0.0.1:8766/` as the review URL.
