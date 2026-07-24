# Trend Industry Breadth, Sorting, Cost, and Discipline Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add auditable industry breadth to CN, US, and HK trend candidate ordering, show each report's own honest API cost, preserve the approved Kelly samples across the new strategy versions, and replace the long discipline lists with frozen lifecycle cards while retaining every existing report component.

**Architecture:** Add one small pure-Python `trend_industry_context` module for breadth validation, prior-day comparison, and atomic daily history. Keep API calls and candidate construction in the existing report runners, pass frozen industry facts into `build_report`, and let the existing report JSON remain the Dashboard source of truth. Extend the current vanilla-JavaScript report workspace with native lifecycle `<details>` cards and CSS Grid; do not add a service layer, database, scoring model, or frontend dependency.

**Tech Stack:** Python 3.12, dataclasses, `Decimal`, JSON files, pytest, existing Trend Animals/Futu clients, existing vanilla JavaScript/CSS Dashboard, Node VM frontend tests, Playwright-backed Dashboard acceptance.

## Global Constraints

- Work only in `/Users/ray/projects/open_trader/.worktrees/trend-industry-breadth-discipline-dashboard` on branch `feat/trend-industry-breadth-discipline-dashboard`, based on local `main` commit `e1ec1cf`.
- Preserve all existing entry gates, position sizing, risk limits, drawdown behavior, protection lines, exits, delivery receipts, locks, candidate limit, and “try the next candidate” behavior.
- CN, US, and HK remain independent reports and independent API-cost boundaries.
- Query new industry strength/member breadth only after existing hard gates have
  identified eligible candidates, and only for their distinct `industryTmId`
  values. Preserve CN's existing temperature-only lookup because that value is
  itself an established hard-gate input.
- Use deterministic lexicographic ordering, never a weighted score.
- If one current eligible-industry context is invalid, use the exact legacy individual ordering for the whole report.
- If one eligible industry lacks valid earlier local history, omit both history-dependent sort keys for the whole report.
- Do not backfill history from today's universe and do not call a nonexistent historical API.
- Version the strategy as CN v8, US v5, and HK v5 without restarting the approved review samples.
- Render discipline from the selected report's frozen `strategy_snapshot.parameter_rows`; never import current rules into a historical report view.
- Use the existing design tokens, native `<details>/<summary>`, 44 px interaction targets, text plus color for states, and no page-level overflow at 375 px.
- Do not add dependencies, abstractions for hypothetical future metrics, broad exception catches, placeholders, `TODO`, or `TBD`.
- Use `apply_patch` for edits. Preserve unrelated working-tree changes.
- Run focused tests while developing. Run `make acceptance` only once all implementation and focused verification are green; it is the final Dashboard gate.
- Before any future merge into `main`, include and commit the dated operator-facing `CHANGELOG.md` entry.
- Only an acceptance `PASS` can be called complete. After `PASS`, redeploy the exact accepted Git SHA and verify PID, cwd, SHA, fresh logs, and HTTP 200.

---

## Task 1: Build Pure Industry-Context Calculation and History

**Files:**

- Create: `src/open_trader/trend_industry_context.py`
- Create: `tests/test_trend_industry_context.py`

**Interface introduced:**

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
    prior_as_of_date: str | None = None
    prior_temperature: str | None = None
    prior_right_share: Decimal | None = None
    temperature_direction: str | None = None
    right_share_change_pp: Decimal | None = None


def calculate_industry_context(
    *,
    industry_tm_id: int,
    industry: str,
    expected_date: str,
    component_tm_ids: Sequence[int],
    member_rows: Sequence[Mapping[str, object]],
    industry_row: Mapping[str, object] | None,
    warm_to_hot_count: int,
) -> IndustryContext: ...


def attach_prior_context(
    contexts: Sequence[IndustryContext],
    prior_by_industry: Mapping[int, IndustryContext],
) -> tuple[IndustryContext, ...]: ...


def load_latest_prior_context(
    history_root: Path,
    *,
    market: str,
    before_date: str,
) -> dict[int, IndustryContext]: ...


def write_industry_context_history(
    history_root: Path,
    *,
    market: str,
    generated_at: str,
    strategy_version: str,
    contexts: Sequence[IndustryContext],
) -> Path: ...
```

History location:

```text
data/trend_industry_context/CN/2026-07-24.json
data/trend_industry_context/US/2026-07-24.json
data/trend_industry_context/HK/2026-07-24.json
```

- [ ] **Step 1: Write failing tests for denominator and validation**

Cover these exact facts in `tests/test_trend_industry_context.py`:

- component IDs are deduplicated;
- duplicate member snapshot rows do not inflate counts;
- only exact-date rows contribute;
- `right_share = right_count / valid_count`;
- snapshot coverage uses `snapshot_count / component_count`;
- right-state coverage uses boolean-state rows over tradable rows;
- the context is valid only with at least 10 components, at least 90% snapshot coverage, at least 90% boolean coverage among tradable rows, at least 10 valid rows, a known temperature, and finite strength from 0 through 100;
- malformed, stale, and missing inputs record stable machine reasons instead of becoming zero.

Use expected reason strings:

```python
(
    "component_count_below_10",
    "snapshot_coverage_below_90pct",
    "right_state_coverage_below_90pct",
    "valid_count_below_10",
    "industry_temperature_invalid",
    "industry_strength_invalid",
)
```

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_trend_industry_context.py -v
```

Expected: FAIL because the module does not exist.

- [ ] **Step 2: Implement the minimal calculation**

Implement:

- known temperature order `("冻", "寒", "凉", "平", "温", "热", "沸")`;
- strict positive integer ID parsing without accepting booleans;
- finite `Decimal` strength validation;
- exact-date row filtering;
- one member row per requested component ID;
- exact counts and unrounded stored ratios.

Do not import `CandidateInput`; accept the precomputed warm-to-hot count so the new module remains independent of the report module.

Run the Task 1 tests again.

Expected: calculation tests PASS.

- [ ] **Step 3: Write failing tests for prior comparison**

Prove:

- temperature direction is `rising`, `unchanged`, or `falling`;
- right-share change is stored in percentage points:

```python
Decimal("0.279") - Decimal("0.221") == Decimal("0.058")
right_share_change_pp == Decimal("5.8")
```

- an invalid prior context is ignored;
- when one current eligible industry has no valid prior, callers can detect that history-dependent ordering is unavailable for the entire report;
- the latest valid date strictly earlier than `before_date` wins, not the previous calendar date.

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_trend_industry_context.py -k 'prior or direction or change' -v
```

Expected: FAIL because comparison and history functions are incomplete.

- [ ] **Step 4: Implement comparison and atomic history**

Serialize:

```json
{
  "schema_version": "open_trader.trend_industry_context.v1",
  "market": "CN",
  "as_of_date": "2026-07-24",
  "generated_at": "2026-07-24T18:30:00+08:00",
  "strategy_version": "v8",
  "industries": []
}
```

Requirements:

- write through `NamedTemporaryFile` in the target directory and `Path.replace`;
- reject a malformed schema, mismatched market, mismatched stored date, duplicate industry ID, or non-object industry row;
- scan only `YYYY-MM-DD.json` files strictly earlier than `before_date`;
- skip invalid earlier contexts and select the latest valid observation per industry;
- use stored prior observations only; never synthesize values from current members.

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_trend_industry_context.py -v
```

Expected: all new tests PASS.

- [ ] **Step 5: Commit the pure domain slice**

```bash
git add src/open_trader/trend_industry_context.py tests/test_trend_industry_context.py
git commit -m "feat: calculate trend industry breadth"
```

Expected: commit contains only the new module and its tests.

---

## Task 2: Integrate Contextual Candidate Ordering and Frozen Report Facts

**Files:**

- Modify: `src/open_trader/a_share_trend.py`
- Modify: `tests/test_a_share_trend.py`
- Modify: `tests/test_trend_review.py`

**Interface changes:**

Add optional arguments without changing legacy callers:

```python
def build_candidate_list(
    rows: Sequence[CandidateInput],
    *,
    held_symbols: set[str],
    expected_date: str | None = None,
    market: str = "CN",
    industry_contexts: Mapping[int, IndustryContext] | None = None,
) -> CandidateDecision: ...


def build_report(
    ...,
    industry_contexts: Sequence[IndustryContext] = (),
    industry_context_status: Mapping[str, object] | None = None,
    estimated_api_cost_complete: bool = True,
) -> TrendReport: ...
```

Store the frozen data as explicit `TrendReport` fields:

```python
industry_contexts: tuple[IndustryContext, ...]
industry_context_status: dict[str, object]
estimated_api_cost_complete: bool
```

Use only these ordering-mode values:

```text
context_with_history
context_current_only
legacy_invalid_current
legacy_no_eligible_candidates
```

`context_current_only` omits keys 1 and 5 for the entire report.
`legacy_invalid_current` omits keys 1–6 for the entire report and records the
affected industry IDs plus validation reasons. `legacy_no_eligible_candidates`
is an explicit no-op state, not a data-quality error.

- [ ] **Step 1: Write failing ordering tests**

In `tests/test_a_share_trend.py`, create eligible candidates whose existing individual keys conflict with industry keys. Assert the exact order:

1. temperature direction `rising`, `unchanged`, `falling`;
2. current temperature by official order, descending;
3. industry strength descending;
4. warm-to-hot count descending;
5. right-share percentage-point change descending;
6. current right-share descending;
7. individual strength descending;
8. individual right-side days ascending;
9. amount descending;
10. symbol ascending.

Add separate tests that prove:

- candidates in the same industry share one context;
- current contexts valid plus one missing prior omit keys 1 and 5 for every candidate;
- one invalid or missing current context restores exact legacy ordering keys 7–10 for every candidate;
- a hard-gate failure still excludes a candidate even when its industry ranks first;
- missing `industryTmId` on an otherwise eligible candidate triggers whole-report legacy fallback.

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_a_share_trend.py -k 'candidate and industr' -v
```

Expected: FAIL because contextual sorting is not wired.

- [ ] **Step 2: Implement one deterministic key builder**

Keep `_candidate_sort_key` unchanged as the legacy key. Add:

```python
def _candidate_context_sort_key(
    item: CandidateInput,
    context: IndustryContext,
    *,
    include_history: bool,
) -> tuple[object, ...]:
    history = (
        (
            TEMPERATURE_DIRECTION_ORDER[context.temperature_direction],
            -context.right_share_change_pp,
        )
        if include_history
        else ()
    )
    return (
        *(history[:1]),
        -KNOWN_TEMPERATURE_ORDER[context.temperature],
        -context.strength,
        -context.warm_to_hot_count,
        *(history[1:]),
        -context.right_share,
        *_candidate_sort_key(item),
    )
```

The concrete implementation must avoid optional-value type ignores by entering this function only after validating the context. Determine `use_current_context` and `include_history` once per candidate decision, not per item.

Run the ordering tests.

Expected: PASS.

- [ ] **Step 3: Write failing report serialization/replay tests**

Assert `_report_payload(report)` includes:

```json
{
  "industry_context_status": {
    "ordering_mode": "context_with_history",
    "current_complete": true,
    "history_complete": true,
    "fallback_reason": null
  },
  "industry_contexts": [
    {
      "industry_tm_id": 621707,
      "right_count": 34,
      "valid_count": 122,
      "right_share": "0.278688524590...",
      "right_share_change_pp": "5.8"
    }
  ],
  "api_cost": {
    "actual": "0.610",
    "estimated": "0.479",
    "estimate_complete": false,
    "unit": "Trend Animals 余额单位"
  }
}
```

Keep the existing top-level `actual_api_cost` and `estimated_api_cost` fields for compatibility. Add `api_cost` as the canonical presentation object rather than deleting prior fields.

For each serialized candidate, add a frozen `ordering_context` object that
contains its resolved industry ID, report-wide ordering mode, and the exact
contextual keys that applied. In legacy mode the object must say
`"applied": false` and name the fallback reason; it must not populate invalid
context values with zero. Candidate order and candidate ordering facts must
therefore be independently auditable without consulting current live data.

In `tests/test_trend_review.py`, prove report evidence rebuild preserves the exact frozen context, ordering status, estimate completeness, and candidate order.

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_a_share_trend.py -k 'payload or replay or industry_context' \
  tests/test_trend_review.py -k 'rebuild and trend' -v
```

Expected: FAIL on missing payload facts.

- [ ] **Step 4: Serialize the frozen context without recalculation**

Update:

- `TrendReport`;
- `build_report`;
- `_report_payload`;
- report-evidence normalization/rebuild paths.

`build_report` passes the same context mapping to `build_candidate_list` and freezes exactly the contexts used. It must not re-read history or query APIs.

Use `_json_value` for `Decimal` serialization and preserve old report loading behavior when the new fields are absent.

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_a_share_trend.py tests/test_trend_review.py -q
```

Expected: both files PASS.

- [ ] **Step 5: Commit ordering and payload**

```bash
git add src/open_trader/a_share_trend.py tests/test_a_share_trend.py tests/test_trend_review.py
git commit -m "feat: rank trend candidates by industry context"
```

Expected: commit contains contextual ordering and frozen report facts, with no API runner changes.

---

## Task 3: Collect Eligible-Industry Context in CN, US, and HK Runners

**Files:**

- Modify: `src/open_trader/a_share_trend.py`
- Modify: `src/open_trader/market_trend.py`
- Modify: `tests/test_a_share_trend.py`
- Modify: `tests/test_market_trend.py`
- Test: `tests/test_trend_industry_context.py`

**Shared constants:**

```python
INDUSTRY_MEMBER_FIELDS = (
    "tmId",
    "asOfDate",
    "tradableFlag",
    "isTrendRightSide",
)
INDUSTRY_STATE_FIELDS = (
    "tmId",
    "asOfDate",
    "trendTemperatureCurr",
    "trendStrengthLocalCurr",
)
```

- [ ] **Step 1: Write failing runner tests for the collection boundary**

Extend the fake Trend Animals clients in both report test files. Assert:

- candidate and holding snapshot/K-line evaluation runs before the new
  eligible-industry breadth/strength requests;
- CN keeps its existing early industry-temperature lookup because industry
  temperature is already an entry hard gate; that lookup does not fetch
  breadth or industry strength;
- existing hard gates identify the eligible candidates after the established
  inputs, including CN industry temperature, are available;
- only distinct eligible `industryTmId` values receive `get_components`;
- an excluded candidate's industry is never queried;
- member IDs are unioned and deduplicated into one minimal snapshot call;
- industry state uses one minimal snapshot call;
- warm-to-hot counts use distinct candidate-pool IDs grouped by industry, including candidates later removed by unrelated hard gates;
- a failed industry API request follows the existing report retry/failure path;
- complete-but-invalid current breadth still creates a report in legacy ordering mode;
- balance-after is read only after every report-attributed Trend Animals call;
- same-date cache events remain in metadata and no request is repeated during receipt recovery.

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_a_share_trend.py -k 'report_runner and industry' \
  tests/test_market_trend.py -k 'report and industry' -v
```

Expected: FAIL because no runner performs the new eligible-only breadth
collection and US/HK have no industry-context request at all.

- [ ] **Step 2: Add one shared collection helper**

Keep the helper in `a_share_trend.py` because both runners already import shared report construction from there:

```python
def collect_industry_contexts(
    *,
    api: object,
    candidates: Sequence[CandidateInput],
    candidate_rows: Sequence[Mapping[str, object]],
    held_symbols: set[str],
    expected_date: str,
    market: str,
    history_root: Path,
) -> tuple[tuple[IndustryContext, ...], dict[str, object], dict[str, object]]:
    """Return contexts, ordering status, and API audit facts."""
```

Within it:

1. call `build_candidate_list` without contextual ordering to identify
   hard-gate-eligible candidates; for CN, the candidates already contain the
   industry temperature obtained by the existing entry-filter lookup;
2. collect their distinct industry IDs;
3. call existing `api.get_components(..., expected_date=...)`;
4. union/deduplicate member IDs;
5. call `get_snapshots` with `INDUSTRY_MEMBER_FIELDS`;
6. call `get_snapshots` with `INDUSTRY_STATE_FIELDS`;
7. calculate warm-to-hot counts from the original candidate pool rows;
8. calculate current context;
9. read the latest earlier valid local context;
10. attach prior facts;
11. compute the report-wide `ordering_mode`.

Return audit counts and field names for `api_facts`; do not create a client protocol or a class hierarchy.

- [ ] **Step 3: Reorder the CN runner**

In `_attempt_report`:

- retain the initial candidate/holding unified snapshot;
- retain the old early all-industry temperature-only request needed by the CN
  entry hard gate, but do not add strength or member breadth to that request;
- evaluate candidate K-lines and construct `CandidateInput` first;
- collect context only for hard-gate-eligible industry IDs;
- supply current industry temperature from the collected context to candidate and holding display fields where available;
- read `balance_after` after context collection;
- calculate snapshot estimate from all paid snapshot requests;
- set `estimated_api_cost_complete=False` whenever any candidate-pool or
  industry component request cannot be proven to be a same-date cache hit,
  because the public billing catalog has no contractual component-call price;
- pass contexts and status into `build_report`.

The actual cost stays:

```python
balance_delta = balance_before - balance_after
actual_cost = balance_delta if balance_delta >= 0 else None
```

- [ ] **Step 4: Apply the same flow to US/HK**

In `market_trend._attempt_report`:

- use the same shared collector after candidate evaluation;
- keep the market-specific symbol normalization, Futu K-lines, account load, and HK lot sizes unchanged;
- move `balance_after` below all report-attributed context calls;
- use `paths.root.parent / "trend_industry_context"` as the shared history root;
- pass the frozen contexts/status/cost completeness to `build_report`.

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_a_share_trend.py \
  tests/test_market_trend.py \
  tests/test_trend_industry_context.py -q
```

Expected: all three files PASS.

- [ ] **Step 5: Freeze history only with a frozen report**

Call `write_industry_context_history` immediately after `_freeze_receipt_report` creates or validates the final Markdown/JSON pair. Also call it from receipt recovery when the final pair is reconstructed and the history file is absent.

The write must:

- deserialize contexts from the already frozen report payload;
- be idempotent for the same market/date/content;
- reject a conflicting same-date context artifact;
- never refetch or recalculate paid data.

Add recovery tests for both CN and shared US/HK delivery paths.

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_a_share_trend.py -k 'receipt or context_history' \
  tests/test_market_trend.py -k 'receipt or context_history' -v
```

Expected: PASS and fake API assertions show no recovery refetch.

- [ ] **Step 6: Commit live collection**

```bash
git add \
  src/open_trader/a_share_trend.py \
  src/open_trader/market_trend.py \
  tests/test_a_share_trend.py \
  tests/test_market_trend.py
git commit -m "feat: collect eligible industry breadth in trend reports"
```

Expected: one runner-focused commit; no Dashboard edits.

---

## Task 4: Version CN v8, US v5, HK v5 Without Resetting Samples

**Files:**

- Modify: `src/open_trader/a_share_trend.py`
- Modify: `src/open_trader/trend_kelly.py`
- Modify: `src/open_trader/trend_api_stats.py` only if display projection needs the contributing version list
- Modify: `tests/test_a_share_trend.py`
- Modify: `tests/test_market_trend.py`
- Modify: `tests/test_trend_kelly.py`
- Modify: `tests/test_trend_api_stats.py`
- Modify: `纪律.md`

- [ ] **Step 1: Write failing version and identity tests**

Assert default live versions:

```python
live_trend_strategy_snapshot("CN", sha, pools)["strategy_version"] == "v8"
live_trend_strategy_snapshot("US", sha, pools)["strategy_version"] == "v5"
live_trend_strategy_snapshot("HK", sha, pools)["strategy_version"] == "v5"
```

Assert exact accepted sample identities:

```text
CN v8 <- CN v4, CN v7, CN v8
US v5 <- US v4, US v5
HK v5 <- HK v4, HK v5
```

Assert rejection of:

```text
CN v5, CN v6, CN US/HK identities, US v1-v3, HK v1-v3,
wrong strategy_id, wrong opening_strategy_version
```

Assert `strategy_snapshot.parameters.kelly_sample_inherits` explicitly lists every inherited identity and that report/API stats expose contributing opening versions.

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_a_share_trend.py -k 'strategy_snapshot' \
  tests/test_market_trend.py -k 'strategy_snapshot or live_strategy' \
  tests/test_trend_kelly.py \
  tests/test_trend_api_stats.py -k 'identity or version or inherits' -v
```

Expected: FAIL on new versions and inheritance sets.

- [ ] **Step 2: Replace the one-off v7 matcher with explicit target sets**

In `trend_kelly.py`, define immutable identity sets for all new targets and one map:

```python
TREND_KELLY_SAMPLE_IDENTITIES = {
    CN_V8_KELLY_IDENTITY: frozenset({
        CN_V4_KELLY_IDENTITY,
        CN_V7_KELLY_IDENTITY,
        CN_V8_KELLY_IDENTITY,
    }),
    US_V5_KELLY_IDENTITY: frozenset({
        US_V4_KELLY_IDENTITY,
        US_V5_KELLY_IDENTITY,
    }),
    HK_V5_KELLY_IDENTITY: frozenset({
        HK_V4_KELLY_IDENTITY,
        HK_V5_KELLY_IDENTITY,
    }),
}
```

`trend_kelly_identity_matches` consults this map and otherwise retains exact identity behavior. Do not implement recursive inheritance.

- [ ] **Step 3: Update frozen strategy rows**

`live_trend_strategy_snapshot` must:

- default CN to v8 and US/HK to v5;
- continue validating old supported frozen versions used in replay;
- add new `候选排序` parameter rows for the exact contextual keys and fallback;
- add data-quality and fee semantics rows in the existing appropriate groups;
- set explicit inherited identities;
- retain every other strategy parameter unchanged.

Update all version allowlists used for Kelly/risk/report validation so v8/v5 receives the same existing risk machinery.

Run the focused tests again.

Expected: PASS.

- [ ] **Step 4: Advance the human discipline document**

Update `纪律.md`:

- document version 1 → 2;
- machine CN version → v8;
- list industry member/state fields;
- record all ten ordering keys;
- record whole-report current-context fallback;
- record whole-report omission of history keys;
- record exact fee label rules and balance unit;
- record the approved non-recursive sample inheritance.

Do not change unrelated entry, sizing, protection, exit, or drawdown discipline.

Run:

```bash
rg -n '文档版本|v8|右侧|温转热|实扣|估算|v4|v7' 纪律.md
rg -n 'v5|v6.*继承|重新积累|TODO|TBD' 纪律.md
```

Expected: the first command shows the new rules; the second shows no accidental CN v5/v6 inheritance and no placeholders.

- [ ] **Step 5: Commit version continuity**

```bash
git add \
  src/open_trader/a_share_trend.py \
  src/open_trader/trend_kelly.py \
  src/open_trader/trend_api_stats.py \
  tests/test_a_share_trend.py \
  tests/test_market_trend.py \
  tests/test_trend_kelly.py \
  tests/test_trend_api_stats.py \
  纪律.md
git commit -m "feat: version contextual trend selection"
```

Expected: strategy/version/docs commit with no UI changes.

---

## Task 5: Make API Cost Semantics Consistent in Every Report Surface

**Files:**

- Modify: `src/open_trader/a_share_trend.py`
- Modify: `src/open_trader/dashboard.py`
- Modify: `tests/test_a_share_trend.py`
- Modify: `tests/test_market_trend.py`
- Modify: `tests/test_dashboard.py`

**Shared formatter:**

```python
def trend_api_cost_label(
    *,
    actual: Decimal | None,
    estimated: Decimal | None,
    estimate_complete: bool,
) -> str:
    ...
```

Required outputs:

```text
本报告 API 费用：实扣 0.610 Trend Animals 余额单位
本报告 API 费用：估算 0.479 Trend Animals 余额单位（实扣不可得）
本报告 API 费用：未知（快照估算 0.479 Trend Animals 余额单位；成分费用未计）
```

- [ ] **Step 1: Write failing formatter and render tests**

Cover all three branches plus:

- zero actual cost is displayed as actual, not missing;
- negative/non-finite balances remain unavailable upstream;
- Markdown contains exactly one canonical cost line;
- Feishu contains the same canonical line;
- JSON retains raw actual, estimated, completeness, and unit;
- CN/US/HK each use only their own balance delta;
- a manual probe made before/after the runner is not present in report cost.

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_a_share_trend.py -k 'cost or markdown or feishu' \
  tests/test_market_trend.py -k 'cost or balance' -v
```

Expected: FAIL because Feishu has no cost and existing Markdown labels are inconsistent.

- [ ] **Step 2: Implement one formatter and reuse it**

Use the formatter from:

- `_report_payload` canonical `api_cost.label`;
- `render_markdown`;
- `render_trend_feishu_text`.

Do not use a currency symbol. Keep number formatting stable by trimming insignificant trailing zeroes while retaining `0` for a zero fee.

- [ ] **Step 3: Project frozen cost and discipline rows to Dashboard**

In `dashboard._project_broker_trend_report`, add:

```python
{
    "api_cost": payload.get("api_cost"),
    "industry_context_status": payload.get("industry_context_status"),
    "industry_contexts": payload.get("industry_contexts", []),
    "strategy_parameter_rows": strategy_snapshot.get("parameter_rows", []),
}
```

Validate types and fail closed for malformed new-version facts. Preserve legacy projections with empty context/rows and the old raw cost values.

Add `tests/test_dashboard.py` coverage proving:

- latest and historical endpoints project identical frozen facts;
- selected historical reports keep their own `parameter_rows`;
- CN, US, and HK all project discipline rows;
- Dashboard does not call `live_trend_strategy_snapshot`;
- raw audit cost fields are still present.

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_a_share_trend.py \
  tests/test_market_trend.py \
  tests/test_dashboard.py -q
```

Expected: PASS.

- [ ] **Step 4: Commit cost and projection**

```bash
git add \
  src/open_trader/a_share_trend.py \
  src/open_trader/dashboard.py \
  tests/test_a_share_trend.py \
  tests/test_market_trend.py \
  tests/test_dashboard.py
git commit -m "feat: report exact trend api cost"
```

Expected: backend/report projection commit; no frontend files.

---

## Task 6: Render the Full Discipline Lifecycle Dashboard

**Files:**

- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Modify: `src/open_trader/dashboard_static/dashboard.css`
- Modify: `tests/test_dashboard_web.py`
- Modify: `src/open_trader/dashboard_acceptance.py`
- Modify: `tests/test_dashboard_acceptance.py`

- [ ] **Step 1: Write failing frontend tests for frozen lifecycle mapping**

Replace hard-coded `renderCnTrendDisciplines()` expectations with a market-neutral renderer fed by `report.strategy_parameter_rows`.

Test exact group mapping:

```text
候选来源 + 入场过滤 -> 入场硬门槛
候选排序 -> 确定性排序
仓位执行 -> 仓位与执行
退出保护 establishment/tracking rows -> 持有管理
退出保护 forced-exit/partial-profit rows -> 退出纪律
累计回撤 -> existing risk summary, not a duplicate lifecycle card
```

Use exact row-name classification sets derived from the existing frozen labels. Unknown rows remain visible in an “其他纪律” card rather than disappearing.

Assert:

- all five primary cards appear for CN, US, and HK;
- the old report's frozen row value renders when historical mode is selected;
- no current hard-coded v8 rule leaks into an old report;
- each native summary exposes card title, compact key facts, affected-count text, and all exact rows when expanded;
- labels and values are escaped through existing helpers.

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_dashboard_web.py -k 'discipline or lifecycle or historical' -v
```

Expected: FAIL because the current renderer is CN-only and hard-coded.

- [ ] **Step 2: Implement lifecycle card rendering**

Add small pure rendering helpers:

```javascript
function trendDisciplineLifecycle(parameterRows) {}
function renderTrendDisciplineCards(report) {}
function renderTrendIndustryContext(report) {}
function trendReportCostLabel(report) {}
```

Requirements:

- use native `<details class="trend-discipline-card">`;
- show the most decision-relevant frozen rows before expansion;
- expansion shows every mapped row plus current evidence/affected count;
- context rows show temperature direction/current temperature, strength,
  warm-to-hot count, and `right_count / valid_count = share`;
- show prior share and percentage-point change only when frozen values exist;
- show an explicit legacy-fallback message and reasons when current context is invalid;
- display the canonical frozen API-cost label in the report header.

Do not calculate sorting metrics in JavaScript; format only frozen report facts.

- [ ] **Step 3: Reorder the integrated report workspace**

Change `renderCnTrendReportWorkspace` into a market-neutral trend workspace while retaining its existing callers.

Desktop DOM order:

1. header identity, dates, version, account, history/back, API cost;
2. action count metrics;
3. compact controller/process strip;
4. discipline lifecycle cards;
5. industry earning-effect context beside today's action priority;
6. risk, drawdown, simulation;
7. formal buy plan with industry confirmation;
8. sell, review, hold, fallback, risk skips;
9. audit.

Add industry confirmation cells to buys without removing existing price, shares, weight, stop, risk, cash, seat, or constraint facts.

Update tests so every pre-existing component title is still present exactly once.

- [ ] **Step 4: Add focused responsive CSS**

Reuse existing custom properties and extend the current `.cn-trend-disciplines`/`.trend-discipline` styles instead of introducing a new visual system.

Required rules:

```css
.trend-discipline-grid {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
}

.trend-discipline-card > summary {
  min-height: 44px;
}

@media (max-width: 760px) {
  .trend-discipline-grid {
    grid-template-columns: 1fr;
  }
}
```

Use `min-width: 0`, wrapping, and existing mobile table/card behavior so a 375 px viewport has no page-level horizontal overflow. Preserve visible `:focus-visible`.

Mobile DOM/CSS visual order:

1. identity/counts/cost;
2. urgent sell/review;
3. lifecycle cards;
4. industry context;
5. buys/holds;
6. risk/simulation;
7. audit.

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_dashboard_web.py -q
```

Expected: PASS.

- [ ] **Step 5: Extend acceptance assertions**

In `dashboard_acceptance.py` and its deterministic helper tests, assert against real browser state:

- current and historical reports show their frozen discipline rows;
- all five lifecycle cards are keyboard-operable;
- CN/US/HK display their own cost labels;
- exact breadth counts and shares are visible;
- all existing action/risk/audit components remain reachable;
- urgent mobile exits precede disciplines;
- 44 px summary height;
- `document.documentElement.scrollWidth <= window.innerWidth` at 375 px.

Do not run `make acceptance` yet.

Run only the helper/unit test:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_dashboard_acceptance.py \
  tests/test_dashboard_web.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit the Dashboard redesign**

```bash
git add \
  src/open_trader/dashboard_static/dashboard.js \
  src/open_trader/dashboard_static/dashboard.css \
  src/open_trader/dashboard_acceptance.py \
  tests/test_dashboard_web.py \
  tests/test_dashboard_acceptance.py
git commit -m "feat: redesign trend discipline dashboard"
```

Expected: UI and acceptance assertion changes in one reviewable commit.

---

## Task 7: Focused Regression, Direct Report Verification, and Documentation

**Files:**

- Modify: `CHANGELOG.md`
- Verify: all changed source and test files
- Verify: live report artifacts under existing configured data/report directories

- [ ] **Step 1: Run all focused automated suites**

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_trend_industry_context.py \
  tests/test_a_share_trend.py \
  tests/test_market_trend.py \
  tests/test_trend_kelly.py \
  tests/test_trend_api_stats.py \
  tests/test_trend_review.py \
  tests/test_dashboard.py \
  tests/test_dashboard_web.py \
  tests/test_dashboard_acceptance.py -q
```

Expected: all selected tests PASS with zero failures.

- [ ] **Step 2: Run the full non-acceptance suite**

```bash
.venv/bin/python -m pytest -q
```

Expected: at least the baseline `3372 passed`, plus the new tests, with zero failures.

- [ ] **Step 3: Run static consistency checks**

```bash
git diff --check
rg -n 'TODO|TBD|FIXME|NotImplementedError|placeholder' \
  src/open_trader/trend_industry_context.py \
  src/open_trader/a_share_trend.py \
  src/open_trader/market_trend.py \
  src/open_trader/trend_kelly.py \
  src/open_trader/dashboard.py \
  src/open_trader/dashboard_static/dashboard.js \
  src/open_trader/dashboard_static/dashboard.css \
  纪律.md
```

Expected: `git diff --check` is silent. The placeholder scan introduces no new unfinished implementation.

- [ ] **Step 4: Run direct current report workflows where practical**

First inspect the three live controller states through the current operator
entry point:

```bash
for MARKET in CN HK US; do
  PYTHONPATH=src .venv/bin/python -m open_trader trend-market status \
    --market "$MARKET" \
    --config /Users/ray/projects/open_trader/config/daily_premarket.env
done
```

Use the repository's configured environment and same-date paid cache. Observe
the next naturally eligible real CN, US, and HK report workflow when each
market's update/account prerequisites are available. Do not create a revision
solely for testing and do not start a second controller beside a launchd-owned
controller. If a controller is not running and its status says a foreground
run is safe, use the exact operator command:

```bash
PYTHONPATH=src .venv/bin/python -m open_trader trend-market run \
  --market CN \
  --config /Users/ray/projects/open_trader/config/daily_premarket.env
```

Repeat with `HK` or `US` only under the same ownership check. For every report
that runs, inspect the generated JSON/Markdown and record:

- strategy version;
- ordering mode;
- exact eligible industry IDs;
- exact right count/denominator/share;
- whether prior comparison is present or correctly omitted;
- report-only actual balance delta;
- estimate completeness;
- Markdown cost label;
- Feishu text cost label without sending a second notification.

Expected: each practical market run generates one internally consistent report. A genuine unavailable external prerequisite is recorded as a direct-workflow limitation; it does not get replaced by fixtures.

- [ ] **Step 5: Inspect background process state before final acceptance**

```bash
launchctl list | rg 'com\\.open-trader\\.trend-market-controller\\.(cn|hk|us)' || true
screen -ls
ps -axo pid,lstart,command | rg 'open_trader (dashboard|trend-market run)'
lsof -nP -iTCP:8766 -sTCP:LISTEN
```

Record any process that is using the main checkout or a pre-change SHA. Do not yet claim live behavior changed.

- [ ] **Step 6: Update the operator changelog**

Add a dated `2026-07-24` entry to `CHANGELOG.md` covering:

- contextual industry ordering and legacy fallback;
- local right-side history;
- per-market API cost;
- CN v8 / US v5 / HK v5 sample continuity;
- lifecycle discipline Dashboard and retained report components.

Commit:

```bash
git add CHANGELOG.md
git commit -m "docs: record industry breadth trend reports"
```

Expected: dated operator-facing entry is committed before any merge into `main`.

- [ ] **Step 7: Self-review the implementation against the approved spec**

Inspect:

```bash
git diff e1ec1cf...HEAD --stat
git diff e1ec1cf...HEAD -- \
  src/open_trader \
  tests \
  纪律.md \
  CHANGELOG.md
```

Check every success criterion in:

`docs/superpowers/specs/2026-07-24-trend-industry-breadth-discipline-dashboard-design.md`

Specifically reject the implementation if it contains:

- partial context ordering;
- a weighted score;
- a fixed 18% threshold;
- market-level ordering;
- candidate-pool heat as a score;
- synthesized prior history;
- hard-coded live discipline in historical views;
- a component fee silently presented as complete;
- sample inheritance for CN v5/v6 or old US/HK versions;
- removal of an existing report component.

- [ ] **Step 8: Commit any self-review correction separately**

If self-review finds an issue, add a focused regression test first, implement the correction, rerun the affected focused suite, and commit with a narrow message. If no issue is found, do not create an empty commit.

---

## Task 8: Final Acceptance, Exact-SHA Deployment, and Review Handoff

**Files:**

- Verify: `Makefile`
- Verify: `src/open_trader/dashboard_acceptance.py`
- Verify: deployed Dashboard process and log

- [ ] **Step 1: Ensure the branch is clean and identify the candidate SHA**

```bash
git status --short
git rev-parse HEAD
```

Expected: status is empty. Save the exact SHA as the acceptance candidate.

- [ ] **Step 2: Run the final Dashboard gate**

Run exactly from the feature worktree:

```bash
make acceptance
```

Expected terminal result: `PASS`.

If it prints `FAIL`, diagnose, add a regression test, fix, rerun focused checks, commit, and rerun `make acceptance`.

If it prints `BLOCKED`, stop and report the exact browser/external blocker. Do not substitute curl, fixtures, mocks, screenshots, or unit tests.

- [ ] **Step 3: Verify the accepted Git SHA did not change**

```bash
git status --short
git rev-parse HEAD
```

Expected: clean tree and the same SHA accepted in Step 2.

- [ ] **Step 4: Redeploy the exact accepted SHA**

Stop only the known Dashboard listener and start it from the accepted worktree:

```bash
screen -S open_trader_dashboard_8766 -X quit 2>/dev/null || true
screen -dmS open_trader_dashboard_8766 zsh -lc \
  'cd /Users/ray/projects/open_trader/.worktrees/trend-industry-breadth-discipline-dashboard && exec env PYTHONPATH=src .venv/bin/python -u -m open_trader dashboard --portfolio /Users/ray/projects/open_trader/data/latest/portfolio.csv --data-dir /Users/ray/projects/open_trader/data --reports-dir /Users/ray/projects/open_trader/reports --config /Users/ray/projects/open_trader/config/daily_premarket.env --poll-seconds 5 --host 127.0.0.1 --port 8766 >> /tmp/open_trader_dashboard_8766.log 2>&1'
```

Do not restart a trend-market launchd controller unless inspection proves it is
intended to be running and still has pre-change code in memory. Never overlap
two controllers for the same market.

- [ ] **Step 5: Verify PID, cwd, SHA, fresh logs, and HTTP**

```bash
NEW_PID=$(lsof -nP -iTCP:8766 -sTCP:LISTEN -t)
ps -p "$NEW_PID" -o pid,lstart,command
lsof -a -p "$NEW_PID" -d cwd -Fn
git -C /Users/ray/projects/open_trader/.worktrees/trend-industry-breadth-discipline-dashboard rev-parse HEAD
tail -n 80 /tmp/open_trader_dashboard_8766.log
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8766/
```

Expected:

- one listener PID;
- cwd is the feature worktree;
- Git SHA equals the exact acceptance SHA;
- fresh log lines show the new process start with no traceback;
- HTTP status is `200`.

- [ ] **Step 6: Handoff the review URL**

Provide:

```text
http://127.0.0.1:8766/
```

Report the acceptance result, exact SHA, deployed PID/cwd, live report workflow results/limitations, and the cost/context behavior observed. Ask the user to review only after every preceding check succeeds.
