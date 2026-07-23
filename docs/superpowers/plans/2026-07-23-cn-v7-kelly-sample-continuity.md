# CN v7 Kelly Sample Continuity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the final relaxed CN v7 strategy inherit eligible CN v4 closed-round samples without admitting v1, v5, v6, other markets, or future versions.

**Architecture:** Add one exact, one-way identity predicate in `trend_kelly.py` and reuse it in both Kelly selection and statistics aggregation. Keep fills and rounds immutable; v7 summary rows are derived from v4 plus v7 records. Preserve v6 report compatibility, publish v7 as current, and expose the approved inheritance in the frozen strategy parameters.

**Tech Stack:** Python 3.12, `Decimal`, pytest, existing Trend API statistics artifacts, existing drawdown preflight, launchd, Dashboard acceptance.

## Global Constraints

- This is a one-time CN compatibility rule, not a general cross-version pooling policy.
- The v7 target accepts exactly CN v4 and CN v7 identities.
- CN v1, v5, and v6 are excluded from v7 samples.
- Existing CN v4, CN v6, HK, US, and unrelated strategy calculations remain exact-identity scoped.
- Only attributed, cost-complete simulation closed rounds remain Kelly eligible.
- Actual closed rounds are display-only and never affect Kelly sizing.
- Do not copy, relabel, or mutate fills, rounds, round IDs, fees, returns, or attribution.
- Keep ATR14 protection and all relaxed CN entry gates unchanged.
- Add no dependency and no generic migration framework.
- Run focused tests during development; run `make acceptance` only as the final Dashboard gate.

---

### Task 1: Add the one-way Kelly sample identity rule

**Files:**
- Modify: `src/open_trader/trend_kelly.py:10-25,187-207`
- Test: `tests/test_trend_kelly.py`

**Interfaces:**
- Produces: `trend_kelly_identity_matches(sample_identity: tuple[str, str, str], target_identity: tuple[str, str, str]) -> bool`
- Consumes: existing `TrendKellyRound` identity fields.
- Later tasks use this predicate when deriving statistics rows.

- [ ] **Step 1: Write failing identity and Kelly-selection tests**

Add tests that state the approved matrix explicitly:

```python
from open_trader.trend_kelly import trend_kelly_identity_matches


@pytest.mark.parametrize(
    ("sample", "target", "expected"),
    [
        (
            ("CN", "trend_animals_warm_to_hot/CN/v4", "v4"),
            ("CN", "trend_animals_warm_to_hot/CN/v7", "v7"),
            True,
        ),
        (
            ("CN", "trend_animals_warm_to_hot/CN/v7", "v7"),
            ("CN", "trend_animals_warm_to_hot/CN/v7", "v7"),
            True,
        ),
        *[
            (
                ("CN", f"trend_animals_warm_to_hot/CN/{version}", version),
                ("CN", "trend_animals_warm_to_hot/CN/v7", "v7"),
                False,
            )
            for version in ("v1", "v5", "v6")
        ],
        (
            ("CN", "trend_animals_warm_to_hot/CN/v7", "v7"),
            ("CN", "trend_animals_warm_to_hot/CN/v4", "v4"),
            False,
        ),
        (
            ("HK", "trend_animals_warm_to_hot/HK/v4", "v4"),
            ("CN", "trend_animals_warm_to_hot/CN/v7", "v7"),
            False,
        ),
    ],
)
def test_cn_v7_sample_identity_compatibility_is_exact_and_one_way(
    sample: tuple[str, str, str],
    target: tuple[str, str, str],
    expected: bool,
) -> None:
    assert trend_kelly_identity_matches(sample, target) is expected


def test_cn_v7_kelly_inherits_v4_and_accumulates_v7_only() -> None:
    rounds = [
        _round(1, "0.10", market="CN",
               strategy_id="trend_animals_warm_to_hot/CN/v4", version="v4"),
        _round(2, "0.10", market="CN",
               strategy_id="trend_animals_warm_to_hot/CN/v7", version="v7"),
        _round(3, "0.10", market="CN",
               strategy_id="trend_animals_warm_to_hot/CN/v5", version="v5"),
        _round(4, "0.10", market="CN",
               strategy_id="trend_animals_warm_to_hot/CN/v6", version="v6"),
    ]

    state = calculate_trend_kelly(
        rounds,
        market="CN",
        strategy_id="trend_animals_warm_to_hot/CN/v7",
        opening_strategy_version="v7",
    )

    assert state.eligible_sample_count == 2
    assert state.selected_round_ids == ("round-001", "round-002")
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_trend_kelly.py::test_cn_v7_sample_identity_compatibility_is_exact_and_one_way \
  tests/test_trend_kelly.py::test_cn_v7_kelly_inherits_v4_and_accumulates_v7_only -q
```

Expected: collection fails because `trend_kelly_identity_matches` does not
exist, or the selection count is `1` instead of `2`.

- [ ] **Step 3: Implement the exact shared predicate and use it once**

Add the immutable identities and predicate:

```python
TrendKellyIdentity = tuple[str, str, str]
CN_V4_KELLY_IDENTITY: TrendKellyIdentity = (
    "CN", "trend_animals_warm_to_hot/CN/v4", "v4",
)
CN_V7_KELLY_IDENTITY: TrendKellyIdentity = (
    "CN", "trend_animals_warm_to_hot/CN/v7", "v7",
)
CN_V7_KELLY_SAMPLE_IDENTITIES = frozenset({
    CN_V4_KELLY_IDENTITY,
    CN_V7_KELLY_IDENTITY,
})


def trend_kelly_identity_matches(
    sample_identity: TrendKellyIdentity,
    target_identity: TrendKellyIdentity,
) -> bool:
    if target_identity == CN_V7_KELLY_IDENTITY:
        return sample_identity in CN_V7_KELLY_SAMPLE_IDENTITIES
    return sample_identity == target_identity
```

Replace the three exact identity comparisons in `calculate_trend_kelly` with:

```python
target_identity = (market, strategy_id, opening_strategy_version)
matching = [
    item
    for item in rounds
    if item.source == "simulation"
    and trend_kelly_identity_matches(
        (item.market, item.strategy_id, item.opening_strategy_version),
        target_identity,
    )
    and item.costs_complete
    and item.attribution_status == "attributed"
    and item.kelly_eligible
]
```

- [ ] **Step 4: Run focused and complete Kelly tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_trend_kelly.py -q
```

Expected: all tests in `tests/test_trend_kelly.py` pass.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/trend_kelly.py tests/test_trend_kelly.py
git commit -m "feat: preserve approved CN Kelly samples"
```

---

### Task 2: Derive v7 simulation and actual statistics from the same lineage

**Files:**
- Modify: `src/open_trader/trend_api_stats.py:1328-1385`
- Test: `tests/test_trend_api_stats.py`

**Interfaces:**
- Consumes: `trend_kelly_identity_matches` from Task 1.
- Produces: current v7 `stats` rows whose simulation and actual values aggregate v4 plus v7 rounds.
- Existing v4/v5/v6 stats rows remain exact to their own identity.

- [ ] **Step 1: Write the failing public-payload aggregation test**

Build one completed CN round for each of v4, v5, v6, and v7 under both
`simulation` and `actual`. Use distinct symbols so each pair closes
independently. For actual Eastmoney fills, include the existing statement
fields required by `fill()` validation.

```python
def _cn_closed_pair(
    label: str, version: str, *, source: str,
) -> list[dict[str, object]]:
    broker = "futu" if source == "simulation" else "eastmoney"
    account_id = "12958918" if source == "simulation" else "eastmoney_main"
    pair = [
        fill(
            f"{label}-buy", side="buy", quantity="1", price="10", fee="0.1",
            filled_at="2026-07-10T15:00:00+08:00",
            strategy_id=f"trend_animals_warm_to_hot/CN/{version}",
            strategy_version=version, source=source, broker=broker,
            account_id=account_id, market="CN", currency="CNY",
        ),
        fill(
            f"{label}-sell", side="sell", quantity="1", price="11", fee="0.1",
            filled_at="2026-07-11T15:00:00+08:00",
            strategy_id=f"trend_animals_warm_to_hot/CN/{version}",
            strategy_version=version, source=source, broker=broker,
            account_id=account_id, market="CN", currency="CNY",
        ),
    ]
    for sequence, item in enumerate(pair, start=1):
        item["symbol"] = label
        if source == "actual":
            item.update({
                "statement_period": f"2026-07-{9 + sequence:02d}",
                "execution_granularity": "statement_trade_date",
                "timestamp_semantics": "market_close_ordering_sentinel",
                "statement_sequence": 1,
            })
    return pair


def test_cn_v7_stats_inherit_only_v4_for_both_sources() -> None:
    fills = [
        item
        for source in ("simulation", "actual")
        for version in ("v4", "v5", "v6", "v7")
        for item in _cn_closed_pair(f"{source}-{version}", version, source=source)
    ]
    payload = build_trend_api_stats_payload(
        fills,
        strategy_versions=[{
            "market": "CN",
            "strategy_id": "trend_animals_warm_to_hot/CN/v7",
            "strategy_version": "v7",
        }],
        generated_at="2026-07-12T00:00:00+08:00",
        statistics_cutoff_at="2026-07-11T23:59:59+08:00",
    )
    stats = {
        (item["source"], item["strategy_id"], item["opening_strategy_version"]): item
        for item in payload["stats"]
    }

    for source in ("simulation", "actual"):
        assert stats[
            (source, "trend_animals_warm_to_hot/CN/v7", "v7")
        ]["eligible_sample_count"] == 2
        assert stats[
            (source, "trend_animals_warm_to_hot/CN/v4", "v4")
        ]["eligible_sample_count"] == 1
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_trend_api_stats.py::test_cn_v7_stats_inherit_only_v4_for_both_sources -q
```

Expected: v7 simulation and actual counts are `1`, not `2`.

- [ ] **Step 3: Reuse the Task 1 predicate in `_strategy_stats`**

Import `trend_kelly_identity_matches` from `trend_kelly`. Replace the exact
four-field identity comparison inside `_strategy_stats` with:

```python
target_identity = (market, strategy_id, version)
eligible = [
    round_
    for round_ in rounds
    if round_["source"] == source
    and trend_kelly_identity_matches(
        (
            str(round_["market"]),
            str(round_["strategy_id"]),
            str(round_["opening_strategy_version"]),
        ),
        target_identity,
    )
    and round_["attribution_status"] == "attributed"
    and round_["costs_complete"] is True
    and round_["net_return"] is not None
]
```

Do not alter round construction, source validation, serialization, or artifact
revalidation. The existing `load_trend_api_stats` rebuild will automatically
verify the new derived rows with the same predicate.

- [ ] **Step 4: Run statistics and Kelly tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_trend_api_stats.py tests/test_trend_kelly.py -q
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/trend_api_stats.py tests/test_trend_api_stats.py
git commit -m "feat: aggregate CN v7 trade statistics"
```

---

### Task 3: Publish v7 while preserving v6 historical reports

**Files:**
- Modify: `src/open_trader/a_share_trend.py:105-120,560-630,2110-2140,2360-2390,2980-3360`
- Modify: `src/open_trader/trend_review.py:2600-2620,4190-4215,5100-5320`
- Modify: `src/open_trader/dashboard.py:1085-1255`
- Modify: `src/open_trader/dashboard_acceptance.py:455-470`
- Test: `tests/test_a_share_trend.py`
- Test: `tests/test_trend_review.py`
- Test: `tests/test_dashboard.py`
- Test: `tests/test_dashboard_acceptance.py`

**Interfaces:**
- Consumes: Task 1 Kelly selection and Task 2 derived v7 statistics.
- Produces: current CN strategy identity `trend_animals_warm_to_hot/CN/v7`.
- Preserves: explicit v6 snapshot/report/review/Dashboard validation.

- [ ] **Step 1: Write failing current-v7 and inherited-report tests**

Change the current snapshot expectation and add the frozen inheritance field:

```python
def test_live_cn_strategy_snapshot_is_v7_with_v4_sample_inheritance() -> None:
    snapshot = trend_module.live_trend_strategy_snapshot(
        "CN", "abc123", (622466, 697199)
    )

    assert snapshot["strategy_id"] == "trend_animals_warm_to_hot/CN/v7"
    assert snapshot["strategy_version"] == "v7"
    assert snapshot["effective_from"] == "2026-07-24"
    assert snapshot["parameters"]["kelly_sample_inherits"] == [{
        "market": "CN",
        "strategy_id": "trend_animals_warm_to_hot/CN/v4",
        "opening_strategy_version": "v4",
    }]
    assert snapshot["parameters"]["allowed_industry_temperatures"] == [
        "温", "热", "沸",
    ]
    assert "max_filter_price" not in snapshot["parameters"]
```

Add a report-level test using v4, v5, v6, and v7 `TrendKellyRound` values:

```python
def test_cn_v7_report_keeps_v4_samples_without_admitting_v5_or_v6() -> None:
    identities = ("v4", "v5", "v6", "v7")
    rounds = tuple(
        replace(
            _trend_kelly_rounds("0.10", market="CN")[0],
            round_id=f"round-{version}",
            strategy_id=f"trend_animals_warm_to_hot/CN/{version}",
            opening_strategy_version=version,
        )
        for version in identities
    )
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account(),
        candidates=[candidate("600001")],
        holding_snapshots={},
        bars_by_symbol={},
        market="CN",
        kelly_rounds=rounds,
    )

    assert built.strategy_snapshot["strategy_version"] == "v7"
    assert built.risk_summary["kelly_eligible_sample_count"] == 2
    assert built.risk_summary["kelly_selected_sample_count"] == 2
```

Keep a separate explicit v6 historical snapshot normalization test so adding
v7 does not reinterpret v6.

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_a_share_trend.py::test_live_cn_strategy_snapshot_is_v7_with_v4_sample_inheritance \
  tests/test_a_share_trend.py::test_cn_v7_report_keeps_v4_samples_without_admitting_v5_or_v6 -q
```

Expected: current snapshot is v6 and the report selects only one v7 round.

- [ ] **Step 3: Make v7 current and freeze the inheritance**

In `live_trend_strategy_snapshot`:

```python
version = strategy_version or ("v7" if market == "CN" else "v4")
if (
    version not in {"v4", "v6", "v7"}
    or version in {"v6", "v7"} and market != "CN"
):
    raise ValueError("unsupported live trend strategy version")

if version in {"v6", "v7"}:
    parameters.pop("max_filter_price", None)
    parameters["allowed_industry_temperatures"] = ["温", "热", "沸"]
    rows = [row for row in rows if row["name"] != "筛选价格"]
    for row in rows:
        if row["name"] == "行业温度":
            row["value"] = "温、热或沸"
if version == "v7":
    parameters["kelly_sample_inherits"] = [{
        "market": "CN",
        "strategy_id": "trend_animals_warm_to_hot/CN/v4",
        "opening_strategy_version": "v4",
    }]
```

Use `2026-07-24` for v6 and v7 effective dates. Accept both v6 and v7 in
`_expected_report_strategy_snapshot`; all Kelly, risk, drawdown, render, and
report-validation version sets that currently contain v6 must contain v7 too.
Map v7 to `valid_v4_risk_contract` without removing the v6 mapping.

- [ ] **Step 4: Propagate v7 through review, Dashboard, and acceptance**

Make the corresponding additive changes:

```python
# dashboard.py risk-contract map
"v6": valid_v4_risk_contract,
"v7": valid_v4_risk_contract,

# dashboard_acceptance.py current Eastmoney identity
expected_version = "v7" if broker == "eastmoney" else "v4"
```

In `trend_review.py` and `dashboard.py`, add v7 to every existing v6
version set while retaining v6. Update test fixtures so current CN payloads
use v7, and retain one explicit v6 fixture to prove historical acceptance.
Add `kelly_sample_inherits` to the parameters removed by
`_legacy_strategy_snapshot_variants`, so v1 snapshots cannot accidentally
inherit the current v7 exception. Extend the existing v4/v6 normalization
test to cover v7 while proving each version keeps its own frozen parameters.

- [ ] **Step 5: Run focused report, review, Dashboard, and acceptance tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_a_share_trend.py \
  tests/test_trend_review.py \
  tests/test_dashboard.py \
  tests/test_dashboard_acceptance.py -q
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit**

```bash
git add \
  src/open_trader/a_share_trend.py \
  src/open_trader/trend_review.py \
  src/open_trader/dashboard.py \
  src/open_trader/dashboard_acceptance.py \
  tests/test_a_share_trend.py \
  tests/test_trend_review.py \
  tests/test_dashboard.py \
  tests/test_dashboard_acceptance.py
git commit -m "feat: publish CN trend strategy v7"
```

---

### Task 4: Document, regenerate, and verify the accepted live workflow

**Files:**
- Modify: `CHANGELOG.md`
- Modify: `纪律.md:247-255`
- Runtime artifact: `reports/trend_a_share/2026-07-23-rN.json`
- Runtime artifact: `reports/trend_a_share/2026-07-23-rN.md`
- Runtime artifact: `data/latest/trend_api_stats.json`
- Runtime state: `data/trend_drawdown/state.json`

**Interfaces:**
- Consumes: final v7 code and unchanged source fills/rounds.
- Produces: audited v7 drawdown state, regenerated v7 statistics, final current
  CN report, and the deployed review URL.

- [ ] **Step 1: Update operator-facing rules**

Add a dated changelog bullet stating that CN v7 inherits only CN v4 samples.
Replace the broad discipline wording with the approved exception:

```markdown
- 纪律版本更新默认不跨版本合并 Kelly 样本；只有用户明确批准的兼容关系可以继承。
- 本次 CN v7 是一次性特例：仅继承 CN v4，并与后续 v7 合格闭环继续累计；CN v1、v5、v6 不进入该样本池。
- 更换策略或未明确批准兼容关系时，建立新的 Kelly 样本池并重新进入冷启动。
```

- [ ] **Step 2: Run focused tests and the full automated suite**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_trend_kelly.py \
  tests/test_trend_api_stats.py \
  tests/test_a_share_trend.py \
  tests/test_trend_review.py \
  tests/test_dashboard.py \
  tests/test_dashboard_acceptance.py -q
make test
git diff --check
```

Expected: both pytest commands pass and `git diff --check` prints nothing.

- [ ] **Step 3: Commit documentation before any merge**

```bash
git add CHANGELOG.md 纪律.md
git commit -m "docs: record CN v7 sample inheritance"
```

- [ ] **Step 4: Establish the audited v7 baseline without rewriting v6**

Use the same safe new-version sequence already exercised for v6:

1. Run one CN report revision for 2026-07-23 from the feature worktree. Verify
   its strategy version is v7, drawdown `state_status` is `missing`, and
   account `source_date` is 2026-07-23.
2. Run:

```bash
PYTHONPATH=src .venv/bin/python -m open_trader trend-drawdown-preflight \
  --config /Users/ray/projects/open_trader/config/daily_premarket.env \
  --repo /Users/ray/projects/open_trader/.worktrees/relax-cn-trend-entry-gates \
  --actor acceptance
```

Expected: CN is `bootstrapped` under
`trend_animals_warm_to_hot/CN/v7`; existing v4/v5/v6 state records remain
unchanged.

- [ ] **Step 5: Rebuild derived statistics and generate the final report**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m open_trader trend-review sync-stats \
  --start 2026-01-01 \
  --end 2026-07-23 \
  --config /Users/ray/projects/open_trader/config/daily_premarket.env
```

Verify `data/latest/trend_api_stats.json` contains v7 simulation and actual
rows. Generate another 2026-07-23 CN report revision and verify:

```text
strategy_snapshot.strategy_version = v7
drawdown_summary.state_status = ok
drawdown_summary.entry_allowed = true
strategy_snapshot.parameters.kelly_sample_inherits = CN v4
risk_summary.kelly_eligible_sample_count = eligible CN v4 + CN v7 rounds
```

Also verify the relaxed industry gate, absent `max_filter_price`, ATR14
protection, and the resulting formal actions are otherwise unchanged.

- [ ] **Step 6: Deploy candidate processes and run the final acceptance gate**

Restart CN, HK, and US controllers from the feature worktree, then restart the
Dashboard on `127.0.0.1:8766`. Run:

```bash
make acceptance
```

Expected: `PASS`. If it returns `FAIL`, fix the reported issue and rerun. If it
returns `BLOCKED`, report the unavailable external/browser environment without
substituting mocks or curl.

- [ ] **Step 7: Redeploy the exact accepted SHA**

After `PASS`, restart CN, HK, US, and Dashboard again from the exact accepted
commit. Verify:

```text
all four processes report the accepted Git SHA
all working directories are the feature worktree
controller heartbeats advance
fresh controller and Dashboard logs contain the new PIDs
GET http://127.0.0.1:8766/ returns HTTP 200
Dashboard API loads the final v7 report and v7 trade statistics
```

Do not rerun acceptance when this restart deploys the exact already-accepted
SHA and makes no source or data changes.
