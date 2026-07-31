# Dashboard Live Price Acceptance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Dashboard acceptance validate successfully fetched live prices against the page's current state instead of a stale pre-navigation API snapshot.

**Architecture:** Keep the production quote and rendering paths unchanged. Move the controller-owned DOM comparison baseline into `_check_controller_owned_rows`, where Playwright can read the exact `state.dashboard.broker_positions` snapshot that produced the visible rows.

**Tech Stack:** Python 3.12, pytest, Playwright sync API, existing Open Trader acceptance helpers.

## Global Constraints

- `/api/quotes` must still pass its existing success, schema, and valid-price checks.
- DOM fields must match the page's current `state.dashboard.broker_positions`.
- Do not freeze, stub, or modify production quotes.
- Do not modify account sync, holdings, market data, or trading logic.
- Use no new dependency or abstraction.

---

### Task 1: Compare controller-owned DOM fields with page state

**Files:**
- Modify: `tests/test_dashboard_acceptance.py:4895`
- Modify: `src/open_trader/dashboard_acceptance.py:2938`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: Playwright-compatible `page.evaluate(expression)` and the existing `state.dashboard.broker_positions` array.
- Produces: `_check_controller_owned_rows(page: Any, section: Any, broker: str) -> None`.

- [ ] **Step 1: Write the failing regression test**

Update `test_check_controller_owned_rows_matches_dom_projection` so the fake page returns a current DRAM row priced at `53.40`, while the conceptual pre-navigation snapshot is `53.38`. Build the fake DOM from the current page row and call the new interface:

```python
def test_check_controller_owned_rows_uses_current_page_projection() -> None:
    stale_position = _controller_position("DRAM")
    stale_position["last_price"] = "53.38"
    page_position = dict(stale_position)
    page_position["last_price"] = "53.40"
    dom_values = dict(page_position)

    class Page:
        def evaluate(self, expression: str) -> list[dict[str, str]]:
            assert expression == (
                "() => state.dashboard?.broker_positions ?? []"
            )
            return [page_position]

    class Row:
        def get_attribute(self, name: str) -> str:
            return {
                "data-broker": "tiger",
                "data-symbol": "DRAM",
                **{
                    attribute: dom_values[field]
                    for field, attribute
                    in dashboard_acceptance.CONTROLLER_DOM_FIELDS.items()
                },
            }[name]

    class Rows:
        def count(self) -> int:
            return 1

        def nth(self, _index: int) -> Row:
            return Row()

    class Section:
        def locator(self, selector: str) -> Rows:
            assert selector == ".account-holding-row:visible"
            return Rows()

    dashboard_acceptance._check_controller_owned_rows(
        Page(), Section(), "tiger"
    )

    dom_values["last_price"] = stale_position["last_price"]
    with pytest.raises(AssertionError, match="last_price"):
        dashboard_acceptance._check_controller_owned_rows(
            Page(), Section(), "tiger"
        )
```

- [ ] **Step 2: Run the regression test and verify RED**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_dashboard_acceptance.py::test_check_controller_owned_rows_uses_current_page_projection -q
```

Expected: FAIL because `_check_controller_owned_rows` still treats its first argument as `section` and does not read page state.

- [ ] **Step 3: Implement the minimal page-state baseline**

Change the helper to read and validate the current positions:

```python
def _check_controller_owned_rows(page: Any, section: Any, broker: str) -> None:
    positions = page.evaluate(
        "() => state.dashboard?.broker_positions ?? []"
    )
    assert isinstance(positions, list), "页面持仓状态无效"
    # Keep the existing row filtering and per-field DOM assertions unchanged.
```

Update `_check_account_holdings` to call:

```python
_check_controller_owned_rows(page, section, broker)
```

- [ ] **Step 4: Verify GREEN and the focused module**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_dashboard_acceptance.py::test_check_controller_owned_rows_uses_current_page_projection -q
PYTHONSAFEPATH=1 PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_dashboard_acceptance.py -q
git diff --check
```

Expected: regression test PASS, `282 passed` or more for the module, and no diff errors.

- [ ] **Step 5: Add the dated operator changelog entry**

Add this bullet under `2026-07-31`:

```markdown
- Made Dashboard browser acceptance compare volatile controller-owned prices
  with the page's current state instead of a pre-navigation snapshot. Live
  quote fetch and valid-price checks remain strict, so normal price movement no
  longer causes a false DOM mismatch.
```

- [ ] **Step 6: Commit the behavior change**

```bash
git add src/open_trader/dashboard_acceptance.py tests/test_dashboard_acceptance.py CHANGELOG.md
git commit -m "fix: accept live dashboard price movement"
```

- [ ] **Step 7: Run final acceptance and deployment gates**

Run `make acceptance` only after the implementation commit. On `PASS`, merge the branch into `main`, redeploy the exact accepted merge SHA, and verify the new Dashboard PID, working directory, Git SHA, fresh logs, HTTP 200, and a live screenshot. On `FAIL` or `BLOCKED`, do not claim completion or deployment acceptance.
