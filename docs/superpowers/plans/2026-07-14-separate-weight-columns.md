# Separate Account and Portfolio Weight Columns Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show account weight and portfolio weight in separate holding-table columns and remove the unused per-row strategy column.

**Architecture:** Reuse the existing `display.account_weight` and `display.portfolio_weight` values; only change rendering, responsive CSS, and acceptance assertions. Keep strategy information in the existing account-level summary and reuse the existing action cell on mobile instead of duplicating buttons.

**Tech Stack:** Vanilla JavaScript, CSS, Python pytest, Playwright acceptance.

## Global Constraints

- Do not change APIs, account data, weight formulas, strategy artifacts, or dependencies.
- Keep all four account sections expanded and preserve trading-decision, 做T, detail, quote, filter, and deep-link behavior.
- Mobile remains a two-row card with no horizontal scrolling and 44px action targets.
- Run `make acceptance`; only `PASS` is complete.
- After `PASS`, redeploy the exact accepted Git SHA and verify PID, cwd, SHA, fresh logs, and HTTP 200.

---

### Task 1: Split Weight Columns and Remove Row Strategy

**Files:**
- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Modify: `src/open_trader/dashboard_static/dashboard.css`
- Modify: `src/open_trader/dashboard_acceptance.py`
- Test: `tests/test_dashboard_web.py`
- Test: `tests/test_dashboard_acceptance.py`

**Interfaces:**
- Consumes: `display.account_weight`, `display.portfolio_weight`, `renderAccountStrategy(group)`, and the existing `.account-holding-actions` buttons.
- Produces: table headers `账户权重` and `组合权重`; mobile grid areas `account-weight`, `portfolio-weight`, and `actions`.

- [ ] **Step 1: Write failing rendering and responsive tests**

Render an account table and assert the exact headers and removed column:

```python
assert "<th>账户权重</th>" in output
assert "<th>组合权重</th>" in output
assert "<th>策略</th>" not in output
assert "account-holding-account-weight" in output
assert "account-holding-portfolio-weight" in output
assert "account-holding-strategy" not in output
```

Update the mobile CSS test to require:

```python
assert '"symbol symbol market-value account-weight portfolio-weight pnl"' in mobile
assert '"market quantity price actions actions actions"' in mobile
for area in ("account-weight", "portfolio-weight", "actions"):
    assert f"grid-area: {area};" in mobile
```

Update acceptance helper fixtures so account-level strategy metrics remain required but row-level `目标` and `漂移` are not.

- [ ] **Step 2: Run focused tests and verify RED**

```bash
.venv/bin/python -m pytest -q tests/test_dashboard_web.py tests/test_dashboard_acceptance.py -k 'account_holdings_mobile_layout_css or account_sections_and_links or check_account_holdings'
```

Expected: FAIL because the table still combines weights, renders the strategy column, and the acceptance helper still requires row strategy text.

- [ ] **Step 3: Make the minimal renderer change**

In `renderAccountTable`, replace the combined weight and strategy cells with:

```javascript
<td class="number-cell account-holding-account-weight"><span class="account-mobile-label">账户权重</span>${escapeHtml(formatPlain(display.account_weight))}</td>
<td class="number-cell account-holding-portfolio-weight"><span class="account-mobile-label">组合权重</span>${escapeHtml(formatPlain(display.portfolio_weight))}</td>
<td class="number-cell account-holding-pnl"><span class="account-mobile-label">盈亏</span>${escapeHtml(formatPlain(display.unrealized_pnl_pct))}</td>
```

Use headers:

```javascript
"明细", "市场", "标的", "数量", "成本价", "实时价", "美元市值", "港元市值", "账户权重", "组合权重", "盈亏"
```

Delete `renderAccountStrategyCell`, `tigerMemberBySymbol`, their row-only label constants, and the duplicated `.account-mobile-actions` markup.

- [ ] **Step 4: Update mobile CSS and acceptance**

Use the existing action cell in the second mobile row:

```css
grid-template-areas:
  "symbol symbol market-value account-weight portfolio-weight pnl"
  "market quantity price actions actions actions";
grid-template-columns: repeat(6, minmax(0, 1fr));
```

Map the new cells and keep only cost/USD hidden. Remove dead strategy-cell and mobile-action rules. In `_check_account_holdings`, retain account-level strategy names, `策略指标待接入`, `夏普比率`, and `卡玛比率`, but remove row-only `目标` and `漂移` requirements.

- [ ] **Step 5: Verify and commit**

```bash
.venv/bin/python -m pytest -q tests/test_dashboard.py tests/test_dashboard_web.py tests/test_dashboard_acceptance.py
git diff --check
git add src/open_trader/dashboard_static/dashboard.js src/open_trader/dashboard_static/dashboard.css src/open_trader/dashboard_acceptance.py tests/test_dashboard_web.py tests/test_dashboard_acceptance.py
git commit -m "refactor: separate holding weight columns"
```

- [ ] **Step 6: Run the project gate and deploy**

Restart Dashboard from the committed SHA, run `make acceptance`, and require `PASS`. Restart the exact accepted SHA again and verify new PID, cwd, SHA, fresh log timestamp, HTTP 200, separate desktop headers, and no mobile horizontal overflow.

---
