# Trend Symbol Mapping and Holding Industry Context Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:executing-plans` to implement this plan task-by-task. Track each
> step with the checkboxes below and use `tdd` for every behavior change.

**Goal:** Resolve every supported holding through a persistent, bidirectional
Futu/Trend Animals identity mapping, and freeze industry context for all
candidate and holding industries without changing trading decisions.

**Architecture:** Deepen the existing `TrendAnimalsClient` cache rather than
adding a symbol service. A complete immutable record owns the Futu symbol,
Trend Animals symbol, Trend Animals `tmId`, market, and asset; two in-memory
indexes resolve either provider identity. Market-aware report paths teach the
cache from accepted provider rows. Industry collection expands its data
collection set to the union of eligible-candidate and holding industries, while
candidate ordering continues to use only eligible-candidate industries.

**Tech Stack:** Python 3, dataclasses, atomic JSON files, pytest, vanilla
JavaScript, Node-based Dashboard rendering tests, launchd, and the repository's
`make acceptance` gate.

## Global constraints

- Work only in
  `/Users/ray/projects/open_trader/.worktrees/trend-symbol-mapping-holding-industries`
  on branch `fix/trend-symbol-mapping-holding-industries`, which was created
  from local `main` at `2a2b56f7bd6dd6ff0b94562db59cbbedbcbceb05`.
- Do not add retries, alternate symbol spellings, a database, a new dependency,
  or a parallel symbol service.
- Do not special-case `515450` in production logic. Its verified mapping is
  migration data for the general contract.
- Do not change report schema, Dashboard layout/copy/interaction, ranking,
  entry/exit rules, formal actions, sizing, risk, Kelly state, or strategy
  version.
- Do not rewrite historical reports. Regenerate only current CN/HK/US
  revisions, after snapshotting the pre-change artifacts.
- Run focused tests while developing. Run `make acceptance` only as the final
  gate after code, tests, current reports, and `CHANGELOG.md` are final. Rerun
  it only if the final gate itself finds a defect that requires another
  committed change.
- Do not merge or push in this plan. After a `PASS`, deploy the exact accepted
  worktree SHA and hand it to the user for review.

---

## Task 1: Add the complete provider-identity record and bidirectional cache

**Files:**

- Modify: `src/open_trader/trend_animals.py`
- Test: `tests/test_trend_animals.py`

- [ ] **Step 1: Write failing round-trip and conflict tests**

Add tests that specify the cache format and both lookup directions:

```python
def test_symbol_mapping_round_trips_both_provider_identities(
    tmp_path: Path,
) -> None:
    client = TrendAnimalsClient(
        api_key="secret-value",
        cache_dir=tmp_path,
        transport=FakeTransport({}),
    )

    mapping = client.remember_symbol_row(
        market="CN",
        expected_futu_symbol="SH.515450",
        row={
            "tmId": 328879,
            "tickerSymbol": "515450",
            "asset": "ETF基金",
        },
    )

    assert mapping.futu_symbol == "SH.515450"
    assert mapping.trend_animals_symbol == "515450"
    assert client.symbol_mapping("SH.515450", market="CN") == mapping
    assert (
        client.symbol_mapping_from_trend_animals("515450", market="CN")
        == mapping
    )
    assert json.loads(
        (
            tmp_path
            / "symbol_mappings"
            / "CN"
            / "SH.515450.json"
        ).read_text(encoding="utf-8")
    ) == {
        "asset": "ETF基金",
        "futu_symbol": "SH.515450",
        "market": "CN",
        "schema_version": "open_trader.trend_symbol_mapping.v1",
        "trend_animals_symbol": "515450",
        "trend_animals_tm_id": 328879,
    }
```

Add a second-client assertion to prove the record is loaded from disk. Add
malformed-record tests for missing fields, invalid market/asset/`tmId`, and
provider codes that cannot map to the stored market. Add conflict tests for:

- the same `(market, futu_symbol)` pointing at a different Trend Animals code
  or `tmId`;
- the same `(market, trend_animals_symbol)` pointing at a different Futu code
  or `tmId`;
- preserving the first valid JSON file byte-for-byte when a conflict is
  rejected.

- [ ] **Step 2: Run the new tests and confirm RED**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  ../../.venv/bin/python -m pytest tests/test_trend_animals.py -q \
  -k 'symbol_mapping'
```

Expected: failures because `remember_symbol_row`, `symbol_mapping`, and
`symbol_mapping_from_trend_animals` do not exist.

- [ ] **Step 3: Implement the smallest complete mapping cache**

In `trend_animals.py`, add the immutable record next to the existing client
errors:

```python
@dataclass(frozen=True)
class TrendSymbolMapping:
    market: str
    futu_symbol: str
    trend_animals_symbol: str
    trend_animals_tm_id: int
    asset: str

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": "open_trader.trend_symbol_mapping.v1",
            "market": self.market,
            "futu_symbol": self.futu_symbol,
            "trend_animals_symbol": self.trend_animals_symbol,
            "trend_animals_tm_id": self.trend_animals_tm_id,
            "asset": self.asset,
        }
```

Keep all cache behavior inside `TrendAnimalsClient`:

- lazily load `symbol_mappings/<MARKET>/*.json` once per market;
- validate every field and allowed asset;
- build:
  `(market, futu_symbol) -> TrendSymbolMapping` and
  `(market, trend_animals_symbol) -> TrendSymbolMapping`;
- fail closed before mutation if either key conflicts;
- use the existing atomic `_write_cache`;
- update both indexes only after the file write succeeds;
- let an identical record be idempotent.

`remember_symbol_row(...)` must preserve the exact returned
`tickerSymbol`. It may use `from_trend_animals_symbol(...)` only while proving
the initial Futu side; once a record exists, the record is authoritative.

- [ ] **Step 4: Run the mapping tests and the existing client suite**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  ../../.venv/bin/python -m pytest tests/test_trend_animals.py -q
```

Expected: all `tests/test_trend_animals.py` tests pass.

- [ ] **Step 5: Commit the record/cache layer**

```bash
git add src/open_trader/trend_animals.py tests/test_trend_animals.py
git commit -m "feat: cache explicit provider symbol mappings"
```

---

## Task 2: Resolve holdings with one deterministic discovery and no retry

**Files:**

- Modify: `src/open_trader/trend_animals.py`
- Test: `tests/test_trend_animals.py`

- [ ] **Step 1: Replace the old formatting expectations with provider-mapping tests**

Update the market-parameterized test so discovery uses exactly one key:

```python
@pytest.mark.parametrize(
    ("market", "futu_symbol", "returned_symbol", "keyword", "asset"),
    [
        ("CN", "SH.600036", "600036.SH", "600036", "A股"),
        ("CN", "SH.515450", "515450", "515450", "ETF基金"),
        ("HK", "HK.00027", "0027.HK", "0027.HK", "港股"),
        ("HK", "HK.03033", "3033.HK", "3033.HK", "香港ETF"),
        ("US", "US.ARWR", "ARWR.US", "ARWR", "美股"),
        ("US", "US.QQQ", "QQQ.US", "QQQ", "美国ETF"),
        ("US", "US.BRK.B", "BRK_B", "BRK_B", "美股"),
    ],
)
def test_search_exact_symbol_discovers_once_and_persists_provider_mapping(
    market: str,
    futu_symbol: str,
    returned_symbol: str,
    keyword: str,
    asset: str,
    tmp_path: Path,
) -> None:
    transport = FakeTransport({
        "searchTicker": success([{
            "tmId": 7,
            "tickerSymbol": returned_symbol,
            "asset": asset,
        }]),
    })
    client = TrendAnimalsClient(
        api_key="secret-value",
        cache_dir=tmp_path,
        transport=transport,
    )

    assert client.search_exact_symbol(
        futu_symbol,
        market=market,
        expected_date="2026-07-31",
    ) == 7
    assert [call[1]["keyword"] for call in transport.calls] == [[keyword]]

    cached_transport = FakeTransport({})
    cached_client = TrendAnimalsClient(
        api_key="another-secret",
        cache_dir=tmp_path,
        transport=cached_transport,
    )
    assert cached_client.search_exact_symbol(
        futu_symbol,
        market=market,
        expected_date="2026-07-31",
    ) == 7
    assert cached_transport.calls == []
```

For `SH.515450`, also assert that no `515450.SH` request appears and the
persisted record contains `trend_animals_symbol == "515450"`.

Add tests proving:

- a complete mapping is checked before a same-day negative miss;
- an unresolved single discovery writes the existing dated miss once and does
  not issue a second spelling;
- a legacy `symbols/600036.json` entry may supply its `tmId`, but does not
  create a complete mapping;
- a later matching authoritative row can upgrade that legacy identity through
  `remember_symbol_row`;
- a cached mapping and a later disagreeing row raise
  `TrendAnimalsError` without rewriting the cache.

- [ ] **Step 2: Run the discovery tests and confirm RED**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  ../../.venv/bin/python -m pytest tests/test_trend_animals.py -q \
  -k 'search_exact_symbol or legacy_symbol'
```

Expected: the CN cases fail because the current implementation queries
`600036.SH`/`515450.SH`, and persistence assertions fail because only
`symbol + tmId` is cached.

- [ ] **Step 3: Make `search_exact_symbol` mapping-first**

Implement this order and no other fallback:

```text
canonical Futu symbol
  -> complete mapping cache
  -> legacy tmId cache
  -> same-day negative miss
  -> one market-specific discovery request
  -> exactly one validated allowed-asset row
  -> complete mapping cache
```

The single discovery key is:

```python
def _symbol_discovery_key(market: str, futu_symbol: str) -> str:
    if market == "CN":
        return futu_symbol.split(".", 1)[1]
    return to_trend_animals_symbol(market, futu_symbol)
```

Keep the public return type as `int` so current callers do not change. Retain
the existing dated miss payload and per-symbol degradation. Change matching to
retain the accepted row, not only a set of `tmId` values, then call
`remember_symbol_row(...)`. Exactly one unique complete identity must remain;
zero or multiple identities is a miss.

- [ ] **Step 4: Run client tests**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  ../../.venv/bin/python -m pytest tests/test_trend_animals.py -q
```

Expected: all tests pass, including existing asset filtering, cross-market
rejection, dated miss, and credential-redaction cases.

- [ ] **Step 5: Commit mapping-first resolution**

```bash
git add src/open_trader/trend_animals.py tests/test_trend_animals.py
git commit -m "fix: resolve trend symbols through provider mappings"
```

---

## Task 3: Teach mappings proactively from accepted report rows

**Files:**

- Modify: `src/open_trader/a_share_trend.py`
- Modify: `src/open_trader/market_trend.py`
- Test: `tests/test_a_share_trend.py`
- Test: `tests/test_market_trend.py`

- [ ] **Step 1: Write failing pipeline-learning tests**

Add one CN runner test and a parameterized HK/US runner test. Their fake API
must expose:

```python
def remember_symbol_row(
    self,
    *,
    market: str,
    row: Mapping[str, object],
    expected_futu_symbol: str | None = None,
) -> object:
    self.remembered.append((market, dict(row), expected_futu_symbol))
    return object()
```

Each test should return a market-valid component/snapshot row and assert:

- the exact provider `tickerSymbol`, `tmId`, and `asset` are recorded;
- recording happens only after the row passes existing market/date/`tmId`
  validation;
- candidate-pool rows are recorded before real-holding enrichment, so a later
  holding lookup can use the learned record;
- malformed or cross-market rows still fail through existing validation and
  are not recorded.

Add a focused `enrich_real_holding_input` test whose search row is recorded
inside `TrendAnimalsClient`, whose returned snapshot agrees with that record,
and whose next lookup uses the cache with zero transport calls.

- [ ] **Step 2: Run the new tests and confirm RED**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  ../../.venv/bin/python -m pytest \
  tests/test_a_share_trend.py tests/test_market_trend.py -q \
  -k 'learns_symbol_mapping or records_provider_mapping'
```

Expected: failures because report ingestion currently consumes rows without
teaching the complete mapping cache.

- [ ] **Step 3: Record only accepted rows**

Add one private helper in `a_share_trend.py`, reused by
`market_trend.py`:

```python
def _remember_symbol_rows(
    api: object,
    *,
    market: str,
    rows: Sequence[Mapping[str, object]],
) -> None:
    remember = getattr(api, "remember_symbol_row", None)
    if not callable(remember):
        return
    for row in rows:
        remember(market=market, row=row)
```

Call it only after existing response validation at the points that accept:

- candidate component rows;
- candidate/holding snapshot rows;
- real-only snapshot rows.

`search_exact_symbol` already records accepted search rows and must not be
wrapped by a second search. Existing lightweight fake APIs without a recorder
remain compatible through the callable check.

- [ ] **Step 4: Run cross-market focused tests**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  ../../.venv/bin/python -m pytest \
  tests/test_trend_animals.py \
  tests/test_a_share_trend.py \
  tests/test_market_trend.py -q \
  -k 'symbol or mapping or real_holding'
```

Expected: all selected tests pass with no changed action/order assertions.

- [ ] **Step 5: Commit proactive learning**

```bash
git add \
  src/open_trader/a_share_trend.py \
  src/open_trader/market_trend.py \
  tests/test_a_share_trend.py \
  tests/test_market_trend.py
git commit -m "feat: learn provider mappings from trend rows"
```

---

## Task 4: Collect the union of candidate and holding industries

**Files:**

- Modify: `src/open_trader/a_share_trend.py`
- Modify: `src/open_trader/market_trend.py`
- Test: `tests/test_a_share_trend.py`
- Test: `tests/test_market_trend.py`

- [ ] **Step 1: Write a failing industry-union unit test**

Extend the existing
`test_collect_industry_contexts_queries_only_eligible_industries_and_unions_members`
coverage with:

```python
holding_snapshots = (
    HoldingSnapshot(
        tm_id=308052,
        symbol="600036",
        exchange="SH",
        name="招商银行",
        as_of_date="2026-07-31",
        right_side=True,
        danger=False,
        boiling=False,
        champagne=False,
        industry="银行",
        industry_tm_id=339103,
    ),
    HoldingSnapshot(
        tm_id=308990,
        symbol="600900",
        exchange="SH",
        name="长江电力",
        as_of_date="2026-07-31",
        right_side=True,
        danger=False,
        boiling=False,
        champagne=False,
        industry="电力",
        industry_tm_id=621693,
    ),
)
```

Use an eligible candidate from a third industry and assert:

```python
assert facts["eligible_industry_ids"] == (candidate_industry_id,)
assert facts["holding_industry_ids"] == (339103, 621693)
assert facts["context_industry_ids"] == (
    candidate_industry_id,
    339103,
    621693,
)
assert [context.industry for context in contexts] == [
    candidate_industry_name,
    "银行",
    "电力",
]
assert facts["component_requests"] == 3
assert facts["state_ids"] == facts["context_industry_ids"]
```

Also add cases proving:

- candidate/real/simulated references to one industry deduplicate by
  `industry_tm_id`;
- `None` snapshots and snapshots without `industry_tm_id` create no context;
- an invalid holding-only context is returned, but
  `industry_context_status` stays `context_current_only` when candidate
  contexts are complete;
- candidate order before and after holding-only contexts is identical.

- [ ] **Step 2: Run the union tests and confirm RED**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  ../../.venv/bin/python -m pytest tests/test_a_share_trend.py -q \
  -k 'collect_industry_contexts and holding'
```

Expected: failure because `collect_industry_contexts` has no
`holding_snapshots` input and only requests eligible-candidate industries.

- [ ] **Step 3: Expand data collection without expanding decision scope**

Change the function signature to:

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
    strategy_version: str | None = None,
    cny_per_local_currency: Decimal | None = None,
    holding_snapshots: Sequence[HoldingSnapshot | None] = (),
) -> tuple[tuple[IndustryContext, ...], dict[str, object], dict[str, object]]:
```

Inside the function:

```python
holding_industry_ids = sorted({
    snapshot.industry_tm_id
    for snapshot in holding_snapshots
    if snapshot is not None and snapshot.industry_tm_id is not None
})
context_industry_ids = sorted(
    set(eligible_industry_ids) | set(holding_industry_ids)
)
```

Use `context_industry_ids` for component requests, member union, state
requests, context construction, and billing facts. Populate names first from
eligible candidates and accepted candidate rows, then fill missing names from
holding snapshots.

Keep `eligible_industry_ids` unchanged and run the second
`build_candidate_list(...)` exactly as today. It may receive the larger context
map, but only candidate-referenced context can affect its status or ordering.

Return these explicit facts:

```python
{
    "eligible_industry_ids": tuple(eligible_industry_ids),
    "holding_industry_ids": tuple(holding_industry_ids),
    "context_industry_ids": tuple(context_industry_ids),
    "component_requests": len(context_industry_ids),
    "state_ids": tuple(context_industry_ids),
}
```

Retain the existing component/member/state response and field facts alongside
these keys.

At both CN and HK/US call sites, pass the union of resolved simulated snapshots
and `real_holdings.holding_snapshots.values()`. Preserve unresolved snapshots
as `None`; collection will ignore them.

- [ ] **Step 4: Update cost-accounting assertions and run report tests**

Any expected component/state request count must use
`industry_facts["context_industry_ids"]` or the already returned
`component_requests`/`state_ids`, not recalculate from candidates.

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  ../../.venv/bin/python -m pytest \
  tests/test_a_share_trend.py tests/test_market_trend.py -q \
  -k 'industry_context or billing or holding'
```

Expected: all selected tests pass, including unchanged formal-action and
candidate-order assertions.

- [ ] **Step 5: Commit the holding-industry union**

```bash
git add \
  src/open_trader/a_share_trend.py \
  src/open_trader/market_trend.py \
  tests/test_a_share_trend.py \
  tests/test_market_trend.py
git commit -m "fix: include holding industries in trend context"
```

---

## Task 5: Keep the Dashboard fallback message scoped to candidate ordering

**Files:**

- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Test: `tests/test_dashboard_web.py`

- [ ] **Step 1: Write a failing rendering regression**

Add a Node rendering test with:

- `industry_context_status.ordering_mode == "context_current_only"`;
- `industry_context_status.current_complete == true`;
- one valid candidate industry context;
- one invalid holding-only industry context, such as 银行 with
  `invalid_reasons: ["snapshot_coverage_below_90pct"]`.

Assert that the invalid row still renders its existing unavailable/invalid
details, but the HTML does not contain `已回退旧排序`. Retain the existing
legacy-status test that must still render that message.

- [ ] **Step 2: Run the regression and confirm RED**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  ../../.venv/bin/python -m pytest tests/test_dashboard_web.py -q \
  -k 'industry_context and fallback'
```

Expected: the new case fails because any invalid context currently triggers
the report-wide fallback paragraph.

- [ ] **Step 3: Make status the only global-ordering authority**

In `trendIndustryContextFallback(status, contexts)`:

- preserve the existing no-context/no-status message;
- compute global invalidity only from
  `ordering_mode.startsWith("legacy")` or
  `status.current_complete === false`;
- when global invalidity is false, return `""` even if a holding-only row is
  invalid;
- when it is true, retain existing status reasons and context reasons for the
  explanatory text.

Do not change row rendering, copy, CSS, layout, or interaction.

- [ ] **Step 4: Run Dashboard rendering tests**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  ../../.venv/bin/python -m pytest \
  tests/test_dashboard_web.py tests/test_dashboard.py -q \
  -k 'industry_context or trend_report'
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit the fallback-scope fix**

```bash
git add \
  src/open_trader/dashboard_static/dashboard.js \
  tests/test_dashboard_web.py
git commit -m "fix: scope trend fallback to candidate ordering"
```

---

## Task 6: Seed the verified ETF mapping and regenerate current reports safely

**Files/data:**

- Runtime cache:
  `/Users/ray/projects/open_trader/data/trend_animals/cache/symbol_mappings/CN/SH.515450.json`
- Obsolete dated miss:
  `/Users/ray/projects/open_trader/data/trend_animals/cache/symbol_misses/CN/2026-07-31/515450.json`
- Current report revisions under `reports/trend_a_share`,
  `reports/trend_hk`, and `reports/trend_us`

- [ ] **Step 1: Run the complete focused regression before touching runtime data**

Confirm that the red-green tests from Tasks 1-5 now prove:

- an ETF search row
  `{"tmId": 328879, "tickerSymbol": "515450", "asset": "ETF基金"}`
  resolves `SH.515450` without `MANUAL_REVIEW`;
- resolved real-holding snapshots add 银行 `339103` and 电力 `621693` to
  `industry_contexts`;
- no holding industry ID creates no synthetic row;
- the same union behavior applies to HK and US runner fixtures.

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  ../../.venv/bin/python -m pytest \
  tests/test_trend_animals.py \
  tests/test_a_share_trend.py \
  tests/test_market_trend.py \
  tests/test_dashboard_web.py \
  tests/test_dashboard.py -q
```

Expected: all focused suites pass before runtime mutation.

- [ ] **Step 2: Snapshot the current report invariants**

Copy the current CN/HK/US report JSON files to a task-specific temporary
directory created with `mktemp -d`. From each report, record:

- report path and SHA-256;
- `strategy_version` and strategy identity facts;
- ordered candidate symbols;
- formal actions;
- risk/Kelly facts;
- `industry_context_status`.

Use a read-only comparison script under `PYTHONSAFEPATH=1` and keep the
temporary path in the operator log for the later post-regeneration comparison.

- [ ] **Step 3: Seed the verified complete mapping through the production API**

Use the new public recorder, not hand-written JSON:

```bash
PYTHONSAFEPATH=1 \
PYTHONPATH="$PWD:$PWD/src" \
/Users/ray/projects/open_trader/.venv/bin/python - <<'PY'
from pathlib import Path

from open_trader.trend_animals import TrendAnimalsClient

cache = Path(
    "/Users/ray/projects/open_trader/data/trend_animals/cache"
)
client = TrendAnimalsClient(
    api_key="seed-only-non-network-key",
    cache_dir=cache,
    transport=lambda *_: (_ for _ in ()).throw(
        AssertionError("seeding must not call the network")
    ),
)
mapping = client.remember_symbol_row(
    market="CN",
    expected_futu_symbol="SH.515450",
    row={
        "tmId": 328879,
        "tickerSymbol": "515450",
        "asset": "ETF基金",
    },
)
assert mapping.futu_symbol == "SH.515450"
assert mapping.trend_animals_symbol == "515450"
assert mapping.trend_animals_tm_id == 328879
PY
```

Read the persisted JSON back and verify all six fields. Move only the exact
obsolete same-day miss to
`515450.json.obsolete-20260731` after verifying its expected
`no_unique_exact_match` payload. Do not delete or bulk-clear any other cache.

Run a second client with a transport that raises and assert:

```python
client.search_exact_symbol(
    "SH.515450", market="CN", expected_date="2026-07-31"
) == 328879
```

This proves the complete mapping wins without retry or network access.

- [ ] **Step 4: Candidate-deploy the branch controllers before requesting revisions**

Link the existing virtual environment if the worktree does not have one:

```bash
test -e .venv || ln -s /Users/ray/projects/open_trader/.venv .venv
```

Install CN/HK/US trend controllers from this exact worktree:

```bash
./scripts/install_daily_premarket_launchd.sh \
  --trend-only \
  --market all \
  --config /Users/ray/projects/open_trader/config/daily_premarket.env
```

Verify each fresh `data/trend_controller/<MARKET>/status.json` reports this
worktree as `working_directory`, the current branch SHA as `git_sha`, and an
advancing heartbeat before requesting a revision.

- [ ] **Step 5: Request CN/HK/US revisions and wait for the branch controllers**

For each market:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  .venv/bin/python -m open_trader.cli trend-market run \
  --market CN \
  --revision \
  --config /Users/ray/projects/open_trader/config/daily_premarket.env
```

Repeat with `HK` and `US`. A live controller may return
`phase=revision_requested`; poll its status and the report revision path until
the exact branch SHA has produced the new artifact. Treat any controller/report
failure as a blocker to diagnose, not as permission to hand-edit report JSON.

- [ ] **Step 6: Compare frozen decisions and inspect the new CN facts**

Compare new reports with the snapshots from Step 2:

- formal actions are identical;
- ordered candidates are identical;
- strategy identity/version, risk, Kelly, sizing, and execution facts are
  identical;
- only provider resolution, holding trend fields, industry-context rows,
  billing/cache facts, report revision metadata, and their hashes may change.

For the CN report assert directly:

```text
515450 trend row: resolved, not MANUAL_REVIEW
industry_contexts includes: 银行 / 339103
industry_contexts includes: 电力 / 621693
industry_context_status: still candidate-scoped
```

If Trend Animals provides no `industryTmId` for `515450`, confirm the ETF is
visible with resolved trend fields and no invented industry context.

- [ ] **Step 7: Commit only source-controlled report revisions, if this repo tracks them**

Inspect `git status --short` before staging. Stage only the intended current
CN/HK/US revision artifacts that the repository tracks; preserve unrelated
runtime/cache files and dirty-root files.

```bash
git add reports/trend_a_share reports/trend_hk reports/trend_us
git commit -m "data: refresh trend reports with holding context"
```

Skip this commit if current runtime reports are intentionally ignored and no
tracked artifact changed.

---

## Task 7: Changelog, full verification, final acceptance, and exact-SHA handoff

**Files:**

- Modify: `CHANGELOG.md`
- Verify: source, tests, live controllers, Dashboard, and current reports

- [ ] **Step 1: Add the dated operator-facing changelog entry**

Under `2026-07-31`, record:

- explicit cached Futu/Trend Animals/`tmId` identities with no alternate query;
- CN ETF `515450` now resolves from the verified mapping;
- industry context now includes all resolved current holding industries;
- candidate ordering and execution behavior remain unchanged.

Commit before any possible later merge:

```bash
git add CHANGELOG.md
git commit -m "docs: log trend identity and holding context fix"
```

- [ ] **Step 2: Run formatting/static checks and the focused suites**

Run:

```bash
git diff --check main...HEAD
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  .venv/bin/python -m pytest \
  tests/test_trend_animals.py \
  tests/test_a_share_trend.py \
  tests/test_market_trend.py \
  tests/test_dashboard.py \
  tests/test_dashboard_web.py \
  tests/test_dashboard_acceptance.py -q
```

Record exact pass/fail counts and duration.

- [ ] **Step 3: Run the repository test target**

Run the repository's normal full automated target from the worktree and record
its exact output:

```bash
make test
```

Fix failures in scope, rerun focused tests, and recommit before proceeding.

- [ ] **Step 4: Candidate-deploy the Dashboard from the final branch SHA**

Record:

```bash
ACCEPT_CANDIDATE_SHA="$(git rev-parse HEAD)"
git status --short --branch
```

Install the Dashboard from the worktree while keeping the canonical runtime
root:

```bash
./scripts/install_dashboard_launchd.sh \
  --repo-root "$PWD" \
  --runtime-root /Users/ray/projects/open_trader \
  --python /Users/ray/projects/open_trader/.venv/bin/python
```

Reinstall all trend controllers from the same SHA if any code or commit changed
after Task 6. Verify new PID, worktree cwd, candidate SHA, fresh logs, advancing
controller heartbeats, and `http://127.0.0.1:8766/` returns HTTP 200.

- [ ] **Step 5: Run `make acceptance` as the final gate**

With source, changelog, current reports, controllers, and Dashboard all on
`$ACCEPT_CANDIDATE_SHA`, run:

```bash
make acceptance
```

Record the exact terminal result:

- `PASS`: continue;
- `FAIL`: diagnose/fix, commit, redeploy the new SHA, then rerun this final
  gate;
- `BLOCKED`: report the external/browser blocker and do not substitute unit
  tests, curl, mocks, or screenshots.

- [ ] **Step 6: Redeploy the exact accepted SHA**

After `PASS`, assert `git rev-parse HEAD` is still the accepted SHA and source/
data have not changed. Redeploy Dashboard and CN/HK/US controllers from that
exact tree. This restart does not require another acceptance run.

Verify:

```text
Dashboard listener PID is new and owns port 8766
dashboard_runtime cwd == worktree
dashboard_runtime git_sha == accepted SHA
Dashboard startup log timestamp is fresh
Dashboard/controller error logs have no fresh startup error
CN/HK/US status working_directory == worktree
CN/HK/US status git_sha == accepted SHA
CN/HK/US heartbeat_at advances
GET http://127.0.0.1:8766/ == HTTP 200
```

- [ ] **Step 7: Capture the live affected view**

Open the live review URL in Chrome and capture one readable desktop screenshot
showing:

- `515450 红利50` with resolved trend fields rather than
  `MANUAL_REVIEW/数据未提供`;
- the holding-only 银行 and 电力 rows in `行业上下文`;
- no false report-wide `已回退旧排序` message when candidate context is
  complete.

No mobile screenshot is required because layout and responsive behavior are
unchanged. Confirm the screenshot is live, non-empty, readable, and from the
accepted SHA.

- [ ] **Step 8: Hand off for user review without merging**

Report:

- exact focused/full/acceptance results;
- accepted SHA;
- deployed PIDs/cwd/SHA/heartbeat/log/HTTP proof;
- direct review URL;
- inline screenshot;
- explicit confirmation that formal actions, candidate order, strategy, risk,
  and Kelly facts did not change.

Do not merge, push, remove the worktree, or delete the branch unless the user
asks.

---

## Execution order and stop conditions

Execute Tasks 1-5 strictly red-green-commit. Task 6 may mutate only the named
current runtime cache/report artifacts after all focused tests pass. Task 7 is
the only place to run `make acceptance`.

Stop and report instead of guessing if:

- an authoritative provider row conflicts with an existing complete mapping;
- discovery returns zero or more than one exact allowed-asset identity;
- current report regeneration changes formal actions, candidate ordering,
  strategy identity, risk, Kelly state, or execution facts;
- the deployed SHA differs from the tested/accepted SHA;
- acceptance returns `BLOCKED`.
