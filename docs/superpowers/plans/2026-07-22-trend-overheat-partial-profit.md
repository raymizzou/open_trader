# Trend Overheat Partial Profit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the existing boiling and champagne fields into one recoverable 30% take-profit action per complete CN/US/HK trend position, while retaining the current full-exit rules, strategy identity, Kelly samples, drawdown state, and manual real-account workflow.

**Architecture:** Add `SELL_PARTIAL` to the existing frozen-report contract and execute it through the existing immutable sell ledger. The first intent freezes a lifecycle target from the live Futu SIMULATE position; later attempts and later reports recover that target and confirmed fills by `position_started_for`. The existing protection state remains a rebuildable projection, and `SELL_ALL` upgrades the same sell path to a `position_zero` goal without submitting overlapping orders.

**Tech Stack:** Python 3.12, standard-library `dataclasses`/`decimal`/`json`/`pathlib`, existing Trend Animals and Futu adapters, immutable JSON facts, pytest, vanilla JavaScript, Playwright, launchd, and screen.

## Global Constraints

- Work only in `/Users/ray/projects/open_trader/.worktrees/trend-overheat-partial-profit` on `feat/trend-overheat-partial-profit`, based on local `main` commit `fffe37f`.
- Preserve `strategy_id`, `strategy_version=v4`, Kelly sample keys/counts, drawdown high-water marks, and manual unlock state.
- Do not rewrite any existing frozen or locked report. The rule begins with the first newly generated report after deployment.
- Do not add a volatility-expansion signal; the live API exposes only boiling, danger, and champagne stop-win fields.
- Do not add a dependency, service, database, queue, second sell ledger, real-money order path, real-fill confirmation step, or voice alert.
- Futu SIMULATE positions remain the only automatic decision and order source. Eastmoney, Tiger, and Phillips real accounts remain manual.
- A complete position may consume at most one combined boiling/champagne trim. A later re-entry after position zero starts a new lifecycle.
- A partial sell never frees a position slot, funds a same-day buy, closes a trade sample, or causes replenishment to the original target weight.
- An explicit full-exit fact wins over a partial exit. Otherwise, an explicit overheat fact wins over unknown signal, K-line, or protection-line fields.
- Missing HK lot size blocks the partial action. A known target below one lot records terminal `below_lot` without an order.
- Run focused tests and direct workflows while developing. Do not run `make acceptance` until the final committed Dashboard gate.
- Only `make acceptance` output `PASS` permits completion or deployment language. After PASS, deploy the exact accepted SHA and verify new processes, logs, and HTTP 200.

---

## File Map and Contracts

- Modify `src/open_trader/a_share_trend.py`: decision priority, `SELL_PARTIAL` report fields, lifecycle/protection projection, strategy parameters, Markdown/Feishu wording, and report/state validation.
- Modify `src/open_trader/market_trend.py`: request HK lot sizes for held symbols as well as candidates.
- Modify `src/open_trader/trend_review.py`: validate partial actions, freeze live quantities, persist sell goals/lifecycle identity, recover cumulative fills across dates, project completion, and upgrade to full exit.
- Modify `src/open_trader/trend_market_controller.py`: accept partial reports, sequence all sells first, distinguish partial/full completion, block conflicting buys, and surface urgent uncertain full exits.
- Modify `src/open_trader/trend_api_stats.py`: attribute `SELL_PARTIAL` actual fills as sells without closing a round until position zero.
- Modify `src/open_trader/dashboard.py`: project partial actions and their execution facts, distinguish simulation quantities from manual real-account guidance, and preserve the unchanged review sample view.
- Modify `src/open_trader/dashboard_static/dashboard.js`: render per-row action labels/quantities and update the visible discipline.
- Modify `src/open_trader/dashboard_acceptance.py`: validate the new frozen and visible contracts on desktop and mobile.
- Modify `纪律.md`, `CONTEXT.md`, and `README.md`: record the approved rule, compatibility exception, and `abandon` lifecycle behavior.
- Test in `tests/test_a_share_trend.py`, `tests/test_market_trend.py`, `tests/test_trend_review.py`, `tests/test_trend_market_controller.py`, `tests/test_trend_api_stats.py`, `tests/test_trend_api_fill_sync.py`, `tests/test_dashboard.py`, `tests/test_dashboard_web.py`, and `tests/test_dashboard_acceptance.py`.

The frozen `SELL_PARTIAL` action must contain these exact facts:

```json
{
  "action": "SELL_PARTIAL",
  "reason": "overheat_take_profit",
  "target_fraction": "0.30",
  "estimated_shares": 300,
  "lot_size": 100,
  "position_started_for": "2026-07-20",
  "overheat_signals": ["boiling"],
  "warnings": []
}
```

Every sell ledger event or intent created under the new rule carries:

```text
sell_goal = partial_30 | position_zero
position_started_for = YYYY-MM-DD
lifecycle_target_qty = frozen first-intent 30% target
filled_qty = cumulative confirmed fills for this lifecycle goal
target_qty = lifecycle target for partial_30, or live remaining position for position_zero
```

`SELL_ALL` reports do not require `target_fraction`, but execution must append or retain `sell_goal=position_zero`. Existing pre-change `SELL_ALL` facts remain readable through the current compatibility validation.

---

### Task 1: Generate one explicit partial action without weakening exits

**Files:**
- Modify: `src/open_trader/a_share_trend.py:64-120,743-765,1788-2250,2390-2480,2670-3090,3117-3180`
- Modify: `src/open_trader/market_trend.py:900-1010`
- Test: `tests/test_a_share_trend.py:1800-2050,2500-2850`
- Test: `tests/test_market_trend.py`

**Interfaces:**
- Add constants `OVERHEAT_TRIM_FRACTION = Decimal("0.30")` and `OVERHEAT_TRIM_SIGNALS = ("boiling", "champagne")`.
- Extend `HoldingDecision` with `position_started_for`, `target_fraction`, `estimated_shares`, `lot_size`, `overheat_signals`, and `warnings`; use `None`/empty tuples for non-partial decisions.
- Extend `_holding_action(..., overheat_trim_terminal: bool = False) -> tuple[str, str]` to return `SELL_PARTIAL, overheat_take_profit` only when the lifecycle opportunity is unconsumed.
- Reuse `_floor_to_lot` semantics for report estimates; no new sizing class.

- [ ] **Step 1: Replace the obsolete no-trim test with failing priority tests**

Replace `test_cn_boiling_and_champagne_never_create_trim_action` and add parameterized CN/US/HK tests proving:

```python
@pytest.mark.parametrize("boiling,champagne", [(True, False), (False, True), (True, True)])
def test_explicit_overheat_creates_one_partial_action(boiling, champagne):
    report = build_report(...)
    action = report.holdings[0]
    assert action.action == "SELL_PARTIAL"
    assert action.reason == "overheat_take_profit"
    assert action.target_fraction == Decimal("0.30")


def test_full_exit_still_wins_over_overheat() -> None:
    snapshot = replace(_holding_snapshot(), danger=True, boiling=True)
    assert _holding_action(symbol="600001", snapshot=snapshot, triggered=set()) == (
        "SELL_ALL", "danger_signal"
    )


def test_explicit_overheat_wins_over_unknown_non_exit_fields() -> None:
    snapshot = replace(
        _holding_snapshot(), danger=None, right_side=None,
        boiling=True, champagne=None,
    )
    assert _holding_action(symbol="600001", snapshot=snapshot, triggered=set()) == (
        "SELL_PARTIAL", "overheat_take_profit"
    )
```

Add cases for protection trigger, right-side false, CN temperature-to-flat, pure unknown, a terminal prior trim, and a later lifecycle after position zero.

- [ ] **Step 2: Add failing K-line/protection-state tests**

Prove an explicit overheat action survives stale/missing daily bars, preserves an old line when present, and creates a state entry with `position_started_for` even when no line exists. Assert the no-line report has no buy actions and a paused risk reason containing `活动保护线缺失`.

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
  tests/test_a_share_trend.py -k 'overheat or partial_action or kline_unavailable or protection_state'
```

Expected: `FAIL`; current code returns `HOLD`/`MANUAL_REVIEW`, omits partial fields, and only persists positions with an active line.

- [ ] **Step 3: Implement the priority and lifecycle projection**

Change `_holding_action` in this order:

```python
if symbol in triggered:
    return "SELL_ALL", "protection_line_already_triggered"
if snapshot is not None and snapshot.danger is True:
    return "SELL_ALL", "danger_signal"
if snapshot is not None and snapshot.right_side is False:
    return "SELL_ALL", "left_trend_right_side"
if cn_temperature_changed_to_flat:
    return "SELL_ALL", "temperature_changed_to_flat"
if snapshot is not None and (snapshot.boiling is True or snapshot.champagne is True):
    if not overheat_trim_terminal:
        return "SELL_PARTIAL", "overheat_take_profit"
if required_exit_fact_is_unknown:
    return "MANUAL_REVIEW", "holding_signal_unknown"
return "HOLD", "trend_intact"
```

In `build_report`:

- read terminal state only from `overheat_trim_status in {"complete", "below_lot"}` for the same `position_started_for`;
- update the trailing line for `HOLD` and `SELL_PARTIAL`, not just `HOLD`;
- never replace `SELL_PARTIAL` with `MANUAL_REVIEW` because K-line data is absent;
- add warning IDs `holding_signal_unknown` and/or `holding_kline_unavailable` without changing the explicit action;
- persist every current position so lifecycle identity survives missing protection lines;
- allow protection-state `initial_line`/`active_line` to be jointly absent, while retaining strict validation when either value is present;
- leave `sell_symbols`, post-sell cash, and post-sell position count restricted to `SELL_ALL`.

The existing watcher already emits a Feishu-only urgent `protection_line_missing` alert and skips price comparison when `active_line` is absent; reuse it without changing `a_share_trend_watch.py` or adding voice.

- [ ] **Step 4: Add report quantities and the HK held-symbol lot query**

For each partial decision, set market lot size to CN `100`, US `1`, or `lot_sizes[symbol]` for HK, and set the estimate to:

```python
estimated = int(_floor_to_lot(
    position.quantity * OVERHEAT_TRIM_FRACTION,
    Decimal(lot_size),
))
```

If HK lot size is absent/invalid, convert only that holding to `MANUAL_REVIEW` with reason `holding_lot_size_unavailable`; do not guess and do not mark `below_lot`.

In `market_trend.py`, request HK lot sizes for the union of candidate and current holding Futu symbols:

```python
symbols = sorted({
    *(to_futu_symbol("HK", item.symbol) for item in candidates),
    *(to_futu_symbol("HK", item.symbol) for item in account.positions),
})
```

Add a market test proving a held symbol outside the candidate list is included exactly once.

- [ ] **Step 5: Add the unchanged-v4 strategy contract**

Add these parameters and Chinese rows to `trend_strategy_snapshot`, without changing ID/version/effective sample key:

```python
"overheat_trim_fraction": "0.30",
"overheat_trim_once_per_position": True,
"overheat_trim_signals": ["boiling", "champagne"],
"overheat_trim_rounding": "floor_to_market_lot",
"overheat_trim_below_lot": "no_order_terminal",
"full_exit_precedes_partial_exit": True,
```

Update `validate_report_strategy_snapshot` to validate the new fields when either the parameters contain `overheat_trim_fraction` or any holding action is `SELL_PARTIAL`. Current code generation always adds the fields; historical frozen v4 reports with neither marker remain readable without being rewritten.

- [ ] **Step 6: Verify report GREEN and commit**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
  tests/test_a_share_trend.py tests/test_market_trend.py
```

Expected: both files `PASS`, including full-exit precedence, explicit-over-unknown behavior, missing-line buy pause, all three lot rules, and unchanged v4 identity.

Commit:

```bash
git add src/open_trader/a_share_trend.py src/open_trader/market_trend.py \
  tests/test_a_share_trend.py tests/test_market_trend.py
git commit -m "feat: emit unified overheat partial exits"
```

---

### Task 2: Freeze a live 30% sell target before the first intent

**Files:**
- Modify: `src/open_trader/trend_review.py:1129-1610,1883-2825`
- Test: `tests/test_trend_review.py:2300-2850,4000-4300`

**Interfaces:**
- `_preflight_open_actions` accepts `BUY`, `SELL_ALL`, and `SELL_PARTIAL`, with strict partial fields.
- Add `_overheat_trim_quantity(position_qty: Decimal, fraction: Decimal, lot_size: int) -> Decimal` using the existing `_floor_to_lot` helper.
- Store `sell_goal`, `position_started_for`, and `lifecycle_target_qty` in intent/result/event payloads created for new partial sells.
- Add terminal event status `below_lot` with reason `overheat_target_below_lot`.

- [ ] **Step 1: Write failing trust-boundary tests**

Add tests that reject partial actions when any of these are missing or invalid: exact fraction `0.30`, positive integral lot, non-negative integral report estimate aligned to lot, canonical `position_started_for`, or recognized overheat signal. Prove `SELL_ALL` and old immutable facts remain compatible.

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
  tests/test_trend_review.py -k 'partial_action_validation or legacy_sell_all'
```

Expected: `FAIL`; `SELL_PARTIAL` is currently rejected.

- [ ] **Step 2: Write failing live-sizing tests**

Use the existing fake Futu SIMULATE client to prove:

- CN live `1,000` with lot `100` freezes target `300`;
- US live `7` with lot `1` freezes target `2`;
- HK live `1,000` with lot `200` freezes target `200`;
- the report estimate is advisory: a report built at `1,000` but live position `800` freezes CN target `200`;
- HK live `300` with lot `200` produces `below_lot`, no intent, and no `place_order` call;
- a missing/invalid live position blocks without writing an intent.

Assert the first partial order is a market sell, the target is written before `place_order`, and its event contains `sell_goal=partial_30` plus the report lifecycle date.

- [ ] **Step 3: Implement strict partial preflight and sizing**

Validate the action with:

```python
fraction = _required_decimal(action.get("target_fraction"), "target fraction")
lot_size = int(action.get("lot_size") or 0)
position_started_for = date.fromisoformat(
    str(action.get("position_started_for") or "")
).isoformat()
if fraction != Decimal("0.30") or lot_size <= 0:
    raise ValueError("trend review partial sell action is invalid")
```

Before writing the first intent, read the matching live position from the already-authoritative account snapshot and compute:

```python
target = _floor_to_lot(live_quantity * fraction, Decimal(lot_size))
```

Do not use estimated proceeds for buys and do not change `quote_prices`; partial sells need no price quote.

- [ ] **Step 4: Make `below_lot` an audited terminal fact**

When the authoritative target is zero, write one event containing the account observation, `sell_goal=partial_30`, lifecycle identity, zero target/fill, status `below_lot`, and reason `overheat_target_below_lot`. Extend `load_trend_action_audit` to validate that exact shape and reject `below_lot` for buys, unknown lots, nonzero targets, or missing broker observations.

- [ ] **Step 5: Verify frozen-target GREEN and commit**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
  tests/test_trend_review.py -k 'partial or below_lot or live_position or open_action'
```

Expected: selected tests `PASS`; the fake client receives only the rounded live target.

Commit:

```bash
git add src/open_trader/trend_review.py tests/test_trend_review.py
git commit -m "feat: freeze partial profit sell targets"
```

---

### Task 3: Recover one lifecycle target across fills, reports, and abandon

**Files:**
- Modify: `src/open_trader/trend_review.py:820-1850,1935-2825`
- Modify: `src/open_trader/a_share_trend.py:1980-2250,3117-3180`
- Modify: `src/open_trader/market_trend.py:790-1060`
- Test: `tests/test_trend_review.py`
- Test: `tests/test_a_share_trend.py`
- Test: `tests/test_market_trend.py`

**Interfaces:**
- Add `overheat_trim_progress(data_dir, *, market, symbol, position_started_for) -> dict[str, object]` that validates immutable facts across execution-date directories and returns frozen target, cumulative confirmed fill, status, and source paths.
- Add `rebuild_overheat_trim_projection(data_dir, *, market, state_path) -> dict[str, object]` that updates only rebuildable protection-state fields.
- Reuse the existing action/resolution directories; do not create a lifecycle ledger.

- [ ] **Step 1: Write failing cumulative-fill and retry tests**

Cover these transitions with immutable fake facts and broker orders:

```text
target 300 -> fill 100 -> active order: no second order
target 300 -> fill 100 -> cancelled order: next attempt 200
target 300 -> fill 100 -> process restart: next attempt remains 200
target 300 -> fill 300 -> later boiling/champagne report: no new action
target 300 -> fill 100 -> later live quantity changes: remaining target remains 200
```

Every `filled_qty` for `partial_30` is cumulative across attempts. A `filled` event is valid only when cumulative confirmed fills reach the lifecycle target.

- [ ] **Step 2: Write failing cross-date and abandon tests**

Prove:

- a triggered locked action continues after the signal disappears until completion;
- `abandon` ends only the current `(market, execution_date, symbol, sell)` action;
- `abandon` does not set lifecycle terminal state;
- no signal on the next new report creates no action;
- the same or other overheat signal on a later report creates `SELL_PARTIAL` for only the original remaining target;
- position zero removes old projection state, and a later re-entry with a new `position_started_for` receives a fresh 30% opportunity.

- [ ] **Step 3: Implement validated cross-date progress**

Scan only:

```text
data/trend_review/ledgers/<MARKET>/actions/*/<sell-action-key>/
data/trend_review/ledgers/<MARKET>/open/*/
data/trend_review/ledgers/<MARKET>/batches/*.json
```

For every candidate date, call the existing strict validators before accepting a target or fill. Match market, symbol, side `sell`, `sell_goal=partial_30`, and exact `position_started_for`. Reject conflicting lifecycle targets instead of choosing one. Treat only broker-observed confirmed `dealt_qty` as filled; submitted, failed, conflict, uncertain, and resolution text are not fills.

- [ ] **Step 4: Reuse the target after abandon**

When a later report produces a new action key for the same lifecycle:

- load the prior immutable `lifecycle_target_qty`;
- subtract cumulative confirmed fills;
- write the new date's first intent only for the remainder;
- preserve the original lifecycle target in every new event;
- if the remainder is zero, rebuild terminal projection and submit nothing.

An `authorize-retry` still authorizes exactly one next attempt. `confirm-submitted` remains terminal for the current action pending broker reconciliation. `abandon` never fabricates a fill or consumes the lifecycle opportunity.

- [ ] **Step 5: Rebuild protection projection before report decisions and after execution**

Store these optional fields inside each position state:

```json
{
  "overheat_trim_status": "pending|complete|below_lot",
  "overheat_trim_target_qty": "300",
  "overheat_trim_filled_qty": "100",
  "overheat_trim_started_for": "2026-07-20"
}
```

Call projection rebuilding:

- immediately before each CN/US/HK report reads prior protection state;
- after open execution writes new broker-observed events;
- before deciding whether a later report may emit another partial action.

Projection writes must preserve active lines and use the existing atomic `write_protection_state`. Immutable action facts remain authoritative if the process crashes between the event and projection write.

- [ ] **Step 6: Verify lifecycle GREEN and commit**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
  tests/test_trend_review.py tests/test_a_share_trend.py tests/test_market_trend.py \
  -k 'partial or overheat or abandon or protection_state or report'
```

Expected: selected tests `PASS`, including crash recovery and re-entry isolation.

Commit:

```bash
git add src/open_trader/trend_review.py src/open_trader/a_share_trend.py \
  src/open_trader/market_trend.py tests/test_trend_review.py \
  tests/test_a_share_trend.py tests/test_market_trend.py
git commit -m "feat: recover overheat trims by position lifecycle"
```

---

### Task 4: Let the controller upgrade partial sells to full exits safely

**Files:**
- Modify: `src/open_trader/trend_market_controller.py:390-465,630-980,1535-1605`
- Modify: `src/open_trader/trend_review.py:1129-1610,1935-3090`
- Test: `tests/test_trend_market_controller.py`
- Test: `tests/test_trend_review.py`

**Interfaces:**
- `_valid_report` accepts strict `SELL_PARTIAL` actions.
- `_execution_completed` evaluates `partial_30` and `position_zero` separately.
- `_locked_action_context` accepts either sell action for side `sell` and validates its goal.
- A protection event appends `sell_goal=position_zero` to the same date/symbol/side path.

- [ ] **Step 1: Write failing controller validation/completion tests**

Add tests proving:

- a valid partial action passes frozen report validation;
- malformed fraction/lot/lifecycle facts fail closed;
- `below_lot` and cumulative target-filled complete a partial action;
- a partial `filled` event does not complete a later `position_zero` goal;
- `abandon` completes the current report action but leaves lifecycle projection unconsumed;
- all partial/full sells run before buys and any sell blocks a same-symbol buy.

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
  tests/test_trend_market_controller.py -k 'partial or execution_completed or valid_report or sell'
```

Expected: `FAIL`; the controller accepts only `BUY` and `SELL_ALL`.

- [ ] **Step 2: Write failing full-exit upgrade tests**

In `tests/test_trend_review.py`, prove:

```text
partial intent active + protection trigger -> record position_zero reason, no overlap
partial order terminal + protection trigger -> sell authoritative live remainder
partial target filled + protection trigger -> sell authoritative live remainder
partial intent uncertain + protection trigger -> urgent/manual state, no overlap
manual resolution of uncertain attempt -> sell current live remainder
```

List sell orders from the earliest relevant lifecycle/action date, not only the new execution date, so a prior-day active or ambiguous order cannot be overlapped.

- [ ] **Step 3: Implement explicit sell-goal completion**

For `SELL_PARTIAL`, controller completion requires either:

```text
status=below_lot and sell_goal=partial_30
or status=filled and sell_goal=partial_30 and filled_qty >= lifecycle_target_qty
```

For `SELL_ALL` or a protection upgrade, completion still requires an authoritative zero-position observation (`position_zero_confirmed`) under `sell_goal=position_zero`. Never let a generic `filled` partial event satisfy that check.

- [ ] **Step 4: Implement no-overlap full-exit upgrade**

When `execute_trend_review_stop` arrives:

1. append one `reason_added`/goal-upgrade fact with the protection event ID;
2. inspect all matching lifecycle sell intents and broker orders;
3. return `uncertain` or `submitted` without another order while an earlier order is active/ambiguous;
4. once terminal, read the authoritative current position and create the next numbered market-sell attempt for the entire remainder;
5. record zero-position observation as the only full-exit terminal fact.

Have `_run_stop` return the execution result. If a `position_zero` upgrade is blocked by an uncertain prior order, use the existing controller notification deduplication to send an urgent Feishu/manual message. Do not add a voice message; the protection trigger already owns the existing urgent protection notification.

- [ ] **Step 5: Verify controller GREEN and commit**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
  tests/test_trend_review.py tests/test_trend_market_controller.py
```

Expected: both files `PASS`, including active/uncertain no-overlap and full-exit escalation.

Commit:

```bash
git add src/open_trader/trend_review.py src/open_trader/trend_market_controller.py \
  tests/test_trend_review.py tests/test_trend_market_controller.py
git commit -m "feat: reconcile partial and full trend exits"
```

---

### Task 5: Keep one complete trade and the existing sample identity

**Files:**
- Modify: `src/open_trader/trend_api_stats.py:840-940`
- Test: `tests/test_trend_api_stats.py:200-280`
- Test: `tests/test_trend_api_fill_sync.py:380-510`
- Test: `tests/test_a_share_trend.py`

**Interfaces:**
- Map both `SELL_ALL` and `SELL_PARTIAL` frozen actions to fill side `sell` for actual attribution.
- Preserve the existing round builder: a round closes only when cumulative position quantity reaches zero and aggregates all orders/fees.

- [ ] **Step 1: Write failing partial-attribution tests**

Create a frozen report with `SELL_PARTIAL`, one matching actual sell fill, and no final exit. Assert it is attributed to the unchanged strategy ID/version but produces no closed round. Add the final `SELL_ALL` fill on a later date and assert exactly one round containing all buy, partial-sell, final-sell, and fee facts.

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
  tests/test_trend_api_stats.py tests/test_trend_api_fill_sync.py \
  -k 'partial or attribution or closed_round'
```

Expected: the `SELL_PARTIAL` actual fill is initially unattributed because the action map recognizes only `SELL_ALL`.

- [ ] **Step 2: Add the one mapping and identity regression assertions**

Use:

```python
side = {
    "BUY": "buy",
    "SELL_ALL": "sell",
    "SELL_PARTIAL": "sell",
}.get(str(action.get("action") or ""))
```

Add assertions that a newly generated report still has the prior `strategy_id`, `strategy_version == "v4"`, unchanged Kelly sample scope, and no drawdown rebase/unlock write.

- [ ] **Step 3: Verify stats GREEN and commit**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
  tests/test_trend_api_stats.py tests/test_trend_api_fill_sync.py \
  tests/test_a_share_trend.py -k 'partial or kelly or drawdown or strategy_snapshot'
```

Expected: selected tests `PASS`; the partial fill leaves the round open and final zero closes one round.

Commit:

```bash
git add src/open_trader/trend_api_stats.py tests/test_trend_api_stats.py \
  tests/test_trend_api_fill_sync.py tests/test_a_share_trend.py
git commit -m "fix: keep partial exits in complete trade samples"
```

---

### Task 6: Render the action accurately and update the discipline

**Files:**
- Modify: `src/open_trader/a_share_trend.py:2390-2890,3030-3090`
- Modify: `src/open_trader/dashboard.py:850-940,1430-1885,1960-2030`
- Modify: `src/open_trader/dashboard_static/dashboard.js:2050-2650,2720-2740`
- Modify: `src/open_trader/dashboard_acceptance.py:1700-1910,2300-2350`
- Modify: `纪律.md`
- Modify: `CONTEXT.md`
- Modify: `README.md:590-625`
- Test: `tests/test_a_share_trend.py`
- Test: `tests/test_dashboard.py`
- Test: `tests/test_dashboard_web.py`
- Test: `tests/test_dashboard_acceptance.py`

**Interfaces:**
- Add `ACTION_LABELS["SELL_PARTIAL"] = "止盈减仓 30%"` and `REASON_LABELS["overheat_take_profit"] = "沸腾/开香槟过热止盈"`.
- Keep `sell_actions` as the existing list, but preserve each row's own `action`; do not create a parallel Dashboard endpoint or table.
- Project execution `sell_goal`, lifecycle target, filled quantity, and remaining quantity.

- [ ] **Step 1: Write failing report and Dashboard projection tests**

Assert frozen JSON, Markdown, and Feishu text show `止盈减仓 30%`, trigger signals, estimated simulation quantity, lot size, active line, warnings, and `below_lot`/execution status. Assert `SELL_ALL` still says `全部卖出`.

Add backend tests that:

- include both partial and full actions in `sell_actions`;
- map ledger facts by symbol/side while retaining `sell_goal`;
- label partial report/order numbers as `模拟预计数量`/`模拟目标数量`;
- provide manual real-account guidance `按实盘下单时持仓的 30% 向下取整` rather than claiming a real order quantity or completion;
- leave trade-stat/review-page fields unchanged and add no sample-mix disclosure.

- [ ] **Step 2: Write failing JavaScript and acceptance tests**

Use the existing `run_dashboard_js` harness with one `SELL_PARTIAL` and one `SELL_ALL` row. Assert visible text contains both distinct action labels, partial target/fill/remainder, simulation labeling, and the real-account manual instruction at 375px and desktop widths.

Update acceptance's copied validators so `SELL_PARTIAL` is a known action only when its strict fields are valid. Change stage checks from one hard-coded `全部卖出` label to the action label on each item.

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
  tests/test_a_share_trend.py tests/test_dashboard.py tests/test_dashboard_web.py \
  tests/test_dashboard_acceptance.py -k 'partial or trend_report or discipline or action_stage'
```

Expected: `FAIL`; current backend filters partial actions and the browser hard-codes all sell rows as full exits.

- [ ] **Step 3: Reuse the existing sell stage with per-item labels**

In Python, include `item.get("action") in {"SELL_ALL", "SELL_PARTIAL"}` in sell projections and action execution maps. In JavaScript, derive the row label from `item.action`:

```javascript
function trendSellActionLabel(item) {
  return item?.action === "SELL_PARTIAL" ? "止盈减仓 30%" : "全部卖出";
}
```

Extend `trendSimulationDeviation` so partial positions are never expected to be zero. Use execution target/fill facts when available; otherwise display `待执行/待核对`, not `漏卖` or `已跟随`. Real-account rows remain read-only/manual and must not synthesize fill confirmation.

Reuse the current responsive sell-stage markup and CSS. Add CSS only if the existing 375px test proves text overflow; do not preemptively add a new layout.

- [ ] **Step 4: Replace the obsolete visible discipline**

Replace `沸腾或开香槟只上移保护线，不减仓` with concise rules covering:

```text
沸腾或开香槟：每个完整持仓生命周期首次出现时止盈减仓 30%
两种信号合并为一次；连续或先后出现不重复减仓
按下单时模拟盘实际持仓向下取整：A 股 100 股、美股 1 股、港股 Futu 整手
剩余仓位继续受 5 日低点活动保护线和强制清仓条件约束
活动保护线、危险、离开右侧、A 股温度转平优先全部卖出
趋势动物 API 没有“波动率放大”字段，不本地推测
```

In `纪律.md`, also record unchanged sample identity, no replenishment, real-account manual execution, and the per-change approval rule for future sample resets. In `CONTEXT.md`, remove the conflicting blanket bans on partial exits and mandatory sample resets; identify this change as an explicitly approved compatible revision. In `README.md`, clarify that `abandon` ends only the current action and a later explicit overheat report may resume the original lifecycle remainder.

- [ ] **Step 5: Verify presentation GREEN and commit**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
  tests/test_a_share_trend.py tests/test_dashboard.py tests/test_dashboard_web.py \
  tests/test_dashboard_acceptance.py
```

Expected: all four files `PASS`, including desktop/mobile action text and no new voice/sample disclosure.

Commit:

```bash
git add src/open_trader/a_share_trend.py src/open_trader/dashboard.py \
  src/open_trader/dashboard_static/dashboard.js \
  src/open_trader/dashboard_acceptance.py 纪律.md CONTEXT.md README.md \
  tests/test_a_share_trend.py tests/test_dashboard.py tests/test_dashboard_web.py \
  tests/test_dashboard_acceptance.py
git commit -m "feat: present partial trend exits and discipline"
```

---

### Task 7: Run focused integration and full automated verification

**Files:**
- Test: all changed test files
- Test: full `tests/`

- [ ] **Step 1: Run the cross-module focused suite**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
  tests/test_a_share_trend.py \
  tests/test_market_trend.py \
  tests/test_trend_review.py \
  tests/test_trend_market_controller.py \
  tests/test_trend_api_stats.py \
  tests/test_trend_api_fill_sync.py \
  tests/test_dashboard.py \
  tests/test_dashboard_web.py \
  tests/test_dashboard_acceptance.py
```

Expected: every selected test passes with an exact final pass count and no warnings/errors hidden by reruns.

- [ ] **Step 2: Run the full suite**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q
```

Expected: all tests pass. Baseline before implementation was `3025 passed in 74.84s`; the final count must be higher because new behavior tests were added.

- [ ] **Step 3: Inspect the complete diff and frozen-history safety**

```bash
git diff --check
git status --short
git diff fffe37f...HEAD --stat
git diff fffe37f...HEAD -- \
  src/open_trader/a_share_trend.py \
  src/open_trader/trend_review.py \
  src/open_trader/trend_market_controller.py \
  src/open_trader/trend_api_stats.py
git status --short -- /Users/ray/projects/open_trader/reports \
  /Users/ray/projects/open_trader/data/trend_review/ledgers
```

Expected: no whitespace errors, no unexpected file, no existing frozen report rewrite, and no source behavior outside the approved scope.

- [ ] **Step 4: Commit any test-only integration adjustments**

Only if Step 1 or 2 required test fixture changes after the preceding commits:

```bash
git add tests
git commit -m "test: cover trend partial exit integration"
```

Do not create an empty commit.

---

### Task 8: Pass acceptance, run real workflows, and deploy the accepted SHA

**Files:**
- Runtime: shared `/Users/ray/projects/open_trader/config/daily_premarket.env`
- Runtime: shared `/Users/ray/projects/open_trader/data` and `/Users/ray/projects/open_trader/reports`
- Runtime: `logs/daily_premarket/launchd-trend-controller-*.{out,err}.log`
- Runtime: `/tmp/open_trader_dashboard_8766.log`

- [ ] **Step 1: Prepare the worktree interpreter link and record old processes**

The Makefile and launchd installer expect `.venv` inside the worktree. Create only an ignored symlink if absent:

```bash
test -e .venv || ln -s /Users/ray/projects/open_trader/.venv .venv
git status --short
launchctl list | rg 'com\.open-trader\.(trend|premarket)' || true
pgrep -f 'open_trader trend-market run|open_trader dashboard' | \
  xargs ps -o pid,lstart,command -p 2>/dev/null || true
screen -ls | rg 'open_trader_dashboard_8766' || true
```

Expected: the source tree is clean; process output records every old PID/start time before restart.

- [ ] **Step 2: Inspect the currently deployed controller state without mutation**

Do not run new report/execution code against shared accounts before it passes acceptance. Record the current read-only state instead:

```bash
for market in CN HK US; do
  PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python \
    -m open_trader trend-market status --market "$market" \
    --config /Users/ray/projects/open_trader/config/daily_premarket.env
done
```

Expected: three valid JSON status documents. This step submits no order and writes no report.

- [ ] **Step 3: Confirm the final source state and capture its candidate SHA**

```bash
git status --short
test -z "$(git status --short)"
CANDIDATE_SHA="$(git rev-parse HEAD)"
printf '%s\n' "$CANDIDATE_SHA" | \
  tee /tmp/open_trader_trend_overheat_candidate_sha
```

Expected: clean tree and one full 40-character SHA. Do not modify source after this point except to fix an acceptance `FAIL`, in which case commit the fix, rerun focused/full tests, and overwrite the candidate SHA.

- [ ] **Step 4: Run the one final Dashboard acceptance gate**

```bash
make acceptance && cp /tmp/open_trader_trend_overheat_candidate_sha \
  /tmp/open_trader_trend_overheat_accepted_sha
```

Expected terminal line: `PASS`.

- If output is `FAIL`, diagnose with the failing assertion/log, make the smallest root-cause fix, rerun focused/full verification, commit, and rerun `make acceptance`.
- If output is `BLOCKED`, stop and report the unavailable browser/external environment. Do not substitute curl, fixtures, mocks, screenshots, or unit tests.
- Do not proceed to deployment for any result other than `PASS`.

- [ ] **Step 5: Stop old controllers, run each accepted workflow once, and redeploy**

```bash
ACCEPTED_SHA="$(cat /tmp/open_trader_trend_overheat_accepted_sha)"
export ACCEPTED_SHA
test "$(git rev-parse HEAD)" = "$ACCEPTED_SHA"
test -z "$(git status --short)"

scripts/uninstall_daily_premarket_launchd.sh --trend-only --market all
if pgrep -f 'open_trader trend-market run' >/dev/null; then
  echo 'old trend controller process remains' >&2
  exit 1
fi

PYTHONPATH=src .venv/bin/python - <<'PY'
from dataclasses import replace
from pathlib import Path
from open_trader.daily_premarket import load_env_config
from open_trader.trend_market_controller import run_trend_market_controller

worktree = Path.cwd().resolve()
config = replace(
    load_env_config(Path('/Users/ray/projects/open_trader/config/daily_premarket.env')),
    repo=worktree,
    python=(worktree / '.venv/bin/python').resolve(),
)
for market in ('CN', 'HK', 'US'):
    result = run_trend_market_controller(config, market, once=True)
    print(market, result['phase'], result['blocker'], result['git_sha'])
PY

scripts/install_daily_premarket_launchd.sh \
  --config /Users/ray/projects/open_trader/config/daily_premarket.env \
  --trend-only --market all

screen -S open_trader_dashboard_8766 -X quit 2>/dev/null || true
screen -dmS open_trader_dashboard_8766 zsh -lc \
  'cd /Users/ray/projects/open_trader/.worktrees/trend-overheat-partial-profit && exec env PYTHONPATH=src .venv/bin/python -u -m open_trader dashboard --portfolio /Users/ray/projects/open_trader/data/latest/portfolio.csv --data-dir /Users/ray/projects/open_trader/data --reports-dir /Users/ray/projects/open_trader/reports --config /Users/ray/projects/open_trader/config/daily_premarket.env --poll-seconds 5 --host 127.0.0.1 --port 8766 >> /tmp/open_trader_dashboard_8766.log 2>&1'
```

Expected: each accepted controller directly reconciles its real Futu SIMULATE account/calendar/report path without revising an old report, every returned SHA matches the accepted SHA, and no blocker/order ambiguity remains. The installer then loads exactly CN/HK/US controllers from this worktree; the Dashboard starts from the same SHA.

- [ ] **Step 6: Verify fresh PID/cwd/SHA/heartbeat/log/HTTP evidence**

```bash
ACCEPTED_SHA="$(cat /tmp/open_trader_trend_overheat_accepted_sha)"
export ACCEPTED_SHA
PYTHONPATH=src .venv/bin/python - <<'PY'
from datetime import datetime
import json
import os
from pathlib import Path
import time

accepted_sha = os.environ['ACCEPTED_SHA']
worktree = '/Users/ray/projects/open_trader/.worktrees/trend-overheat-partial-profit'
root = Path('/Users/ray/projects/open_trader/data/trend_controller')

def read(market):
    return json.loads((root / market / 'status.json').read_text(encoding='utf-8'))

before = {market: read(market) for market in ('CN', 'HK', 'US')}
time.sleep(10)
for market, previous in before.items():
    current = read(market)
    pid = int(current['pid'])
    os.kill(pid, 0)
    assert current['working_directory'] == worktree
    assert current['git_sha'] == accepted_sha
    assert datetime.fromisoformat(current['heartbeat_at']) > datetime.fromisoformat(
        previous['heartbeat_at']
    )
    print(market, pid, current['git_sha'], current['heartbeat_at'], current['phase'])
PY

launchctl list | rg 'com\.open-trader\.trend-market-controller\.(cn|hk|us)'
pgrep -f 'open_trader trend-market run|open_trader dashboard' | \
  xargs ps -o pid,lstart,command -p
tail -n 100 logs/daily_premarket/launchd-trend-controller-*.out.log
tail -n 100 logs/daily_premarket/launchd-trend-controller-*.err.log
tail -n 100 /tmp/open_trader_dashboard_8766.log
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8766/
curl -sS http://127.0.0.1:8766/api/dashboard | .venv/bin/python -m json.tool >/dev/null
```

Expected: three live controller PIDs with advancing heartbeats, accepted worktree/SHA, fresh post-restart timestamps/logs, Dashboard PID from the accepted worktree, HTTP `200`, and valid Dashboard JSON.

- [ ] **Step 7: Hand off the review URL**

Only after all preceding checks pass, report the exact test counts, `make acceptance` `PASS`, accepted SHA, new PIDs/timestamps, and review URL:

```text
http://127.0.0.1:8766/
```

Do not claim that a real account was traded. State that Futu SIMULATE remains automatic and all three real accounts remain manually executed by the user.
