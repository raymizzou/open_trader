# Task 4 Report: Reuse clients across controller loops

## Status

Implemented and committed as `1855a4f fix: reuse trend controller Futu connections`.

## TDD evidence

### Process-lifetime reuse

RED:

```text
.venv/bin/python -m pytest tests/test_trend_market_controller.py::test_controller_reuses_quote_and_account_clients_across_loops -q
FAILED tests/test_trend_market_controller.py::test_controller_reuses_quote_and_account_clients_across_loops
E assert 2 == 1
1 failed in 0.35s
```

The old controller constructed one quote client per loop and never reached the borrowed account reader.

GREEN:

```text
.venv/bin/python -m pytest tests/test_trend_market_controller.py::test_controller_reuses_quote_and_account_clients_across_loops -q
1 passed in 1.15s
```

### Failed-reader rebuilds

Quote reset RED before adding the protection exception reset:

```text
.venv/bin/python -m pytest tests/test_trend_market_controller.py::test_controller_rebuilds_shared_quote_after_quote_failure -q
FAILED tests/test_trend_market_controller.py::test_controller_rebuilds_shared_quote_after_quote_failure
E assert 1 == 2
1 failed in 0.34s
```

Account reset RED after extending the same regression and temporarily removing the account exception reset:

```text
.venv/bin/python -m pytest tests/test_trend_market_controller.py::test_controller_rebuilds_shared_clients_after_reader_failures -q
FAILED tests/test_trend_market_controller.py::test_controller_rebuilds_shared_clients_after_reader_failures
E assert 1 == 2
1 failed in 0.41s
```

Final GREEN for reuse plus quote/account rebuilds:

```text
.venv/bin/python -m pytest \
  tests/test_trend_market_controller.py::test_controller_reuses_quote_and_account_clients_across_loops \
  tests/test_trend_market_controller.py::test_controller_rebuilds_shared_clients_after_reader_failures -q
2 passed in 0.39s
```

## Verification

Controller suite during compatibility updates:

```text
.venv/bin/python -m pytest tests/test_trend_market_controller.py -q -x
95 passed in 3.93s
```

Final affected suites:

```text
.venv/bin/python -m pytest \
  tests/test_futu_quote.py \
  tests/test_a_share_trend.py \
  tests/test_a_share_trend_watch.py \
  tests/test_market_trend_watch.py \
  tests/test_trend_market_controller.py \
  -q
476 passed in 5.97s
```

Final full suite:

```text
make test
2975 passed in 92.31s (0:01:32)
```

`git diff --check` passed before commit. A read-only process/screen/launchd inspection found no `trend-market run` controller or matching launchd job. The pre-existing detached Dashboard screen remained untouched. Per task instruction, no live controller workflow was started or restarted.

## Files changed

- `src/open_trader/trend_market_controller.py`
  - Added optional borrowed quote support to calendar derivation and reconciliation.
  - Added optional borrowed quote/account-loader support to protection passes.
  - Added one lazy process-lifetime quote reader and one lazy account reader to the controller.
  - Reset failed quote/account readers and closed both on shutdown.
  - Left order clients on the existing short-lived action paths.
- `tests/test_trend_market_controller.py`
  - Added multi-loop reuse/cleanup coverage.
  - Added failed quote/account rebuild coverage.
  - Updated controller-loop fakes for the new keyword arguments and narrowly stubbed quote construction only where both quote consumers were already mocked.

## Self-review

- Spec: all Task 4 interfaces and lifecycle rules are implemented; `_new_order_client`, `_execute_locked_report`, and `_run_stop` were not changed or given shared readers.
- Ownership: standalone helpers still create and close their own clients; borrowed clients are not closed by helpers.
- Failure semantics: only `FutuQuoteError` resets the shared quote; domain-level abnormal protection results do not. Any account read failure closes and clears the shared account reader.
- Scope/standards: no pool/resource-manager abstraction, dependency, configuration, or unrelated behavior was added. Test stubbing is opt-in rather than autouse, preserving quote-sensitive tests.

## Concerns

None. Live controller behavior was intentionally not exercised because the task required controllers to remain stopped; automated and process-state verification passed.

## High review fix: close-failure lifecycle safety

RED:

```text
.venv/bin/python -m pytest \
  tests/test_trend_market_controller.py::test_controller_rebuilds_quote_when_failed_quote_close_raises \
  tests/test_trend_market_controller.py::test_controller_rebuilds_account_when_failed_account_close_raises \
  tests/test_trend_market_controller.py::test_controller_shutdown_attempts_every_cleanup_after_close_failure \
  -q
FFF                                                                      [100%]
E RuntimeError: quote close failed
E RuntimeError: account close failed
E AssertionError: assert ['account'] == ['account', 'quote', 'pool']
3 failed in 0.52s
```

The reset close errors replaced the quote/account operation failures, retained the failed clients, and the first shutdown close error skipped the remaining cleanup.

GREEN:

```text
.venv/bin/python -m pytest \
  tests/test_trend_market_controller.py::test_controller_rebuilds_quote_when_failed_quote_close_raises \
  tests/test_trend_market_controller.py::test_controller_rebuilds_account_when_failed_account_close_raises \
  tests/test_trend_market_controller.py::test_controller_shutdown_attempts_every_cleanup_after_close_failure \
  -q
...                                                                      [100%]
3 passed in 1.34s
```

Focused lifecycle regressions:

```text
.venv/bin/python -m pytest \
  tests/test_trend_market_controller.py::test_controller_reuses_quote_and_account_clients_across_loops \
  tests/test_trend_market_controller.py::test_controller_rebuilds_shared_clients_after_reader_failures \
  tests/test_trend_market_controller.py::test_controller_rebuilds_quote_when_failed_quote_close_raises \
  tests/test_trend_market_controller.py::test_controller_rebuilds_account_when_failed_account_close_raises \
  tests/test_trend_market_controller.py::test_controller_shutdown_attempts_every_cleanup_after_close_failure \
  -q
.....                                                                    [100%]
5 passed in 0.29s
```

Full controller suite:

```text
.venv/bin/python -m pytest tests/test_trend_market_controller.py -q
........................................................................ [ 73%]
..........................                                               [100%]
98 passed in 3.91s
```

Affected suites:

```text
.venv/bin/python -m pytest \
  tests/test_futu_quote.py \
  tests/test_a_share_trend.py \
  tests/test_a_share_trend_watch.py \
  tests/test_market_trend_watch.py \
  tests/test_trend_market_controller.py \
  -q
........................................................................ [ 15%]
........................................................................ [ 30%]
........................................................................ [ 45%]
........................................................................ [ 60%]
........................................................................ [ 75%]
........................................................................ [ 90%]
...............................................                          [100%]
479 passed in 5.07s
```

Full repository suite:

```text
make test
2978 passed in 91.46s (0:01:31)
```

The controllers remained stopped; no live process or service-manager action was taken for this review fix.
