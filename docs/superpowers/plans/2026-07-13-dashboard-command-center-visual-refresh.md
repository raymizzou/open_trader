# Dashboard Command Center Visual Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Apply the approved light Command Center styling to the existing Dashboard without changing any displayed data, DOM contract, JavaScript behavior, or backend API.

**Architecture:** Keep `index.html` and `dashboard.js` unchanged. Add two focused regression tests to the existing static-asset test module, then restyle the current semantic regions directly in `dashboard.css`: first the desktop theme and hierarchy, then responsive and accessibility states. Restart the real Dashboard from the repository root and use `make acceptance` as the final gate.

**Tech Stack:** Python 3.12, pytest, static HTML/CSS/JavaScript, existing Dashboard acceptance runner, Playwright/Chrome through `make acceptance`.

## Global Constraints

- Preserve every current Header field, broker summary, source status, control, empty state, and interaction.
- Preserve the holdings table's existing ten columns in their current order.
- Do not modify `src/open_trader/dashboard_static/index.html`.
- Do not modify `src/open_trader/dashboard_static/dashboard.js`.
- Do not modify Dashboard APIs, backend code, models, notifications, watchers, or service behavior.
- Use system fonts only; add no dependency, web-font request, icon package, chart library, build tool, or design-system framework.
- Do not add a global portfolio conclusion, trade-action presentation, action badges, charts, sorting, filters, or dark mode.
- Keep normal text contrast at least 4.5:1, visible keyboard focus, 44 px mobile controls, reduced-motion support, and tabular numeric figures.
- Run `make acceptance` last. Only its JSON result with `"status": "PASS"` permits completion.

---

## File Structure

- Modify `tests/test_dashboard_web.py`: protect the approved visual tokens and prove the existing data/DOM contract remains unchanged.
- Modify `src/open_trader/dashboard_static/dashboard.css`: own all Command Center colors, spacing, hierarchy, responsive behavior, focus states, and reduced-motion behavior.
- Verify only `src/open_trader/dashboard_static/index.html`: its current element IDs, controls, and ten holdings headers remain unchanged.
- Verify only `src/open_trader/dashboard_static/dashboard.js`: all rendering and interaction behavior remains unchanged.

No files are created for runtime code.

---

### Task 1: Apply The Desktop Command Center Theme

**Files:**
- Modify: `tests/test_dashboard_web.py` after `test_dashboard_static_keeps_existing_columns_and_adds_cn()`
- Modify: `src/open_trader/dashboard_static/dashboard.css:1-340`
- Modify: `src/open_trader/dashboard_static/dashboard.css:1028-1218`

**Interfaces:**
- Consumes: the existing Header class names, holdings table markup, `holdings_table_header_labels(html)`, and CSS custom properties.
- Produces: the Command Center theme tokens and desktop hierarchy used by every existing Dashboard renderer.

- [ ] **Step 1: Write the failing desktop visual-contract test**

Add this test after `test_dashboard_static_keeps_existing_columns_and_adds_cn()`:

```python
def test_dashboard_command_center_theme_preserves_the_data_contract() -> None:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    css = (STATIC_DIR / "dashboard.css").read_text(encoding="utf-8")

    assert holdings_table_header_labels(html) == [
        "明细", "市场", "标的", "数量", "成本价", "实时价", "美元市值",
        "港元市值", "持仓占总资产的占比", "盈亏",
    ]
    for element_id in (
        "open-standard-backtest", "header-market-filters",
        "header-broker-filters", "current-view-value",
        "broker-summary-cards", "quote-status", "refresh-quotes",
        "source-status-list", "last-refresh", "kelly-lab-panel",
        "holdings-body", "cash-detail-panel", "symbol-detail-panel",
        "standard-backtest-workspace", "research-chat-layer",
    ):
        assert f'id="{element_id}"' in html
    assert "今日结论" not in html
    assert 'id="trade-actions"' not in html
    assert "--bg: #f5f7fa;" in css
    assert "--text: #101828;" in css
    assert "--accent: #2563eb;" in css
    assert "--primary: #101828;" in css
    assert "font-variant-numeric: tabular-nums;" in css
```

- [ ] **Step 2: Run the test and confirm it fails only on new theme tokens**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py::test_dashboard_command_center_theme_preserves_the_data_contract -v
```

Expected: `FAILED`; all HTML/data-contract assertions pass, and the first failure is `assert "--bg: #f5f7fa;" in css`.

- [ ] **Step 3: Replace the root palette with the approved system-font theme**

Replace the existing `:root` block at the top of `dashboard.css` with:

```css
:root {
  color-scheme: light;
  --bg: #f5f7fa;
  --surface: #ffffff;
  --surface-soft: #f8fafc;
  --text: #101828;
  --muted: #667085;
  --line: #e4e7ec;
  --accent: #2563eb;
  --accent-strong: #1d4ed8;
  --primary: #101828;
  --on-primary: #ffffff;
  --warning: #b54708;
  --danger: #b42318;
  --ok: #027a48;
  --shadow: 0 12px 32px rgba(16, 24, 40, 0.07);
}
```

Keep the existing system font stack in `body`; do not add `@import`, `@font-face`, or external assets.

- [ ] **Step 4: Restyle the shell and Header without changing its DOM**

Update the existing selectors to these values, preserving declarations not shown only when they do not conflict:

```css
.dashboard-shell {
  margin: 0 auto;
  max-width: 1600px;
  min-height: 100vh;
  padding: 16px;
}

.dashboard-header {
  display: grid;
  gap: 10px;
  grid-template-areas: "brand assets source";
  grid-template-columns: minmax(280px, 1fr) minmax(480px, 1.5fr) minmax(300px, 1.05fr);
  margin-bottom: 10px;
}

.header-brand-panel,
.header-assets-panel,
.header-source-panel {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 12px;
  box-shadow: var(--shadow);
  min-width: 0;
  padding: 14px;
}

.brand {
  font-size: 20px;
  font-weight: 800;
  letter-spacing: -0.03em;
}

.current-view-card {
  background: var(--primary);
  border: 1px solid var(--primary);
  border-radius: 10px;
  color: var(--on-primary);
  display: grid;
  gap: 7px;
  min-width: 0;
  padding: 12px;
}

.current-view-card .summary-label,
.current-view-card .summary-note,
.current-view-card .current-view-breakdown span {
  color: #98a2b3;
}

.current-view-card strong {
  font-size: 25px;
  font-variant-numeric: tabular-nums;
  letter-spacing: -0.04em;
  line-height: 1.15;
  overflow-wrap: anywhere;
}

.current-view-breakdown strong {
  color: #84adff;
  flex: 0 0 auto;
  font-size: 13px;
}

.broker-summary-cards {
  display: grid;
  gap: 6px;
  grid-template-columns: repeat(2, minmax(0, 1fr));
}

.broker-summary-card,
.broker-summary-empty {
  background: var(--surface);
  border: 1px solid var(--line);
  border-left: 3px solid transparent;
  border-radius: 8px;
  display: grid;
  gap: 5px;
  min-width: 0;
  padding: 8px;
}

.broker-summary-card strong,
.broker-summary-empty strong,
.number-cell {
  font-variant-numeric: tabular-nums;
}
```

Change the existing selected broker colors to the blue theme:

```css
.broker-summary-card.active,
.broker-summary-card[aria-current="true"],
.broker-summary-card[aria-selected="true"],
body:has(#header-broker-filters [data-broker="futu"].active) .broker-summary-card[data-broker="futu"],
body:has(#header-broker-filters [data-broker="tiger"].active) .broker-summary-card[data-broker="tiger"],
body:has(#header-broker-filters [data-broker="phillips"].active) .broker-summary-card[data-broker="phillips"] {
  background: #eff4ff;
  border-color: #b2ccff;
  border-left-color: var(--accent);
}
```

- [ ] **Step 5: Restyle existing buttons, Kelly entry, holdings panel, and table**

Update the existing visual rules; do not add or remove elements:

```css
.primary-button,
.secondary-button,
.filter-button,
.expand-button {
  border: 1px solid var(--line);
  border-radius: 7px;
  cursor: pointer;
  min-height: 38px;
}

.filter-button {
  background: var(--surface);
  color: #475467;
  padding: 0 10px;
  text-align: left;
}

.filter-button.active {
  background: var(--primary);
  border-color: var(--primary);
  color: var(--on-primary);
  font-weight: 700;
}

.kelly-lab-panel,
.holdings-panel {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 12px;
  box-shadow: var(--shadow);
}

.kelly-lab-panel {
  padding: 12px 14px;
}

.section-heading {
  align-items: center;
  display: flex;
  gap: 12px;
  justify-content: space-between;
  padding: 14px 16px;
}

.table-wrap {
  border-top: 1px solid var(--line);
  overflow-x: auto;
}

th,
td {
  border-bottom: 1px solid #f0f2f5;
  padding: 11px 12px;
  text-align: left;
  vertical-align: top;
}

th {
  background: #f9fafb;
  color: var(--muted);
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.02em;
  position: sticky;
  top: 0;
}

tbody tr:hover {
  background: #f8fafc;
}

tbody tr.active-row {
  background: #eff4ff;
}
```

Keep all existing `nth-child` widths and the `min-width: 1120px` holdings-table rule unchanged so no column disappears.

- [ ] **Step 6: Run the focused test and the existing static-shell test**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_dashboard_web.py::test_dashboard_command_center_theme_preserves_the_data_contract \
  tests/test_dashboard_web.py::test_dashboard_static_assets_include_local_shell -v
```

Expected: `2 passed`.

- [ ] **Step 7: Commit the desktop theme**

```bash
git add tests/test_dashboard_web.py src/open_trader/dashboard_static/dashboard.css
git commit -m "style: refresh dashboard command center"
```

Expected: one commit containing only the CSS file and its focused regression test.

---

### Task 2: Add Responsive And Accessibility Polish

**Files:**
- Modify: `tests/test_dashboard_web.py` after the Task 1 visual-contract test
- Modify: `src/open_trader/dashboard_static/dashboard.css` near interactive-state rules and existing media queries

**Interfaces:**
- Consumes: Task 1's `--accent`, `--primary`, panel styles, and unchanged Dashboard DOM.
- Produces: visible focus, bounded transitions, reduced-motion behavior, and mobile 44 px controls without changing interaction logic.

- [ ] **Step 1: Write the failing responsive/accessibility CSS test**

Add:

```python
def test_dashboard_command_center_css_keeps_accessible_responsive_states() -> None:
    css = (STATIC_DIR / "dashboard.css").read_text(encoding="utf-8")

    assert "button:focus-visible" in css
    assert "outline: 3px solid rgba(37, 99, 235, 0.32);" in css
    assert "@media (prefers-reduced-motion: reduce)" in css
    assert "transition-duration: 0.01ms !important;" in css
    mobile = css.split("@media (max-width: 760px) {", 1)[1]
    assert "min-height: 44px;" in mobile
    assert 'grid-template-areas: "brand" "assets" "source";' in mobile
```

- [ ] **Step 2: Run the test and confirm it fails on missing focus styles**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py::test_dashboard_command_center_css_keeps_accessible_responsive_states -v
```

Expected: `FAILED` at `assert "button:focus-visible" in css`.

- [ ] **Step 3: Add bounded interactive transitions and visible focus**

Place this block after the shared button typography rules near the top of `dashboard.css`:

```css
button,
[role="tab"],
.broker-summary-card {
  transition: background-color 180ms ease, border-color 180ms ease,
    box-shadow 180ms ease, color 180ms ease;
}

button:focus-visible,
[role="tab"]:focus-visible,
input:focus-visible,
select:focus-visible {
  outline: 3px solid rgba(37, 99, 235, 0.32);
  outline-offset: 2px;
}

@media (prefers-reduced-motion: reduce) {
  *,
  *::before,
  *::after {
    scroll-behavior: auto !important;
    transition-duration: 0.01ms !important;
  }
}
```

Do not animate position, dimensions, or table layout.

- [ ] **Step 4: Preserve the existing responsive structure and enlarge mobile controls**

Keep the existing `@media (max-width: 1180px)` two-row Header layout. Inside the existing `@media (max-width: 760px)` block, preserve the existing one-column Header and add:

```css
  .primary-button,
  .secondary-button,
  .filter-button,
  .expand-button,
  .raw-toggle {
    min-height: 44px;
  }

  .dashboard-shell {
    max-width: 100%;
    padding: 10px;
  }

  .header-brand-panel,
  .header-assets-panel,
  .header-source-panel {
    border-radius: 10px;
    padding: 10px;
  }

  .broker-summary-cards {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
```

Retain these existing rules unchanged in the same media block:

```css
  .dashboard-header {
    gap: 10px;
    grid-template-areas: "brand" "assets" "source";
  }

  .holdings-panel > .table-wrap > table {
    min-width: 1120px;
  }
```

If the second rule is currently outside the media block, leave it there; do not duplicate it. The table wrapper, not the page, owns horizontal scrolling.

- [ ] **Step 5: Run the focused accessibility test and all Dashboard web tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py::test_dashboard_command_center_css_keeps_accessible_responsive_states -v
.venv/bin/python -m pytest tests/test_dashboard_web.py -q
```

Expected: focused test `1 passed`; full Dashboard web module exits `0` with no failures.

- [ ] **Step 6: Confirm the implementation boundary**

Run:

```bash
git diff --name-only HEAD~1
git diff --check
```

Expected: only `src/open_trader/dashboard_static/dashboard.css` and `tests/test_dashboard_web.py`; `git diff --check` has no output.

- [ ] **Step 7: Commit responsive and accessibility styling**

```bash
git add tests/test_dashboard_web.py src/open_trader/dashboard_static/dashboard.css
git commit -m "style: polish dashboard responsive states"
```

Expected: commit succeeds with no HTML, JavaScript, API, or backend files staged.

---

### Task 3: Restart The Real Dashboard And Pass Acceptance

**Files:**
- Verify: `src/open_trader/dashboard_static/dashboard.css`
- Verify: `tests/test_dashboard_web.py`
- Verify: `/tmp/open_trader_dashboard_8766.log`
- Verify: the `open_trader_dashboard_8766` screen session and listener on port `8766`

**Interfaces:**
- Consumes: the committed CSS refresh and the existing Dashboard launch command.
- Produces: a fresh Dashboard process running this repository's current Git SHA and a final `make acceptance` result.

- [ ] **Step 1: Run the focused test suite before touching the live process**

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py -q
```

Expected: exit `0` and no failures.

- [ ] **Step 2: Inspect the currently running Dashboard before replacing it**

```bash
screen -ls
ps aux | rg '[o]pen_trader dashboard'
lsof -nP -iTCP:8766 -sTCP:LISTEN
```

Expected: output identifies the old screen session, process PID, working command, and the unique listener on `127.0.0.1:8766`.

- [ ] **Step 3: Stop the old process and start the current committed code**

```bash
screen -S open_trader_dashboard_8766 -X quit 2>/dev/null || true
rm -f /tmp/open_trader_dashboard_8766.log
screen -dmS open_trader_dashboard_8766 zsh -lc \
  'cd /Users/ray/projects/open_trader/.worktrees/dashboard-command-center-refresh && exec env PYTHONPATH=src .venv/bin/python -m open_trader dashboard --portfolio /Users/ray/projects/open_trader/data/latest/portfolio.csv --data-dir /Users/ray/projects/open_trader/data --reports-dir /Users/ray/projects/open_trader/reports --poll-seconds 5 --host 127.0.0.1 --port 8766 >> /tmp/open_trader_dashboard_8766.log 2>&1'
```

Expected: the old listener exits and a new detached screen session starts from `/Users/ray/projects/open_trader/.worktrees/dashboard-command-center-refresh`.

- [ ] **Step 4: Verify the fresh PID, working directory, SHA, API, and log**

```bash
PID=$(lsof -tiTCP:8766 -sTCP:LISTEN)
ps -p "$PID" -o pid,lstart,command
lsof -a -p "$PID" -d cwd -Fn
git rev-parse HEAD
curl -sS http://127.0.0.1:8766/api/dashboard | .venv/bin/python -m json.tool | sed -n '1,40p'
sed -n '1,120p' /tmp/open_trader_dashboard_8766.log
```

Expected: one fresh PID; cwd is `/Users/ray/projects/open_trader/.worktrees/dashboard-command-center-refresh`; the API returns JSON; the new log contains no traceback or `看板数据加载失败`.

- [ ] **Step 5: Run the mandatory final gate**

Run this as the final verification command:

```bash
make acceptance
```

Expected: all tests pass, two real refresh cycles complete, desktop/mobile browser flows pass, the running SHA matches the repository, and the final JSON contains `"status": "PASS"`, `"errors": []`, and `"blocker": null`.

If the result is `FAIL`, diagnose, fix, recommit, restart the process, and rerun `make acceptance`. If the result is `BLOCKED`, report the blocker and do not present the Dashboard for review.

---

## Completion Criteria

- Only CSS and focused CSS-contract tests changed after the approved design/spec commits.
- Current Header data, four broker summaries, source rows, controls, ten holdings columns, Kelly Lab, details, research chat, and backtest behavior remain present.
- The live process runs the current repository SHA from `/Users/ray/projects/open_trader/.worktrees/dashboard-command-center-refresh`.
- `make acceptance` returns `PASS` as the final command.
