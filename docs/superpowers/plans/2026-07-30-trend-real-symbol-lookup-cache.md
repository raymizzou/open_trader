# Real Holding Trend Symbol Lookup Cache Implementation Plan

> **For Codex:** Execute this plan inline with test-driven development. Keep the existing real/simulated tabs, all ten columns, option-anomaly behavior, simulated-account strategy decisions, and Feishu output unchanged.

**Goal:** Make Trend Animals symbol resolution market-aware and cache completed lookups, exclude `US.AGRZ` from trend queries, and degrade missing real-position trend data per symbol instead of disabling the full real-holdings table.

**Architecture:** Keep symbol lookup and persistence inside `TrendAnimalsClient`. Propagate a small `trend_excluded_symbols` set through the existing real-holding input model, while representing ordinary unresolved symbols through their existing `None` holding snapshot. The report evaluator remains shared across CN/HK/US and gives excluded real positions a dedicated manual-review reason. The dashboard only adds the corresponding label and a stable final sort bucket; it does not alter columns or markup structure.

**Tech Stack:** Python 3.11, pytest, dataclasses, existing JSON cache helpers, existing dashboard JavaScript and Playwright acceptance harness.

---

## Task 1: Filter Trend Animals search results by market asset and cache date-scoped misses

**Files:**

- Modify: `src/open_trader/trend_animals.py:100`
- Test: `tests/test_trend_animals.py:280-530`

### Step 1: Write failing lookup tests

Update every existing `search_exact_symbol` call in `tests/test_trend_animals.py` to pass `expected_date="2026-07-29"`, then add:

```python
def test_search_exact_symbol_ignores_same_code_crypto_asset(tmp_path: Path) -> None:
    transport = FakeTransport(
        {
            "searchTicker": success(
                [
                    {
                        "tickerSymbol": "MSFT",
                        "tmId": 335795,
                        "asset": "美股",
                    },
                    {
                        "tickerSymbol": "MSFT",
                        "tmId": 698310,
                        "asset": "加密币",
                    },
                ]
            )
        }
    )
    client = TrendAnimalsClient(
        api_key="secret-value",
        cache_dir=tmp_path,
        transport=transport,
    )

    assert (
        client.search_exact_symbol(
            "MSFT",
            market="US",
            expected_date="2026-07-29",
        )
        == 335795
    )


def test_search_exact_symbol_reuses_miss_only_for_same_data_date(
    tmp_path: Path,
) -> None:
    first_transport = FakeTransport({"searchTicker": success([])})
    first_client = TrendAnimalsClient(
        api_key="secret-value",
        cache_dir=tmp_path,
        transport=first_transport,
    )

    with pytest.raises(TrendAnimalsLookupError):
        first_client.search_exact_symbol(
            "EUV",
            market="US",
            expected_date="2026-07-29",
        )

    cached_transport = FakeTransport({})
    cached_client = TrendAnimalsClient(
        api_key="secret-value",
        cache_dir=tmp_path,
        transport=cached_transport,
    )
    with pytest.raises(TrendAnimalsLookupError):
        cached_client.search_exact_symbol(
            "EUV",
            market="US",
            expected_date="2026-07-29",
        )
    assert cached_transport.calls == []

    retry_transport = FakeTransport(
        {
            "searchTicker": success(
                [
                    {
                        "tickerSymbol": "EUV",
                        "tmId": 800001,
                        "asset": "美国ETF",
                    }
                ]
            )
        }
    )
    retry_client = TrendAnimalsClient(
        api_key="secret-value",
        cache_dir=tmp_path,
        transport=retry_transport,
    )
    assert (
        retry_client.search_exact_symbol(
            "EUV",
            market="US",
            expected_date="2026-07-30",
        )
        == 800001
    )
    assert len(retry_transport.calls) == 1


def test_search_exact_symbol_does_not_cache_transport_failure(
    tmp_path: Path,
) -> None:
    def failing_transport(url: str, timeout: float) -> object:
        raise RuntimeError(f"temporary transport failure: {url}")

    failing_client = TrendAnimalsClient(
        api_key="secret-value",
        cache_dir=tmp_path,
        transport=failing_transport,
    )
    with pytest.raises(TrendAnimalsError):
        failing_client.search_exact_symbol(
            "EUV",
            market="US",
            expected_date="2026-07-29",
        )

    retry_transport = FakeTransport(
        {
            "searchTicker": success(
                [
                    {
                        "tickerSymbol": "EUV",
                        "tmId": 800001,
                        "asset": "美国ETF",
                    }
                ]
            )
        }
    )
    retry_client = TrendAnimalsClient(
        api_key="secret-value",
        cache_dir=tmp_path,
        transport=retry_transport,
    )
    assert (
        retry_client.search_exact_symbol(
            "EUV",
            market="US",
            expected_date="2026-07-29",
        )
        == 800001
    )
```

Use the existing `FakeTransport` constructor/call shape when integrating the snippets. Also extend the existing market-conversion parametrization with explicit allowed assets:

```python
[
    ("CN", "600025", "A股"),
    ("CN", "510300", "ETF基金"),
    ("HK", "00027", "港股"),
    ("HK", "03033", "香港ETF"),
    ("US", "MSFT", "美股"),
    ("US", "QQQ", "美国ETF"),
]
```

### Step 2: Run the lookup tests and confirm RED

Run:

```bash
pytest -q tests/test_trend_animals.py
```

Expected: failures because `expected_date` is not accepted, crypto duplicates remain ambiguous, and misses are not cached.

### Step 3: Implement market-aware result filtering and miss caching

In `src/open_trader/trend_animals.py`, define:

```python
SEARCH_ASSETS_BY_MARKET = {
    "CN": frozenset({"A股", "ETF基金"}),
    "HK": frozenset({"港股", "香港ETF"}),
    "US": frozenset({"美股", "美国ETF"}),
}
```

Change the public method to:

```python
def search_exact_symbol(
    self,
    symbol: str,
    *,
    market: str,
    expected_date: str,
) -> int:
```

Validate `expected_date` with the existing date validator. Preserve the permanent positive cache at `symbols/<canonical-symbol>.json`. Before calling `searchTicker`, read:

```python
miss_path = (
    self.cache_dir
    / "symbol_misses"
    / market.upper()
    / expected_date
    / f"{normalized}.json"
)
```

Only accept rows whose explicit non-empty `asset` belongs to `SEARCH_ASSETS_BY_MARKET[market.upper()]`. Continue accepting rows without an `asset` field for compatibility with existing API fixtures. When exact market-filtered matches do not contain exactly one `tmId`, atomically persist:

```python
{
    "market": market.upper(),
    "date": expected_date,
    "symbol": normalized,
    "error": "no_unique_exact_match",
}
```

Then raise the existing `TrendAnimalsLookupError`. Read that exact payload on later same-date calls and raise without a network request. Do not write a miss for transport, server, JSON-schema, or cache-corruption errors.

### Step 4: Run lookup tests and confirm GREEN

Run:

```bash
pytest -q tests/test_trend_animals.py
```

Expected: all tests pass.

### Step 5: Commit

```bash
git add src/open_trader/trend_animals.py tests/test_trend_animals.py
git commit -m "fix: filter and cache Trend Animals symbol lookups"
```

---

## Task 2: Degrade unresolved real holdings per symbol and exclude AGRZ

**Files:**

- Modify: `src/open_trader/a_share_trend.py:1113,1380,2964,3723,5658`
- Modify: `src/open_trader/market_trend.py:960`
- Test: `tests/test_a_share_trend.py:3380-3580,5274`
- Test: `tests/test_market_trend.py:913,1143,1352,1563`

### Step 1: Write failing enrichment tests

Add a real-input test with `US.AGRZ`, `US.EUV`, and a fake quote client that returns usable bars for both. Its fake Trend Animals client must:

```python
def search_exact_symbol(
    self,
    symbol: str,
    *,
    market: str,
    expected_date: str,
) -> int:
    self.searches.append((symbol, market, expected_date))
    raise TrendAnimalsLookupError(f"missing {symbol}")
```

Assert all of the following:

```python
assert enriched.status == "available"
assert enriched.trend_excluded_symbols == frozenset({"US.AGRZ"})
assert fake_api.searches == [("US.EUV", "US", "2026-07-29")]
assert enriched.holding_snapshots == {
    "US.AGRZ": None,
    "US.EUV": None,
}
assert set(enriched.bars_by_symbol) == {"US.AGRZ", "US.EUV"}
```

Build the report from this enriched input and assert:

```python
assert real_decisions["US.AGRZ"].action == "MANUAL_REVIEW"
assert real_decisions["US.AGRZ"].reason == "holding_trend_excluded"
assert real_decisions["US.AGRZ"].temperature is None
assert real_decisions["US.AGRZ"].season is None
assert real_decisions["US.AGRZ"].strength is None
assert real_decisions["US.EUV"].reason == "holding_signal_unknown"
```

Add a second test whose fake raises `TrendAnimalsError("service unavailable")`; assert the enriched real input remains globally `unavailable`. This preserves fail-closed behavior for system-wide dependencies.

Update fake `search_exact_symbol` methods in `tests/test_a_share_trend.py` and `tests/test_market_trend.py` to accept and assert the passed `expected_date`.

### Step 2: Run focused real-holding tests and confirm RED

Run:

```bash
pytest -q tests/test_a_share_trend.py -k "real_holding or enrich_real"
```

Expected: AGRZ is still requested and one unresolved symbol makes the complete real input unavailable.

### Step 3: Implement the narrow real-holding state

In `src/open_trader/a_share_trend.py`, define:

```python
REAL_HOLDING_TREND_EXCLUDED_SYMBOLS = frozenset({"US.AGRZ"})
```

Extend `RealHoldingInput` with a defaulted field:

```python
trend_excluded_symbols: frozenset[str] = frozenset()
```

In `enrich_real_holding_input`:

1. Convert each position to its canonical Futu symbol.
2. If it is in `REAL_HOLDING_TREND_EXCLUDED_SYMBOLS`, add it to the excluded set and skip both `searchTicker` and snapshot requests.
3. Pass `expected_date=as_of_date` on every non-excluded lookup.
4. Catch `TrendAnimalsLookupError` per symbol and leave only that symbol's snapshot as `None`.
5. Continue treating any other Trend Animals failure as a whole-input dependency failure.
6. Fetch Futu K-lines for every real position, including excluded and unresolved positions.
7. Return the still-available input with `trend_excluded_symbols` populated.

Add a keyword-only `trend_excluded_symbols` parameter to `_evaluate_holding_positions`. For read-only real decisions, override an excluded position to:

```python
action = "MANUAL_REVIEW"
reason = "holding_trend_excluded"
```

Pass the real input's excluded set from the real-decision call site and an empty set from the simulated call site. Add:

```python
"holding_trend_excluded": "已排除趋势查询",
```

to `REASON_LABELS`.

In both `a_share_trend.py` and `market_trend.py`, pass `expected_date=as_of_date` to the simulated-holding symbol lookup without changing strategy decisions.

### Step 4: Run shared market/report tests and confirm GREEN

Run:

```bash
pytest -q \
  tests/test_a_share_trend.py \
  tests/test_market_trend.py
```

Expected: all tests pass for CN, HK, and US report generation.

### Step 5: Commit

```bash
git add \
  src/open_trader/a_share_trend.py \
  src/open_trader/market_trend.py \
  tests/test_a_share_trend.py \
  tests/test_market_trend.py
git commit -m "fix: isolate unresolved real holding trend data"
```

---

## Task 3: Put excluded real holdings last without changing the dashboard table

**Files:**

- Modify: `src/open_trader/dashboard.py:1243`
- Modify: `src/open_trader/dashboard_static/dashboard.js:3045`
- Modify: `src/open_trader/dashboard_acceptance.py:112`
- Test: `tests/test_dashboard.py`
- Test: `tests/test_dashboard_web.py`
- Test: `tests/test_dashboard_acceptance.py`

### Step 1: Write failing projection and rendering tests

Extend the existing frozen-real-position projection test with an AGRZ item:

```python
{
    "symbol": "US.AGRZ",
    "name": "Global X US Infrastructure Development ETF",
    "action": "MANUAL_REVIEW",
    "reason": "holding_trend_excluded",
    "temperature": None,
    "season": None,
    "strength": None,
}
```

Assert the projected symbol order places `US.AGRZ` after every ordinary real holding, even ordinary `HOLD` rows.

Extend the real/simulated tab web test to render that row and assert:

```python
assert "已排除趋势查询" in real_rows_html
assert real_rows_html.count(">—<") >= 3
```

The dash assertion confirms empty trend-related cells render through the existing placeholder instead of inventing values.

### Step 2: Run dashboard tests and confirm RED

Run:

```bash
pytest -q \
  tests/test_dashboard.py \
  tests/test_dashboard_web.py \
  tests/test_dashboard_acceptance.py
```

Expected: AGRZ follows the normal manual-review urgency bucket rather than sorting last, and the reason label is unknown.

### Step 3: Implement the sort bucket and labels

In `_project_trend_real_actions`, prepend the exclusion flag to the existing sort key:

```python
return (
    item.get("reason") == "holding_trend_excluded",
    urgency.get(str(item.get("action") or ""), 4),
    str(item.get("symbol") or ""),
)
```

Add the same reason mapping to `TREND_REASON_LABELS` in both:

- `src/open_trader/dashboard_static/dashboard.js`
- `src/open_trader/dashboard_acceptance.py`

Do not add a column, wrapper, badge, new tab, or CSS rule.

### Step 4: Run dashboard tests and confirm GREEN

Run:

```bash
pytest -q \
  tests/test_dashboard.py \
  tests/test_dashboard_web.py \
  tests/test_dashboard_acceptance.py
```

Expected: all tests pass.

### Step 5: Commit

```bash
git add \
  src/open_trader/dashboard.py \
  src/open_trader/dashboard_static/dashboard.js \
  src/open_trader/dashboard_acceptance.py \
  tests/test_dashboard.py \
  tests/test_dashboard_web.py \
  tests/test_dashboard_acceptance.py
git commit -m "fix: place excluded real trend holdings last"
```

---

## Task 4: Directly verify caching, report behavior, and all three markets

### Step 1: Run all focused automated tests

Run:

```bash
pytest -q \
  tests/test_trend_animals.py \
  tests/test_a_share_trend.py \
  tests/test_market_trend.py \
  tests/test_dashboard.py \
  tests/test_dashboard_web.py \
  tests/test_dashboard_acceptance.py
```

Expected: all tests pass.

### Step 2: Verify real API symbol resolution and cache behavior

Using the configured Trend Animals client and report data date `2026-07-29`:

1. Resolve `MSFT`, `QQQ`, `SMH`, `TSM`, and `DRAM`.
2. Confirm the selected rows use `美股` or `美国ETF`, never `加密币`.
3. Request `EUV` twice and confirm the second same-date call creates no HTTP request.
4. Request `EUV` with the next data date and confirm it retries.
5. Exercise real-input enrichment and confirm `US.AGRZ` creates no Trend Animals request.

Record the cache file paths and request counts in the command output.

### Step 3: Run direct CN/HK/US report workflows safely

For each market, run the non-ordering report generation path against current account/runtime data. Do not overwrite an immutable report or a report with an execution batch. Verify:

- CN uses A-share/ETF asset filtering.
- HK uses Hong Kong stock/ETF asset filtering.
- US keeps AGRZ visible at the end with empty trend fields and keeps EUV as an individual manual-review row.
- Real-input degradation does not change any simulated holding action.

If today's report is locked, use the existing revision mechanism or a temporary output path rather than mutating the original report.

---

## Task 5: Changelog, integration, acceptance, and exact-SHA deployment

**Files:**

- Modify: `CHANGELOG.md`

### Step 1: Add the operator-facing changelog entry before merge

Under the dated `2026-07-30` section, record:

- Trend Animals search now filters symbol duplicates by market asset.
- Successful lookups remain cached; same-market/same-data-date misses are cached and retried on the next data date.
- `US.AGRZ` remains visible but skips trend lookup and sorts last.
- An unresolved real symbol affects only its own row; simulated strategy remains unchanged.

Commit:

```bash
git add CHANGELOG.md
git commit -m "docs: log real holding trend lookup recovery"
```

### Step 2: Confirm a clean feature branch and run final acceptance on its exact SHA

Run:

```bash
git status --short
git log --oneline --decorate -6
git diff main...HEAD --check
```

Deploy the candidate feature SHA using the feature worktree as code root and the main workspace as runtime-data root. Restart the dashboard and CN/HK/US controllers on that candidate SHA, then run:

```bash
make acceptance
```

This is the final dashboard gate and must return `PASS`. On `FAIL`, diagnose, fix on the feature branch, update the changelog if operator-visible meaning changes, redeploy the new candidate, and rerun acceptance. On `BLOCKED`, report the blocker and do not claim completion.

### Step 3: Fast-forward local main to the exact accepted SHA

After `PASS`, confirm local `main` has not advanced beyond the feature baseline:

```bash
git merge-base --is-ancestor main HEAD
```

Preserve the root checkout's unrelated `.gitignore`, research, output, plan, spec, and prototype files with a uniquely named temporary stash. Fast-forward local `main` to the accepted feature SHA so the integration creates no different, unaccepted merge commit:

```bash
git merge --ff-only fix/trend-real-symbol-lookup-cache
```

If `main` advanced, merge current `main` into the feature branch, rerun the focused tests, commit any conflict resolution, and rerun the single final acceptance gate on the new candidate before integrating.

### Step 4: Verify live processes and restore unrelated work

Redeploy the exact accepted SHA from the `main` checkout without source/data changes. Verify:

```bash
curl -fsS http://127.0.0.1:8766/ >/dev/null
```

Also verify:

- dashboard PID, working directory, Git SHA, and a fresh `dashboard_runtime` log line;
- CN/HK/US controller PID, working directory, Git SHA, heartbeat timestamp, and status JSON;
- accepted SHA equals local `main`;
- the temporary stash restores the root checkout to its original unrelated dirty-file set.

### Step 5: Push the accepted main SHA

Run:

```bash
git push origin main
git fetch origin main
git rev-parse main
git rev-parse origin/main
```

Expected: local `main`, `origin/main`, the accepted SHA, and all four live processes identify the same commit.
