# Broker Account Colors Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give each broker account header and strategy summary a distinct low-saturation background while keeping holding tables white.

**Architecture:** Use one CSS custom property per existing `#account-<broker>` section. Reuse that property for the header and strategy summary; no JavaScript, API, or component changes.

**Tech Stack:** CSS and Python pytest static/browser assertions.

## Global Constraints

- Futu is light blue, Tiger light orange, Phillips light green, and Eastmoney light red.
- Only account headers and strategy summaries are tinted; holding tables remain white.
- Broker names and strategy text remain visible, so color is not the only identifier.
- No gradients, animation, interaction changes, dependencies, JavaScript, or API changes.
- Run `make acceptance`; only `PASS` is complete, then redeploy the exact accepted SHA.

---

### Task 1: Add Broker Tint Variables

**Files:**
- Modify: `src/open_trader/dashboard_static/dashboard.css`
- Test: `tests/test_dashboard_web.py`

**Interfaces:**
- Consumes: existing section IDs `#account-futu`, `#account-tiger`, `#account-phillips`, `#account-eastmoney`.
- Produces: `--account-tint` used by `.account-section-header` and `.account-strategy-summary`.

- [ ] **Step 1: Write the failing CSS test**

```python
def test_dashboard_account_sections_use_distinct_broker_tints() -> None:
    css = (STATIC_DIR / "dashboard.css").read_text(encoding="utf-8")
    for selector, color in {
        "#account-futu": "#eff6ff",
        "#account-tiger": "#fff7ed",
        "#account-phillips": "#f0fdf4",
        "#account-eastmoney": "#fef2f2",
    }.items():
        assert f"{selector} {{ --account-tint: {color}; }}" in css
    assert "background: var(--account-tint, var(--surface-soft));" in css
    assert ".account-holdings-table {\n  background: var(--surface);" in css
```

- [ ] **Step 2: Verify RED**

```bash
.venv/bin/python -m pytest -q tests/test_dashboard_web.py -k account_sections_use_distinct_broker_tints
```

Expected: FAIL because the account tint variables do not exist.

- [ ] **Step 3: Add the minimum CSS**

```css
#account-futu { --account-tint: #eff6ff; }
#account-tiger { --account-tint: #fff7ed; }
#account-phillips { --account-tint: #f0fdf4; }
#account-eastmoney { --account-tint: #fef2f2; }

.account-section-header,
.account-strategy-summary {
  background: var(--account-tint, var(--surface-soft));
}

.account-holdings-table {
  background: var(--surface);
}
```

Remove the old standalone header background declaration so one rule owns the background.

- [ ] **Step 4: Verify and commit**

```bash
.venv/bin/python -m pytest -q tests/test_dashboard_web.py
git diff --check
git add src/open_trader/dashboard_static/dashboard.css tests/test_dashboard_web.py
git commit -m "style: color broker account headers"
```

- [ ] **Step 5: Run the live gate**

Start the Dashboard on a checked-free dedicated validation port from the committed SHA, run `make acceptance` against that same port, and require `PASS`. Stop the validation process afterward. Restart the exact accepted SHA from main on the review port and verify PID, cwd, SHA, fresh logs, HTTP 200, four distinct computed header colors, white table backgrounds, and no mobile horizontal overflow.

---
