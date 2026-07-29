# Trend Industry Minimum and Buy Display Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the local 10-row industry minimum, restore contextual ordering for complete small groups, and show frozen industry temperature in every market's buy plan.

**Architecture:** Keep the existing industry calculation, report-wide fallback, and deterministic sort. Delete only the two minimum-count invalidation branches, then reuse the Dashboard's existing report-to-industry lookup for the buy-row temperature fallback.

**Tech Stack:** Python 3.12, pytest, browser-side JavaScript executed by the existing Node test harness.

## Global Constraints

- Start from local `main` in an isolated worktree.
- Do not add a score, threshold, dependency, ETF remapping, entry gate, risk rule, or position-sizing rule.
- Keep exact-date, 90% snapshot coverage, 90% right-state coverage, known-temperature, and finite-strength validation.
- Preserve direct buy-action `industry_temperature` when present.
- Run `make acceptance` only after all source, test, documentation, and changelog changes are final.

---

### Task 1: Remove the Local Minimum-Count Validation

**Files:**
- Modify: `tests/test_trend_industry_context.py`
- Modify: `src/open_trader/trend_industry_context.py`
- Modify: `docs/superpowers/specs/2026-07-24-trend-industry-breadth-discipline-dashboard-design.md`

**Interfaces:**
- Consumes: `calculate_industry_context(...) -> IndustryContext`
- Produces: complete small contexts with `valid=True`; all other validation behavior remains unchanged.

- [ ] **Step 1: Write the failing calculation test**

Add a test using two exact-date, tradable members with boolean right-side states and a valid industry state:

```python
def test_complete_small_industry_context_is_valid() -> None:
    context = calculate_industry_context(
        industry_tm_id=700001,
        industry="美国医疗ETF",
        expected_date="2026-07-24",
        component_tm_ids=[1, 2],
        member_rows=[_member(1), _member(2)],
        industry_row=_industry(),
        warm_to_hot_count=2,
    )

    assert context.component_count == 2
    assert context.valid_count == 2
    assert context.right_share == Decimal("1")
    assert context.valid
    assert context.invalid_reasons == ()
```

Update `test_calculation_records_stable_reasons_for_invalid_context_inputs` so its expected reasons retain coverage, temperature, and strength failures but no longer include `component_count_below_10` or `valid_count_below_10`.

- [ ] **Step 2: Run the test to verify RED**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_trend_industry_context.py::test_complete_small_industry_context_is_valid \
  tests/test_trend_industry_context.py::test_calculation_records_stable_reasons_for_invalid_context_inputs -q
```

Expected: the small-context test fails because the current validator records the two minimum-count reasons.

- [ ] **Step 3: Delete the two invalidation branches**

Remove only:

```python
if component_count < 10:
    invalid_reasons.append("component_count_below_10")
if valid_count < 10:
    invalid_reasons.append("valid_count_below_10")
```

Remove the two minimum-count bullets from the earlier approved design and add a dated amendment pointing to the replacement design.

- [ ] **Step 4: Run the focused calculation suite to verify GREEN**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_trend_industry_context.py -q
```

Expected: all tests pass.

### Task 2: Prove Small Context Restores Existing Ordering

**Files:**
- Modify: `tests/test_a_share_trend.py`

**Interfaces:**
- Consumes: `build_candidate_list(...) -> CandidateDecision`
- Produces: regression evidence that a two-member complete context does not select `legacy_invalid_current`.

- [ ] **Step 1: Write the ordering regression**

Construct a two-member valid `IndustryContext` and a larger valid context. Give the candidate in the small context the higher individual strength but the weaker industry, then assert the stronger-industry candidate ranks first:

```python
def test_complete_small_context_keeps_industry_ordering_enabled() -> None:
    small = replace(
        _industry_context(1, temperature="平", strength="30"),
        component_count=2,
        snapshot_count=2,
        tradable_count=2,
        valid_count=2,
        right_count=2,
    )
    decisions = build_candidate_list(
        [
            candidate("600001", strength="99", industry_tm_id=1),
            candidate("600002", strength="96", industry_tm_id=2),
        ],
        held_symbols=set(),
        industry_contexts={
            1: small,
            2: _industry_context(2, temperature="热", strength="100"),
        },
    )

    assert decisions.ordering_mode == "context_with_history"
    assert [item.symbol for item in decisions.eligible] == ["600002", "600001"]
```

- [ ] **Step 2: Run the ordering test**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_a_share_trend.py::test_complete_small_context_keeps_industry_ordering_enabled -q
```

Expected: PASS after Task 1; this locks the existing sorting contract without production changes.

### Task 3: Render Frozen Context Temperature in Buy Rows

**Files:**
- Modify: `tests/test_dashboard_web.py`
- Modify: `src/open_trader/dashboard_static/dashboard.js`

**Interfaces:**
- Consumes: a buy action and `report.industry_contexts`
- Produces: `trendIndustryContext(report, item)` and a truthful buy-row temperature value.

- [ ] **Step 1: Write the failing cross-market rendering test**

Render CN, US, and HK reports whose buy action has an `industry_tm_id` but no direct `industry_temperature`, and whose frozen context has `temperature: "热"`:

```javascript
for (const market of ["CN", "US", "HK"]) {
  const html = renderTrendBuyStage({
    market,
    buy_window:"常规交易时段",
    buy_actions:[{symbol:"BUY",industry:"金融",industry_tm_id:7}],
    risk_skips:[],
    industry_contexts:[{
      industry_tm_id:7,industry:"金融",temperature:"热",valid:true,
      right_count:10,valid_count:20,right_share:"0.5",
    }],
  });
  if (!html.includes('data-label="行业温度">热</td>')) throw new Error(market + html);
}
```

- [ ] **Step 2: Run the rendering test to verify RED**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_dashboard_web.py::test_dashboard_buy_rows_use_frozen_industry_temperature_for_every_market -q
```

Expected: FAIL because the row currently renders `数据未提供`.

- [ ] **Step 3: Reuse one context lookup**

Extract the existing lookup from `trendIndustryBuyContext`:

```javascript
function trendIndustryContext(report, item) {
  const direct = item && item.industry_context && typeof item.industry_context === "object"
    ? item.industry_context : null;
  const contexts = Array.isArray(report?.industry_contexts) ? report.industry_contexts : [];
  const itemIndustryId = item?.industry_tm_id ?? item?.industry_id;
  return direct || contexts.find((candidate) => candidate && typeof candidate === "object"
    && ((hasValue(itemIndustryId) && String(candidate.industry_tm_id) === String(itemIndustryId))
      || (hasValue(item?.industry) && String(candidate.industry) === String(item.industry))));
}
```

Use it in `trendIndustryBuyContext`. For the temperature cell, keep a present direct action value; otherwise use the matching context temperature only when `context.valid !== false`.

- [ ] **Step 4: Run the rendering test to verify GREEN**

Run the exact command from Step 2. Expected: PASS.

### Task 4: Finalize, Verify, and Deploy

**Files:**
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: final source and tests
- Produces: an accepted, deployed review SHA with no submitted trades.

- [ ] **Step 1: Update the dated operator changelog**

Record the removed minimum-count rule, restored contextual ordering, and cross-market buy-row temperature display.

- [ ] **Step 2: Run focused suites and formatting checks**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_trend_industry_context.py tests/test_a_share_trend.py \
  tests/test_dashboard.py tests/test_dashboard_web.py \
  tests/test_dashboard_acceptance.py -q
git diff --check
```

Re-run the repository-fixture test from `/Users/ray/projects/open_trader` with the worktree `src` in `PYTHONPATH`.

- [ ] **Step 3: Regenerate three-market reports without submission**

Use the existing revision/no-submit workflow for CN, HK, and US. Compare old and new ordering, verify the matching context temperature for every buy row, and confirm no order or ledger submission occurred.

- [ ] **Step 4: Commit the implementation**

Commit source, tests, amended documentation, and changelog together with the root cause in the message.

- [ ] **Step 5: Run the final acceptance gate**

Run `make acceptance` once from the final committed SHA. Continue fixing on `FAIL`; report `BLOCKED` without substituting other checks; proceed only on `PASS`.

- [ ] **Step 6: Redeploy the accepted SHA**

Restart the Dashboard on the exact accepted SHA, then verify PID, working directory, Git SHA, fresh logs, and HTTP 200 at `http://127.0.0.1:8766`.
