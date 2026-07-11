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

- Legacy `/api/backtests/run` remains intentionally because the current dashboard JavaScript and compatibility tests are proven callers; it is not used by the new standard API.
- The old JavaScript presentation code still contains defensive references to absent per-holding readiness fields. Payload behavior is removed; replacing that UI is outside this API task and should be handled by the standard-backtest result UI task.
- No live Futu backtest was run because this task changes request/API orchestration and tests use injected providers; execution compatibility is covered by the Task 1-3 focused suite.
