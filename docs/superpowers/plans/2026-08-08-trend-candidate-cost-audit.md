# Three-Market Trend Candidate Cost and Audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce Trend Animals candidate-screening cost in CN, HK, and US while preserving discipline, risk, exits, sizing, and rotation rules, then expose only final-plan skip reasons in the existing report UI.

**Architecture:** Keep holdings on the existing complete-snapshot path and add one shared, fail-closed staged candidate fetch in `a_share_trend.py` for all three markets. New strategy versions use individual global strength as the primary rank, collect temperature-only industry context, and reuse `risk_skips` as the final-plan audit; old strategy versions keep their frozen ranking and replay behavior. Report regeneration uses the existing report generators with a no-op notifier and a staged reports directory, so no broker execution path is reachable and all three revisions are validated before publication.

**Tech Stack:** Python 3.12, `Decimal`, dataclasses, pytest, existing Trend Animals/Futu clients, existing vanilla JavaScript Dashboard renderer, stdlib temporary-file and immutable-write primitives.

**2026-08-09 operator update:** Historical 20-day replay is not required. Validate the deterministic CN/HK/US contracts and regenerate only the latest immutable reports.

## Global Constraints

- Apply the same behavior to CN, HK, and US; failure in any market blocks activation of all three.
- New strategy versions are CN `v13`, HK `v11`, and US `v11`; old versions remain replayable.
- Local-strength discipline remains `>= 95`; all other discipline, risk, sizing, exit, drawdown, and rotation-comparison rules remain unchanged.
- New-version rank order is global strength descending, industry temperature descending only on an exact global-strength tie, right-side days ascending, amount descending, then symbol ascending.
- Stocks and ETFs share one ten-position pool.
- A discipline-qualified candidate with missing global strength is not plan-eligible and is audited as `全局强度缺失，无法排序`; do not fall back to local strength.
- Do not request eligible-industry components or member snapshots. Do not add a new cache, adaptive optimizer, weighted score, quota, or runtime hard cost cap.
- Every snapshot stage validates exact requested IDs, unique returned IDs, and exact data date. Any malformed stage fails that market closed and must not fall back to a complete candidate snapshot.
- Simulated and real-only holdings keep complete snapshots.
- The final audit contains discipline-qualified but unplanned candidates, excludes every normal or rotation BUY target, and puts generic `没有通过纪律` rows last.
- Omit an empty normal BUY section from Markdown and Dashboard output. Rotation sections remain in their current position.
- New industry projection contains only industry name, temperature, and temperature direction. Historical reports retain and can read their old fields.
- The frozen 2026-08-07 US known-field estimate must not exceed `2.852` Trend Animals balance units. CN and HK use exact deterministic staged-request budgets from their frozen fixtures; live balance deltas are observations, not hard caps.
- No broker order submission is allowed during validation or regeneration.
- Run focused checks during implementation. Run `make acceptance` exactly once, after the three new revisions are current, as the final Dashboard gate.
- Before merging, commit a dated operator-facing `CHANGELOG.md` entry.
- After `make acceptance` returns `PASS`, redeploy the exact accepted SHA and verify PID, cwd, SHA, fresh logs, HTTP 200, and the review URL.

---

## File Map

- `src/open_trader/a_share_trend.py`: version routing, staged field sets and fetch, new rank key, temperature-only contexts, cost trace, final-plan audit, Markdown output.
- `src/open_trader/market_trend.py`: HK/US runner integration with the shared staged fetch and revised evidence/cost facts.
- `src/open_trader/trend_industry_context.py`: allow a temperature-only context without weakening legacy full-context validation.
- `src/open_trader/strategy_drawdown.py`: activate allocation-era versions CN v13 / HK v11 / US v11 while retaining the immediately preceding versions.
- `src/open_trader/drawdown_preflight.py`: declare the approved predecessors v12 / v10 / v10.
- `src/open_trader/trend_review.py`: recognize and rebuild the new versions while retaining old-version normalization.
- `src/open_trader/dashboard.py`: project all-market candidate signals and final `risk_skips` without recomputing strategy decisions.
- `src/open_trader/dashboard_static/dashboard.js`: omit the empty normal BUY stage, render the final-plan audit, and reduce industry columns.
- `scripts/regenerate_trend_reports_no_submit.py`: stage, validate, and publish the three latest immutable revisions without entering controller/order code.
- `tests/test_a_share_trend.py`: version, rank, fetch-waterfall, final audit, Markdown, and CN integration coverage.
- `tests/test_market_trend.py`: HK/US staged-request and cost coverage.
- `tests/test_trend_review.py`: new/old version normalization and replay coverage.
- `tests/test_strategy_drawdown.py`, `tests/test_trend_kelly.py`: predecessor, allocation identity, and inherited-sample coverage.
- `tests/test_dashboard.py`, `tests/test_dashboard_web.py`: projection and browser-rendered report contract.
- `tests/test_trend_report_regeneration.py`: staging, all-market gate, immutable publication, and no-submit proof.
- `CHANGELOG.md`: dated cost/ranking/audit/release entry before merge.

---

### Task 1: Freeze the new strategy versions and rank contract

**Files:**
- Modify: `src/open_trader/a_share_trend.py`
- Modify: `src/open_trader/strategy_drawdown.py`
- Modify: `src/open_trader/drawdown_preflight.py`
- Modify: `src/open_trader/trend_review.py`
- Test: `tests/test_a_share_trend.py`
- Test: `tests/test_strategy_drawdown.py`
- Test: `tests/test_trend_kelly.py`
- Test: `tests/test_trend_review.py`

**Interfaces:**
- Produces: `_uses_individual_global_ranking(market: str, strategy_version: str | None) -> bool`.
- Produces: `_candidate_global_sort_key(item: CandidateInput) -> tuple[object, ...]`.
- Produces: current allocation versions `{"CN": "v13", "HK": "v11", "US": "v11"}`.
- Consumes: existing `live_trend_strategy_snapshot`, `_candidate_reasons`, `build_candidate_list`, and Kelly inheritance structures.

- [ ] **Step 1: Write failing version and ranking tests**

Add tests with mixed A-stock/ETF and stock/ETF candidates. Freeze both the new order and the old order:

```python
def test_new_versions_rank_mixed_assets_by_global_strength() -> None:
    rows = [
        candidate("ETF", asset="ETF基金", global_strength="97", strength="95", industry_temperature="温"),
        candidate("STOCK", asset="A股", global_strength="98", strength="96", industry_temperature="温"),
        candidate("TIE-HOT", global_strength="96", industry_temperature="热", days=4, amount="2"),
        candidate("TIE-WARM", global_strength="96", industry_temperature="温", days=1, amount="9"),
    ]
    decision = build_candidate_list(
        rows, held_symbols=set(), market="CN", strategy_version="v13"
    )
    assert [item.symbol for item in decision.eligible] == [
        "STOCK", "ETF", "TIE-HOT", "TIE-WARM",
    ]


def test_cn_v12_keeps_industry_first_order() -> None:
    rows = [
        candidate("600001", global_strength="99", strength="99", industry_tm_id=1),
        candidate("600002", global_strength="98", strength="98", industry_tm_id=2),
    ]
    decision = build_candidate_list(
        rows,
        held_symbols=set(),
        market="CN",
        strategy_version="v12",
        industry_contexts={
            1: _industry_context(1, temperature="温", strength="80"),
            2: _industry_context(2, temperature="热", strength="90"),
        },
    )
    assert [item.symbol for item in decision.eligible] == ["600002", "600001"]
```

Also assert `live_trend_strategy_snapshot()` returns v13/v11/v11 with an allocation reference, new parameter rows name only the approved rank keys, and Kelly inheritance is exactly CN `(v4,v7,v8,v9,v10,v11)` and HK/US `(v4,v5,v6,v7,v8,v9)`.

- [ ] **Step 2: Run the new tests and confirm the old code fails**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_a_share_trend.py -k 'new_versions_rank or predecessor_versions' \
  tests/test_strategy_drawdown.py \
  tests/test_trend_kelly.py \
  tests/test_trend_review.py -k 'strategy_version or normalization'
```

Expected: FAIL because v13 and HK/US v11 are unsupported and the current key is industry-first.

- [ ] **Step 3: Implement the minimal version-gated key and version recognition**

Use a three-entry set rather than a new strategy class:

```python
INDIVIDUAL_GLOBAL_RANKING_VERSIONS = frozenset({
    ("CN", "v13"), ("HK", "v11"), ("US", "v11"),
})


def _uses_individual_global_ranking(market: str, strategy_version: str | None) -> bool:
    return (market.upper(), strategy_version) in INDIVIDUAL_GLOBAL_RANKING_VERSIONS


def _candidate_global_sort_key(item: CandidateInput) -> tuple[object, ...]:
    global_strength = item.global_strength
    assert item.days is not None and item.amount is not None
    return (
        global_strength is None,
        -global_strength if global_strength is not None else Decimal("0"),
        -KNOWN_TEMPERATURE_ORDER[item.industry_temperature],
        item.days,
        -item.amount,
        item.symbol,
    )
```

In `build_candidate_list`, choose this key only for the three new versions; leave the legacy industry-context path untouched for every predecessor. Update `ALLOCATION_PROJECTION_VERSIONS`, `LEGACY_ALLOCATION_PROJECTION_VERSIONS`, recognized-version sets, drawdown predecessors, current entry/exit discipline sets, replay validators, parameter rows, and Kelly declarations. Add `("CN", "v13") -> v12`, `("HK", "v11") -> v10`, and `("US", "v11") -> v10` to `APPROVED_DRAWDOWN_PREDECESSORS`.

- [ ] **Step 4: Run version/ranking tests**

Run the Step 2 command again.

Expected: PASS. In addition, run:

```bash
rg -n 'v1[123]|ALLOCATION_PROJECTION_VERSIONS|CURRENT_(ENTRY|EXIT)_DISCIPLINES' \
  src/open_trader/a_share_trend.py src/open_trader/strategy_drawdown.py \
  src/open_trader/drawdown_preflight.py src/open_trader/trend_review.py
```

Inspect that new versions appear in every live/replay/risk guard and old versions remain present.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/a_share_trend.py src/open_trader/strategy_drawdown.py \
  src/open_trader/drawdown_preflight.py src/open_trader/trend_review.py \
  tests/test_a_share_trend.py tests/test_strategy_drawdown.py \
  tests/test_trend_kelly.py tests/test_trend_review.py
git commit -m "feat: version individual-first trend ranking"
```

---

### Task 2: Add one fail-closed staged candidate fetch

**Files:**
- Modify: `src/open_trader/a_share_trend.py`
- Test: `tests/test_a_share_trend.py`

**Interfaces:**
- Produces: `fetch_staged_candidates(api, candidate_ids, component_pools, held_symbols, holding_snapshots, expected_date, market, strategy_version, cny_per_local_currency, billing, resolve_bars) -> StagedCandidateFetch`.
- Produces: `StagedCandidateFetch.candidates`, `.industry_rows`, `.request_trace`, `.estimated_cost`, and `.estimate_complete`.
- Consumes: existing `evaluate_candidate`, `_candidate_reasons`, `load_industry_temperatures`, exact Trend Animals IDs, and a runner-supplied Futu bar resolver.

- [ ] **Step 1: Write failing waterfall and malformed-stage tests**

Use a recording fake API with candidates failing one successive gate. Assert exact IDs at each request:

```python
assert calls == [
    (IDENTITY_FIELDS, [1, 2, 3, 4, 5, 6, 7]),
    (LOCAL_STRENGTH_FIELDS, [2, 3, 4, 5, 6, 7]),
    (MARKET_CAP_FIELDS, [3, 4, 5, 6, 7]),
    (TEMPERATURE_FIELDS, [4, 5, 6, 7]),
    (DISCIPLINE_FIELDS, [5, 6, 7]),
    (A_SHARE_INDUSTRY_FIELDS, [700001, 700002]),
    (CANDIDATE_EXPANSION_FIELDS, [6, 7]),
]
```

Add parameterized duplicate-ID, missing-ID, extra-ID, and stale-date cases for every stage. Assert each raises `TrendAnimalsError`, no later stage is called, and `UNIFIED_TREND_FIELDS` is never requested for candidate IDs.

- [ ] **Step 2: Run the focused tests and confirm failure**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_a_share_trend.py -k 'staged_candidate or malformed_stage'
```

Expected: FAIL because the staged helper and field sets do not exist.

- [ ] **Step 3: Implement the staged field sets and helper**

Keep the stage order explicit and reuse `_candidate_reasons` by filtering reasons through a fixed stage map. The return contract is:

```python
@dataclass(frozen=True)
class StagedCandidateFetch:
    candidates: tuple[CandidateInput, ...]
    industry_contexts: tuple[IndustryContext, ...]
    industry_context_status: dict[str, object]
    industry_rows: tuple[Mapping[str, object], ...]
    request_trace: tuple[dict[str, object], ...]
    estimated_cost: Decimal
    estimate_complete: bool
```

The fixed field definitions and exact merge check are:

```python
IDENTITY_FIELDS = ("tmId", "tickerName", "tickerSymbol", "asset", "asOfDate")
LOCAL_STRENGTH_FIELDS = ("tmId", "asOfDate", "trendStrengthLocalCurr")
MARKET_CAP_FIELDS = ("tmId", "asOfDate", "marketCap")
TEMPERATURE_FIELDS = (
    "tmId", "asOfDate", "trendTemperaturePrev", "trendTemperatureCurr",
)
DISCIPLINE_FIELDS = (
    "tmId", "asOfDate", "tradableFlag", "industryTmId", "industryName",
    "amount1d", "isTrendRightSide", "daysSinceTrendEntry",
    "trendPhaseCurr", "stopwinFlagByDangerSignal",
)
CANDIDATE_EXPANSION_FIELDS = tuple(
    field for field in UNIFIED_TREND_FIELDS
    if field not in set(IDENTITY_FIELDS + LOCAL_STRENGTH_FIELDS + MARKET_CAP_FIELDS
                        + TEMPERATURE_FIELDS + DISCIPLINE_FIELDS)
)


def _merge_exact_snapshot_stage(rows_by_id, requested_ids, rows, expected_date):
    returned = [_row_tm_id(row) for row in rows]
    if returned != list(dict.fromkeys(returned)) or sorted(returned) != sorted(requested_ids):
        raise TrendAnimalsError("getTickerSnapshot stage returned mismatched tmIds")
    if any(row.get("asOfDate") != expected_date for row in rows):
        raise TrendAnimalsError("getTickerSnapshot stage returned a stale data date")
    for row in rows:
        rows_by_id.setdefault(_row_tm_id(row), {}).update(row)
```

At stages 1-6, construct the partial `CandidateInput`, call `_candidate_reasons`, and consider only reasons assigned to that stage or earlier. Resolve Futu bars/ATR only for stage-5 survivors. Fetch industry temperature once for unique stage-5 candidate industries plus required simulated/real holding industries. Fetch expansion fields only for candidates with no discipline reason. Return partial candidates for early failures so `signal_snapshots` can list their identity without paid expansion.

Do not catch a stage validation error. Do not call a complete-snapshot fallback.

- [ ] **Step 4: Run the focused staged-fetch tests**

Run the Step 2 command again.

Expected: PASS and the recording fake proves every later request is a strict subset of the previous stage.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/a_share_trend.py tests/test_a_share_trend.py
git commit -m "feat: stage trend candidate snapshots"
```

---

### Task 3: Wire all three runners and remove industry breadth cost

**Files:**
- Modify: `src/open_trader/a_share_trend.py`
- Modify: `src/open_trader/market_trend.py`
- Modify: `src/open_trader/trend_industry_context.py`
- Test: `tests/test_a_share_trend.py`
- Test: `tests/test_market_trend.py`

**Interfaces:**
- Consumes: `fetch_staged_candidates` from Task 2.
- Produces: new-version `industry_context_status.ordering_mode == "individual_global"`.
- Produces: zero eligible-industry component requests and zero industry-member/state breadth requests.

- [ ] **Step 1: Rewrite runner fakes as failing request-ledger tests**

For CN, HK, and US, assert:

```python
assert complete_snapshot_ids == simulated_holding_ids | real_only_holding_ids
assert all(candidate_id not in complete_snapshot_ids for candidate_id in nonheld_candidate_ids)
assert eligible_industry_component_calls == []
assert industry_member_snapshot_calls == []
assert industry_state_snapshot_calls == []
assert result.industry_context_status["ordering_mode"] == "individual_global"
```

Freeze the US 2026-08-07 ledger at `<= Decimal("2.852")`. For CN/HK fixtures, compute the expected budget from their exact recorded `(fields, ids)` stage ledger plus complete holding rows and assert equality; this makes any future field or survivor expansion fail without imposing a live global cap.

- [ ] **Step 2: Run runner/cost tests and confirm failure**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_a_share_trend.py -k 'runner or paid_scope or cost' \
  tests/test_market_trend.py -k 'report or snapshot or cost'
```

Expected: FAIL because the runners still fetch complete candidate snapshots and eligible-industry breadth.

- [ ] **Step 3: Integrate holdings-first and staged candidates in both runners**

In CN `_attempt_report` and HK/US `_attempt_market_report`:

1. Resolve simulated holding IDs.
2. Fetch `UNIFIED_TREND_FIELDS` only for those holding IDs.
3. Build holding snapshots and enrich real-only holdings through the unchanged complete path.
4. Call `fetch_staged_candidates` for non-held pool/favorite IDs, passing both simulated and real holding contexts.
5. Use the returned candidate rows, industry rows, request trace, and stage estimate in evidence and `api_facts`.

Add an opt-in argument to `calculate_industry_context` that removes only the legacy strength requirement:

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
    require_strength: bool = True,
) -> IndustryContext:
    if require_strength and strength is None:
        invalid_reasons.append("industry_strength_invalid")
```

New-version contexts call it with `component_tm_ids=()`, `member_rows=()`, `member_breadth_collected=False`, and `require_strength=False`, then attach prior temperature history. Legacy callers keep both defaults and therefore retain their old validity semantics.

Add `individual_global` to `INDUSTRY_ORDERING_MODES`; new reports use that status without consulting legacy strength/right-share validity, while predecessor modes keep their existing validation and fallback behavior.

Replace the estimate with the sum of actual staged field-row prices, complete holding rows, real-only complete rows, and unique industry-temperature rows. `estimate_complete` requires every requested field price plus the existing candidate-pool component cache facts; remove eligible-industry breadth from completeness.

- [ ] **Step 4: Run all runner and context tests**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_a_share_trend.py tests/test_market_trend.py \
  tests/test_trend_industry_context.py
```

Expected: PASS. Inspect emitted request traces and confirm `UNIFIED_TREND_FIELDS` appears only on holding requests.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/a_share_trend.py src/open_trader/market_trend.py \
  src/open_trader/trend_industry_context.py tests/test_a_share_trend.py \
  tests/test_market_trend.py tests/test_trend_industry_context.py
git commit -m "feat: remove candidate industry breadth cost"
```

---

### Task 4: Turn `risk_skips` into the final-plan audit

**Files:**
- Modify: `src/open_trader/a_share_trend.py`
- Test: `tests/test_a_share_trend.py`

**Interfaces:**
- Produces: `_final_plan_risk_skips(ranked, ordinary_skips, buy_actions, simulate_pairs, simulate_comparisons, real_pairs, real_comparisons, market, strategy_version) -> list[dict[str, object]]` using the existing `_risk_skip` row shape.
- Consumes: normal `buy_actions`, simulation/real rotation pairs and comparisons, full ranked qualified sequence, and ordinary `_plan_buy_actions` skips.

- [ ] **Step 1: Write failing frozen-report audit tests**

Use the 2026-08-07 US sequence and assert:

```python
assert [item.symbol for item in built.candidates] == [
    "GRMN", "WTW", "ABNB", "REGN", "TEAM", "CRWD", "HPQ", "PATH", "SWK", "WSM",
]
assert "GRMN" not in {item["symbol"] for item in built.risk_skips}
assert next(item for item in built.risk_skips if item["symbol"] == "WTW")["reason"] == (
    "10 个持仓席位已满；强度差 12.3 小于门槛 20"
)
assert next(item for item in built.risk_skips if item["symbol"] == "ABNB")["reason"] == (
    "10 个持仓席位已满；未进入 2 个轮换比较席位"
)
```

Add a candidate ranked below displayed Top 10 and assert it still appears in final audit. Add missing-global and planned-normal-BUY cases.

- [ ] **Step 2: Run focused report tests and confirm failure**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_a_share_trend.py -k 'final_plan_audit or missing_global or frozen_2026_08_07'
```

Expected: FAIL because planned rotation targets remain in ordinary full-slot skips and comparison reasons are not folded in.

- [ ] **Step 3: Implement final plan eligibility and audit after rotation planning**

Keep discipline qualification separate from plan eligibility:

```python
plan_eligible = tuple(
    item for item in candidate_decision.eligible
    if item.global_strength is not None and item.global_strength.is_finite()
) if _uses_individual_global_ranking(market, snapshot_version) else candidate_decision.eligible
```

Pass the full `plan_eligible` sequence, not `displayed_candidates`, to normal buying and rotation. After both rotation paths finish, derive planned symbols from normal and rotation BUY targets, index ordinary skips and comparisons by candidate symbol, and build one ordered `risk_skips` list from `candidate_decision.eligible`. Planned symbols are omitted. Missing global strength wins first; otherwise preserve the ordinary decisive reason and append the applicable rotation comparison outcome or the two-slot exclusion.

In `render_markdown`, emit the normal BUY heading and rows only when `report.buy_actions` is non-empty. Remove `无允许买入标的` and the appended `NO_ACTION_TEXT` from that empty branch. Replace detailed discipline-failure output with symbol/name plus `没有通过纪律`, after the qualified final-plan skips. Reduce new-version industry Markdown to name, temperature, and direction.

- [ ] **Step 4: Run report and Markdown tests**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_a_share_trend.py
```

Expected: PASS, with GRMN present only in rotation and WTW present only in final audit.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/a_share_trend.py tests/test_a_share_trend.py
git commit -m "feat: audit final trend buy plans"
```

---

### Task 5: Match the approved report Mock in the production Dashboard

**Files:**
- Modify: `src/open_trader/dashboard.py`
- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Test: `tests/test_dashboard.py`
- Test: `tests/test_dashboard_web.py`

**Interfaces:**
- Consumes: top-level `risk_skips`, `audit.candidates`, normal BUY actions, and rotation pairs from Tasks 3-4.
- Produces: one existing-language report workspace; no new navigation or interaction model.

- [ ] **Step 1: Write failing projection and HTML contract tests**

For each market, render a report containing one rotation BUY, two qualified final skips, and two discipline failures. Assert:

```javascript
for (const text of [
  "候选审计 · 为什么没有进入买入计划",
  "通过纪律，但未纳入最终计划",
  "全局强度缺失，无法排序",
  "最后 · 没有通过纪律 2",
]) if (!html.includes(text)) throw new Error(text);

for (const text of [
  "无允许买入标的", "模拟盘正式买入计划",
  "行业趋势强度", "温转热数量", "右侧个数占比", "右侧市值占比",
]) if (html.includes(text)) throw new Error(text);
```

Also assert the planned rotation BUY symbol never appears in a skip row, discipline failures are after qualified skips, and a non-empty normal BUY still renders the existing table/accessibility labels.

- [ ] **Step 2: Run Dashboard tests and confirm failure**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_dashboard.py tests/test_dashboard_web.py -k 'trend'
```

Expected: FAIL because the current Dashboard renders an empty BUY stage, detailed discipline failures, and seven industry columns.

- [ ] **Step 3: Make the smallest renderer/projection changes**

In `dashboard.py`, use `signal_snapshots["candidates"]` for audit candidates in every market, not CN only. Keep projected `risk_skips` unchanged.

In `dashboard.js`:

```javascript
const buyStage = cnTrendRows(report.buy_actions).length
  ? renderTrendBuyStage(report)
  : "";
```

Remove the duplicate real-rotation table from `renderTrendBuyStage`; `renderTrendRotations` already owns automatic and manual rotation. Rework the existing trend audit renderer to take qualified rows from `report.risk_skips`, then render `audit.candidates.filter(item => item.eligible === false)` in a final collapsed group with only identity and `没有通过纪律`. Render new industry rows with three columns: industry, current temperature, temperature direction. Reuse existing table/details classes; do not add CSS unless a focused browser check finds a real overflow or accessibility failure.

- [ ] **Step 4: Run Dashboard tests and a focused live browser check**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_dashboard.py tests/test_dashboard_web.py
```

Then serve the real Dashboard from the feature worktree, open the current report at desktop and mobile widths, expand the audit, and verify no horizontal page overflow, no console error, the empty normal BUY section is absent, and rotation/audit hierarchy matches the approved Mock. Do not run `make acceptance` here.

Expected: tests PASS and focused browser checks show the approved hierarchy.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/dashboard.py src/open_trader/dashboard_static/dashboard.js \
  tests/test_dashboard.py tests/test_dashboard_web.py
git commit -m "feat: show final trend candidate audit"
```

---

### Task 7: Add a no-submit three-market revision publisher

**Files:**
- Create: `scripts/regenerate_trend_reports_no_submit.py`
- Create: `tests/test_trend_report_regeneration.py`

**Interfaces:**
- Produces: `stage_and_publish(config, *, publish: bool) -> dict[str, object]`.
- Consumes: existing `run_a_share_trend_report`, `run_market_trend_report`, `NullNotifier`, immutable report validation, and stdlib `TemporaryDirectory`/exclusive file creation.

- [ ] **Step 1: Write failing all-market/no-submit tests**

Fake each report generator and assert all three are called with `revision=True` and `NullNotifier`. If HK fails, assert no staged report is copied into the real reports directory. On success, assert all six `.json`/`.md` files are created with exclusive writes, prior revisions remain byte-identical, and the manifest records old/new hashes plus costs.

- [ ] **Step 2: Run the publisher tests and confirm failure**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_trend_report_regeneration.py
```

Expected: FAIL because the publisher does not exist.

- [ ] **Step 3: Implement staging and immutable publication**

Copy only the three existing report directories into a temporary reports root so revision numbering remains correct. Point a copied config at that temporary `reports_dir` while retaining the real read-only market/account inputs. Call the report generators directly with `revision=True` and `NullNotifier`; never import or call `run_trend_market_controller`, `_execute_locked_report`, or any order client.

Validate before publication:

```python
expected_versions = {"CN": "v13", "HK": "v11", "US": "v11"}
for market, artifact in staged.items():
    payload = json.loads(artifact.json_path.read_text())
    assert payload["strategy_snapshot"]["strategy_version"] == expected_versions[market]
    assert Decimal(payload["actual_api_cost"] or "0") >= 0
    assert artifact.previous_path.read_bytes() == artifact.previous_bytes
```

When `publish=True`, create each final file with `O_CREAT | O_EXCL`; if a path already exists, accept it only when bytes match. Because the Dashboard/controllers are stopped during release, publish the validated set, then restart readers only after all six files and referenced replay evidence validate. Never overwrite or delete a prior revision.

- [ ] **Step 4: Run publisher tests**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_trend_report_regeneration.py
```

Expected: PASS, with `submitted_orders == 0` in the returned manifest and no controller/order fake called.

- [ ] **Step 5: Commit**

```bash
git add scripts/regenerate_trend_reports_no_submit.py \
  tests/test_trend_report_regeneration.py
git commit -m "feat: publish trend revisions without orders"
```

---

### Task 8: Full verification, release, regeneration, and final acceptance

**Files:**
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: all earlier tasks, the existing launchd installers, Dashboard acceptance gate, and the no-submit publisher.
- Produces: exact accepted SHA live with three current new-version revisions and a verified review URL.

- [ ] **Step 1: Run all relevant automated tests before the final gate**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_a_share_trend.py tests/test_market_trend.py \
  tests/test_trend_industry_context.py tests/test_trend_review.py \
  tests/test_strategy_drawdown.py tests/test_trend_kelly.py \
  tests/test_dashboard.py tests/test_dashboard_web.py \
  tests/test_trend_report_regeneration.py
```

Expected: PASS with the exact count recorded in the operator log.

- [ ] **Step 2: Record the deterministic three-market cost ledgers**

Inspect the focused CN/HK/US request-ledger tests, estimated cost, actual debit completeness, and confirm US `<= 2.852`, no eligible-industry components, no member fields, and no earlier-stage failures in later paid requests.

- [ ] **Step 3: Update and commit the changelog before merge**

Add a dated entry containing the three new versions, unchanged discipline/risk boundary, exact frozen CN/HK/US cost results, new audit behavior, and no-submit revision procedure.

```bash
git add CHANGELOG.md
git commit -m "docs: log three-market trend cost cut"
```

- [ ] **Step 4: Deploy with controllers stopped and stage the latest live reports**

Record the pre-release runtime SHA and current report hashes. Stop the three trend controllers so no old process can submit or regenerate. Deploy the feature SHA to the review runtime, verify the Dashboard process SHA, but keep report readers unavailable until all three staged revisions validate.

Run first without publication:

```bash
.venv/bin/python scripts/regenerate_trend_reports_no_submit.py \
  --config config/daily_premarket.env
```

Expected: `status=PASS`, versions v13/v11/v11, non-negative actual debit, honest estimate completeness, zero submitted orders, and three staged revisions. Inspect discipline set, rank, audit, rotation, request trace, estimate, and actual debit for each market.

- [ ] **Step 5: Publish all three revisions and verify current selection**

Run:

```bash
.venv/bin/python scripts/regenerate_trend_reports_no_submit.py \
  --config config/daily_premarket.env --publish
```

Verify each new JSON/Markdown pair has a higher immutable revision, each prior revision still exists with its original hash, Dashboard selects the new revision, and no broker order/action ledger was created. If any market fails, leave readers/controllers stopped and redeploy the recorded prior SHA; do not describe any new version as current.

- [ ] **Step 6: Run `make acceptance` once as the final Dashboard gate**

Restart the required review services/controllers from the feature worktree exact SHA, then run:

```bash
make acceptance
```

Expected: final result `PASS`. `FAIL` must be fixed and the gate rerun; `BLOCKED` must be reported as blocked. Do not substitute screenshots, curl, fixtures, or focused tests.

- [ ] **Step 7: Redeploy the exact accepted SHA and verify runtime evidence**

Redeploy the exact SHA accepted in Step 6. Verify:

```bash
git rev-parse HEAD
launchctl print gui/$(id -u)/com.open-trader.dashboard
lsof -nP -iTCP:8766 -sTCP:LISTEN
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8766/
```

Inspect PID, cwd, Git SHA, fresh log timestamps, controller SHA/phase, all three current report artifacts and versions, and HTTP `200`. This exact-SHA restart needs no second acceptance run.

- [ ] **Step 8: Fast-forward local main and hand off the review URL**

Confirm the worktree is clean and `CHANGELOG.md` is already committed. Fast-forward local `main` to the accepted SHA without including unrelated root changes. The runtime SHA must remain identical to the accepted/merged SHA. Provide `http://127.0.0.1:8766/` and the exact focused-test count, acceptance `PASS`, report filenames/hashes, costs, PID/cwd/SHA/log evidence, and confirmation that no order was submitted.
