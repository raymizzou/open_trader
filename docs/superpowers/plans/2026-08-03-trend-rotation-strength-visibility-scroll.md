# Hybrid Trend Rotation Strength and Refresh Stability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Use category-local strength for same-category rotations and global strength for stock/ETF rotations, explain every frozen decision in reports and Dashboard, preserve scroll position during automatic refresh, and regenerate all three current market reports safely.

**Architecture:** Extend the existing rotation dataclasses and planner in `a_share_trend.py`; do not add a module or dependency. Freeze at most two explanatory comparisons beside the existing executable pairs, keep legacy pair/report readers compatible, and bump only the allocation-era strategy identities to CN v12 and HK/US v10 with explicit Kelly/drawdown inheritance. Project frozen values into the current Dashboard components and fix the refresh jump at its single shared cause with native `focus({preventScroll: true})`.

**Tech Stack:** Python 3.12, stdlib `dataclasses` and `Decimal`, existing JSON/replay/reservation contracts, pytest, existing Node VM/Playwright Dashboard checks, launchd, and `make acceptance`.

## Global Constraints

- Implement in the existing isolated worktree `/Users/ray/projects/open_trader/.worktrees/trend-rotation-strength-visibility-scroll-design`, created from local `main` `31be3be45d56afa0943207fc8341e261c225e977`; preserve unrelated dirty files in `/Users/ray/projects/open_trader`.
- Treat `docs/superpowers/specs/2026-08-03-trend-rotation-strength-visibility-scroll-design.md` as the approved contract.
- Same `asset` means compare `strength`; different stock/ETF `asset` means compare `global_strength`. The inclusive threshold stays 20. Missing required data never falls back to the other metric.
- Keep existing forced exits, ordinary buys, ten-slot account model, 6%/4%/2% allocation weights, Kelly/risk/cash/lot rules, two-pair cap, and resource ranking unchanged.
- Simulated accounts may execute frozen formal pairs automatically. Real accounts remain manual advice only.
- Comparison rows explain decisions and are never order input. `simulate_rotation_pairs` remains the sole automatic rotation input to the Controller.
- Do not create a second planner, UI state store, scroll persistence layer, schema migration command, or dependency. Reuse current report, reservation, replay, Dashboard, and launchd paths.
- Read old reports/reservations without recomputing them. New strategy reports must use the new frozen comparison contract.
- Use focused tests during development. Run `make acceptance` only at the final Dashboard gate, after the merged SHA is deployed and all three immutable report revisions are generated.
- Only acceptance `PASS` permits completion. After PASS, redeploy the exact accepted SHA and verify PID, cwd, SHA, fresh logs, controller state, and HTTP 200.
- No screenshots are required because the user did not request them for this change.

## File Map

- Modify `src/open_trader/a_share_trend.py`: holding asset snapshot, hybrid pair comparison, frozen comparison rows, report payload/Markdown, and new report-contract validation.
- Modify `src/open_trader/trend_review.py`: backward-compatible pair validation, reservation/replay of new pair fields, and frozen comparison replay evidence.
- Modify `src/open_trader/strategy_drawdown.py`, `src/open_trader/drawdown_preflight.py`, and `src/open_trader/trend_kelly.py`: CN v12 and HK/US v10 continuity without resetting state.
- Modify `src/open_trader/dashboard.py`: project both strengths and frozen comparison rows without recalculation.
- Modify `src/open_trader/dashboard_static/dashboard.js`: dual-strength columns, comparison-basis cards, precise non-trigger reasons, and `preventScroll` focus restoration.
- Modify `src/open_trader/dashboard_acceptance.py`: accept and verify the new allocation-era identities and visible frozen fields.
- Modify `tests/test_a_share_trend.py`, `tests/test_trend_review.py`, `tests/test_trend_market_controller.py`, `tests/test_strategy_drawdown.py`, `tests/test_drawdown_preflight.py`, and `tests/test_trend_kelly.py`: strategy and contract coverage.
- Modify `tests/test_dashboard.py` and `tests/test_dashboard_web.py`: projection, rendering, and refresh-position coverage.
- Modify `CHANGELOG.md`: dated operator-facing entry committed before merge.

---

### Task 1: Build the Hybrid Comparison Once in the Existing Planner

**Files:**

- Modify: `src/open_trader/a_share_trend.py:1596-1650, 2891-3095, 4620-4725`
- Test: `tests/test_a_share_trend.py:430-590, 760-930`

**Interfaces:**

- `HoldingSnapshot.asset: str` freezes the same Trend Animals category already present in unified holding rows.
- `RotationComparison` freezes the selected buy/sell identities, both strengths, basis, compared values, gap, threshold, outcome, and reason.
- `plan_rotation_pairs(...)` returns executable proposals plus at most two stable comparison rows; `_plan_account_rotation_pairs(...)` applies existing sizing/risk rules and converts blocked proposals to `sizing_blocked`.

- [ ] **Step 1: Add failing same-category and cross-category tests**

Update the test holding helper to default `asset="A股"`. Add tests equivalent to:

```python
def test_same_category_rotation_uses_local_strength() -> None:
    pairs, comparisons = plan_rotation_pairs(
        holdings=(holding("PM", asset="美股", strength="76", global_strength="86.18"),),
        candidates=(candidate("SHEL", asset="美股", strength="98.6", global_strength="95.36"),),
        entry_weight=Decimal("0.04"), available_slots=0, pair_slots=(0, 1),
        market="US",
    )

    assert [(pair.sell_symbol, pair.buy_symbol) for pair in pairs] == [("PM", "SHEL")]
    assert comparisons[0].strength_basis == "local"
    assert comparisons[0].strength_gap == Decimal("22.6")
    assert comparisons[0].outcome == "planned"


def test_cross_category_rotation_uses_global_strength() -> None:
    pairs, comparisons = plan_rotation_pairs(
        holdings=(holding("SPY", asset="美国ETF", strength="99", global_strength="70"),),
        candidates=(candidate("SHEL", asset="美股", strength="75", global_strength="90"),),
        entry_weight=Decimal("0.04"), available_slots=0, pair_slots=(0, 1),
        market="US",
    )

    assert len(pairs) == 1
    assert comparisons[0].strength_basis == "global"
    assert comparisons[0].strength_gap == Decimal("20")
```

Also add the 19.9 boundaries, same-category missing local, cross-category missing global, missing/invalid asset, and stock-to-ETF plus ETF-to-stock cases.

- [ ] **Step 2: Run the focused planner tests and confirm failure**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  .venv/bin/python -m pytest tests/test_a_share_trend.py -k rotation -q
```

Expected: FAIL because holdings do not freeze `asset`, the planner always compares global strength, and it returns no comparison rows.

- [ ] **Step 3: Add the minimum frozen fields**

Add `asset` to `HoldingSnapshot`, populate it in `_holding_snapshot`, and preserve it through signal snapshots. Extend `RotationPair` with the audit fields required by the approved spec. Add one immutable comparison dataclass:

```python
@dataclass(frozen=True)
class RotationComparison:
    pair_index: int
    sell_symbol: str
    sell_name: str
    sell_asset: str
    sell_local_strength: Decimal | None
    sell_global_strength: Decimal | None
    buy_symbol: str
    buy_name: str
    buy_asset: str
    buy_local_strength: Decimal | None
    buy_global_strength: Decimal | None
    strength_basis: str | None
    sell_compared_strength: Decimal | None
    buy_compared_strength: Decimal | None
    strength_gap: Decimal | None
    threshold: Decimal = Decimal("20")
    outcome: str = "data_unavailable"
    reason: str = ""
```

Do not add a generic scoring framework. A small private helper returns `("local", holding.strength, candidate.strength)` for equal assets and `("global", holding.global_strength, candidate.global_strength)` otherwise.

- [ ] **Step 4: Replace zip pairing with stable non-overlapping selection**

In the existing planner:

1. enumerate unheld candidate × HOLD holding combinations;
2. compute the required basis and finite values once;
3. sort comparable combinations by gap descending, same-category first, buy symbol, then sell symbol;
4. place unavailable combinations after comparable combinations with the same symbol tie-breakers;
5. greedily choose non-overlapping symbols, at most two;
6. set `gap_below_threshold` below 20 and create a formal proposal only at or above 20.

Keep existing close/ATR/mapping/lot gates. Mark those failures `data_unavailable` or `sizing_blocked` with the existing precise skip reason; never switch strength basis.

- [ ] **Step 5: Reuse ordinary sizing and preserve its reason**

Change `_plan_account_rotation_pairs` to return `(pairs, comparisons)`. Continue calling `_plan_buy_actions` exactly once per selected proposal. If it returns no action, keep the comparison, set `outcome="sizing_blocked"`, and use the first existing risk-skip reason. If it returns an action, copy its final amount/quantity/risk fields into the pair and retain `outcome="planned"`.

Build simulated and real results independently in `build_report`; real comparisons may be planned but never submitted.

- [ ] **Step 6: Add ordering and independence tests**

Prove gap-descending selection, same-category tie priority, symbol tie stability, unique holding/candidate use, max two, empty-slot suppression, and independent simulated/real sizing reasons. Retain the SHEL/PM regression as a literal acceptance fixture, not a production special case.

- [ ] **Step 7: Run the complete trend builder suite**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  .venv/bin/python -m pytest tests/test_a_share_trend.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit the planner slice**

```bash
git add src/open_trader/a_share_trend.py tests/test_a_share_trend.py
git commit -m "feat: compare rotation strength by asset category"
```

---

### Task 2: Freeze the New Contract and Continue Strategy State

**Files:**

- Modify: `src/open_trader/a_share_trend.py:800-970, 3060-3180, 4660-4880, 5320-5400, 5650-5725, 6060-6135`
- Modify: `src/open_trader/trend_review.py:1040-1175, 560-610, 8250-8345`
- Modify: `src/open_trader/strategy_drawdown.py:1-80, 1140-1180`
- Modify: `src/open_trader/drawdown_preflight.py:1-90`
- Modify: `src/open_trader/trend_kelly.py:1-170`
- Test: `tests/test_a_share_trend.py`
- Test: `tests/test_trend_review.py`
- Test: `tests/test_trend_market_controller.py:1810-1870`
- Test: `tests/test_strategy_drawdown.py`
- Test: `tests/test_drawdown_preflight.py`
- Test: `tests/test_trend_kelly.py`

**Interfaces:**

- New allocation-era versions: CN v12, HK v10, US v10.
- New report keys: `simulate_rotation_comparisons` and `real_rotation_comparisons` under `strategy_judgments`.
- Existing pair reservation schema remains readable; new pair fields are validated when present.
- The Controller continues reading only `simulate_rotation_pairs`.

- [ ] **Step 1: Write failing report-contract tests**

Build one same-category and one cross-category report. Assert JSON, Markdown, replay evidence, and immutable reservation all preserve:

```python
assert comparison == {
    "pair_index": 0,
    "sell_symbol": "PM",
    "sell_asset": "美股",
    "sell_local_strength": "76",
    "sell_global_strength": "86.18",
    "buy_symbol": "SHEL",
    "buy_asset": "美股",
    "buy_local_strength": "98.6",
    "buy_global_strength": "95.36",
    "strength_basis": "local",
    "sell_compared_strength": "76",
    "buy_compared_strength": "98.6",
    "strength_gap": "22.6",
    "threshold": "20",
    "outcome": "planned",
    "reason": "relative_rotation",
    # existing identity fields remain present
}
```

Add invalid-field tests for mismatched gap, wrong basis for equal/different assets, wrong compared values, threshold other than 20, duplicate symbols/index, more than two rows, and a `planned` comparison that does not match a formal pair. Add a legacy fixture with no new fields and prove it still validates and replays unchanged.

- [ ] **Step 2: Run contract and replay tests and confirm failure**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  .venv/bin/python -m pytest \
  tests/test_a_share_trend.py -k 'contract or replay or rotation' \
  tests/test_trend_review.py -k rotation \
  tests/test_trend_market_controller.py -k rotation -q
```

Expected: FAIL because comparison rows are not serialized/replayed and reservation validation assumes global-only gaps.

- [ ] **Step 3: Serialize comparisons beside executable pairs**

Add `simulate_rotation_comparisons` and `real_rotation_comparisons` to `TrendReport`, `_report_payload`, Markdown, and frozen replay evidence. Always include the keys for CN v12/HK-US v10 allocation reports, even when there is no formal pair, so the report can explain `gap_below_threshold`, `sizing_blocked`, or `data_unavailable`.

Render Markdown from comparisons, not by recalculating from candidates. A planned row includes execution amount/quantity from its matching pair; a non-planned row prints basis, gap/threshold when available, and the frozen reason instead of `无`.

- [ ] **Step 4: Make reservations backward compatible and explanations truthful**

Update `_valid_rotation_pair` with two explicit branches:

- legacy pair without `strength_basis`: retain the current global-strength validation exactly;
- new pair with `strength_basis`: validate assets, both optional strength pairs, compared fields, exact gap, threshold 20, and the category/basis rule.

Do not rename or rewrite existing reservation files. After `reserve_rotation_pairs` returns, rebuild every reserved pair's `planned` comparison from that exact frozen pair. Fill any remaining explanation slot with the highest-ranked non-conflicting non-planned comparison. This prevents a later report revision from explaining a different pair than the one already reserved.

- [ ] **Step 5: Prove the Controller ignores comparison-only rows**

Add a controller test with zero `simulate_rotation_pairs` and a stronger `simulate_rotation_comparisons` row. Assert zero order intents. Add the inverse test showing one formal pair still follows the existing sell-fill-buy path. No production Controller branch should read the comparison keys.

- [ ] **Step 6: Bump only allocation-era strategy identities**

Set:

```python
ALLOCATION_PROJECTION_VERSIONS = {"CN": "v12", "HK": "v10", "US": "v10"}
```

Add CN v12 ← v11 and HK/US v10 ← v9 to drawdown predecessor approval. Extend Kelly identity/version allowlists so the new versions inherit the same eligible samples. Add the new parameter snapshots/hashes using the existing canonical hash helper; do not reset baseline equity, high-water mark, pause state, position lifecycle, or Kelly samples.

Update every explicit report/acceptance version allowlist found by:

```bash
rg -n 'v11|"v9"|ALLOCATION_PROJECTION_VERSIONS|TREND_STRATEGY_VERSIONS' \
  src/open_trader tests
```

Do not change the non-allocation fallback versions in `CURRENT_TREND_STRATEGY_VERSIONS`.

- [ ] **Step 7: Run continuity and contract suites**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  .venv/bin/python -m pytest \
  tests/test_a_share_trend.py tests/test_trend_review.py \
  tests/test_trend_market_controller.py tests/test_strategy_drawdown.py \
  tests/test_drawdown_preflight.py tests/test_trend_kelly.py -q
```

Expected: PASS, including legacy reports and reservations.

- [ ] **Step 8: Commit the frozen-contract slice**

```bash
git add src/open_trader/a_share_trend.py src/open_trader/trend_review.py \
  src/open_trader/strategy_drawdown.py src/open_trader/drawdown_preflight.py \
  src/open_trader/trend_kelly.py tests/test_a_share_trend.py \
  tests/test_trend_review.py tests/test_trend_market_controller.py \
  tests/test_strategy_drawdown.py tests/test_drawdown_preflight.py \
  tests/test_trend_kelly.py
git commit -m "feat: freeze hybrid rotation decisions"
```

---

### Task 3: Show Both Strengths and Stop Refresh from Moving the Page

**Files:**

- Modify: `src/open_trader/dashboard.py:2315-2400`
- Modify: `src/open_trader/dashboard_static/dashboard.js:4400-4750, 5235-5280`
- Modify: `src/open_trader/dashboard_acceptance.py:420-470, 3630-3670, 4410-4660`
- Test: `tests/test_dashboard.py:1930-2020`
- Test: `tests/test_dashboard_web.py:6600-6720, 12450-12600`

**Interfaces:**

- Dashboard API projects frozen local/global strengths and comparison rows only.
- Existing rotation cards render `planned`, `gap_below_threshold`, `sizing_blocked`, and `data_unavailable`.
- Automatic account refresh restores focus with `preventScroll`; explicit user navigation remains unchanged.

- [ ] **Step 1: Write failing API projection tests**

Add a current-report fixture containing both strength fields and all four comparison outcomes. Assert `dashboard.py` returns them unchanged. For holdings, join only against the report's frozen `signal_snapshots.holdings` by symbol; do not call Trend Animals or derive percentiles in the Dashboard.

Assert historical legacy reports remain available and show missing global strength as unavailable rather than inventing a value.

- [ ] **Step 2: Write failing renderer and scroll tests**

In the existing Node VM harness, assert:

- candidate and holding headers contain `大类内强度` followed by `全局强度`;
- same-category cards say `比较口径：大类内强度`;
- cross-category cards say `比较口径：全局强度`;
- 19.9 shows `未触发`, `门槛 20`, and `还差 0.1`;
- sizing/data failures show the frozen reason and never only `无`;
- a focused account-view tab receives `{preventScroll: true}` during `renderAccountHoldings`.

Use a focus stub that captures the option:

```javascript
focus(options) {
  focused = selector;
  focusOptions = options;
}
```

- [ ] **Step 3: Run focused Dashboard tests and confirm failure**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  .venv/bin/python -m pytest \
  tests/test_dashboard.py -k trend \
  tests/test_dashboard_web.py -k 'rotation or account_view or disclosure or scroll' -q
```

Expected: FAIL because the API omits comparisons, the UI labels one generic strength, and refresh calls bare `focus()`.

- [ ] **Step 4: Project and render frozen values with existing components**

Add the two comparison lists to `_project_broker_trend_report`. Extend current buy/holding rows with `strength` and `global_strength` from already-frozen report facts. Reuse `.trend-rotation-pair`, `.trend-rotation-route`, and existing table/mobile `data-label` behavior; do not add a new component or CSS system.

Change `renderTrendRotations` to consume comparison rows and look up its matching formal pair only for amount, quantity, and execution status. The comparison row supplies basis, strengths, gap, threshold, outcome, and reason. Legacy reports may continue rendering their formal pair card with the old global-only copy.

- [ ] **Step 5: Fix the single refresh root cause**

Change only the automatic post-render focus restoration in `renderAccountHoldings`:

```javascript
container
  .querySelector(`[data-account-view="${focusedView}"]`)
  ?.focus({preventScroll: true});
```

Leave explicit account/view/history navigation focus calls unchanged. Do not add scroll capture, timers, `localStorage`, or a browser-version branch.

- [ ] **Step 6: Extend acceptance assertions**

Accept CN v12/HK-US v10 and verify a current report visibly contains both strength labels plus the frozen basis/outcome. In the real browser flow, scroll below the report header, focus an account-view tab, trigger the existing in-place refresh path, and assert the post-refresh `scrollY` equals the pre-refresh value while focus, account view, and disclosure state remain intact.

- [ ] **Step 7: Run Dashboard suites**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  .venv/bin/python -m pytest \
  tests/test_dashboard.py tests/test_dashboard_web.py \
  tests/test_dashboard_acceptance.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit the Dashboard slice**

```bash
git add src/open_trader/dashboard.py \
  src/open_trader/dashboard_static/dashboard.js \
  src/open_trader/dashboard_acceptance.py \
  tests/test_dashboard.py tests/test_dashboard_web.py \
  tests/test_dashboard_acceptance.py
git commit -m "fix: explain rotations without refresh jumps"
```

---

### Task 4: Merge, Regenerate All Three Reports, Accept, and Redeploy

**Files:**

- Modify: `CHANGELOG.md`
- Verify: all files and runtime surfaces from Tasks 1-3
- Generate: new immutable revisions under `reports/trend_a_share`, `reports/trend_hk_phillips`, and `reports/trend_us_tiger`

**Interfaces:**

- `open_trader trend-market run --market CN|HK|US --revision` creates and promotes one new immutable report revision per market.
- Existing controllers lock only formal pairs from the promoted report SHA.
- `make acceptance` is the sole final Dashboard review gate.

- [ ] **Step 1: Run the complete focused behavior suite**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  .venv/bin/python -m pytest \
  tests/test_a_share_trend.py tests/test_trend_review.py \
  tests/test_trend_market_controller.py tests/test_strategy_drawdown.py \
  tests/test_drawdown_preflight.py tests/test_trend_kelly.py \
  tests/test_dashboard.py tests/test_dashboard_web.py \
  tests/test_dashboard_acceptance.py -q
```

Record the exact pass count and duration.

- [ ] **Step 2: Run full repository tests before touching live reports**

```bash
make test
```

Expected: PASS. Fix and recommit every failure before continuing.

- [ ] **Step 3: Run deterministic direct workflow checks**

Using temporary report/data roots and existing provider/broker fakes, invoke the real report builder, reservation, replay, and Controller entry points. Prove:

- same-category local and cross-category global comparisons freeze identically in JSON/Markdown/Dashboard projection;
- a comparison-only row emits no order;
- a formal simulated pair follows MARKET sell → proven full fill → MARKET buy;
- a formal real pair produces no order request;
- a revised report retains an existing reserved pair and reconciles its explanation.

- [ ] **Step 4: Update and commit the merge log before merge**

Add a dated `## 2026-08-03` operator entry describing hybrid strength basis, visible decision reasons, the native refresh-position fix, CN v12/HK-US v10 continuity, three-report regeneration, and exact test evidence.

```bash
git add CHANGELOG.md
git commit -m "docs: log hybrid trend rotation strength"
```

- [ ] **Step 5: Merge into local main without disturbing user files**

Confirm the feature worktree is clean and the dirty root files are unrelated. Merge non-interactively into `/Users/ray/projects/open_trader`, preserving all pre-existing user modifications. Record the resulting main SHA; this merged SHA is the only candidate permitted for live generation and acceptance.

- [ ] **Step 6: Deploy the merged candidate SHA and prove process identity**

From the merged root, reinstall/restart the Dashboard stack and all trend controllers with the repository scripts:

```bash
scripts/install_dashboard_launchd.sh --mode stack
scripts/install_daily_premarket_launchd.sh --trend-only --market all \
  --config config/daily_premarket.env
```

Verify new launchd PIDs, cwd `/Users/ray/projects/open_trader`, exact merged Git SHA, fresh status timestamps, and fresh stdout/stderr logs. Stop any stale pre-change process before proceeding.

- [ ] **Step 7: Recheck execution safety immediately before report generation**

Run all three status commands:

```bash
.venv/bin/python -m open_trader trend-market status --market CN --config config/daily_premarket.env
.venv/bin/python -m open_trader trend-market status --market HK --config config/daily_premarket.env
.venv/bin/python -m open_trader trend-market status --market US --config config/daily_premarket.env
```

Inspect the target-date execution batches, rotation ledgers, and broker reconciliation facts. Continue per market only when there is no locked different report SHA and no submitted, partial, unknown, or conflicting simulated order. A blocked market must keep its existing facts unchanged and be reported explicitly.

- [ ] **Step 8: Generate and promote one new immutable revision for each safe market**

Run sequentially so each result can be inspected before the next:

```bash
.venv/bin/python -m open_trader trend-market run --market CN --revision --config config/daily_premarket.env
.venv/bin/python -m open_trader trend-market run --market HK --revision --config config/daily_premarket.env
.venv/bin/python -m open_trader trend-market run --market US --revision --config config/daily_premarket.env
```

For every output, verify a new `-rN` JSON/Markdown exists, the previous revision still exists, the current pointer/API selects the new SHA, strategy identity is CN v12 or HK/US v10, the same allocation snapshot path/SHA is frozen, generated date and target trade date are explicit, and real execution remains manual.

For US, inspect the current facts rather than assuming the prior screenshot: if SHEL and PM still qualify in the same `美股` category with local gap at least 20 and sizing/risk pass, the report must contain formal simulated PM → SHEL. If current data changed, the frozen comparison must explain the actual winner or exact non-trigger reason.

- [ ] **Step 9: Verify Dashboard/API and Controller selection before acceptance**

Confirm HTTP 200 at `http://127.0.0.1:8766/`, Dashboard API selects all three new report SHAs, both strengths and comparison reasons render, auto-refresh preserves scroll, and each Controller status references the expected promoted SHA without treating comparison-only rows as orders.

- [ ] **Step 10: Run the one final Dashboard acceptance gate**

```bash
make acceptance
```

Expected: `PASS`. On `FAIL`, fix, recommit, redeploy the new SHA, rerun safety checks and affected report revisions when report semantics changed, then rerun acceptance. On `BLOCKED`, report the blocker and do not substitute fixtures, curl, unit tests, or screenshots.

- [ ] **Step 11: Redeploy the exact accepted SHA without changing source or reports**

After PASS, rerun the Dashboard and trend-controller installers against the exact accepted SHA. Verify:

```text
Dashboard/frontend-gateway PID, cwd, Git SHA, fresh logs
legacy Dashboard PID, cwd, Git SHA, fresh logs
allocation task PID, cwd, Git SHA, fresh status/log
CN/HK/US controller PID, cwd, Git SHA, fresh status/log
HTTP 200 from http://127.0.0.1:8766/
```

Recheck execution batch SHA, blocking state, rotation ledger, and submitted-count status for all markets. The exact-SHA restart does not require another acceptance run because it changes neither source nor report data.

- [ ] **Step 12: Hand off the proven result**

Provide the final SHA, exact test counts, acceptance PASS line, three new artifact names/SHAs, their generated and target trade dates, each market's rotation basis/outcome, controller PID/cwd/SHA proof, and the review URL. State explicitly that simulated formal pairs execute automatically on their report trading date and real pairs remain manual.

---

## Self-Review Checklist

- [ ] Same-category uses only local strength; cross-category uses only global strength; threshold is inclusive 20.
- [ ] Missing metric/asset never falls back or freezes the whole market.
- [ ] Stocks and ETFs can replace each other, symbols are non-overlapping, and each account has at most two groups.
- [ ] Comparison rows are explanatory only; Controller order input remains formal simulated pairs.
- [ ] Formal pairs and planned comparisons agree after immutable reservation reuse.
- [ ] Legacy reports/reservations remain readable and are never recomputed.
- [ ] CN v12/HK-US v10 inherit Kelly and drawdown state without reaccumulation.
- [ ] Dashboard shows both strengths, actual basis/gap/threshold, and exact non-trigger reasons.
- [ ] Only automatic post-render focus restoration uses `preventScroll`; explicit navigation is unchanged.
- [ ] Changelog is committed before merge; all three reports are new immutable revisions, not overwrites.
- [ ] Final `make acceptance` occurs once the merged SHA and regenerated live reports are in place.
- [ ] Exact accepted SHA is redeployed and verified by PID/cwd/SHA/log/HTTP evidence.
- [ ] No new dependency, module, generic scoring framework, scroll store, or screenshot requirement was added.
