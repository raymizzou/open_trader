# Dashboard Broker Source Times Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Dashboard's global account/quote freshness copy with two source groups whose four broker rows show each broker's own accepted data time.

**Architecture:** Keep the existing file-backed `account_sync.brokers` projection and quote polling unchanged. Format broker-specific timestamps only in the frontend source-list renderer, remove the now-unused global DOM/rendering paths, and make Dashboard acceptance validate the grouped rows directly.

**Tech Stack:** Existing HTML, CSS, vanilla JavaScript, Python 3.12, pytest, Playwright-backed Dashboard acceptance, launchd.

## Global Constraints

- Use the existing warm Dashboard palette, surfaces, semantic colors, radii, borders, typography, and spacing.
- `富途` and `老虎` belong to `实时账户`; `辉立` and `东方财富` belong to `券商结单`.
- Live healthy rows show `同步正常 · HH:MM`; statement healthy rows show `数据截至 · MM-DD`.
- Live rows may fall back from `data_as_of` to `last_success_at`; statement rows must never replace their data date with an import time.
- Failed, stale, unknown, and missing-time states remain explicit text and never rely on color alone.
- Remove the quote-status pill, global account-sync status, controller heartbeat, controller source row, and global refresh time.
- Do not change account-sync files, API schemas, controller behavior, quote polling, portfolio values, holdings, or trading actions.
- Do not add a dependency, design system, time component, backend field, interaction, or animation.
- Desktop and mobile use the same DOM; at 375px broker name and source status remain in the same row without horizontal page scrolling.
- Run focused tests while developing. Run `make acceptance` only after all source and changelog commits.
- Only `PASS` permits deployment or a completed claim. After `PASS`, restart the exact accepted SHA and capture desktop and mobile screenshots from that deployment.

---

### Task 1: Render grouped broker-specific source times

**Files:**
- Modify: `tests/test_dashboard_web.py:4781-4830`
- Modify: `src/open_trader/dashboard_static/dashboard.js:65-75`
- Modify: `src/open_trader/dashboard_static/dashboard.js:8368-8455`

**Interfaces:**
- Consumes: `state.dashboard.account_sync.brokers[broker]` with `status`, `display`, `data_as_of`, and `last_success_at`.
- Preserves: `brokerSyncStatus(broker) -> {status: string, display: string, unsafe: boolean}` for broker cards and account sections.
- Produces: `brokerSourceTime(broker: string, source: object) -> string`; `brokerSourceStatus(broker: string) -> {status: string, display: string, unsafe: boolean}`; grouped HTML from `renderSourceStatusList()`.

- [ ] **Step 1: Add a failing renderer test for grouping, normal times, abnormal states, and fallback**

Add this focused test next to
`test_dashboard_renders_file_backed_account_sync_health_and_accepted_positions`:

```python
def test_dashboard_groups_broker_sources_and_shows_each_source_time() -> None:
    output = run_dashboard_js(r'''
state.dashboard={account_sync:{brokers:{
  futu:{status:"ok",display:"同步正常",data_as_of:"2026-07-31T13:48:44+08:00"},
  tiger:{status:"ok",display:"同步正常",data_as_of:"2026-07-31T13:49:01+08:00"},
  phillips:{status:"ok",display:"同步正常",data_as_of:"2026-07-29"},
  eastmoney:{status:"ok",display:"同步正常",data_as_of:"2026-07-30"},
}}};
const normal=renderSourceStatusList();
state.dashboard.account_sync.brokers={
  futu:{status:"failed",data_as_of:"2026-07-31T12:10:00+08:00"},
  tiger:{status:"ok",data_as_of:"",last_success_at:"2026-07-31T13:47:00+08:00"},
  phillips:{status:"stale",data_as_of:"2026-07-29"},
  eastmoney:{status:"unknown",data_as_of:""},
};
console.log(JSON.stringify({normal,abnormal:renderSourceStatusList()}));
''')
    rendered = json.loads(output)
    normal = rendered["normal"]
    abnormal = rendered["abnormal"]

    assert normal.index("实时账户") < normal.index("富途账户")
    assert normal.index("老虎账户") < normal.index("券商结单")
    assert normal.index("券商结单") < normal.index("辉立账户")
    assert normal.index("辉立账户") < normal.index("东方财富账户")
    assert "同步正常 · 13:48" in normal
    assert "同步正常 · 13:49" in normal
    assert "数据截至 · 07-29" in normal
    assert "数据截至 · 07-30" in normal
    assert "控制器" not in normal
    assert "同步失败 · 上次 12:10" in abnormal
    assert "同步正常 · 13:47" in abnormal
    assert "数据已过期 · 截至 07-29" in abnormal
    assert "同步状态未知 · 数据未验证" in abnormal
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python \
  -m pytest -q \
  tests/test_dashboard_web.py::test_dashboard_groups_broker_sources_and_shows_each_source_time
```

Expected: FAIL because the existing renderer has no group labels, shows a
controller row, and healthy broker rows omit their own time.

- [ ] **Step 3: Add the fixed group mapping and minimal time formatter**

Add beside `ACCOUNT_BROKERS`:

```javascript
const ACCOUNT_SOURCE_GROUPS = [
  {label: "实时账户", brokers: ["futu", "tiger"]},
  {label: "券商结单", brokers: ["phillips", "eastmoney"]},
];
```

Keep `brokerSyncStatus()` unchanged and add:

```javascript
function brokerSourceTime(broker, source) {
  const live = ["futu", "tiger"].includes(broker);
  const raw = String(firstPresent(
    source?.data_as_of,
    live ? source?.last_success_at : "",
  ) || "");
  if (live) {
    const match = raw.match(/(?:T|\s|^)(\d{2}:\d{2})(?::\d{2})?/);
    return match ? match[1] : "";
  }
  const match = raw.match(/\b\d{4}-(\d{2}-\d{2})\b/);
  return match ? match[1] : "";
}

function brokerSourceStatus(broker) {
  const sync = brokerSyncStatus(broker);
  const source = state.dashboard?.account_sync?.brokers?.[broker] || {};
  const live = ["futu", "tiger"].includes(broker);
  const time = brokerSourceTime(broker, source);
  const suffix = time ? ` · ${time}` : "";
  const display = sync.status === "ok"
    ? (live ? `同步正常${suffix}` : (time ? `数据截至${suffix}` : "同步正常"))
    : sync.status === "failed"
      ? `同步失败${time ? ` · ${live ? "上次 " : "数据截至 "}${time}` : ""}`
      : sync.status === "stale"
        ? `数据已过期${time ? ` · 截至 ${time}` : ""}`
        : "同步状态未知 · 数据未验证";
  return {...sync, display};
}
```

Replace `renderSourceStatusList()` with:

```javascript
function renderSourceStatusList() {
  return ACCOUNT_SOURCE_GROUPS.map((group) => `
    <div class="source-status-group">${escapeHtml(group.label)}</div>
    ${group.brokers.map((broker) => {
      const sync = brokerSourceStatus(broker);
      return `
        <div class="source-status-row ${escapeHtml(sourceStatusClass(sync.status))}" data-broker="${escapeHtml(broker)}">
          <strong>${escapeHtml(brokerDisplayName(broker))}账户</strong>
          <span>${escapeHtml(sync.display)}</span>
        </div>
      `;
    }).join("")}
  `).join("");
}
```

- [ ] **Step 4: Run renderer tests and verify GREEN**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python \
  -m pytest -q \
  tests/test_dashboard_web.py::test_dashboard_groups_broker_sources_and_shows_each_source_time \
  tests/test_dashboard_web.py::test_dashboard_renders_file_backed_account_sync_health_and_accepted_positions
```

Update the second test only where it directly calls the removed controller
status renderer. Replace its `render()` return value:

```javascript
return {
  card: renderBrokerSummaryCards(),
  sources: renderSourceStatusList(),
  section: renderAccountSection(group),
};
```

Delete the three assertions that read `rendered[*]["status"]`, add:

```python
assert "控制器" not in rendered["ok"]["sources"]
```

Preserve all broker-card, holding safety, status-class, and `人工复核`
assertions.

Expected: both tests pass.

- [ ] **Step 5: Commit the broker-row behavior**

Run:

```bash
git diff --check
git add src/open_trader/dashboard_static/dashboard.js tests/test_dashboard_web.py
git commit -m "feat: show broker source timestamps"
```

---

### Task 2: Remove global freshness UI and update browser acceptance

**Files:**
- Modify: `src/open_trader/dashboard_static/index.html:76-85`
- Modify: `src/open_trader/dashboard_static/dashboard.css:160-175`
- Modify: `src/open_trader/dashboard_static/dashboard.css:294-350`
- Modify: `src/open_trader/dashboard_static/dashboard.css:5450-5488`
- Modify: `src/open_trader/dashboard_static/dashboard.js:157-175`
- Modify: `src/open_trader/dashboard_static/dashboard.js:945-980`
- Modify: `src/open_trader/dashboard_static/dashboard.js:8239-8278`
- Modify: `src/open_trader/dashboard_static/dashboard.js:8383-8397`
- Modify: `src/open_trader/dashboard_static/dashboard.js:8629-8647`
- Modify: `src/open_trader/dashboard_acceptance.py:3872-3950`
- Modify: `src/open_trader/dashboard_acceptance.py:4168-4190`
- Modify: `tests/test_dashboard_web.py:548-575`
- Modify: `tests/test_dashboard_web.py:4240-4270`
- Modify: `tests/test_dashboard_web.py:4480-4710`
- Modify: `tests/test_dashboard_acceptance.py:1171-1205`
- Modify: `tests/test_dashboard_acceptance.py:4280-4385`
- Modify: `tests/test_dashboard_acceptance.py:4740-4795`
- Modify: `tests/test_dashboard_acceptance.py:5260-5355`

**Interfaces:**
- Consumes: grouped HTML and `brokerSourceStatus()` from Task 1.
- Produces: a header source panel containing only `#source-status-list`; `_check_source_status_panel(page, payload) -> None`.
- Preserves: quote fetch/polling, `state.quotePayload`, holding-price rendering, connection diagnostics, page-error state, account-sync safety classes.

- [ ] **Step 1: Change the static and acceptance tests to express the approved UI**

Update `test_dashboard_warm_ledger_theme_and_broker_accents` and
`test_dashboard_static_assets_include_local_shell` so the mount contract is:

```python
assert 'id="source-status-list"' in html
for removed_id in ("quote-status", "account-sync-status", "last-refresh"):
    assert f'id="{removed_id}"' not in html
assert "renderAccountSyncStatus" not in js
assert "quoteRefreshText" not in js
assert "quoteStatusText" not in js
assert ".source-status-group" in css
```

Replace the header-time assertions in
`test_dashboard_renders_one_compact_us_session_price_and_header_time` with a
price-only contract and rename it:

```python
def test_dashboard_renders_one_compact_us_session_price() -> None:
    # Keep the overnight/pre-market/regular/after-hours and price-cell checks.
    assert "ok" in output
```

Add these responsive assertions to
`test_dashboard_command_center_css_keeps_accessible_responsive_states`:

```python
source_row_css = css.split(".source-status-row {", 1)[1].split("}", 1)[0]
assert "grid-template-columns: minmax(70px, max-content) minmax(0, 1fr);" in source_row_css
assert ".source-status-row {\n    grid-template-columns: 1fr;\n  }" not in mobile
assert ".source-status-row span {\n    text-align: left;\n  }" not in mobile
```

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python \
  -m pytest -q \
  tests/test_dashboard_web.py::test_dashboard_warm_ledger_theme_and_broker_accents \
  tests/test_dashboard_web.py::test_dashboard_static_assets_include_local_shell \
  tests/test_dashboard_web.py::test_dashboard_renders_one_compact_us_session_price \
  tests/test_dashboard_web.py::test_dashboard_command_center_css_keeps_accessible_responsive_states
```

Expected: FAIL because the three global elements and their render functions
still exist, the group label has no style, and mobile still stacks row columns.

- [ ] **Step 2: Remove the global DOM and dead rendering paths**

Reduce the source panel HTML to:

```html
<section class="header-source-panel" aria-label="账户数据来源">
  <div id="source-status-list" class="source-status-list" role="status"></div>
</section>
```

In `dashboard.js`:

- Remove `"quote-status"`, `"last-refresh"`, and `"account-sync-status"` from `bindElements()`.
- Remove `renderAccountSyncStatusIntoHeader()` from `renderDashboard()`.
- Delete `renderAccountSyncStatus()` and `renderAccountSyncStatusIntoHeader()`.
- Delete `quoteRefreshText()` and `quoteStatusText()`.
- Delete `renderQuoteStatus()`.
- After a successful quote fetch, rely on `loadDashboard()` and its normal render.
- In the quote-fetch exception path, call `renderConnectionPanel()` after assigning `state.quotePayload`.
- Remove the `elements["last-refresh"]` write from `renderLoadError()`; the existing `renderDashboardErrorState()` remains the visible failure path.

The resulting quote path is:

```javascript
async function refreshQuotes() {
  if (state.refreshActive) return;
  state.refreshActive = true;
  try {
    const response = await fetch("/api/quotes", {cache: "no-store"});
    if (!response.ok) throw new Error(`quotes ${response.status}`);
    const payload = await response.json();
    state.quotePayload = payload;
    state.quotes = payload.quotes || {};
    await loadDashboard({preserveOnError: true});
  } catch (error) {
    state.quotePayload = {
      status: "failed",
      stale: true,
      last_success_at: "",
      diagnostic: {message: error.message},
      quotes: state.quotes,
    };
    renderConnectionPanel();
  } finally {
    if (!["report", "review"].includes(state.accountViews[state.brokerFilter])) {
      renderHoldings();
    }
    state.refreshActive = false;
  }
}
```

- [ ] **Step 3: Reuse existing tokens for group labels and preserve two columns on mobile**

Delete `.last-refresh`, `.source-header-row`, and `.account-sync-status` rules.
Add:

```css
.source-status-group {
  color: var(--muted);
  font-size: 11px;
  font-weight: 700;
  margin: 2px 2px 0;
}
```

Keep the existing `.source-status-row` grid and right-aligned status text.
Delete only these mobile overrides:

```css
.source-status-row {
  grid-template-columns: 1fr;
}

.source-status-row span {
  text-align: left;
}
```

Do not add a breakpoint-specific duplicate component.

- [ ] **Step 4: Make acceptance validate grouped rows instead of deleted global copy**

Add beside `_check_session_prices()`:

```python
def _source_time_text(broker: str, source: Mapping[str, object]) -> str:
    raw = source.get("data_as_of")
    if broker in {"futu", "tiger"} and (
        not isinstance(raw, str) or not raw.strip()
    ):
        raw = source.get("last_success_at")
    raw = raw if isinstance(raw, str) else ""
    pattern = (
        r"(?:T|\s|^)(\d{2}:\d{2})(?::\d{2})?"
        if broker in {"futu", "tiger"}
        else r"\b\d{4}-(\d{2}-\d{2})\b"
    )
    match = re.search(pattern, raw)
    return match.group(1) if match else ""


def _expected_source_copy(broker: str, source: Mapping[str, object]) -> str:
    status = str(source.get("status") or "unknown").lower()
    live = broker in {"futu", "tiger"}
    time = _source_time_text(broker, source)
    if status == "ok":
        return (
            f"同步正常{f' · {time}' if time else ''}"
            if live
            else (f"数据截至 · {time}" if time else "同步正常")
        )
    if status == "failed":
        return (
            f"同步失败 · {'上次' if live else '数据截至'} {time}"
            if time
            else "同步失败"
        )
    if status == "stale":
        return f"数据已过期{f' · 截至 {time}' if time else ''}"
    return "同步状态未知 · 数据未验证"


def _check_source_status_panel(page: Any, payload: Mapping[str, object]) -> None:
    panel = page.locator("#source-status-list")
    assert panel.count() == 1, "缺少券商数据来源面板"
    panel_text = re.sub(r"\s+", " ", panel.inner_text()).strip()
    assert "实时账户" in panel_text and "券商结单" in panel_text, "券商来源未分组"
    for removed in ("控制器心跳", "控制器", "刷新于", "部分标的当前时段无报价"):
        assert removed not in panel_text, f"来源面板仍显示冗余信息：{removed}"
    account_sync = payload.get("account_sync")
    account_sync = account_sync if isinstance(account_sync, Mapping) else {}
    brokers = account_sync.get("brokers")
    brokers = brokers if isinstance(brokers, Mapping) else {}
    for broker in ACCOUNT_BROKERS:
        row = page.locator(f'#source-status-list [data-broker="{broker}"]')
        assert row.count() == 1, f"缺少 {broker} 券商来源行"
        source = brokers.get(broker)
        source = source if isinstance(source, Mapping) else {}
        assert _expected_source_copy(broker, source) in re.sub(
            r"\s+", " ", row.inner_text()
        ), f"{broker} 券商来源时间或状态不正确"
```

Call `_check_source_status_panel(page, payload)` immediately after
`_check_visual_contract(page)` in every browser viewport.

Remove the `#last-refresh` lookup and CST assertion from
`_check_session_prices()`. Remove `#last-refresh` from `_check_visual_contract`
style expectations, and replace its deleted `#account-sync-status` assertion
with:

```python
assert page.locator("#source-status-list").count() == 1, "缺少券商数据来源面板"
```

- [ ] **Step 5: Add acceptance unit coverage for the new contract**

Change `valid_payload()` broker sources to:

```python
"brokers": {
    "futu": {
        "status": "ok", "display": "同步正常",
        "data_as_of": "2026-07-31T13:48:44+08:00",
    },
    "tiger": {
        "status": "ok", "display": "同步正常",
        "data_as_of": "2026-07-31T13:49:01+08:00",
    },
    "phillips": {
        "status": "ok", "display": "同步正常",
        "data_as_of": "2026-07-29",
    },
    "eastmoney": {
        "status": "ok", "display": "同步正常",
        "data_as_of": "2026-07-30",
    },
},
```

Add a focused fake-page test:

```python
def test_acceptance_checks_grouped_broker_source_times() -> None:
    payload = valid_payload()
    text_by_selector = {
        "#source-status-list": (
            "实时账户 富途账户 同步正常 · 13:48 老虎账户 同步正常 · 13:49 "
            "券商结单 辉立账户 数据截至 · 07-29 "
            "东方财富账户 数据截至 · 07-30"
        ),
        '#source-status-list [data-broker="futu"]': "富途账户 同步正常 · 13:48",
        '#source-status-list [data-broker="tiger"]': "老虎账户 同步正常 · 13:49",
        '#source-status-list [data-broker="phillips"]': "辉立账户 数据截至 · 07-29",
        '#source-status-list [data-broker="eastmoney"]': "东方财富账户 数据截至 · 07-30",
    }

    class Locator:
        def __init__(self, text: str | None) -> None:
            self.text = text

        def count(self) -> int:
            return int(self.text is not None)

        def inner_text(self) -> str:
            assert self.text is not None
            return self.text

    class Page:
        def locator(self, selector: str) -> Locator:
            return Locator(text_by_selector.get(selector))

    dashboard_acceptance._check_source_status_panel(Page(), payload)
```

Update existing acceptance fakes so deleted selectors return zero and
`#source-status-list` returns one. Remove header parameters and assertions from
`session_price_page()` and its broken-contract parametrization.

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python \
  -m pytest -q \
  tests/test_dashboard_web.py \
  tests/test_dashboard_acceptance.py::test_acceptance_checks_grouped_broker_source_times \
  tests/test_dashboard_acceptance.py::test_acceptance_visual_contract_accepts_exact_warm_ledger \
  tests/test_dashboard_acceptance.py::test_check_session_prices_accepts_compact_session_price \
  tests/test_dashboard_acceptance.py::test_check_session_prices_requires_exactly_one_quote_per_us_price_cell \
  tests/test_dashboard_acceptance.py::test_check_session_prices_rejects_broken_contract \
  tests/test_dashboard_acceptance.py::test_acceptance_opens_real_tool_workspaces_and_checks_mobile_targets
```

Expected: all selected tests pass.

- [ ] **Step 6: Run the complete Dashboard-focused suite**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python \
  -m pytest -q \
  tests/test_dashboard.py \
  tests/test_dashboard_web.py \
  tests/test_dashboard_acceptance.py \
  tests/test_account_sync_state.py
```

Expected: all selected modules pass with no failures.

- [ ] **Step 7: Review and commit the complete UI change**

Confirm the diff contains no backend projection, controller, or data-file
change:

```bash
git diff --check
git diff --stat
git status --short
git add \
  src/open_trader/dashboard_static/index.html \
  src/open_trader/dashboard_static/dashboard.css \
  src/open_trader/dashboard_static/dashboard.js \
  src/open_trader/dashboard_acceptance.py \
  tests/test_dashboard_web.py \
  tests/test_dashboard_acceptance.py
git commit -m "feat: simplify broker source status panel"
```

---

### Task 3: Changelog, final gate, exact-SHA deployment, and screenshots

**Files:**
- Modify: `CHANGELOG.md`
- Runtime only: ignored config link, launchd plist/logs, desktop screenshot, mobile screenshot.

**Interfaces:**
- Consumes: committed UI and acceptance behavior from Tasks 1 and 2.
- Produces: an operator-facing changelog entry, final accepted Git SHA, exact-SHA review deployment, PID/cwd/SHA/log/HTTP proof, and desktop/mobile screenshot evidence.

- [ ] **Step 1: Add and commit the dated operator-facing changelog entry**

Under `## 2026-07-31`, add:

```markdown
- Simplified the Dashboard account-source panel by grouping live accounts and
  broker statements and showing each broker's own accepted data time. Removed
  the redundant global quote, heartbeat, controller, and refresh labels while
  preserving file-backed status, quote polling, and per-broker failure states.
```

Run:

```bash
git diff --check
git add CHANGELOG.md
git commit -m "docs: log broker source timestamps"
```

- [ ] **Step 2: Run the full suite against the worktree source and shared runtime data**

From `/Users/ray/projects/open_trader`, run:

```bash
PYTHONSAFEPATH=1 \
PYTHONPATH=/Users/ray/projects/open_trader/.worktrees/dashboard-broker-source-times:/Users/ray/projects/open_trader/.worktrees/dashboard-broker-source-times/src \
/Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
  /Users/ray/projects/open_trader/.worktrees/dashboard-broker-source-times/tests
```

Expected: all tests pass. The verified pre-change baseline was
`3900 passed in 92.23s`.

- [ ] **Step 3: Prepare the exact worktree runtime without tracked changes**

Ensure the worktree uses the existing Python environment and prediction config:

```bash
test -L .venv || ln -s /Users/ray/projects/open_trader/.venv .venv
test -e config/prediction_arbitrage.json || \
  ln -s /Users/ray/projects/open_trader/config/prediction_arbitrage.json \
  config/prediction_arbitrage.json
git status --short
```

Expected: ignored runtime links do not appear in `git status`; the tracked tree
is clean.

- [ ] **Step 4: Put the exact candidate SHA on port 8766**

Record the candidate SHA and stop only the known Dashboard launchd service:

```bash
candidate_sha="$(git rev-parse HEAD)"
printf 'candidate_sha=%s\n' "$candidate_sha"
/bin/launchctl bootout "gui/$UID/com.open-trader.dashboard" 2>/dev/null || true
for attempt in 1 2 3 4 5 6 7 8 9 10; do
  test -z "$(lsof -nP -tiTCP:8766 -sTCP:LISTEN 2>/dev/null)" && break
  sleep 1
done
test -z "$(lsof -nP -tiTCP:8766 -sTCP:LISTEN 2>/dev/null)"
scripts/install_dashboard_launchd.sh \
  --repo-root /Users/ray/projects/open_trader/.worktrees/dashboard-broker-source-times \
  --runtime-root /Users/ray/projects/open_trader \
  --python /Users/ray/projects/open_trader/.venv/bin/python
```

If port 8766 is owned by an unknown process, stop and report it; do not kill an
unresolved PID.

- [ ] **Step 5: Run the final Dashboard acceptance gate**

Run once, after the candidate service is ready:

```bash
make acceptance
```

Expected final line: `PASS`. On `FAIL`, continue diagnosing and fixing, commit
the fix and changelog adjustment if needed, then rerun the final gate. On
`BLOCKED`, report the blocker and do not substitute mocks, curl, or screenshots.

- [ ] **Step 6: Restart the exact accepted SHA and prove the review deployment**

Confirm no source or data change occurred after acceptance, then restart:

```bash
accepted_sha="$(git rev-parse HEAD)"
test -z "$(git status --porcelain)"
scripts/install_dashboard_launchd.sh \
  --repo-root /Users/ray/projects/open_trader/.worktrees/dashboard-broker-source-times \
  --runtime-root /Users/ray/projects/open_trader \
  --python /Users/ray/projects/open_trader/.venv/bin/python
pid="$(lsof -nP -tiTCP:8766 -sTCP:LISTEN)"
cwd="$(lsof -a -p "$pid" -d cwd -Fn | sed -n 's/^n//p')"
http_code="$(curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:8766/)"
printf 'accepted_sha=%s\npid=%s\ncwd=%s\nhttp=%s\n' \
  "$accepted_sha" "$pid" "$cwd" "$http_code"
tail -n 80 logs/dashboard/launchd.out.log
tail -n 80 logs/dashboard/launchd.err.log
```

Verify:

```text
accepted_sha equals the candidate SHA recorded immediately before acceptance
PID is new and alive
cwd == /Users/ray/projects/open_trader/.worktrees/dashboard-broker-source-times
dashboard_runtime Git SHA == accepted_sha
fresh startup log names the new PID and accepted working directory
no fresh error log entry
HTTP == 200
```

- [ ] **Step 7: Capture the affected live view at desktop and mobile widths**

Open `http://127.0.0.1:8766/` after the post-acceptance restart. Capture the
source panel from:

```text
Desktop: 1440px viewport, showing 实时账户, 券商结单, all four broker rows and times
Mobile: 375px viewport, showing the same rows without horizontal scrolling
```

Inspect both images before handoff. They must be readable, show the accepted
layout, and come from the exact deployed SHA. Include both screenshots inline
with the URL, accepted SHA, test count, acceptance result, PID, cwd, runtime
SHA, fresh-log result, and HTTP 200 proof.

- [ ] **Step 8: Leave merge and push for explicit user direction**

Report the accepted branch and review deployment without merging or pushing.
The changelog gate is already satisfied if the user later asks to merge:

```bash
git status --short
git log -5 --oneline
git rev-parse HEAD
```
