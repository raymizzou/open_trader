# Task 4 Report: Dashboard Symbol Universe and Standard Backtest APIs

## Outcome

- Dashboard state now exposes a deduplicated `backtest_universe` split into holdings and watchlist rows.
- Universe accepts HK/US stocks and ETFs only, excludes cash/options, preserves display symbols, and provides a five-digit HK `futu_symbol`.
- Added `GET /api/backtests/options` and `POST /api/backtests/standard/run`.
- Standard requests reject unknown keys and UI adapter selection; validate universe membership, strategy, range, ISO dates, and decimal/percent inputs with Chinese errors.
- Validation failures map to HTTP 400. Execution/provider/adapter failures map to HTTP 502.
- Holding payloads no longer include `backtest` or `backtest_readiness`; normal dashboard GET no longer auto-fetches prices.
- Retained legacy `/api/backtests/run` because `dashboard.js` and compatibility tests still call it. Removed unused `/api/backtests/prices` and auto-fetch helpers.

## TDD Evidence

### RED 1: symbol universe

Command:

```text
../../.venv/bin/python -m pytest tests/test_dashboard.py::test_dashboard_backtest_universe_combines_holdings_and_watchlist -q
```

Expected failure observed:

```text
E KeyError: 'backtest_universe'
1 failed in 0.28s
```

### GREEN 1

```text
.                                                                        [100%]
1 passed in 0.19s
```

### RED 2: options and standard run builders

Command:

```text
../../.venv/bin/python -m pytest tests/test_dashboard_web.py::test_backtest_options_payload_exposes_fixed_catalog_and_defaults tests/test_dashboard_web.py::test_standard_backtest_run_rejects_adapter_choice -q
```

Expected failures observed:

```text
ImportError: cannot import name 'build_standard_backtest_options_payload'
ImportError: cannot import name 'build_standard_backtest_run_payload'
2 failed in 0.33s
```

### GREEN 2

```text
..                                                                       [100%]
2 passed in 0.25s
```

### RED 3: HTTP routes and status mapping

Command:

```text
../../.venv/bin/python -m pytest tests/test_dashboard_web.py::test_standard_backtest_http_routes_expose_options_and_map_validation_to_400 -q
```

Expected failure observed:

```text
urllib.error.HTTPError: HTTP Error 404: Not Found
1 failed in 0.89s
```

### GREEN 3

```text
.                                                                        [100%]
1 passed in 0.70s
```

## Final Verification

Dashboard tests:

```text
../../.venv/bin/python -m pytest tests/test_dashboard.py tests/test_dashboard_web.py -q
........................................................................ [ 77%]
.....................                                                    [100%]
93 passed in 10.30s
```

Task 1-3 compatibility:

```text
../../.venv/bin/python -m pytest tests/test_standard_strategies.py tests/test_backtest_prices.py tests/test_strategy_backtest.py -q
..................................................                       [100%]
50 passed in 0.45s
```

## Self-review and concerns

- Legacy `/api/backtests/run` remains for backend compatibility, but the dashboard JavaScript no longer calls or exposes it.
- The old holding-level backtest presentation, readiness filters, and price-sync status UI were removed during review fixes; Task 5 can add the new global workspace cleanly.
- No live Futu backtest was run because this task changes request/API orchestration and tests use injected providers; execution compatibility is covered by the Task 1-3 focused suite.

## Review fixes

The review identified four gaps. All were reproduced and fixed with focused tests:

1. Symbols now use a shared strict HK/US equity grammar in universe construction, API parsing, and price-path construction. Price targets are resolved below the market price directory and containment is asserted before reads/writes.
2. Blank or unknown asset classes are classified with the repository parser before inclusion, excluding OCC-style and name-labelled options while retaining `BRK.B`, `SPY`, and `00700`.
3. The shipped holding-row legacy backtest button/detail mode/fetch caller, backtest filters, and price-sync status UI were removed. The backend legacy route remains for non-UI compatibility.
4. Owned-provider close failures become Chinese execution errors; a close failure never masks an earlier run failure. HTTP tests verify both paths return 502.

### Review RED evidence

Focused command:

```text
../../.venv/bin/python -m pytest tests/test_dashboard.py::test_dashboard_backtest_universe_rejects_unsafe_and_option_symbols tests/test_backtest_prices.py::test_fetch_backtest_prices_rejects_unsafe_symbols tests/test_dashboard_web.py::test_owned_backtest_provider_close_failure_is_execution_error tests/test_dashboard_web.py::test_owned_backtest_provider_close_failure_does_not_mask_run_failure tests/test_dashboard_web.py::test_dashboard_static_removes_legacy_holding_backtest_ui -q
```

Observed before fixes:

```text
10 failed in 0.56s
```

The API defense-in-depth test separately failed with the prior membership-only error:

```text
../../.venv/bin/python -m pytest tests/test_dashboard_web.py::test_standard_backtest_request_rejects_unsafe_symbol_grammar -q
6 failed in 0.36s
Actual message: 所选标的不在可回测范围内
```

### Review GREEN and final verification

```text
../../.venv/bin/python -m pytest tests/test_dashboard.py tests/test_dashboard_web.py tests/test_standard_strategies.py tests/test_backtest_prices.py tests/test_strategy_backtest.py -q
........................................................................ [ 47%]
........................................................................ [ 94%]
.........                                                                [100%]
153 passed in 11.15s
```

Additional verification:

```text
../../.venv/bin/python -m py_compile src/open_trader/dashboard.py src/open_trader/dashboard_web.py src/open_trader/backtest_prices.py
git diff --check
```

Both commands exited 0 with no output. A source scan also found no remaining holding-row `查看回测`, `data-detail-mode="backtest"`, legacy fetch call, backtest filter element, or price-sync status element.
