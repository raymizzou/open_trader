# Trend Symbol Mapping, Simulated Execution, and Holding Context Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:executing-plans` task-by-task and `tdd` for every behavior
> change. Do not use subagents unless the user explicitly requests them.

**Goal:** Persist verified Futu/Trend Animals/`tmId` identities, route Futu
simulated actions through the exact verified code, and display every candidate
and holding industry in frozen strength order.

**Architecture:** Extend `TrendAnimalsClient`'s existing atomic JSON cache. A
mapping is created only after both providers have independently accepted their
identities. New reports freeze `futu_symbol` into formal actions; legacy frozen
reports keep their current fallback. Industry collection keeps candidate
decision context unchanged and appends an isolated holding-only collection
phase before sorting the frozen result by strength.

**Tech Stack:** Python 3, dataclasses, atomic JSON, pytest, vanilla JavaScript,
launchd, and `make acceptance`.

## Constraints

- Work only in
  `/Users/ray/projects/open_trader/.worktrees/trend-symbol-mapping-holding-industries`
  on `fix/trend-symbol-mapping-holding-industries`.
- Reuse `TrendAnimalsClient`, `to_futu_symbol`, the existing Futu K-line call,
  report actions, and current Dashboard tables. Add no database, dependency,
  retry service, manual mapping UI, column, or layout.
- No alternate symbol spelling. A discovery is attempted once for one exact
  rule version; failure is permanent until manual intervention or a rule
  version change.
- Do not change scoring, thresholds, candidate ranking, sizing, risk, Kelly, or
  strategy version. A missing mapping may block a simulated BUY because order
  identity must fail closed.
- Preserve historical reports. New reports add frozen `futu_symbol`; legacy
  actions without the new metadata marker retain conversion compatibility.
- Run focused tests during development. Run `make acceptance` only after all
  source, current report data, and `CHANGELOG.md` changes are final.
- Do not merge or push. After acceptance, deploy the exact accepted worktree
  SHA and ask the user to review it.

---

## Task 1: Add a three-key immutable mapping cache

**Files:**

- Modify: `src/open_trader/trend_animals.py`
- Test: `tests/test_trend_animals.py`

- [ ] **RED: specify the complete record, three lookups, and conflicts**

Add `test_symbol_mapping_round_trips_all_provider_keys` using:

```python
mapping = client.remember_symbol_row(
    market="CN",
    expected_futu_symbol="SH.515450",
    row={
        "tmId": 328879,
        "tickerSymbol": "515450",
        "asset": "ETF基金",
    },
)
assert client.symbol_mapping("SH.515450", market="CN") == mapping
assert client.symbol_mapping_from_trend("515450", market="CN") == mapping
assert client.symbol_mapping_from_tm_id(328879, market="CN") == mapping
```

Assert the persisted payload is exactly:

```json
{
  "asset": "ETF基金",
  "futu_symbol": "SH.515450",
  "market": "CN",
  "schema_version": "open_trader.trend_symbol_mapping.v1",
  "trend_animals_symbol": "515450",
  "trend_animals_tm_id": 328879
}
```

Create a second client and prove all three lookups load from disk. Add separate
tests that conflict on Futu symbol, Trend symbol, and `tmId`; each must raise
`TrendAnimalsError` and preserve the original file bytes. Add malformed-cache
tests for every required field and market/asset mismatch.

Run and confirm missing-method failures:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  ../../.venv/bin/python -m pytest tests/test_trend_animals.py -q \
  -k 'symbol_mapping'
```

- [ ] **GREEN: implement only the record and cache indexes**

Add:

```python
@dataclass(frozen=True)
class TrendSymbolMapping:
    market: str
    futu_symbol: str
    trend_animals_symbol: str
    trend_animals_tm_id: int
    asset: str
```

Inside `TrendAnimalsClient`, lazily load
`symbol_mappings/<MARKET>/*.json`, validate them, and build three dictionaries
for Futu code, Trend code, and `tmId`. `remember_symbol_row` must require an
`expected_futu_symbol`; it cannot derive the missing provider side from the row
alone. Validate all three keys before `_write_cache`, then update indexes only
after the atomic write succeeds. Identical records are idempotent.

Run the whole client suite:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  ../../.venv/bin/python -m pytest tests/test_trend_animals.py -q
```

- [ ] **Commit**

```bash
git add src/open_trader/trend_animals.py tests/test_trend_animals.py
git commit -m "feat: cache verified trend symbol identities"
```

---

## Task 2: Make discovery one-shot and misses permanent

**Files:**

- Modify: `src/open_trader/trend_animals.py`
- Test: `tests/test_trend_animals.py`

- [ ] **RED: replace format assumptions with one-query contracts**

Parameterize exact discovery:

```python
cases = [
    ("CN", "SH.600036", "600036", "600036.SH", "A股"),
    ("CN", "SH.515450", "515450", "515450", "ETF基金"),
    ("HK", "HK.03033", "3033.HK", "3033.HK", "香港ETF"),
    ("US", "US.BRK.B", "BRK_B", "BRK_B", "美股"),
]
```

For each case assert one `searchTicker` call, exact keyword, exact returned
Trend code in the complete mapping, and zero calls from a second client.

Add `test_failed_discovery_is_permanent_across_dates`: the first client gets no
unique allowed-asset result and writes one miss; clients using later
`expected_date` values must raise the cached lookup error with zero transport
calls. Assert the miss includes:

```json
{
  "discovery_query": "515450",
  "discovery_rule_version": "trend_symbol_discovery.v1",
  "error": "no_unique_exact_match",
  "futu_symbol": "SH.515450",
  "market": "CN"
}
```

Add tests proving a complete mapping wins over a miss, a changed discovery rule
may attempt once, and no alternate spelling is called.

Run and confirm RED:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  ../../.venv/bin/python -m pytest tests/test_trend_animals.py -q \
  -k 'search_exact_symbol or permanent_miss'
```

- [ ] **GREEN: use mapping, legacy cache, permanent miss, then one query**

Keep `search_exact_symbol(...) -> int` for caller compatibility. Its order is:

```text
complete mapping
legacy Futu-token/tmId cache
current permanent miss for exact rule/query
one searchTicker request
one uniquely validated result
complete mapping write
```

Store permanent misses at
`symbol_misses/<MARKET>/<FUTU_SYMBOL>.json`. Ignore old dated miss directories.
The query is a discovery token only; preserve the returned `tickerSymbol`.
Accept exactly one row after market token and allowed-asset checks.

Legacy `symbols/*.json` still returns its `tmId` for one snapshot cycle but does
not create a complete mapping.

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  ../../.venv/bin/python -m pytest tests/test_trend_animals.py -q
```

- [ ] **Commit**

```bash
git add src/open_trader/trend_animals.py tests/test_trend_animals.py
git commit -m "fix: resolve trend symbols without retries"
```

---

## Task 3: Establish mappings only from verified report interactions

**Files:**

- Modify: `src/open_trader/a_share_trend.py`
- Modify: `src/open_trader/market_trend.py`
- Test: `tests/test_a_share_trend.py`
- Test: `tests/test_market_trend.py`

- [ ] **RED: prove rows alone cannot invent Futu identity**

Add tests that pass candidate/component rows to the CN/HK/US runners without a
successful Futu K-line result and assert no mapping recorder call occurs. Add a
matching case where the existing `get_daily_kline(futu_symbol, ...)` succeeds;
assert exactly one recorder call contains:

```python
{
    "market": market,
    "expected_futu_symbol": futu_symbol,
    "row": trend_row,
}
```

Add a legacy-upgrade test: a known Futu token/`tmId` plus a current snapshot
with the same `tmId`, valid exact Trend code, and allowed asset creates the full
record. A wrong `tmId`, market, or asset must not upgrade.

Run and confirm RED:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  ../../.venv/bin/python -m pytest \
  tests/test_a_share_trend.py tests/test_market_trend.py -q \
  -k 'verified_symbol_mapping or legacy_mapping_upgrade'
```

- [ ] **GREEN: record only after the existing provider success**

Add one small helper in `a_share_trend.py` that calls
`api.remember_symbol_row(...)` only when the API implements it. Use it:

- after a legacy `tmId` snapshot matches the known Futu holding;
- after the existing required Futu K-line call succeeds for a Trend candidate.

`search_exact_symbol` already records a successful holding discovery and must
not be wrapped by another recorder call. `market_trend.py` reuses the helper.
Do not loop over arbitrary component or snapshot rows to infer their Futu
codes. A conflict propagates to the existing per-symbol degradation path and
becomes `symbol_mapping_conflict`.

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  ../../.venv/bin/python -m pytest \
  tests/test_trend_animals.py tests/test_a_share_trend.py \
  tests/test_market_trend.py -q \
  -k 'mapping or symbol or holding or candidate'
```

- [ ] **Commit**

```bash
git add src/open_trader/a_share_trend.py src/open_trader/market_trend.py \
  tests/test_a_share_trend.py tests/test_market_trend.py
git commit -m "feat: verify mappings through provider calls"
```

---

## Task 4: Freeze verified Futu codes into simulated actions

**Files:**

- Modify: `src/open_trader/a_share_trend.py`
- Modify: `src/open_trader/market_trend.py`
- Modify: `src/open_trader/trend_review.py`
- Modify: `src/open_trader/trend_market_controller.py`
- Test: `tests/test_a_share_trend.py`
- Test: `tests/test_market_trend.py`
- Test: `tests/test_trend_review.py`
- Test: `tests/test_trend_market_controller.py`

- [ ] **RED: freeze BUY/SELL identities and fail closed**

Add cross-market report tests proving:

- a BUY action copies `mapping.futu_symbol`;
- a SELL action copies the exact Futu simulated-account position code;
- report metadata contains
  `symbol_mapping_schema=open_trader.trend_symbol_mapping.v1`;
- a visible eligible candidate with no complete mapping produces no BUY and an
  existing skip/reason entry `symbol_mapping_unavailable`;
- real `515450` remains outside `formal_actions` even when its advisory holding
  decision becomes resolved.

Add execution tests where report symbol and frozen Futu code differ in format.
Assert quote lookup, action key, audit payload, and `place_order` all use the
frozen field for CN, HK, and US. Add a new-contract report missing
`futu_symbol` and assert preflight fails. Keep one historical report without
the metadata marker and assert its current conversion still executes.

Run and confirm RED:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  ../../.venv/bin/python -m pytest \
  tests/test_a_share_trend.py tests/test_market_trend.py \
  tests/test_trend_review.py tests/test_trend_market_controller.py -q \
  -k 'futu_symbol or symbol_mapping'
```

- [ ] **GREEN: carry one optional field and centralize action resolution**

Preserve exact account codes while loading Futu simulated positions. Carry the
verified `futu_symbol` through `CandidateInput`/`BuyAction` and
`AccountPosition`/`HoldingDecision` with optional defaults so unrelated test
constructors stay compatible.

New reports mark the mapping schema. Add one shared action-code helper in
`trend_review.py`:

```text
new marker present -> require canonical action.futu_symbol
new marker absent  -> legacy to_futu_symbol(market, action.symbol)
```

Use that helper everywhere the controller/reviewer currently reconstructs an
action code. Do not change `FutuSimulateOrderExecutionClient`; it already sends
`request["futu_code"]` directly.

Run the four complete suites from RED, without `-k`.

- [ ] **Commit**

```bash
git add src/open_trader/a_share_trend.py src/open_trader/market_trend.py \
  src/open_trader/trend_review.py src/open_trader/trend_market_controller.py \
  tests/test_a_share_trend.py tests/test_market_trend.py \
  tests/test_trend_review.py tests/test_trend_market_controller.py
git commit -m "fix: freeze verified Futu codes for simulated orders"
```

---

## Task 5: Add holding industries without changing candidate decisions

**Files:**

- Modify: `src/open_trader/a_share_trend.py`
- Modify: `src/open_trader/market_trend.py`
- Test: `tests/test_a_share_trend.py`
- Test: `tests/test_market_trend.py`

- [ ] **RED: specify union, isolation, cost facts, and sort order**

Extend `collect_industry_contexts` tests with:

- one eligible candidate industry;
- simulated 银行 `339103` at strength `92.4`;
- real 电力 `621693` at strength `98.7`;
- a duplicate bank holding;
- one holding snapshot without `industry_tm_id`;
- one holding-only API failure.

Assert:

```python
assert facts["eligible_industry_ids"] == (candidate_industry_id,)
assert facts["holding_industry_ids"] == (339103, 621693)
assert facts["context_industry_ids"] == (
    candidate_industry_id,
    339103,
    621693,
)
assert [item.industry_tm_id for item in contexts] == [
    621693,               # 98.7
    candidate_industry_id,  # 95.0
    339103,               # 92.4
    invalid_industry_id,  # missing strength
]
```

Use explicit expected industry IDs as the final order assertion; put invalid or
missing strength last and tie by `industry_tm_id`. Assert the holding-only
failure returns an invalid row, leaves candidate `ordering_mode` unchanged,
and does not fail the report. Assert candidate order, side, size, risk, and
Kelly outputs are unchanged.

Run and confirm RED:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  ../../.venv/bin/python -m pytest \
  tests/test_a_share_trend.py tests/test_market_trend.py -q \
  -k 'holding_industry or industry_context_sort'
```

- [ ] **GREEN: keep candidate collection intact, append holding-only context**

Add `holding_snapshots: Sequence[HoldingSnapshot | None] = ()` to
`collect_industry_contexts`.

Keep the current candidate-industry request phase and strict error behavior.
Then collect only `holding_industry_ids - eligible_industry_ids` in a separate
phase so its errors cannot poison candidate data. Reuse already returned member
rows, request only remaining member IDs, and record holding errors in facts.
Return `eligible_industry_ids`, `holding_industry_ids`, and
`context_industry_ids`; make billing/state/member facts cover both phases.

After candidate ordering/status has been computed, sort the returned frozen
contexts with:

```python
key=lambda item: (
    item.strength is None or not item.strength.is_finite(),
    (
        -item.strength
        if item.strength is not None and item.strength.is_finite()
        else Decimal("0")
    ),
    item.industry_tm_id,
)
```

Pass resolved simulated and real holding snapshots at CN/HK/US call sites.
Never invent an industry for a missing ID.

Run both complete report suites.

- [ ] **Commit**

```bash
git add src/open_trader/a_share_trend.py src/open_trader/market_trend.py \
  tests/test_a_share_trend.py tests/test_market_trend.py
git commit -m "fix: include holding industries in strength order"
```

---

## Task 6: Render row-local mapping and industry errors truthfully

**Files:**

- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Test: `tests/test_dashboard_web.py`

- [ ] **RED: add two rendering regressions**

Add a real-holding row with `MANUAL_REVIEW` and
`reason=symbol_mapping_conflict`; assert the current reason surface renders
`趋势代码映射异常` without a new column or banner.

Add a complete candidate `industry_context_status` plus one invalid
holding-only context. Assert the invalid row renders, but the HTML does not
contain `已回退旧排序`. Retain the existing legacy candidate-status case that
must show the fallback message.

Run and confirm RED:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  ../../.venv/bin/python -m pytest tests/test_dashboard_web.py -q \
  -k 'symbol_mapping_conflict or holding_industry_fallback'
```

- [ ] **GREEN: reuse the reason cell and trust candidate status globally**

Add one reason-label mapping for `symbol_mapping_conflict`. In
`trendIndustryContextFallback`, decide global fallback only from legacy
`ordering_mode` or `current_complete === false`; invalid holding rows remain
row-local. The Dashboard renders the already strength-sorted report list.

Do not add CSS, columns, layout, or browser-side sorting.

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  ../../.venv/bin/python -m pytest \
  tests/test_dashboard_web.py tests/test_dashboard.py -q
```

- [ ] **Commit**

```bash
git add src/open_trader/dashboard_static/dashboard.js tests/test_dashboard_web.py
git commit -m "fix: show row-local trend mapping errors"
```

---

## Task 7: Initialize the known ETF mapping and regenerate current reports

**Runtime data:**

- Mapping:
  `/Users/ray/projects/open_trader/data/trend_animals/cache/symbol_mappings/CN/SH.515450.json`
- Legacy misses under:
  `/Users/ray/projects/open_trader/data/trend_animals/cache/symbol_misses/CN`
- Current CN/HK/US reports

- [ ] **Run all focused suites before runtime mutation**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  ../../.venv/bin/python -m pytest \
  tests/test_trend_animals.py tests/test_a_share_trend.py \
  tests/test_market_trend.py tests/test_trend_review.py \
  tests/test_trend_market_controller.py tests/test_dashboard.py \
  tests/test_dashboard_web.py -q
```

- [ ] **Snapshot pre-change report invariants**

Copy the selected current CN/HK/US JSON files into a task-specific directory
created by `mktemp -d`. Record SHA-256, strategy identity/version, candidate
order, formal action side/symbol/quantity, risk, Kelly, real holding decisions,
industry contexts/status, and API cost.

- [ ] **Initialize `515450` without a network call**

Invoke `TrendAnimalsClient.remember_symbol_row` against the canonical runtime
cache with the already observed successful fact:

```python
client.remember_symbol_row(
    market="CN",
    expected_futu_symbol="SH.515450",
    row={
        "tmId": 328879,
        "tickerSymbol": "515450",
        "asset": "ETF基金",
    },
)
```

Use a transport that raises if called. Read the JSON back and assert all six
fields. Start a second client with the same raising transport and assert
`search_exact_symbol("SH.515450", market="CN", expected_date="2026-07-31")`
returns `328879`. Leave old dated miss files untouched; the new contract must
ignore them.

- [ ] **Candidate-deploy controllers and request revisions**

Link the shared environment if necessary, then install all three controllers
from this worktree:

```bash
test -e .venv || ln -s /Users/ray/projects/open_trader/.venv .venv
./scripts/install_daily_premarket_launchd.sh \
  --trend-only --market all \
  --config /Users/ray/projects/open_trader/config/daily_premarket.env
```

Verify fresh CN/HK/US status files show the worktree cwd, current SHA, and
advancing heartbeat. Request one revision for each market:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  .venv/bin/python -m open_trader.cli trend-market run \
  --market CN --revision \
  --config /Users/ray/projects/open_trader/config/daily_premarket.env
```

Repeat for HK and US and wait for the branch controllers to finish.

- [ ] **Compare intended changes only**

Require unchanged strategy identity, candidate order, action side/symbol/qty,
risk, sizing, and Kelly facts. Allow:

- additive frozen `futu_symbol` and mapping metadata;
- `515450` real advisory decision changing from `MANUAL_REVIEW` to its computed
  signal, while remaining outside `formal_actions`;
- candidate blocking only if a complete mapping genuinely cannot be proven;
- industry membership/order, row-local invalid details, billing/cache facts,
  revision metadata, and hashes.

Assert CN includes `515450` resolved, 银行 `339103`, 电力 `621693`, descending
industry strength, and candidate-scoped context status. If the ETF has no
`industryTmId`, assert no synthetic context.

Current report artifacts appear to be runtime data; stage them only if
`git status` proves they are tracked. Never bulk-add ignored data.

---

## Task 8: Changelog, full gate, deployment proof, and screenshot

**Files:**

- Modify: `CHANGELOG.md`

- [ ] **Update and commit the dated operator log**

Record verified three-key mappings, permanent no-retry misses, exact simulated
`futu_symbol`, resolved `515450`, holding-industry union, and strength order.

```bash
git add CHANGELOG.md
git commit -m "docs: log verified trend symbol routing"
```

- [ ] **Run focused and full automated verification**

```bash
git diff --check main...HEAD
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  .venv/bin/python -m pytest \
  tests/test_trend_animals.py tests/test_a_share_trend.py \
  tests/test_market_trend.py tests/test_trend_review.py \
  tests/test_trend_market_controller.py tests/test_dashboard.py \
  tests/test_dashboard_web.py tests/test_dashboard_acceptance.py -q
make test
```

Record exact counts and durations. Fix, commit, and redeploy any failure before
the final gate.

- [ ] **Candidate-deploy the final SHA**

```bash
ACCEPT_CANDIDATE_SHA="$(git rev-parse HEAD)"
./scripts/install_dashboard_launchd.sh \
  --repo-root "$PWD" \
  --runtime-root /Users/ray/projects/open_trader \
  --python /Users/ray/projects/open_trader/.venv/bin/python
./scripts/install_daily_premarket_launchd.sh \
  --trend-only --market all \
  --config /Users/ray/projects/open_trader/config/daily_premarket.env
```

Verify Dashboard/controller PIDs, cwd, candidate SHA, fresh logs, advancing
heartbeats, and HTTP 200.

- [ ] **Run the final acceptance gate**

```bash
make acceptance
```

Only `PASS` continues. On `FAIL`, fix and rerun after a new committed/deployed
SHA. On `BLOCKED`, report the blocker without substituting mocks, curl, or unit
tests.

- [ ] **Redeploy the exact accepted SHA and capture the live view**

Assert `git rev-parse HEAD` still equals the accepted SHA, redeploy Dashboard
and controllers, then recheck PID/cwd/SHA/log/heartbeat/HTTP proof.

Capture one readable live desktop screenshot showing:

- resolved `515450 红利50` advisory trend fields;
- 银行 and 电力 in the strength-sorted industry table;
- no false global fallback message;
- mapping conflict copy if a controlled acceptance fixture exercises it.

No mobile screenshot is required because responsive behavior is unchanged.

- [ ] **Hand off without merge or push**

Report exact test/acceptance results, accepted SHA, deployed process proof,
direct review URL, screenshot, action-invariant comparison, expected real
holding/context changes, and observed Trend Animals cost. Wait for the user's
merge instruction.

## Stop conditions

Stop instead of guessing when:

- any Futu code, Trend code, or `tmId` conflicts;
- discovery is not uniquely valid;
- a new-contract formal action lacks a verified `futu_symbol`;
- report regeneration changes candidate ranking, action side/symbol/quantity,
  strategy, sizing, risk, or Kelly facts outside the approved mapping gate;
- live SHA differs from the accepted SHA;
- acceptance is `BLOCKED`.
