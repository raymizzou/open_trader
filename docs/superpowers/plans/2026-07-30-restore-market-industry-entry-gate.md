# Restore Cross-Market Trend Entry Discipline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore the approved common CN/US/HK entry discipline, regenerate the US and HK reports, and prove below-warm industries cannot appear in buy-related rows.

**Architecture:** Restore the shared decision at the existing `_candidate_reasons()` seam and preserve market-specific pools, currencies, lots, and sessions. Restore the existing two-stage US/HK industry snapshot load before candidate evaluation, then reuse the resulting temperature facts for candidate filtering; keep the later industry breadth context as ordering and display evidence.

**Tech Stack:** Python 3.12 stdlib, existing Trend Animals/Futu clients, pytest, existing report controllers and Dashboard acceptance.

## Global Constraints

- Use the approved design in `docs/superpowers/specs/2026-07-24-unified-trend-discipline-design.md`.
- Current CN v10, US v8, and HK v8 entries share: 温→热/沸, strength `>= 95`, industry temperature in 温/热/沸, phase in 谷雨/立夏/夏至, CNY-equivalent market cap `>= 100` hundred-million, CNY-equivalent amount `>= 2` hundred-million, right side, tradable, no danger, matching date, not held, right-side days present, and ATR14 present.
- Preserve historical US/HK v4, v5, v6, and v7 replay and parameter-hash behavior.
- Preserve market-specific pools, exchange scope, lot sizes, account currencies, and buy windows.
- Missing or failed industry temperature data fails new entries closed but must not suppress holding exits.
- Do not add dependencies, configuration, a new policy module, or unrelated refactors.
- Update and commit `CHANGELOG.md` before any merge to `main`.
- Run `make acceptance` only on the final source SHA.

---

### Task 1: Restore the shared current-version decision contract

**Files:**
- Modify: `src/open_trader/a_share_trend.py`
- Test: `tests/test_a_share_trend.py`

**Interfaces:**
- Extends: `_candidate_reasons(..., strategy_version: str | None, cny_per_local_currency: Decimal | None) -> list[str]`
- Extends: `build_candidate_list(..., strategy_version: str | None, cny_per_local_currency: Decimal | None) -> CandidateDecision`
- Extends: `_candidate_signal(..., strategy_version: str | None, cny_per_local_currency: Decimal | None) -> dict[str, object]`
- Produces: current US/HK snapshots with the same common entry parameters as CN plus frozen currency conversion facts.

- [ ] **Step 1: Add current-version snapshot and behavioral gate tests**

Add parameterized tests for US v8 and HK v8. Assert their strategy snapshots contain literal shared values:

```python
{
    "temperature_transition": {"from": ["温"], "to": ["热", "沸"]},
    "min_strength": "95",
    "allowed_industry_temperatures": ["温", "热", "沸"],
    "allowed_phases": ["谷雨", "立夏", "夏至"],
    "min_market_cap_cny_100m": "100",
    "min_amount_cny_100m": "2",
    "requires_right_side_days": True,
}
```

Add a parameterized decision test whose otherwise-valid US/HK candidate changes one field at a time and is excluded for:

```python
[
    ({"temperature_prev": "平"}, "temperature_transition_not_entry"),
    ({"strength": Decimal("94.9")}, "strength_below_95"),
    ({"industry_temperature": "凉"}, "industry_temperature_not_hot"),
    ({"phase": "小暑"}, "phase_after_summer_solstice"),
    ({"market_cap": Decimal("0")}, "market_cap_below_100_cny"),
    ({"amount": Decimal("0")}, "amount_below_2_cny"),
    ({"days": None}, "right_side_days_missing"),
]
```

Name the mutation caught: reverting `_candidate_reasons()` to its old non-CN branch must make these tests fail.

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH=src \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_a_share_trend.py \
  -k 'current_market_entry or current_market_strategy_snapshot' -q
```

Expected: failures showing US/HK snapshots retain old `> 90`/`< 10 days` parameters and cold-industry candidates remain eligible.

- [ ] **Step 3: Restore the minimal shared decision**

Treat CN plus the current US/HK v8 versions as shared discipline. Preserve v4,
v5, v6, and v7 snapshots because their parameter hashes and frozen reports are
historical audit identities:

```python
shared_discipline = market == "CN" or (
    market in {"US", "HK"} and strategy_version == "v8"
)
```

For shared discipline, reuse the existing CN checks and multiply US/HK raw local-currency `market_cap` and `amount` by the snapshot-frozen CNY rate before comparing. Preserve the old branch for US/HK v4.

Pass the resolved version and rate through `build_report()`, `build_candidate_list()`, `_candidate_reasons()`, and `_candidate_signal()`. Add only the existing audit facts:

```python
{
    "market_value_currency": "USD" or "HKD",
    "cny_per_local_currency": rate,
    "market_cap_cny_100m": item.market_cap * rate,
    "amount_cny_100m": item.amount * rate,
}
```

- [ ] **Step 4: Run the new and focused decision tests to verify GREEN**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH=src \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_a_share_trend.py \
  -k 'current_market_entry or current_market_strategy_snapshot or candidate or strategy_snapshot' -q
```

Expected: PASS.

---

### Task 2: Restore US/HK industry-temperature input and report exclusion

**Files:**
- Modify: `src/open_trader/a_share_trend.py`
- Modify: `src/open_trader/market_trend.py`
- Test: `tests/test_market_trend.py`

**Interfaces:**
- Restores: `load_industry_temperatures(api: object, *, tm_ids: Sequence[int], expected_date: str) -> tuple[list[Mapping[str, object]], dict[int, str | None]]`
- Consumes: `evaluate_candidate(..., industry_temperature=...)`
- Produces: regenerated reports where below-warm industries exist only in excluded-candidate audit evidence.

- [ ] **Step 1: Add a report-level regression test**

Use the existing market report fake boundary. Return one otherwise-valid US candidate with a valid “凉” industry snapshot and enough cash/position capacity. Assert:

```python
assert payload["strategy_judgments"]["formal_actions"] == []
assert payload["strategy_judgments"]["risk_skips"] == []
assert payload["strategy_judgments"]["top10_candidates"] == []
assert payload["excluded"]["GRMN"] == ["industry_temperature_not_hot"]
```

Add the same behavioral case for HK. Name the mutation caught: omitting `industry_temperature=` in the market runner must make the candidate reappear in buy-related rows.

- [ ] **Step 2: Run the report test and verify RED**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH=src \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_market_trend.py -k 'below_warm_industry' -q
```

Expected: FAIL because the current runner passes `industry_temperature=None` and the old market branch admits the candidate.

- [ ] **Step 3: Restore the existing two-stage loader and fail-closed behavior**

Restore `load_industry_temperatures()` using `A_SHARE_INDUSTRY_FIELDS`, exact requested IDs, exact date validation, duplicate rejection, and known-temperature normalization. In `market_trend.py`, load unique `industryTmId` values before candidate evaluation and pass the mapped temperature to both candidate and holding parsers.

If the whole industry request fails, set the existing critical-data reason so holding decisions still run and all entries pause. Freeze the request/response and cost evidence using existing report evidence fields; do not introduce a second evidence format.

- [ ] **Step 4: Run market and shared trend tests to verify GREEN**

Run from the root runtime-data context:

```bash
PYTHONSAFEPATH=1 \
PYTHONPATH=/Users/ray/projects/open_trader/.worktrees/restore-market-industry-gate/src \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  .worktrees/restore-market-industry-gate/tests/test_a_share_trend.py \
  .worktrees/restore-market-industry-gate/tests/test_market_trend.py -q
```

Expected: all tests pass.

---

### Task 3: Operator proof, report regeneration, and acceptance

**Files:**
- Modify: `CHANGELOG.md`

**Interfaces:**
- Produces: current US/HK report JSON, Markdown, Dashboard projection, and exact-SHA runtime proof.

- [ ] **Step 1: Add the dated changelog entry**

Record the restored common entry discipline, the merge-conflict regression cause, the report regeneration result, and exact test/acceptance evidence. Commit before any merge.

- [ ] **Step 2: Review the branch diff**

Run the project `code-review` skill against baseline `481a72c`. Resolve all standards/spec findings and rerun focused tests.

- [ ] **Step 3: Run the full test suite**

Use the worktree source with main-workspace runtime data. Expected: zero failures.

- [ ] **Step 4: Regenerate real US and HK reports without submitting orders**

Run the existing US/HK report commands in no-submit/revision mode with worktree source and main runtime configuration. Verify:

```text
US: GRMN absent from formal_actions, risk_skips, and top10_candidates
HK: below-warm candidates absent from formal_actions, risk_skips, and top10_candidates
US/HK strategy snapshots: common entry rows equal CN except documented market mechanics
Excluded audit: rejected symbols retain deterministic exclusion reasons
```

- [ ] **Step 5: Commit the final source state**

Commit source, tests, regenerated tracked report revisions if the workflow creates them, and the completed changelog entry. Record the exact SHA.

- [ ] **Step 6: Run the final Dashboard gate**

Run `make acceptance` once on the final SHA. Only `PASS` permits completion.

- [ ] **Step 7: Deploy the accepted SHA and verify live state**

Redeploy Dashboard and CN/HK/US controllers from the exact accepted SHA. Verify new PID, cwd, SHA, fresh logs, advancing heartbeats, HTTP 200, and the Dashboard report rows. Provide the review URL.
