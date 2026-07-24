# Unified Trend Discipline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make CN, US, and HK trend reports use the approved shared entry and exit discipline while preserving market mechanics, frozen history, and sample attribution.

**Architecture:** Keep the existing report pipeline. Extend the current strategy snapshot and candidate/holding decision functions in `a_share_trend.py`, reuse one industry-snapshot validator from both report runners, and make `trend_review.py` select the latest compatible strategy identity instead of hard-coding v1/v3. Do not add a new policy module, FX service, configuration layer, or UI.

**Tech Stack:** Python 3.12 stdlib (`dataclasses`, `datetime`, `decimal`, `json`), existing Trend Animals/Futu clients, pytest, existing Dashboard acceptance and launchd deployment scripts.

## Global Constraints

- CN remains `trend_animals_warm_to_hot/CN/v7`, effective 2026-07-24.
- US becomes `trend_animals_warm_to_hot/US/v5`, effective 2026-07-24.
- HK becomes `trend_animals_warm_to_hot/HK/v5`, effective 2026-07-27.
- Reports with earlier execution dates remain v4; frozen reports are never rewritten.
- Shared entry rules are 温→热/沸, strength `>= 95`, industry temperature in 温/热/沸, phase in 谷雨/立夏/夏至, CNY-equivalent market cap `>= 100` hundred-million and amount `>= 2` hundred-million, right side, tradable, no danger, matching date, not held, right-side days present, and ATR14 present.
- There is no static price cap.
- Frozen CNY rates are CN `1`, US `7.85 / 1.08`, and HK `1 / 1.08`; no live FX fetch or drift alert.
- Raw `marketCap` and `amount1d` remain in local-currency hundred-million units; comparisons and audit add CNY equivalents.
- Shared full exits are danger, left right-side trend, temperature turning to 平, and protection trigger.
- CN v7 keeps only the approved CN v4+v7 sample relation. US/HK v5 are exact-identity cold starts and never inherit v4.
- Existing v4-opened positions follow current v5 exits but retain v4 sample attribution when closed.
- Whole-industry request failure must preserve holding exits, pause entries, and be visible in report evidence.
- Reuse current candidate pools, trading windows, lot sizes, accounts, brokers, ordering, limits, risk, Kelly, drawdown, and overheat behavior.
- Add no dependencies.
- Update and commit `CHANGELOG.md` before merging.
- Run `make acceptance` only as the final Dashboard gate. Only `PASS` allows completion.
- After `PASS`, deploy the exact accepted SHA and verify controller/Dashboard PID, cwd, SHA, fresh logs, heartbeat, and HTTP 200.

---

## File Map

- Modify `src/open_trader/a_share_trend.py`: v5 snapshots, execution-date version selection, frozen FX parameters, shared candidate gates, CNY audit values, shared flat-temperature exit, and reusable industry snapshot validation.
- Modify `src/open_trader/market_trend.py`: fetch US/HK industry snapshots, pass industry temperatures into candidate/holding parsing, freeze evidence, and degrade safely on whole-industry failure.
- Modify `src/open_trader/trend_review.py`: normalize v5 and project the latest compatible strategy interval.
- Modify `tests/test_a_share_trend.py`: snapshot, FX boundary, shared filter, audit, historical v4, and exit tests.
- Modify `tests/test_market_trend.py`: two-stage industry API, evidence, missing-row, whole-request failure, and effective-date report tests.
- Modify `tests/test_trend_review.py`: v5 normalization and current compatible projection tests.
- Modify `tests/test_trend_api_stats.py`: prove US/HK v5 cold start and unchanged CN v4+v7 aggregation.
- Modify `CHANGELOG.md`: dated operator-facing summary and verification.
- Do not create a new runtime module or frontend file.

---

### Task 1: Freeze v5 Parameters and Apply the Shared Decision Rules

**Files:**
- Modify: `src/open_trader/a_share_trend.py:67-95`
- Modify: `src/open_trader/a_share_trend.py:379-647`
- Modify: `src/open_trader/a_share_trend.py:1249-1418`
- Modify: `src/open_trader/a_share_trend.py:1904-1943`
- Modify: `src/open_trader/a_share_trend.py:2054-2490`
- Test: `tests/test_a_share_trend.py`

**Interfaces:**
- Produces: `load_industry_temperatures(api: object, *, tm_ids: Sequence[int], expected_date: str) -> tuple[list[Mapping[str, object]], dict[int, str | None]]`.
- Produces: `live_trend_strategy_snapshot(market: str, process_version: str, candidate_pool_ids: Sequence[int], *, normal_cost_rate: Decimal = NORMAL_COST_RATE, strategy_version: str | None = None, execution_date: str | None = None) -> dict[str, object]`.
- Extends internal `_candidate_reasons()` and `build_candidate_list()` with the frozen strategy version and `Decimal` CNY conversion rate.
- Keeps `CandidateInput.market_cap` and `.amount` raw; `_candidate_signal()` adds audit values without changing the dataclass or frozen source fields.

- [ ] **Step 1: Add failing snapshot and effective-date tests**

Add focused tests that assert the exact approved identity and frozen parameters:

```python
@pytest.mark.parametrize(
    ("market", "execution_date", "version"),
    [
        ("US", "2026-07-23", "v4"),
        ("US", "2026-07-24", "v5"),
        ("HK", "2026-07-26", "v4"),
        ("HK", "2026-07-27", "v5"),
    ],
)
def test_market_strategy_version_follows_execution_date(
    market: str, execution_date: str, version: str
) -> None:
    snapshot = trend_module.live_trend_strategy_snapshot(
        market, "sha", (622460,), execution_date=execution_date
    )
    assert snapshot["strategy_version"] == version


@pytest.mark.parametrize(
    ("market", "rate", "currency"),
    [
        ("US", "7.268518518518518518518518519", "USD"),
        ("HK", "0.9259259259259259259259259259", "HKD"),
    ],
)
def test_v5_freezes_shared_entry_rules_and_cny_rate(
    market: str, rate: str, currency: str
) -> None:
    snapshot = trend_module.live_trend_strategy_snapshot(
        market, "sha", (622460,), strategy_version="v5"
    )
    assert snapshot["parameters"] | {
        "temperature_transition": {"from": ["温"], "to": ["热", "沸"]},
        "min_strength": "95",
        "allowed_industry_temperatures": ["温", "热", "沸"],
        "allowed_phases": ["谷雨", "立夏", "夏至"],
        "min_market_cap_cny_100m": "100",
        "min_amount_cny_100m": "2",
        "market_value_currency": currency,
        "cny_per_local_currency": rate,
        "requires_right_side_days": True,
    } == snapshot["parameters"]
    assert "max_filter_price" not in snapshot["parameters"]
    assert snapshot["parameters"]["exit_reasons"] == [
        "danger", "left_right_side", "temperature_to_flat", "protection"
    ]
```

- [ ] **Step 2: Run the snapshot tests and verify failure**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_a_share_trend.py::test_market_strategy_version_follows_execution_date \
  tests/test_a_share_trend.py::test_v5_freezes_shared_entry_rules_and_cny_rate -q
```

Expected: FAIL because `execution_date` and v5 are not supported.

- [ ] **Step 3: Implement the minimal snapshot extension**

Keep the existing function and add constants, not configuration:

```python
MARKET_V5_EFFECTIVE_FROM = {"US": "2026-07-24", "HK": "2026-07-27"}
MARKET_CURRENCY = {"CN": "CNY", "US": "USD", "HK": "HKD"}
CNY_PER_LOCAL_CURRENCY = {
    "CN": Decimal("1"),
    "US": Decimal("7.85") / Decimal("1.08"),
    "HK": Decimal("1") / Decimal("1.08"),
}
```

In `live_trend_strategy_snapshot()`, select the version before validation:

```python
if strategy_version is not None:
    version = strategy_version
elif market == "CN":
    version = "v7"
elif execution_date is None or execution_date >= MARKET_V5_EFFECTIVE_FROM[market]:
    version = "v5"
else:
    version = "v4"
```

Allow v5 only for US/HK. For v5, replace the old US/HK entry keys and rows with the shared values, add the frozen currency/rate keys, remove the right-side `< 10` and price-cap concepts, and set the market-specific effective date. Keep v4 byte-for-byte canonical.

Add v5 to the existing live-risk version gates: Kelly-enabled versions become
`{"v3", "v4", "v5", "v6", "v7"}` and drawdown-enabled versions become
`{"v4", "v5", "v6", "v7"}`. Include v5 in
`_expected_report_strategy_snapshot()` so supplied v5 reports validate against
their exact canonical snapshot. Do not change the identity matcher: exact
matching already gives US/HK v5 a zero-sample cold start.

- [ ] **Step 4: Add failing shared-filter, audit, and exit tests**

Build US/HK candidates at exact boundaries and one below each boundary. Assert:

```python
decision = build_candidate_list(
    candidates,
    held_symbols=set(),
    expected_date="2026-07-23",
    market="US",
    strategy_version="v5",
    cny_per_local_currency=Decimal("7.85") / Decimal("1.08"),
)
assert [item.symbol for item in decision.eligible] == ["PASS"]
assert decision.excluded["LOW_STRENGTH"] == ["strength_below_95"]
assert decision.excluded["COLD_INDUSTRY"] == ["industry_temperature_not_hot"]
assert decision.excluded["LATE_PHASE"] == ["phase_after_summer_solstice"]
assert decision.excluded["NO_DAYS"] == ["right_side_days_missing"]
assert decision.excluded["LOW_CAP"] == ["market_cap_below_100_cny"]
assert decision.excluded["LOW_AMOUNT"] == ["amount_below_2_cny"]
```

Add a report payload assertion for one US candidate:

```python
signal = payload["signal_snapshots"]["candidates"][0]
assert signal["market_value_currency"] == "USD"
assert Decimal(signal["cny_per_local_currency"]) == Decimal("7.85") / Decimal("1.08")
assert Decimal(signal["market_cap_cny_100m"]) == (
    Decimal(signal["market_cap"]) * Decimal(signal["cny_per_local_currency"])
)
assert Decimal(signal["amount_cny_100m"]) == (
    Decimal(signal["amount"]) * Decimal(signal["cny_per_local_currency"])
)
```

Add US/HK v5 holding tests where `temperature_prev` is 温/热/沸 and current is 平, expecting `SELL_ALL / temperature_changed_to_flat`. Add a US v4 replay test expecting the old behavior so frozen history is not reinterpreted.

- [ ] **Step 5: Run the decision tests and verify failure**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_a_share_trend.py \
  -k 'shared or cny or temperature_transition_to_flat or market_strategy_version' -q
```

Expected: FAIL on old US/HK thresholds, absent CNY audit, and CN-only flat-temperature exit.

- [ ] **Step 6: Implement the shared gates at the existing decision seam**

Pass the resolved snapshot version and rate from `build_report()` into `build_candidate_list()` and `_candidate_reasons()`. Use the shared path only for CN or v5:

```python
shared_discipline = market == "CN" or strategy_version == "v5"
if shared_discipline:
    market_cap_cny = (
        item.market_cap * cny_per_local_currency
        if item.market_cap is not None else None
    )
    amount_cny = (
        item.amount * cny_per_local_currency
        if item.amount is not None else None
    )
    # Append the existing CN reason names where semantics match.
    # Use *_cny reason names for converted market-cap and amount failures.
else:
    # Preserve the existing v4 US/HK checks unchanged.
```

Update `_candidate_signal()` to accept the same optional rate and, for shared-discipline reports, append only:

```python
{
    "market_value_currency": MARKET_CURRENCY[market],
    "cny_per_local_currency": cny_per_local_currency,
    "market_cap_cny_100m": (
        item.market_cap * cny_per_local_currency
        if item.market_cap is not None else None
    ),
    "amount_cny_100m": (
        item.amount * cny_per_local_currency
        if item.amount is not None else None
    ),
}
```

Do not mutate `CandidateInput.market_cap` or `.amount`.

Pass `temperature_to_flat=(market == "CN" or snapshot_version == "v5")` into `_holding_action()` and require known temperature fields only when that flag is active. This applies current v5 exits to existing positions while v4 evidence replay remains unchanged.

- [ ] **Step 7: Extract and test the existing industry validator**

Move the existing CN industry snapshot request/validation into `load_industry_temperatures()` without changing behavior:

```python
def load_industry_temperatures(
    api: object,
    *,
    tm_ids: Sequence[int],
    expected_date: str,
) -> tuple[list[Mapping[str, object]], dict[int, str | None]]:
    rows = (
        api.get_snapshots(
            tm_ids=sorted(set(tm_ids)),
            fields=A_SHARE_INDUSTRY_FIELDS,
            expected_date=expected_date,
        )
        if tm_ids else []
    )
    returned = [_row_tm_id(row) for row in rows]
    if len(returned) != len(set(returned)) or any(
        tm_id not in tm_ids for tm_id in returned
    ):
        raise TrendAnimalsError("industry snapshot returned mismatched tmIds")
    if any(row.get("asOfDate") != expected_date for row in rows):
        raise TrendAnimalsError("industry snapshot returned a stale data date")
    return rows, {
        _row_tm_id(row): (
            str(row["trendTemperatureCurr"])
            if row.get("trendTemperatureCurr") in KNOWN_TEMPERATURES
            else None
        )
        for row in rows
    }
```

Call it from the CN runner and retain CN’s current fail-closed behavior.

- [ ] **Step 8: Run focused tests**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_a_share_trend.py \
  tests/test_trend_kelly.py -q
```

Expected: PASS, including unchanged CN v4+v7 sample tests and historical v4 snapshot tests.

- [ ] **Step 9: Commit**

```bash
git add src/open_trader/a_share_trend.py tests/test_a_share_trend.py
git commit -m "feat: share trend entry and exit discipline"
```

Expected: commit contains only the shared decision/snapshot work and tests.

---

### Task 2: Fetch and Freeze US/HK Industry Evidence

**Files:**
- Modify: `src/open_trader/market_trend.py:789-1070`
- Test: `tests/test_market_trend.py`

**Interfaces:**
- Consumes: `load_industry_temperatures()` from Task 1.
- Consumes: `live_trend_strategy_snapshot(market, process_version, pool_ids, execution_date=execution_date)`.
- Produces: report evidence with `query.industry_fields`, `responses.industries`, and metadata `industry_data_reason`.

- [ ] **Step 1: Extend the market-report fake API and write failing request tests**

Make the fake distinguish individual and industry snapshot calls. Assert:

```python
assert api.snapshot_calls == [
    {
        "tm_ids": [candidate_tm_id, holding_tm_id],
        "fields": UNIFIED_TREND_FIELDS,
        "expected_date": "2026-07-23",
    },
    {
        "tm_ids": [industry_tm_id],
        "fields": A_SHARE_INDUSTRY_FIELDS,
        "expected_date": "2026-07-23",
    },
]
assert payload["strategy_judgments"]["top10_candidates"][0][
    "industry_temperature"
] == "温"
```

Read the frozen `replay_evidence.path` and assert:

```python
assert evidence["query"]["industry_fields"] == list(A_SHARE_INDUSTRY_FIELDS)
assert evidence["responses"]["industries"] == industry_rows
```

- [ ] **Step 2: Run the two-stage request test and verify failure**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_market_trend.py \
  -k 'industry_snapshot or freezes_industry' -q
```

Expected: FAIL because market reports currently make only the individual snapshot call.

- [ ] **Step 3: Add the second request with no new abstraction**

After validating individual rows, collect unique positive `industryTmId` values and call Task 1’s helper. Pass the mapping into both existing parsers:

```python
industry_temperature=industry_temperatures.get(
    _optional_int(row.get("industryTmId"))
)
```

Use it in both `evaluate_candidate()` and `_holding_snapshot()`. Include industry row cost in `estimated_cost`, and include the fields/rows in `api_facts`, evidence query, and evidence responses exactly as the CN runner does.

- [ ] **Step 4: Write failing missing-row and whole-request-failure tests**

Extend the existing fake-API report test with two explicit scenarios. In the
missing-row scenario return an industry row only for `HAS_INDUSTRY`; in the
request-failure scenario raise `TrendAnimalsError("industry unavailable")`
only for `A_SHARE_INDUSTRY_FIELDS`. Assert:

```python
assert [
    item["symbol"]
    for item in missing_row_payload["signal_snapshots"]["candidates"]
    if item["eligible"]
] == ["HAS_INDUSTRY"]
assert missing_row_payload["excluded"]["MISSING_INDUSTRY"] == [
    "industry_temperature_missing"
]

formal_actions = failure_payload["strategy_judgments"]["formal_actions"]
assert [(item["action"], item["symbol"], item["reason"]) for item in formal_actions] == [
    ("SELL_ALL", "HELD", "temperature_changed_to_flat")
]
assert failure_payload["strategy_judgments"]["top10_candidates"] == []
assert failure_payload["metadata"]["industry_data_reason"] == (
    "行业温度数据不可用，暂停新开仓：industry unavailable"
)
assert failure_payload["replay_evidence"]
```

The fake must fail only when `fields == A_SHARE_INDUSTRY_FIELDS`; individual snapshots and holding temperature facts remain available.

- [ ] **Step 5: Run the degradation tests and verify failure**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_market_trend.py \
  -k 'missing_industry_row or industry_request_failure' -q
```

Expected: FAIL because the market runner does not yet isolate industry-fetch failure.

- [ ] **Step 6: Implement safe degradation**

Catch `TrendAnimalsError` around only `load_industry_temperatures()`:

```python
industry_rows: list[Mapping[str, object]] = []
industry_temperatures: dict[int, str | None] = {}
industry_data_reason = ""
try:
    industry_rows, industry_temperatures = load_industry_temperatures(
        api, tm_ids=industry_ids, expected_date=as_of_date
    )
except TrendAnimalsError as exc:
    industry_data_reason = f"行业温度数据不可用，暂停新开仓：{exc}"
```

Do not catch individual snapshot, account, quote-system, or report-validation failures. With no industry mapping, every v5 candidate fails the mandatory industry gate while holding decisions still use their individual temperature facts. Store `industry_data_reason` in report metadata and an API fact so JSON/Markdown/evidence cannot look like a normal zero-candidate day.

- [ ] **Step 7: Run market report tests**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_market_trend.py -q
```

Expected: PASS. Update old fake call counts and cost expectations only where the new paid industry request intentionally changes them.

- [ ] **Step 8: Commit**

```bash
git add src/open_trader/market_trend.py tests/test_market_trend.py
git commit -m "feat: load market industry temperatures"
```

Expected: commit contains only market-runner integration and its tests.

---

### Task 3: Project the Current Compatible Strategy

**Files:**
- Modify: `src/open_trader/trend_review.py:4169-4230`
- Modify: `src/open_trader/trend_review.py:4789-5090`
- Test: `tests/test_trend_review.py`
- Test: `tests/test_trend_api_stats.py`

**Interfaces:**
- Consumes: canonical v5 snapshots from Task 1.
- Reuses: `trend_kelly_identity_matches(sample_identity, target_identity)` from `src/open_trader/trend_kelly.py`.
- Produces: `build_trend_review_projection()` whose snapshot, interval, and samples belong to the latest compatible strategy identity.

- [ ] **Step 1: Add failing v5 normalization tests**

```python
@pytest.mark.parametrize("market", ["US", "HK"])
def test_normalize_current_v5_snapshot(market: str) -> None:
    snapshot = live_trend_strategy_snapshot(
        market, "sha", (622460,), strategy_version="v5"
    )
    assert trend_review.normalize_trend_strategy_snapshot(snapshot, market) == snapshot
```

- [ ] **Step 2: Run normalization tests and verify failure**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_trend_review.py \
  -k normalize_current_v5_snapshot -q
```

Expected: FAIL because normalization only routes v4/v6/v7 to the live snapshot builder.

- [ ] **Step 3: Normalize v5 through the existing live snapshot builder**

Change the supported-version check from `{"v4", "v6", "v7"}` to `{"v4", "v5", "v6", "v7"}` everywhere replay requirements and canonical live-snapshot normalization need v5. Do not alter v1-v3 compatibility variants.

- [ ] **Step 4: Add failing current-projection tests**

Create dated facts with a v4 snapshot followed by v5. For US/HK assert:

```python
projection = trend_review.build_trend_review_projection(tmp_path, market)
assert projection["strategy_snapshot"]["strategy_version"] == "v5"
assert projection["interval"]["start"] == v5_effective_date
assert projection["sample_counts"] == {
    "discipline": 0,
    "actual": 0,
    "required": 30,
}
```

Create CN v4 facts followed by v7 facts and assert the projection selects v7 while accepting only v4/v7-compatible cycles. Add an unrelated v5/v6 fact and prove it does not enter the count.

- [ ] **Step 5: Run projection tests and verify failure**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_trend_review.py \
  -k 'current_v5_projection or current_cn_v7_projection' -q
```

Expected: FAIL because `normalize_v1_snapshot_or_none()` discards current live versions.

- [ ] **Step 6: Replace the hard-coded projection filter with the existing identity matcher**

Normalize every canonical supported snapshot. Select the latest normalized fact as `current_snapshot`, then filter both streams with:

```python
target = (
    market,
    str(current_snapshot["strategy_id"]),
    str(current_snapshot["strategy_version"]),
)

def compatible(fact: Mapping[str, object]) -> bool:
    snapshot = fact["strategy_snapshot"]
    return trend_kelly_identity_matches(
        (
            market,
            str(snapshot["strategy_id"]),
            str(snapshot["strategy_version"]),
        ),
        target,
    )
```

Set the interval start to the earliest `effective_from` among compatible canonical snapshots. This yields CN’s approved v4+v7 interval and exact v5-only US/HK intervals. Keep malformed/noncanonical facts excluded and preserve current completeness/cutoff calculations.

Remove the old `len(strategy_identities) > 1` rejection; compatibility filtering is the single identity rule.

- [ ] **Step 7: Prove statistics attribution needs no implementation change**

Add tests to `tests/test_trend_api_stats.py`:

```python
assert us_v5_stat["eligible_sample_count"] == 0  # only US v4 rounds exist
assert hk_v5_stat["eligible_sample_count"] == 0  # only HK v4 rounds exist
assert cn_v7_stat["eligible_sample_count"] == cn_v4_rounds + cn_v7_rounds
```

Also assert a position opened under US v4 and closed during a v5 date retains `opening_strategy_version == "v4"` in the derived round. Do not change `trend_kelly.py` or relabel source fills.

- [ ] **Step 8: Run review/statistics tests**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_trend_review.py \
  tests/test_trend_api_stats.py \
  tests/test_dashboard.py -q
```

Expected: PASS; Dashboard backend now receives current strategy snapshots and the already-derived compatible statistics rows.

- [ ] **Step 9: Commit**

```bash
git add src/open_trader/trend_review.py \
  tests/test_trend_review.py tests/test_trend_api_stats.py
git commit -m "fix: project current trend strategy versions"
```

Expected: no Dashboard frontend files changed.

---

### Task 4: Verify Real Reports, Record the Change, and Run the Final Gate

**Files:**
- Modify: `CHANGELOG.md`
- Verify: `src/open_trader/a_share_trend.py`
- Verify: `src/open_trader/market_trend.py`
- Verify: `src/open_trader/trend_review.py`
- Verify: `tests/test_a_share_trend.py`
- Verify: `tests/test_market_trend.py`
- Verify: `tests/test_trend_review.py`
- Verify: `tests/test_trend_api_stats.py`

**Interfaces:**
- Consumes: all prior task commits.
- Produces: one clean accepted Git SHA suitable for merge and exact-SHA deployment.

- [ ] **Step 1: Make the worktree’s baseline data visible without committing it**

The initial `make test` had `3366 passed, 6 failed` only because worktrees do not contain the root checkout’s ignored legacy facts. Symlink only the required ignored subdirectory:

```bash
test ! -e data/trend_review
ln -s /Users/ray/projects/open_trader/data/trend_review data/trend_review
```

Expected: `data/trend_review/daily/{CN,HK,US}/2026-07-16.json` is readable and `git status --short` does not show tracked data changes.

- [ ] **Step 2: Run focused and full automated tests before acceptance**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_a_share_trend.py \
  tests/test_market_trend.py \
  tests/test_trend_review.py \
  tests/test_trend_api_stats.py \
  tests/test_dashboard.py -q
make test
git diff --check
```

Expected: all commands PASS; record the exact counts for the changelog and handoff.

- [ ] **Step 3: Run the real report workflow where the effective date permits**

Inspect first:

```bash
.venv/bin/python -m open_trader trend-market status --market US \
  --config /Users/ray/projects/open_trader/config/daily_premarket.env
.venv/bin/python -m open_trader trend-market status --market HK \
  --config /Users/ray/projects/open_trader/config/daily_premarket.env
```

Generate a US revision only if the execution batch is not locked:

```bash
.venv/bin/python -m open_trader trend-market run --market US --revision \
  --config /Users/ray/projects/open_trader/config/daily_premarket.env
```

Expected for a generated US v5 report:

- strategy identity is `trend_animals_warm_to_hot/US/v5`;
- current six 91.0–94.9 candidates are excluded by strength rather than bought;
- industry evidence contains the second request and raw USD/CNY-equivalent audit;
- Kelly is v5 cold start at 0 samples;
- existing holdings can still produce v5 exits.

Do not force a HK v5 live report before its 2026-07-27 effective date. Verify the HK boundary with automated tests; if the real workflow is already at or after that date, run the same revision check for HK.

- [ ] **Step 4: Regenerate current review/statistics projections and inspect output**

Use the existing controller/report commands rather than a new migration script. Confirm `data/latest/trend_review_{cn,us,hk}.json` shows CN v7, US v5, and HK v5 only when its effective report exists. Confirm US/HK v5 `0 / 30` and CN’s approved compatible count.

Expected: no historical report file is modified.

- [ ] **Step 5: Add the dated changelog entry and commit it before merge**

Add one concise 2026-07-24 entry:

```markdown
- Unified CN/US/HK trend entry and flat-temperature exit discipline; US/HK now
  use frozen local-currency-to-CNY thresholds, industry snapshots, and v5
  cold-start samples while CN retains its v4+v7 exception. Verified the full
  test suite, real US report/evidence, and current review projections.
```

Commit this entry before acceptance:

```bash
git add CHANGELOG.md
git commit -m "docs: record unified trend discipline"
```

Do not amend source, tests, or changelog after acceptance.

- [ ] **Step 6: Confirm clean committed source and inspect live old processes**

Run:

```bash
git status --short
git rev-parse HEAD
launchctl list | rg 'com\\.open-trader\\.trend-market-controller\\.(cn|hk|us)'
pgrep -f 'open_trader trend-market run' | xargs ps -o pid,lstart,command -p || true
screen -ls | rg 'open_trader_dashboard_8766' || true
```

Expected: tracked worktree clean; record the pre-deployment PIDs and SHA.

- [ ] **Step 7: Run the one final acceptance gate**

Run only now:

```bash
make acceptance
```

Expected terminal result: `PASS`. `FAIL` must be fixed and the gate rerun. `BLOCKED` must be reported as blocked; do not substitute curl, fixtures, unit tests, or screenshots.

- [ ] **Step 8: Redeploy the exact accepted SHA**

Set and validate the immutable SHA:

```bash
export ACCEPTED_SHA="$(git rev-parse HEAD)"
test -z "$(git status --short)"
test "$(git rev-parse HEAD)" = "$ACCEPTED_SHA"
scripts/install_daily_premarket_launchd.sh \
  --config /Users/ray/projects/open_trader/config/daily_premarket.env \
  --trend-only --market all
screen -S open_trader_dashboard_8766 -X quit 2>/dev/null || true
screen -dmS open_trader_dashboard_8766 zsh -lc \
  'cd /Users/ray/projects/open_trader/.worktrees/unify-trend-discipline && exec env PYTHONPATH=src .venv/bin/python -u -m open_trader dashboard --portfolio /Users/ray/projects/open_trader/data/latest/portfolio.csv --data-dir /Users/ray/projects/open_trader/data --reports-dir /Users/ray/projects/open_trader/reports --config /Users/ray/projects/open_trader/config/daily_premarket.env --poll-seconds 5 --host 127.0.0.1 --port 8766 >> /tmp/open_trader_dashboard_8766.log 2>&1'
```

Expected: all new processes run from this worktree at exactly `$ACCEPTED_SHA`.

- [ ] **Step 9: Verify live controller and Dashboard evidence**

Run the README heartbeat verifier with:

```text
worktree=/Users/ray/projects/open_trader/.worktrees/unify-trend-discipline
accepted_sha=$ACCEPTED_SHA
```

Then:

```bash
pgrep -f 'open_trader trend-market run' | xargs ps -o pid,lstart,command -p
tail -n 80 logs/daily_premarket/launchd-trend-controller-*.out.log
tail -n 80 logs/daily_premarket/launchd-trend-controller-*.err.log
tail -n 80 /tmp/open_trader_dashboard_8766.log
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8766/
curl -sS http://127.0.0.1:8766/api/dashboard | \
  .venv/bin/python -m json.tool >/dev/null
```

Expected: new PIDs, exact cwd/SHA, advancing heartbeats, fresh logs without startup errors, HTTP `200`, and valid Dashboard JSON.

- [ ] **Step 10: Merge only after the changelog gate**

Verify the feature branch contains the committed 2026-07-24 changelog entry, then merge into local `main` without touching root checkout untracked files. If main advanced, rebase/merge safely and rerun affected tests; any source change invalidates the prior accepted SHA and requires a new final acceptance.

Expected: local `main` contains the exact accepted source and changelog; user-owned untracked files remain untouched.
