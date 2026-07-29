# Cross-Market Holding Order and Industry Display Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make CN, HK, and US `盘中持续 · 已有持仓` rows show industry and follow the approved industry-first discipline order without rewriting frozen report facts.

**Architecture:** Keep `_project_trend_actions` as the Dashboard boundary. Enrich projected holding rows from frozen holding snapshots, map them to the report's frozen industry contexts, and sort only the projected `HOLD` list. Keep the market-neutral JavaScript renderer responsible only for displaying the projected industry field.

**Tech Stack:** Python 3.12, pytest, Node VM dashboard renderer, existing report/controller CLI, Playwright-backed `make acceptance`.

## Global Constraints

- Work only in the isolated `fix/cross-market-holding-order` worktree based on local `main`.
- Do not mutate the source report payload during projection.
- Do not rewrite existing frozen report JSON or change formal actions, strategy versions, risk rules, sizing, or ledgers.
- Use the existing discipline order: complete history change, industry temperature, industry strength, warm-to-hot count, right-side share, then individual strength, days, amount when present, and symbol.
- If a held row lacks a valid matching context, sort the complete holding list by individual fields; missing optional fields remain missing.
- Regenerate CN/HK/US with the existing revision/no-submit workflow and verify no order submission.
- Run `make acceptance` only after source, tests, changelog, and report verification are final.

---

### Task 1: Lock projection enrichment and holding order

**Files:**
- Modify: `tests/test_dashboard.py` near `test_dashboard_holding_phase_projection_uses_frozen_snapshot`
- Modify: `src/open_trader/dashboard.py` near `_project_trend_actions`
- Modify: `src/open_trader/a_share_trend.py` near `_holding_signal`

**Interfaces:**
- Consumes: `_project_trend_actions(payload, executions)` and frozen `signal_snapshots.holdings` / `industry_contexts`.
- Produces: projected `hold_actions` with missing `industry`, `industry_tm_id`, and `days` copied from frozen snapshots, sorted for all three markets.

- [ ] **Step 1: Read the test-writing rules and add the failing regression**

Read `skills/test-driven-development/writing-good-tests.md`. Add a parameterized test for `CN`, `HK`, and `US` with two holdings supplied in the wrong order: a medical row with lower-priority context and a financial row with a rising, hotter, stronger context. Keep `industry` and `industry_tm_id` absent from the holding decisions but present in the frozen snapshots. Assert:

```python
_, _, holds, _ = dashboard_module._project_trend_actions(payload, {})
assert [item["symbol"] for item in holds] == ["FIN", "MED"]
assert holds[0]["industry"] == "金融"
assert holds[0]["industry_tm_id"] == 2
assert holds[0]["days"] == 8
assert payload == original_payload
```

Add a second case with one invalid referenced context and stronger individual stock strength on the other row; assert the complete list falls back to individual strength order.

- [ ] **Step 2: Run the focused test and verify the expected RED result**

Run:

```bash
PYTHONPATH=.:src /Users/ray/projects/open_trader/.venv/bin/pytest -q tests/test_dashboard.py -k 'holding_industry or holding_order'
```

Expected: the new test fails because the current projection neither copies the missing sort/display fields nor reorders `hold_actions`.

- [ ] **Step 3: Add the smallest projection helpers**

Import the existing `KNOWN_TEMPERATURE_ORDER` and `TEMPERATURE_DIRECTION_ORDER` constants from `a_share_trend`. Add internal helpers beside `_project_trend_actions` that:

1. Copy only missing holding fields from the symbol-matched frozen snapshot.
2. Resolve each holding's context by `industry_tm_id`, then by industry name.
3. Validate the current context fields needed by the existing candidate order.
4. Use the history direction/change keys only when every referenced context has complete history.
5. Sort the entire list by context keys followed by individual strength, days, optional amount, and symbol; use individual sorting for the entire list when any referenced context is invalid or missing.

Add the existing holding `days` value to every market's frozen holding signal so the CN report has the same available individual sort key as HK and US.

Use `Decimal` parsing that treats malformed/missing values as absent. Do not invent zeroes for absent fields and do not modify `payload`.

- [ ] **Step 4: Run the focused projection tests and verify GREEN**

Run the command from Step 2. Expected: all new projection tests pass.

- [ ] **Step 5: Run the existing Dashboard projection slice**

Run:

```bash
PYTHONPATH=.:src /Users/ray/projects/open_trader/.venv/bin/pytest -q tests/test_dashboard.py -k 'trend_actions or holding_phase_projection or trend_money'
```

Expected: all selected existing tests pass with no mutation regressions.

### Task 2: Add the industry cell to the shared holding renderer

**Files:**
- Modify: `tests/test_dashboard_web.py` in `test_dashboard_cross_market_trend_report_tables_are_identical`
- Modify: `src/open_trader/dashboard_static/dashboard.js` in `renderTrendSellOrHoldStage`

**Interfaces:**
- Consumes: projected `hold_actions` containing `industry`.
- Produces: CN/HK/US holding tables with an `行业` header and escaped industry cell.

- [ ] **Step 1: Extend the cross-market renderer regression**

Change the test fixture's hold row to include `industry:"金融"`, change `expectedHold` to insert `"行业"` after `"强度"`, and assert each market contains `data-label="行业">金融</td>`.

- [ ] **Step 2: Run the web regression and verify the expected RED result**

Run:

```bash
PYTHONPATH=.:src /Users/ray/projects/open_trader/.venv/bin/pytest -q tests/test_dashboard_web.py -k cross_market_trend_report_tables_are_identical
```

Expected: FAIL because the shared hold renderer has no industry header/cell.

- [ ] **Step 3: Render the minimum new cell**

In `renderTrendSellOrHoldStage`, add `行业` and `renderTrendCell("行业", item.industry)` only for `kind === "hold"`, after `强度`. Leave sell/review headings and rows unchanged. Reuse the existing escaping/missing-value helper.

- [ ] **Step 4: Run the web regression and verify GREEN**

Run the command from Step 2. Expected: PASS for CN, US, and HK.

- [ ] **Step 5: Run the wider trend renderer slice**

Run:

```bash
PYTHONPATH=.:src /Users/ray/projects/open_trader/.venv/bin/pytest -q tests/test_dashboard_web.py -k 'trend_report or trend_stages or cross_market'
```

Expected: all selected renderer tests pass.

### Task 3: Finalize source, tests, and operator log

**Files:**
- Modify: `CHANGELOG.md` in the `2026-07-30` section

**Interfaces:**
- Consumes: the passing projection and renderer changes.
- Produces: a dated operator-facing record of the cross-market holding display/order correction.

- [ ] **Step 1: Add the dated changelog entry**

Record that CN/HK/US holding rows now show industry and use frozen industry-first ordering in Dashboard projection, with invalid/missing context falling back to individual ordering. State that the focused tests and no-submit report regeneration were verified.

- [ ] **Step 2: Run the affected Python suites and formatting check**

Run:

```bash
PYTHONPATH=.:src /Users/ray/projects/open_trader/.venv/bin/pytest -q tests/test_dashboard.py tests/test_dashboard_web.py tests/test_a_share_trend.py tests/test_market_trend.py
git diff --check
```

Expected: exit 0 and zero test failures.

- [ ] **Step 3: Commit source, tests, and changelog**

```bash
git add src/open_trader/dashboard.py src/open_trader/dashboard_static/dashboard.js tests/test_dashboard.py tests/test_dashboard_web.py CHANGELOG.md
git commit -m "fix: show and order cross-market held trend rows"
```

### Task 4: Regenerate reports and run the final dashboard gate

**Files:**
- Runtime outputs: `reports/trend_a_share`, `reports/trend_hk_phillips`, `reports/trend_us_tiger`
- Runtime evidence: `data/trend_controller/<MARKET>/status.json`, dashboard runtime/log files

**Interfaces:**
- Consumes: the committed source SHA and existing report/controller configuration.
- Produces: fresh no-submit CN/HK/US report artifacts and a verified Dashboard process using the accepted SHA.

- [ ] **Step 1: Regenerate each market in revision/no-submit mode**

Use the repository's existing command form:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m open_trader trend-market run --market CN --revision --config /Users/ray/projects/open_trader/config/daily_premarket.env
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m open_trader trend-market run --market HK --revision --config /Users/ray/projects/open_trader/config/daily_premarket.env
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m open_trader trend-market run --market US --revision --config /Users/ray/projects/open_trader/config/daily_premarket.env
```

Confirm each command reports a generated/revised artifact, no broker order submission, and no new action-ledger submission.

- [ ] **Step 2: Verify regenerated artifacts and projection output**

For the selected latest CN/HK/US JSON, assert the report metadata market is correct, formal actions are unchanged from the selected prior artifact when the revision is display-only, and the Dashboard projection returns a nonempty `industry` for every held row whose frozen snapshot provides one. Assert the hold symbols are in the expected industry-first order.

- [ ] **Step 3: Run the final acceptance gate**

Run exactly once after all source/data changes are final:

```bash
make acceptance
```

Only `PASS` is review-ready. On `FAIL`, fix and rerun; on `BLOCKED`, report the blocker without substituting local tests.

- [ ] **Step 4: Redeploy the accepted SHA and verify fresh runtime identity**

Restart the Dashboard serving port 8766 with the exact accepted worktree/SHA. Verify the new PID, process cwd, Git SHA, fresh startup log, and HTTP 200 from the review URL. Fetch the live dashboard payload and re-check the CN/HK/US holding headers/order after restart.

- [ ] **Step 5: Commit any runtime-safe changelog-only adjustment if needed**

If acceptance requires a source or changelog correction, make it in the same branch, rerun the final gate, and redeploy that new accepted SHA. Do not claim completion for an unaccepted SHA.
