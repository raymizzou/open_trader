# Trend Animals Holding-Industry Cost Reduction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop paid component/member expansion for holding-only industries while preserving candidate decisions and every same-input Dashboard field/status except the lower API-cost value.

**Architecture:** Extend the existing `IndustryContext` contract with one backward-compatible provenance flag, then reuse `calculate_industry_context()` in a state-only mode for holding-only industries. Keep the candidate-industry branch untouched, delete the holding component/member branch, and prove the reduced call scope with the frozen 2026-07-31 ledger plus semantic UI parity tests.

**Tech Stack:** Python 3.12, frozen dataclasses, `Decimal`, JSON history/evidence, pytest, existing Node VM Dashboard renderer tests, launchd, and the repository `make acceptance` gate.

## Global Constraints

- Work only in `/Users/ray/projects/open_trader/.worktrees/trend-animals-holding-industry-cost` on `fix/trend-animals-holding-industry-cost`.
- Do not change entry gates, exit rules, risk controls, candidate-industry breadth, candidate ordering for identical current inputs, formal actions, or holding decisions.
- Do not add dependencies, configuration, caches, refresh schedules, report-schema versions, columns, labels, badges, or UI fallback states.
- `member_breadth_collected` is audit-only. Missing historical/report values mean `true`; new holding-only contexts explicitly store `false`.
- A holding-only state failure stays local and never falls back to paid component/member breadth calls.
- For identical inputs, only holding-only local breadth values, `member_breadth_collected`, API facts/costs, evidence hashes, and generation timestamps may differ.
- The frozen ledger must prove 22 → 4 component calls, 4,821 → 1,211 member snapshots, and exactly 10.830 fewer priced member-field units.
- The accepted future exception is that a holding-only industry later becoming eligible may make that report use `context_current_only` ordering.
- Run focused tests during development. Run `make acceptance` only after source, tests, changelog, and candidate deployment are final.
- Only `make acceptance` `PASS` permits review handoff. After `PASS`, redeploy the exact accepted SHA and verify new PIDs, cwd, SHA, fresh logs/status, and HTTP 200.
- Do not capture screenshots; the user did not request them.

## File Map

- Modify `src/open_trader/trend_industry_context.py`: provenance flag, state-only calculation, history compatibility, and state/aggregate prior attachment.
- Modify `src/open_trader/a_share_trend.py`: delete holding-only component/member calls and build state-only holding contexts.
- Modify `src/open_trader/dashboard.py`: accept the optional audit flag in frozen reports without projecting a new UI field.
- Modify `src/open_trader/trend_review.py`: preserve the optional flag during evidence replay.
- Modify `tests/test_trend_industry_context.py`: contract, legacy history, state-only validation, and prior-comparison regressions.
- Modify `tests/test_a_share_trend.py`: paid-call boundary and frozen 2026-07-31 ledger regressions.
- Modify `tests/test_dashboard.py`: frozen-report validation/projection compatibility.
- Modify `tests/test_dashboard_web.py`: same-input rendered industry fields/status parity.
- Modify `tests/test_trend_review.py`: evidence replay compatibility.
- Modify `CHANGELOG.md`: dated operator-facing cost and verification entry.

---

### Task 1: Add a Backward-Compatible Breadth Provenance Contract

**Files:**

- Modify: `src/open_trader/trend_industry_context.py:13-38,476-640`
- Modify: `src/open_trader/dashboard.py:1540-1670`
- Modify: `src/open_trader/trend_review.py:5580-5665`
- Test: `tests/test_trend_industry_context.py`
- Test: `tests/test_dashboard.py`
- Test: `tests/test_trend_review.py`

**Interfaces:**

- Consumes: existing `IndustryContext`, `_context_to_mapping()`, `_context_from_mapping()`, Dashboard frozen-report validation, and evidence replay.
- Produces: `IndustryContext.member_breadth_collected: bool = True`; missing JSON fields deserialize as `True`; explicit booleans round-trip through history, Dashboard projection, and replay.

- [ ] **Step 1: Write failing compatibility tests**

Add loader and no-rewrite history tests for a missing new field, a Dashboard test that projects an explicit `false`, and extend the existing evidence replay test with an explicit `false`:

```python
def test_history_loader_defaults_missing_member_breadth_flag_to_true(
    tmp_path: Path,
) -> None:
    path = write_industry_context_history(
        tmp_path,
        market="CN",
        generated_at="2026-07-23T18:00:00+08:00",
        strategy_version="v10",
        contexts=(_valid_context(as_of_date="2026-07-23"),),
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["industries"][0].pop("member_breadth_collected")
    path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = load_latest_prior_context(
        tmp_path, market="CN", before_date="2026-07-24"
    )

    assert loaded[700001].member_breadth_collected is True
```

Prove a same-date idempotent write does not mutate a legacy file merely because the new provenance field is absent:

```python
def test_history_writer_does_not_rewrite_when_only_breadth_flag_is_missing(
    tmp_path: Path,
) -> None:
    context = _valid_context(as_of_date="2026-07-23")
    path = write_industry_context_history(
        tmp_path,
        market="CN",
        generated_at="2026-07-23T18:00:00+08:00",
        strategy_version="v10",
        contexts=(context,),
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["industries"][0].pop("member_breadth_collected")
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    legacy_bytes = path.read_bytes()

    assert write_industry_context_history(
        tmp_path,
        market="CN",
        generated_at="2026-07-23T19:00:00+08:00",
        strategy_version="v10",
        contexts=(context,),
    ) == path
    assert path.read_bytes() == legacy_bytes
```

In `test_dashboard_accepts_frozen_provider_aggregate_industry_ratios`, set and assert:

```python
payload["industry_contexts"][0]["member_breadth_collected"] = False
assert projected["industry_contexts"][0]["member_breadth_collected"] is False
```

In `test_rebuild_preserves_frozen_industry_context_ordering_facts`, construct the context with `member_breadth_collected=False` and retain the existing full equality assertion between rebuilt and source contexts.

- [ ] **Step 2: Run the tests and verify the expected failures**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_trend_industry_context.py::test_history_loader_defaults_missing_member_breadth_flag_to_true \
  tests/test_trend_industry_context.py::test_history_writer_does_not_rewrite_when_only_breadth_flag_is_missing \
  tests/test_dashboard.py::test_dashboard_accepts_frozen_provider_aggregate_industry_ratios \
  tests/test_trend_review.py::test_rebuild_preserves_frozen_industry_context_ordering_facts -q
```

Expected: FAIL because `IndustryContext` has no `member_breadth_collected` field and frozen-report/replay readers do not accept it.

- [ ] **Step 3: Implement the minimal optional contract**

Add the defaulted dataclass field:

```python
@dataclass(frozen=True)
class IndustryContext:
    industry_tm_id: int
    industry: str
    as_of_date: str
    component_count: int
    snapshot_count: int
    tradable_count: int
    valid_count: int
    right_count: int
    snapshot_coverage: Decimal
    right_state_coverage: Decimal
    right_share: Decimal | None
    warm_to_hot_count: int
    temperature: str | None
    strength: Decimal | None
    valid: bool
    invalid_reasons: tuple[str, ...]
    member_breadth_collected: bool = True
    aggregate_right_count_ratio: Decimal | None = None
```

In `_context_from_mapping()`, treat the flag like the existing optional aggregate fields and reject non-booleans:

```python
optional_fields = aggregate_fields | {"member_breadth_collected"}
# Exclude optional_fields from the required-field check.
member_breadth_collected = row.get("member_breadth_collected", True)
if not isinstance(member_breadth_collected, bool):
    return None
```

Pass `member_breadth_collected=member_breadth_collected` into `IndustryContext(...)`. Before the existing aggregate-field enrichment branch, recognize the case where only the new field is missing and return the existing path without writing:

```python
same_history_identity = (
    isinstance(existing, Mapping)
    and existing.get("schema_version") == _HISTORY_SCHEMA_VERSION
    and existing.get("market") == market_name
    and existing.get("as_of_date") == as_of_date
)
existing_industries = (
    existing.get("industries") if isinstance(existing, Mapping) else None
)
flag_legacy_rows = [
    {
        key: value
        for key, value in row.items()
        if key != "member_breadth_collected"
    }
    for row in payload["industries"]
]
if same_history_identity and existing_industries == flag_legacy_rows:
    return path
```

Keep the current aggregate-field enrichment behavior. For history old enough to lack both aggregate fields and the new flag, its existing aggregate-legacy comparison may strip both missing optional groups before doing the already-supported enrichment write. Do not rewrite when the new flag is the only missing field. Every other same-date shape keeps the existing conflict error.

In Dashboard frozen-context validation, allow `member_breadth_collected` as an optional key and require a boolean when present:

```python
optional_context_keys = aggregate_ratio_keys | {"member_breadth_collected"}
if set(context) - context_keys - optional_context_keys:
    return False
if "member_breadth_collected" in context and type(
    context["member_breadth_collected"]
) is not bool:
    return False
```

In evidence replay, validate and pass the optional flag:

```python
member_breadth_collected = raw.get("member_breadth_collected", True)
if not isinstance(member_breadth_collected, bool):
    raise ValueError
```

Pass `member_breadth_collected=member_breadth_collected` to the existing validated `IndustryContext(...)` constructor call.

- [ ] **Step 4: Run compatibility tests**

Run the command from Step 2.

Expected: `4 passed`.

- [ ] **Step 5: Run the complete contract consumer suites**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_trend_industry_context.py tests/test_dashboard.py \
  tests/test_trend_review.py -q
```

Expected: PASS with no old-history, frozen-report, or evidence replay regression.

- [ ] **Step 6: Commit the contract slice**

```bash
git add src/open_trader/trend_industry_context.py src/open_trader/dashboard.py \
  src/open_trader/trend_review.py tests/test_trend_industry_context.py \
  tests/test_dashboard.py tests/test_trend_review.py
git commit -m "feat: mark collected industry member breadth"
```

---

### Task 2: Support Valid State-Only Holding Contexts

**Files:**

- Modify: `src/open_trader/trend_industry_context.py:40-175,230-275,640-690`
- Test: `tests/test_trend_industry_context.py`

**Interfaces:**

- Consumes: `IndustryContext.member_breadth_collected` from Task 1.
- Produces: `calculate_industry_context(..., member_breadth_collected: bool = True)`; state-only contexts retain temperature/strength/aggregate history without claiming local breadth.

- [ ] **Step 1: Write failing state-only and history tests**

```python
def test_state_only_context_is_valid_without_member_coverage() -> None:
    context = calculate_industry_context(
        industry_tm_id=700001,
        industry="工业",
        expected_date="2026-07-24",
        component_tm_ids=(),
        member_rows=(),
        industry_row={
            "tmId": 700001,
            "asOfDate": "2026-07-24",
            "trendTemperatureCurr": "热",
            "trendStrengthLocalCurr": "90",
            "TrendRightSideCountRatio": "0.191",
            "TrendRightSideMktCapRatio": "0.650",
        },
        warm_to_hot_count=0,
        member_breadth_collected=False,
    )

    assert context.member_breadth_collected is False
    assert context.valid is True
    assert context.invalid_reasons == ()
    assert context.component_count == context.valid_count == 0
    assert context.right_share is None
    assert context.aggregate_right_count_ratio == Decimal("0.191")
```

Add a history/attachment regression:

```python
def test_state_only_history_keeps_display_transitions_without_local_breadth(
    tmp_path: Path,
) -> None:
    prior = replace(
        _valid_context(as_of_date="2026-07-23", temperature="温"),
        component_count=0, snapshot_count=0, tradable_count=0,
        valid_count=0, right_count=0,
        snapshot_coverage=Decimal("0"), right_state_coverage=Decimal("0"),
        right_share=None, member_breadth_collected=False,
        aggregate_right_count_ratio=Decimal("0.150"),
        aggregate_right_market_cap_ratio=Decimal("0.600"),
    )
    write_industry_context_history(
        tmp_path, market="CN", generated_at="2026-07-23T18:00:00+08:00",
        strategy_version="v10", contexts=(prior,),
    )
    loaded = load_latest_prior_context(
        tmp_path, market="CN", before_date="2026-07-24"
    )
    current = replace(
        prior, as_of_date="2026-07-24", temperature="热",
        aggregate_right_count_ratio=Decimal("0.191"),
        aggregate_right_market_cap_ratio=Decimal("0.650"),
    )

    [attached] = attach_prior_context((current,), loaded)

    assert attached.temperature_direction == "rising"
    assert attached.prior_aggregate_right_count_ratio == Decimal("0.150")
    assert attached.prior_right_share is None
    assert attached.right_share_change_pp is None
```

- [ ] **Step 2: Run the tests and verify they fail**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_trend_industry_context.py::test_state_only_context_is_valid_without_member_coverage \
  tests/test_trend_industry_context.py::test_state_only_history_keeps_display_transitions_without_local_breadth -q
```

Expected: FAIL because the calculator has no state-only mode, history rejects zero-member contexts, and temperature direction currently depends on local `right_share`.

- [ ] **Step 3: Implement state-only calculation and history validation**

Add the defaulted keyword argument to `calculate_industry_context()` and skip only the two member-coverage reasons when it is false:

```python
def calculate_industry_context(
    *,
    industry_tm_id: int,
    industry: str,
    expected_date: str,
    component_tm_ids: Sequence[int],
    member_rows: Sequence[Mapping[str, object]],
    industry_row: Mapping[str, object] | None,
    warm_to_hot_count: int,
    member_breadth_collected: bool = True,
) -> IndustryContext:
    invalid_reasons: list[str] = []
    if member_breadth_collected:
        if snapshot_coverage < Decimal("0.9"):
            invalid_reasons.append("snapshot_coverage_below_90pct")
        if right_state_coverage < Decimal("0.9"):
            invalid_reasons.append("right_state_coverage_below_90pct")
    if normalized_warm_to_hot_count is None:
        invalid_reasons.append("warm_to_hot_count_invalid")
    if temperature is None:
        invalid_reasons.append("industry_temperature_invalid")
    if strength is None:
        invalid_reasons.append("industry_strength_invalid")
```

Return the flag on the context. In `_context_is_valid_for_history()`, put the current exact breadth checks under the collected branch and add one exact state-only branch:

```python
if context.member_breadth_collected:
    if context.component_count < 10:
        return False
    if not (
        0 <= context.snapshot_count <= context.component_count
        and context.snapshot_coverage >= Decimal("0.9")
        and context.snapshot_coverage
        == Decimal(context.snapshot_count) / Decimal(context.component_count)
    ):
        return False
    if not (
        0 <= context.tradable_count <= context.snapshot_count
        and 0 <= context.valid_count <= context.tradable_count
        and context.valid_count >= 10
        and context.right_state_coverage >= Decimal("0.9")
        and context.right_state_coverage
        == Decimal(context.valid_count) / Decimal(context.tradable_count)
    ):
        return False
    if not (
        0 <= context.right_count <= context.valid_count
        and context.right_share is not None
        and 0 <= context.right_share <= 1
        and context.right_share
        == Decimal(context.right_count) / Decimal(context.valid_count)
    ):
        return False
elif not (
    context.component_count
    == context.snapshot_count
    == context.tradable_count
    == context.valid_count
    == context.right_count
    == 0
    and context.snapshot_coverage == Decimal("0")
    and context.right_state_coverage == Decimal("0")
    and context.right_share is None
):
    return False
```

After the branch, apply the existing warm-count, temperature, and strength checks to both modes. Do not weaken any collected-context condition.

In `attach_prior_context()`, compute `prior_temperature` and `temperature_direction` whenever both temperatures are known. Compute `prior_right_share` and `right_share_change_pp` only when both local shares are present. Keep provider aggregate baselines independent from both branches.

```python
if prior.temperature in temperature_order and context.temperature in temperature_order:
    current_temperature = temperature_order[context.temperature]
    prior_temperature = temperature_order[prior.temperature]
    changes.update(
        prior_temperature=prior.temperature,
        temperature_direction=(
            "rising" if current_temperature > prior_temperature
            else "falling" if current_temperature < prior_temperature
            else "unchanged"
        ),
    )
if prior.right_share is not None and context.right_share is not None:
    changes.update(
        prior_right_share=prior.right_share,
        right_share_change_pp=(context.right_share - prior.right_share)
        * Decimal("100"),
    )
```

- [ ] **Step 4: Run the focused tests**

Run the command from Step 2.

Expected: `2 passed`.

- [ ] **Step 5: Run all industry-context tests**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_trend_industry_context.py -q
```

Expected: PASS; collected candidate contexts retain identical validation and history behavior.

- [ ] **Step 6: Commit state-only semantics**

```bash
git add src/open_trader/trend_industry_context.py tests/test_trend_industry_context.py
git commit -m "feat: retain state-only holding industry context"
```

---

### Task 3: Delete Holding-Only Component and Member Calls

**Files:**

- Modify: `src/open_trader/a_share_trend.py:2397-2655`
- Test: `tests/test_a_share_trend.py:6000-6300`

**Interfaces:**

- Consumes: `calculate_industry_context(..., member_breadth_collected=False)` from Task 2.
- Produces: `collect_industry_contexts()` facts whose component/member scope contains eligible candidate industries only while state/context scope still contains holding-only industries.

- [ ] **Step 1: Write the failing paid-boundary regression**

Add this focused test beside `test_collect_industry_contexts_appends_holding_industries_in_strength_order`:

```python
def test_collect_industry_contexts_skips_holding_only_member_breadth(
    tmp_path: Path,
) -> None:
    component_calls: list[int] = []
    member_snapshot_calls: list[list[int]] = []
    state_snapshot_calls: list[list[int]] = []

    class Api:
        def get_components(
            self, *, tm_id: int, expected_date: str
        ) -> list[dict[str, object]]:
            component_calls.append(tm_id)
            assert tm_id == 700001
            return [
                {"tmId": member_id, "asOfDate": expected_date}
                for member_id in range(1, 13)
            ]

        def get_snapshots(
            self, *, tm_ids: list[int], fields: tuple[str, ...], expected_date: str
        ) -> list[dict[str, object]]:
            if fields == trend_module.INDUSTRY_MEMBER_FIELDS:
                member_snapshot_calls.append(list(tm_ids))
                return [
                    {"tmId": member_id, "asOfDate": expected_date,
                     "tradableFlag": True, "isTrendRightSide": True}
                    for member_id in tm_ids
                ]
            assert fields == trend_module.INDUSTRY_STATE_FIELDS
            state_snapshot_calls.append(list(tm_ids))
            strengths = {700001: "95", 339103: "92.4", 621693: "98.7"}
            return [
                {"tmId": industry_id, "asOfDate": expected_date,
                 "trendTemperatureCurr": "热",
                 "trendStrengthLocalCurr": strengths[industry_id],
                 "TrendRightSideCountRatio": "0.191",
                 "TrendRightSideMktCapRatio": "0.650"}
                for industry_id in tm_ids
            ]

    contexts, status, facts = trend_module.collect_industry_contexts(
        api=Api(),
        candidates=(candidate("600001", industry="候选行业", industry_tm_id=700001),),
        candidate_rows=[{
            "tmId": 600001, "industryTmId": 700001,
            "industryName": "候选行业", "trendTemperaturePrev": "温",
            "trendTemperatureCurr": "热",
        }],
        held_symbols=set(),
        holding_snapshots=(
            holding("600010", industry="银行", industry_tm_id=339103),
            holding("600011", industry="电力", industry_tm_id=621693),
        ),
        expected_date="2026-07-14",
        market="CN",
        history_root=tmp_path / "trend_industry_context",
    )

    assert component_calls == [700001]
    assert member_snapshot_calls == [list(range(1, 13))]
    assert state_snapshot_calls == [[700001], [339103, 621693]]
    assert facts["component_requests"] == 1
    assert facts["member_ids"] == tuple(range(1, 13))
    assert facts["member_rows"] == 12

    holding_contexts = {
        item.industry_tm_id: item
        for item in contexts
        if item.industry_tm_id in {339103, 621693}
    }
    assert all(
        item.member_breadth_collected is False
        and item.component_count == 0
        and item.right_share is None
        for item in holding_contexts.values()
    )
    assert [item.industry_tm_id for item in contexts] == [621693, 700001, 339103]
    assert status["ordering_mode"] == "context_current_only"
```

- [ ] **Step 2: Run the regression and verify it fails on the expensive path**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_a_share_trend.py::test_collect_industry_contexts_skips_holding_only_member_breadth -q
```

Expected: FAIL because current code calls `get_components()` for `339103` and `621693` and includes their members in the paid snapshot.

- [ ] **Step 3: Remove the holding-only expansion branch**

In `collect_industry_contexts()`:

1. Delete the loop that calls `api.get_components()` for `holding_only_ids`.
2. Delete `holding_member_ids`, `holding_member_rows`, and the holding member snapshot request.
3. Keep the existing holding state request and its local error handling.
4. Build each holding-only context with empty components/members and the explicit provenance flag:

```python
holding_contexts = tuple(
    calculate_industry_context(
        industry_tm_id=industry_id,
        industry=industry_names.get(industry_id, ""),
        expected_date=expected_date,
        component_tm_ids=(),
        member_rows=(),
        industry_row=holding_state_by_id.get(industry_id),
        warm_to_hot_count=len(warm_to_hot_ids[industry_id]),
        member_breadth_collected=False,
    )
    for industry_id in holding_only_ids
)
```

5. Keep candidate contexts and the candidate `context_map` byte-for-byte unchanged.
6. Set `component_requests`, `component_rows`, `member_ids`, `member_rows`, and `member_response` from candidate variables only. Keep `context_industry_ids` and `state_ids` as the candidate/holding union.
7. Update the existing holding-error test so it injects a holding state failure; assert no component/member fallback occurs.

- [ ] **Step 4: Run the paid-boundary regression**

Run the command from Step 2.

Expected: `1 passed`.

- [ ] **Step 5: Run both market report suites**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_a_share_trend.py tests/test_market_trend.py -q
```

Expected: PASS after updating exact call-sequence assertions to remove only holding-only component/member calls.

- [ ] **Step 6: Commit the paid-call deletion**

```bash
git add src/open_trader/a_share_trend.py tests/test_a_share_trend.py \
  tests/test_market_trend.py
git commit -m "fix: skip holding-only industry member breadth"
```

---

### Task 4: Lock the Frozen Cost Ledger and Semantic UI Invariant

**Files:**

- Modify: `tests/test_a_share_trend.py`
- Modify: `tests/test_dashboard_web.py`

**Interfaces:**

- Consumes: Task 3's invariant that only candidate industries produce component/member calls.
- Produces: reproducible proof mapping that invariant onto the real 2026-07-31 CN/HK/US frozen scope, plus rendered-field/status equality for same-day inputs.

- [ ] **Step 1: Add the frozen ledger regression**

Record only the observed industry IDs and component counts, not thousands of member rows:

```python
def test_frozen_2026_07_31_paid_scope_ledger() -> None:
    frozen = {
        "CN": {
            "candidate": {621715: 34, 621743: 68},
            "holding_only": {339103: 42, 328115: 51, 621693: 102},
        },
        "HK": {
            "candidate": {},
            "holding_only": {
                621783: 37, 621784: 75, 621772: 83, 621781: 129,
                621766: 151, 621779: 63, 621768: 113, 669417: 0,
            },
        },
        "US": {
            "candidate": {332177: 247, 332182: 862},
            "holding_only": {
                332176: 1260, 692047: 2, 332179: 171, 692034: 3,
                332181: 655, 692011: 3, 332174: 670,
            },
        },
    }
    old_component_calls = sum(
        len(scope["candidate"]) + len(scope["holding_only"])
        for scope in frozen.values()
    )
    new_component_calls = sum(
        len(scope["candidate"]) for scope in frozen.values()
    )
    old_member_ids = sum(
        sum(scope["candidate"].values()) + sum(scope["holding_only"].values())
        for scope in frozen.values()
    )
    new_member_ids = sum(
        sum(scope["candidate"].values()) for scope in frozen.values()
    )

    assert (old_component_calls, new_component_calls) == (22, 4)
    assert (old_member_ids, new_member_ids) == (4821, 1211)
    assert old_component_calls - new_component_calls == 18
    assert old_member_ids - new_member_ids == 3610
    assert Decimal(old_member_ids - new_member_ids) * Decimal("0.003") == Decimal(
        "10.830"
    )
```

The Task 3 call-recorder test is the executable link proving production follows the ledger's candidate-only rule.

- [ ] **Step 2: Add the rendered industry-context parity test**

Use `run_dashboard_js()` to render a collected before-context and a state-only after-context with identical visible state/provider fields:

```javascript
const before = {
  industry_tm_id:339103, industry:"银行", component_count:42,
  snapshot_count:42, tradable_count:42, valid_count:42, right_count:29,
  snapshot_coverage:"1", right_state_coverage:"1", right_share:"0.690476",
  member_breadth_collected:true, warm_to_hot_count:0,
  temperature:"热", temperature_direction:"unchanged", strength:"92.4",
  aggregate_right_count_ratio:"0.691", aggregate_right_market_cap_ratio:"0.74",
  prior_aggregate_right_count_ratio:"0.680",
  prior_aggregate_right_market_cap_ratio:"0.72",
  valid:true, invalid_reasons:[],
};
const after = {...before, component_count:0, snapshot_count:0,
  tradable_count:0, valid_count:0, right_count:0,
  snapshot_coverage:"0", right_state_coverage:"0", right_share:null,
  member_breadth_collected:false};
const status = {ordering_mode:"context_current_only", current_complete:true,
  history_complete:false};
const beforeHtml = renderTrendIndustryContext({industry_context_status:status,
  industry_contexts:[before]});
const afterHtml = renderTrendIndustryContext({industry_context_status:status,
  industry_contexts:[after]});
if (beforeHtml !== afterHtml) throw new Error(`${beforeHtml}\n---\n${afterHtml}`);
console.log("ok");
```

Assert `"ok"` in the Node output. Do not add frontend production code.

- [ ] **Step 3: Run both proof tests**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_a_share_trend.py::test_frozen_2026_07_31_paid_scope_ledger \
  tests/test_dashboard_web.py::test_holding_state_only_context_preserves_rendered_fields_and_status -q
```

Expected: `2 passed`, with the exact `18`, `3,610`, and `10.830` assertions executed.

- [ ] **Step 4: Run all changed-surface suites**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_trend_industry_context.py tests/test_a_share_trend.py \
  tests/test_market_trend.py tests/test_dashboard.py \
  tests/test_dashboard_web.py tests/test_trend_review.py -q
```

Expected: PASS. Do not run `make acceptance` yet.

- [ ] **Step 5: Commit the verification gates**

```bash
git add tests/test_a_share_trend.py tests/test_dashboard_web.py
git commit -m "test: prove Trend Animals holding cost reduction"
```

---

### Task 5: Changelog, Candidate Runtime, Final Acceptance, and Exact-SHA Deployment

**Files:**

- Modify: `CHANGELOG.md`
- Runtime: Dashboard launchd stack and CN/HK/US trend controller launchd jobs

**Interfaces:**

- Consumes: all committed source/tests and the existing install/acceptance scripts.
- Produces: one clean accepted SHA deployed to the Dashboard and all three controllers, with live process/runtime proof and review URL.

- [ ] **Step 1: Add and commit the dated operator log**

Under `## 2026-08-01`, add:

```markdown
- 趋势报告不再为仅持仓行业付费展开全体成分；候选行业的精确宽度、当日排序、动作、风险及 Dashboard 行业字段/状态保持不变，仅持仓行业继续展示供应商聚合比例。若该行业日后首次重新成为候选，可能暂时使用仅当前数据排序。使用 2026-07-31 三市场冻结账本验证减少 18 次成分调用和 3,610 个成员快照，成员字段费用减少 10.830 Trend Animals 余额单位。
```

Commit before any merge:

```bash
git add CHANGELOG.md
git commit -m "docs: log Trend Animals holding cost cut"
```

- [ ] **Step 2: Confirm the final branch is clean and focused**

```bash
git status --short
git diff main...HEAD --check
git log --oneline --decorate -8
```

Expected: clean worktree; only the design, plan, source, tests, and changelog for this change differ from `main`.

- [ ] **Step 3: Prepare ignored runtime links required by the worktree**

```bash
test -e .venv || ln -s /Users/ray/projects/open_trader/.venv .venv
test -e config/daily_premarket.env || \
  ln -s /Users/ray/projects/open_trader/config/daily_premarket.env \
  config/daily_premarket.env
test -e config/prediction_arbitrage.json || \
  ln -s /Users/ray/projects/open_trader/config/prediction_arbitrage.json \
  config/prediction_arbitrage.json
```

These ignored links are runtime setup only and must not be committed.

- [ ] **Step 4: Deploy the candidate SHA to the Dashboard and trend controllers**

Record the candidate SHA, then install from the feature worktree while retaining the main checkout's runtime data/config:

```bash
candidate_sha="$(git rev-parse HEAD)"
scripts/install_dashboard_launchd.sh \
  --repo-root /Users/ray/projects/open_trader/.worktrees/trend-animals-holding-industry-cost \
  --runtime-root /Users/ray/projects/open_trader \
  --python /Users/ray/projects/open_trader/.venv/bin/python
scripts/install_daily_premarket_launchd.sh --trend-only --market all \
  --config /Users/ray/projects/open_trader/config/daily_premarket.env
```

Verify the Dashboard health endpoints and all three status files identify `candidate_sha`, the feature-worktree cwd, fresh PIDs/heartbeats, and `blocker: null`. Because this is a holiday, `phase: holiday` is valid and must not trigger a paid report.

- [ ] **Step 5: Re-run the offline cost and semantic parity proof on the candidate**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest \
  tests/test_a_share_trend.py::test_frozen_2026_07_31_paid_scope_ledger \
  tests/test_a_share_trend.py::test_collect_industry_contexts_skips_holding_only_member_breadth \
  tests/test_dashboard_web.py::test_holding_state_only_context_preserves_rendered_fields_and_status -q
```

Expected: `3 passed`; the ledger prints no network activity and asserts the exact reduced quantities.

- [ ] **Step 6: Run the final Dashboard acceptance gate**

```bash
make acceptance
```

Expected final status: `PASS`. On `FAIL`, diagnose and fix, rerun the relevant focused tests, commit the fix, redeploy the new candidate SHA, and rerun this gate. On `BLOCKED`, report the blocker and do not substitute curl, fixtures, screenshots, or unit tests.

- [ ] **Step 7: Redeploy the exact accepted SHA**

Without changing source or runtime data:

```bash
accepted_sha="$(git rev-parse HEAD)"
test "$accepted_sha" = "$candidate_sha"
test -z "$(git status --porcelain)"
scripts/install_dashboard_launchd.sh \
  --repo-root /Users/ray/projects/open_trader/.worktrees/trend-animals-holding-industry-cost \
  --runtime-root /Users/ray/projects/open_trader \
  --python /Users/ray/projects/open_trader/.venv/bin/python
scripts/install_daily_premarket_launchd.sh --trend-only --market all \
  --config /Users/ray/projects/open_trader/config/daily_premarket.env
```

This restart deploys the already accepted SHA and does not require another acceptance run.

- [ ] **Step 8: Prove the review runtime**

```bash
launchctl print "gui/$UID/com.open-trader.frontend-gateway"
launchctl print "gui/$UID/com.open-trader.legacy-dashboard"
for market in cn hk us; do
  launchctl print "gui/$UID/com.open-trader.trend-market-controller.$market"
done
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8766/
curl -fsS http://127.0.0.1:8766/healthz
curl -fsS http://127.0.0.1:8766/api/dashboard >/dev/null
for market in CN HK US; do
  jq '{pid,working_directory,git_sha,heartbeat_at,phase,blocker}' \
    "/Users/ray/projects/open_trader/data/trend_controller/$market/status.json"
done
```

Verify new live PIDs, cwd equals the feature worktree, every Git SHA equals `accepted_sha`, logs/status timestamps are newer than the restart, all blockers are null, and `/` returns HTTP `200`. Provide `http://127.0.0.1:8766/` only with those facts and `make acceptance: PASS`.

- [ ] **Step 9: Leave merge and push for explicit user direction**

Do not merge or push during implementation handoff. Report the accepted/deployed feature SHA and wait for the user's merge instruction; the changelog is already committed before that future merge.
