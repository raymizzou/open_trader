# Futu-Only A-Share Market Data Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace AKShare with the existing Futu OpenD client for every supported A-share quote and historical-data workflow.

**Architecture:** Add one small Python symbol normalizer shared by quote-universe, watcher, T-signal, and historical K-line paths. Keep the existing Futu client and Dashboard quote service; extend them to CN and remove the Dashboard's AKShare branch. The browser uses the same SH/SZ rule when indexing the quote payload.

**Tech Stack:** Python 3.12, futu-api, pytest, vanilla JavaScript, Node runtime checks, Make acceptance.

## Global Constraints

- Futu OpenD is the only A-share market-data source; no fallback is allowed.
- Preserve the existing actionable Futu diagnostics and stale-quote behavior.
- Support existing Shanghai and Shenzhen stocks/ETFs plus CSI 300; Beijing Stock Exchange is out of scope.
- Work only on branch `feat/futu-cn-market-data` in `.worktrees/futu-cn-market-data`.
- `make acceptance` must return `PASS` before merge.
- Merge and deploy the exact accepted SHA to `main`, then verify process identity, logs, and HTTP 200.

---

### Task 1: Normalize A-share symbols once

**Files:**
- Create: `src/open_trader/futu_symbols.py`
- Modify: `src/open_trader/t_signal.py`
- Modify: `src/open_trader/futu_watch.py`
- Modify: `src/open_trader/futu_universe.py`
- Test: `tests/test_t_signal.py`
- Test: `tests/test_futu_watch.py`
- Test: `tests/test_futu_universe.py`

**Interfaces:**
- Produces: `to_futu_symbol(market: str, symbol: str) -> str`.
- Mapping: `CN.600025 -> SH.600025`, `CN.000001 -> SZ.000001`, and `CN.000300 -> SH.000300`.
- Invalid market, prefix, digit count, or exchange family raises `ValueError`; CSV trigger loading converts that error into the existing skipped-row behavior.

- [ ] **Step 1: Write failing symbol and consumer tests**

```python
assert to_futu_symbol("CN", "600025") == "SH.600025"
assert to_futu_symbol("CN", "000001") == "SZ.000001"
assert to_futu_symbol("CN", "000300") == "SH.000300"
with pytest.raises(ValueError):
    to_futu_symbol("CN", "800001")
```

Add a CN watchlist-row assertion for `SH.600025`, and add CN Shanghai and
Shenzhen rows to the Futu universe expectation.

- [ ] **Step 2: Run the focused tests and confirm RED**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_t_signal.py tests/test_futu_watch.py tests/test_futu_universe.py
```

Expected: failures showing CN remains unsupported.

- [ ] **Step 3: Add the minimal shared normalizer and route consumers through it**

```python
KNOWN_PREFIXES = {"HK", "US", "CN", "SH", "SZ"}


def to_futu_symbol(market: str, symbol: str) -> str:
    normalized_market = market.strip().upper()
    normalized_symbol = symbol.strip().upper()
    if normalized_market not in {"HK", "US", "CN"}:
        raise ValueError(f"unsupported Futu market: {market}")
    if "." in normalized_symbol:
        prefix, remainder = normalized_symbol.split(".", 1)
        if prefix == normalized_market:
            normalized_symbol = remainder
        elif normalized_market == "CN" and prefix in {"SH", "SZ"}:
            if prefix != _cn_exchange(remainder):
                raise ValueError(f"symbol prefix {prefix} does not match {symbol}")
            return f"{prefix}.{remainder}"
        elif not (normalized_market == "US" and prefix not in KNOWN_PREFIXES):
            raise ValueError(
                f"symbol prefix {prefix} does not match market {normalized_market}"
            )
    if not normalized_symbol:
        raise ValueError(f"empty symbol for market {normalized_market}")
    if normalized_market == "US":
        return f"US.{normalized_symbol}"
    if normalized_market == "HK" and normalized_symbol.isdigit() and len(normalized_symbol) <= 5:
        return f"HK.{normalized_symbol.zfill(5)}"
    if normalized_market == "CN":
        return f"{_cn_exchange(normalized_symbol)}.{normalized_symbol}"
    raise ValueError(f"invalid symbol for market {normalized_market}: {symbol}")


def _cn_exchange(symbol: str) -> str:
    if len(symbol) != 6 or not symbol.isdigit():
        raise ValueError(f"invalid CN symbol: {symbol}")
    if symbol == "000300" or symbol[0] in "569":
        return "SH"
    if symbol[0] in "0123":
        return "SZ"
    raise ValueError(f"unsupported CN symbol: {symbol}")
```

Import this function from `t_signal.py`, `futu_watch.py`, and
`futu_universe.py`; delete their duplicate conversion code and add `CN` to the
universe's supported markets.

- [ ] **Step 4: Run focused tests and confirm GREEN**

Run the Step 2 command.

Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/futu_symbols.py src/open_trader/t_signal.py \
  src/open_trader/futu_watch.py src/open_trader/futu_universe.py \
  tests/test_t_signal.py tests/test_futu_watch.py tests/test_futu_universe.py
git commit -m "feat: normalize A-share Futu symbols"
```

### Task 2: Use Futu for CN snapshots, history, and backtests

**Files:**
- Modify: `src/open_trader/futu_quote.py`
- Modify: `src/open_trader/dashboard_web.py`
- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Test: `tests/test_futu_quote.py`
- Test: `tests/test_dashboard_quotes.py`
- Test: `tests/test_dashboard_web.py`

**Interfaces:**
- Consumes: `to_futu_symbol(market: str, symbol: str) -> str` from Task 1.
- Preserves: `FutuQuoteClient.get_daily_kline(futu_symbol, start, end)` protocol.
- Produces: CN Dashboard quote requests using SH/SZ keys and CN standard backtests owned by `FutuQuoteClient`.

- [ ] **Step 1: Write failing integration tests**

```python
bars = client.get_daily_kline("CN.600025", start="2026-07-01", end="2026-07-14")
assert client.context.requested_history["symbol"] == "SH.600025"
```

Extend the Dashboard quote fixture with a CN holding and assert the service
requests `SH.600025`. Replace `test_cn_standard_backtest_owns_akshare_provider`
with an assertion that CN constructs `FutuQuoteClient`. Add a JavaScript runtime
check that `futuSymbolForHolding({market: "CN", symbol: "600025"})` is
`SH.600025` and code `000001` is `SZ.000001`.

- [ ] **Step 2: Run the focused tests and confirm RED**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_futu_quote.py \
  tests/test_dashboard_quotes.py tests/test_dashboard_web.py
```

Expected: CN is absent from the quote request, history receives `CN.600025`,
and the backtest still selects AKShare.

- [ ] **Step 3: Implement the shortest Futu-only path**

Normalize the first argument inside `FutuQuoteClient.get_daily_kline()` before
calling `request_history_kline()`. In `build_standard_backtest_run_payload()`,
always construct the existing `FutuQuoteClient` when no provider is injected.
Extend `futuSymbolForHolding()` with the same tested SH/SZ mapping so CN quote
rows resolve from the existing payload without changing its schema.

- [ ] **Step 4: Run focused tests and confirm GREEN**

Run the Step 2 command.

Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/futu_quote.py src/open_trader/dashboard_web.py \
  src/open_trader/dashboard_static/dashboard.js tests/test_futu_quote.py \
  tests/test_dashboard_quotes.py tests/test_dashboard_web.py
git commit -m "feat: serve A-share market data from Futu"
```

### Task 3: Remove AKShare and verify the live system

**Files:**
- Delete: `src/open_trader/akshare_quote.py`
- Delete: `tests/test_akshare_quote.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Removes: `AkShareDailyKlineProvider` and the `akshare` dependency.
- Retains: the shared `DailyKlineProvider` protocol and Futu error contract.

- [ ] **Step 1: Delete the unused provider and dependency**

Remove the two files and remove only the `"akshare",` dependency line.

- [ ] **Step 2: Prove no AKShare production reference remains**

Run:

```bash
rg -n "akshare|AkShare" pyproject.toml src tests
```

Expected: no output.

- [ ] **Step 3: Run the complete automated suite**

Run:

```bash
.venv/bin/python -m pytest -q
```

Expected: all tests pass.

- [ ] **Step 4: Commit the removal**

```bash
git add pyproject.toml src/open_trader/akshare_quote.py tests/test_akshare_quote.py
git commit -m "chore: remove AKShare market data"
```

- [ ] **Step 5: Verify real OpenD data**

Use the project client to fetch current snapshots for the five checked-in CN
holdings and daily K-lines for `CN.600025` and `CN.000300`. Confirm OpenD PID,
`qot_logined=True`, non-empty prices/bars, and SH-prefixed wire symbols.

- [ ] **Step 6: Run the final acceptance gate**

Start/restart the Dashboard from this worktree so its working directory and Git
SHA match the candidate, then run:

```bash
make acceptance
```

Expected: `PASS`. `FAIL` must be fixed and rerun; `BLOCKED` must be reported.

- [ ] **Step 7: Review, merge, and deploy the accepted SHA**

Run the repository code-review workflow, fix any valid findings, rerun focused
tests and `make acceptance` after source changes, then merge the branch into
`main`. Restart the Dashboard from `main` at the exact accepted tree, and verify
new PID, main working directory, accepted Git SHA, fresh logs, and HTTP 200 from
the configured review URL.
