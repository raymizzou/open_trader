# Trend Symbol Mapping and Price Fetch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Correct Trend Animals/Futu symbols for CN, HK, and US, keep holding quote retrieval independent from Trend Animals lookup, and recover only audited simulated actions caused by the defect.

**Architecture:** Keep `futu_symbols.py` as the sole market-code boundary and make `TrendAnimalsClient.search_exact_symbol` market-aware. Both report runners fetch every simulated holding's Futu daily K line before independently resolving its optional Trend Animals snapshot. Reuse the existing immutable report revision, late-buy authorization, intent/result/observation, and Kelly attribution paths for live recovery.

**Tech Stack:** Python 3.12, pytest, existing Futu and Trend Animals clients, JSON audit ledgers.

## Global Constraints

- Start and remain on the isolated `fix/trend-symbol-mapping-price` worktree based on local `main`.
- Add no dependency, configuration key, database, background task, retry framework, or second execution path.
- Keep existing NAV, cash, FX, quantity, protection, Kelly, drawdown, 0.4% single-trade, 4% portfolio, and 1% abnormal-loss guards unchanged.
- Preserve frozen reports; write only explicit report revisions.
- Run focused tests while developing and run `make acceptance` exactly once as the final Dashboard gate.
- Before merging, commit a dated operator-facing `CHANGELOG.md` entry.
- Only `PASS` permits deployment; deploy the exact accepted SHA and verify PID, cwd, SHA, fresh logs, and HTTP 200.

---

### Task 1: Shared CN/HK/US Symbol Boundary

**Files:**
- Modify: `src/open_trader/futu_symbols.py`
- Modify: `src/open_trader/trend_animals.py`
- Test: `tests/test_t_signal.py`
- Test: `tests/test_trend_animals.py`

**Interfaces:**
- Produces: `to_trend_animals_symbol(market: str, symbol: str) -> str`
- Produces: `from_trend_animals_symbol(market: str, symbol: str) -> str`
- Changes: `TrendAnimalsClient.search_exact_symbol(symbol: str, *, market: str) -> int`

- [ ] **Step 1: Write failing literal conversion tests**

Add table-driven cases whose hand-derived outputs include:

```python
assert to_trend_animals_symbol("CN", "SH.600036") == "600036.SH"
assert to_trend_animals_symbol("CN", "SZ.000001") == "000001.SZ"
assert to_trend_animals_symbol("HK", "HK.00027") == "0027.HK"
assert to_trend_animals_symbol("HK", "HK.00622") == "0622.HK"
assert to_trend_animals_symbol("HK", "HK.00939") == "0939.HK"
assert to_trend_animals_symbol("HK", "HK.02800") == "2800.HK"
assert to_trend_animals_symbol("US", "US.ARWR") == "ARWR"
assert from_trend_animals_symbol("HK", "0027.HK") == "HK.00027"
assert from_trend_animals_symbol("CN", "600036.SH") == "SH.600036"
assert from_trend_animals_symbol("US", "ARWR.US") == "US.ARWR"
```

Also reject cross-market suffixes and malformed HK codes.

- [ ] **Step 2: Run conversion tests and verify RED**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_t_signal.py -k 'trend_animals_symbol'
```

Expected: collection/import failure because the two conversion functions do not exist.

- [ ] **Step 3: Implement the two deterministic conversion functions**

Use `to_futu_symbol` for canonical input validation. For HK, remove exactly one Futu padding zero when present and add exactly one on the reverse path; do not use `lstrip("0")`. For CN, preserve and validate SH/SZ/BJ. For US, preserve valid class-share punctuation and strip only a terminal `.US`.

- [ ] **Step 4: Run conversion tests and verify GREEN**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_t_signal.py -k 'trend_animals_symbol or to_futu_symbol'
```

Expected: all selected tests pass.

- [ ] **Step 5: Write failing market-aware lookup tests**

Change the existing exact-search test to call:

```python
client.search_exact_symbol("HK.00027", market="HK")
```

Assert the request keyword is the literal `0027.HK`, a returned `0027.HK` maps to the requested Futu code, and a returned `000027.SZ` is rejected. Add CN and US request cases. Keep the symbol-cache test and require cache hits to remain shape-validated.

- [ ] **Step 6: Run lookup tests and verify RED**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_trend_animals.py -k 'exact_symbol or symbol_cache'
```

Expected: failure because the old method has no market argument and compares only the ticker prefix.

- [ ] **Step 7: Implement market-aware exact lookup**

Build the query with `to_trend_animals_symbol`, convert each returned `tickerSymbol` with `from_trend_animals_symbol`, compare canonical Futu codes, and require exactly one unique matching `tmId`. Keep the existing atomic cache and secret-redaction behavior.

- [ ] **Step 8: Run focused symbol tests and commit**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_t_signal.py tests/test_trend_animals.py
git add src/open_trader/futu_symbols.py src/open_trader/trend_animals.py tests/test_t_signal.py tests/test_trend_animals.py
git commit -m "fix: normalize Trend Animals symbols by market"
```

Expected: all tests pass and the commit succeeds.

---

### Task 2: HK and US Holding Prices Independent of Trend Mapping

**Files:**
- Modify: `src/open_trader/market_trend.py`
- Test: `tests/test_market_trend.py`

**Interfaces:**
- Consumes: `TrendAnimalsClient.search_exact_symbol(symbol, market=market)`
- Consumes: `from_trend_animals_symbol(market, ticker_symbol)`
- Preserves: `run_market_trend_report(...) -> AShareTrendRunResult`

- [ ] **Step 1: Write a failing report regression test**

Use a real report build with one simulated holding. Make `search_exact_symbol` raise `TrendAnimalsLookupError`, return a valid dated K line for the holding's Futu code, and assert:

```python
assert decision["action"] == "MANUAL_REVIEW"
assert decision["reason"] == "holding_signal_unknown"
assert decision["close"] == "10"
assert "价格缺失" not in str(payload["risk_summary"]["pause_reason"])
assert "US.VIXY" in quote_requests  # use HK.00027 in the HK parameter case
```

Add a stale-cache case where the holding `tmId` returns another market's `tickerSymbol`; it must keep the quote but discard the trend snapshot.

- [ ] **Step 2: Run the regression test and verify RED**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_market_trend.py -k 'holding_lookup or cached_holding_symbol'
```

Expected: `close` is null or the Futu holding code was never requested.

- [ ] **Step 3: Implement the minimal data-flow reorder**

Pass `market=market` into exact lookup. Fetch and store daily K lines by iterating every `account.positions` entry, not `holding_ids`. In a second loop, accept a holding snapshot only when its returned `tickerSymbol` converts to the same canonical Futu holding code.

- [ ] **Step 4: Run HK/US focused tests and commit**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_market_trend.py
git add src/open_trader/market_trend.py tests/test_market_trend.py
git commit -m "fix: fetch HK and US holding prices independently"
```

Expected: the full market-trend test file passes.

---

### Task 3: CN Holding Prices Independent of Trend Mapping

**Files:**
- Modify: `src/open_trader/a_share_trend.py`
- Test: `tests/test_a_share_trend.py`

**Interfaces:**
- Consumes: `TrendAnimalsClient.search_exact_symbol(symbol, market="CN")`
- Consumes: `from_trend_animals_symbol("CN", ticker_symbol)`
- Preserves: `run_a_share_trend_report(...) -> AShareTrendRunResult`

- [ ] **Step 1: Extend the existing lookup-miss test to verify price and risk**

For the existing `TrendAnimalsLookupError("missing")` fixture, assert the holding remains `MANUAL_REVIEW/holding_signal_unknown`, has the valid Futu close, and does not set the portfolio pause reason to holding price missing. Add a cached wrong-exchange snapshot case.

- [ ] **Step 2: Run the CN regressions and verify RED**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_a_share_trend.py -k 'lookup_miss or cached_holding_symbol'
```

Expected: the lookup-miss holding has no close because the old K-line loop iterates only mapped holdings.

- [ ] **Step 3: Implement the same independent Futu-first flow**

Pass `market="CN"` into exact lookup. Fetch each holding's K line from `to_futu_symbol("CN", position.symbol)` before using any snapshot row. Validate the returned Trend Animals symbol against the account Futu code before building `_holding_snapshot`; never derive the quote exchange from the snapshot.

- [ ] **Step 4: Run all three focused suites and commit**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_trend_animals.py tests/test_market_trend.py tests/test_a_share_trend.py
git add src/open_trader/a_share_trend.py tests/test_a_share_trend.py
git commit -m "fix: fetch CN holding prices independently"
```

Expected: all selected suites pass.

---

### Task 4: Direct Three-Market Workflow and Audited Recovery

**Files:**
- Modify: `CHANGELOG.md`
- Runtime artifacts only: existing `reports/trend_*`, `data/trend_controller/<MARKET>`, and `data/trend_review/ledgers/<MARKET>`

**Interfaces:**
- Reuses: report revision workflow and `_record_revision_migration`
- Reuses: `late_buy_authorization.v1`
- Reuses: `execute_trend_review_open`, immutable intent/result/observation/action ledgers

- [ ] **Step 1: Run direct report generation for CN, HK, and US**

Request one revision per market through the running controllers:

```bash
for market in CN HK US; do
  PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m open_trader trend-market run \
    --market "$market" \
    --revision \
    --config /Users/ray/projects/open_trader/config/daily_premarket.env
done
```

Poll `trend-market status` and the revision completion artifacts until each report is terminal. Record each new report path and `_report_hash`.

- [ ] **Step 2: Produce the causal old/new action diff**

For each market, compare holding decisions, formal actions, risk pause reason, report execution date, and exact report SHA. Recovery candidates are only old mapping/price-failure decisions that become formal actions and have no matching ledger or Futu order.

- [ ] **Step 3: Add the dated changelog entry and commit all code**

Document the three-market mapping fix, Futu-first holding quotes, and audited simulated recovery behavior. Run `git diff --check`, review the branch diff from `42802c1`, and commit.

- [ ] **Step 4: Run code review and fix every valid finding**

Review standards and spec coverage. Re-run the focused three-market tests after any fix and commit the correction.

- [ ] **Step 5: Run the single final acceptance gate**

Run:

```bash
make acceptance
```

Expected: `PASS`. Do not run this command earlier or a second time.

- [ ] **Step 6: Deploy the exact accepted SHA**

Merge only after the changelog commit, restart affected controller/dashboard processes from the exact accepted SHA, and verify new PID, working directory, Git SHA, fresh timestamped logs, and:

```bash
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8766
```

Expected: `200`.

- [ ] **Step 7: Execute each eligible simulated recovery action**

At the relevant market's open session, bind authorization to the corrected report's path/SHA and execution date. Execute sells first, then buys serially. Before each buy reread account, cash, and quote; never exceed the frozen report quantity or existing risk cap. Stop at the first reject, partial fill, state mismatch, or incomplete audit.

- [ ] **Step 8: Verify broker and Kelly evidence**

For every submitted action, verify the Futu simulated account id, order id, filled quantity/price, resulting position, exactly one matching order, immutable intent/result/observation/action files, and frozen report attribution. Sync statistics and verify the eventual round remains `attribution_status=attributed` and `kelly_eligible=true` when closed.
