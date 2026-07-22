# Final Whole-Branch Review Fix Report

## Scope

Fixed all findings from the final review of `f3f8f6e..f8c759f`:

1. Every new synchronous Futu trade context in the controller now requires a
   successful uncached quote-protocol request immediately before construction.
2. Controller close capture borrows the controller quote and lazy shared account
   reader, while standalone capture retains owned-client behavior.
3. Incomplete requested snapshot maps stay abnormal and cannot record monitor
   recovery; a later complete snapshot set records one durable recovery.

No controller, launchd job, screen session, Dashboard process, or OpenD process
was started, stopped, restarted, or otherwise mutated during this fix.

## RED

### Calendar cache bypass

Command:

```text
.venv/bin/python -m pytest tests/test_futu_quote.py::test_trading_day_cache_can_be_bypassed_for_protocol_gate -q
```

Exact result:

```text
F                                                                        [100%]
=================================== FAILURES ===================================
___________ test_trading_day_cache_can_be_bypassed_for_protocol_gate ___________

E           TypeError: FutuQuoteClient.get_trading_days() got an unexpected keyword argument 'use_cache'

tests/test_futu_quote.py:449: TypeError
=========================== short test summary info ============================
FAILED tests/test_futu_quote.py::test_trading_day_cache_can_be_bypassed_for_protocol_gate
1 failed in 0.57s
```

### Central order gate and lazy shared-account gate

Command:

```text
.venv/bin/python -m pytest tests/test_trend_market_controller.py::test_new_order_client_does_not_construct_trade_context_when_gate_fails tests/test_trend_market_controller.py::test_controller_lazy_account_does_not_construct_trade_context_when_gate_fails -q
```

Exact result:

```text
FF                                                                       [100%]
=================================== FAILURES ===================================
____ test_new_order_client_does_not_construct_trade_context_when_gate_fails ____

E           TypeError: _new_order_client() got an unexpected keyword argument 'quote_client'

tests/test_trend_market_controller.py:266: TypeError
_ test_controller_lazy_account_does_not_construct_trade_context_when_gate_fails _

E   Failed: dead gate constructed account context

tests/test_trend_market_controller.py:298: Failed
=========================== short test summary info ============================
FAILED tests/test_trend_market_controller.py::test_new_order_client_does_not_construct_trade_context_when_gate_fails
FAILED tests/test_trend_market_controller.py::test_controller_lazy_account_does_not_construct_trade_context_when_gate_fails
2 failed in 0.48s
```

### Borrowed controller close capture

Command:

```text
.venv/bin/python -m pytest tests/test_trend_market_controller.py::test_controller_close_capture_borrows_readers_without_closing_or_recreating -q
```

Exact result:

```text
F                                                                        [100%]
=================================== FAILURES ===================================
_ test_controller_close_capture_borrows_readers_without_closing_or_recreating __

E       TypeError: _capture_close() got an unexpected keyword argument 'quote_client'

tests/test_trend_market_controller.py:3784: TypeError
=========================== short test summary info ============================
FAILED tests/test_trend_market_controller.py::test_controller_close_capture_borrows_readers_without_closing_or_recreating
1 failed in 0.39s
```

### Incomplete snapshot recovery

Command:

```text
.venv/bin/python -m pytest tests/test_a_share_trend_watch.py::test_once_watcher_waits_for_complete_snapshots_before_recovery -q
```

Exact result:

```text
F                                                                        [100%]
=================================== FAILURES ===================================
________ test_once_watcher_waits_for_complete_snapshots_before_recovery ________

E       AssertionError: assert 'completed' == 'abnormal'
E
E         - abnormal
E         + completed

tests/test_a_share_trend_watch.py:429: AssertionError
=========================== short test summary info ============================
FAILED tests/test_a_share_trend_watch.py::test_once_watcher_waits_for_complete_snapshots_before_recovery
1 failed in 1.18s
```

## Implementation

- Added default-true `use_cache` controls to both trading-calendar entry points.
  `use_cache=False` bypasses cache reads and still refreshes the cache after a
  successful protocol response.
- Added one controller quote-protocol gate using the current market date and
  `use_cache=False`. `_new_order_client` and the lazy shared-account constructor
  call it immediately before their synchronous Futu trade-context factories.
- Passed the controller quote through stop callbacks and locked-report action
  execution. Action order wrappers remain short-lived and unshared.
- Made close capture ownership explicit. Controller calls borrow its shared quote
  and lazy shared account reader; standalone calls own and close clients they
  create.
- Required complete requested snapshot coverage before monitor recovery and made
  incomplete once-passes return `abnormal` without recovery/interruption churn.

## GREEN

### Focused regressions

Command:

```text
.venv/bin/python -m pytest tests/test_futu_quote.py::test_trading_day_cache_can_be_bypassed_for_protocol_gate tests/test_trend_market_controller.py::test_new_order_client_does_not_construct_trade_context_when_gate_fails tests/test_trend_market_controller.py::test_controller_lazy_account_does_not_construct_trade_context_when_gate_fails tests/test_trend_market_controller.py::test_controller_close_capture_borrows_readers_without_closing_or_recreating tests/test_a_share_trend_watch.py::test_once_watcher_waits_for_complete_snapshots_before_recovery -q
```

Exact output:

```text
.....                                                                    [100%]
5 passed in 0.60s
```

### Quote suite

Command:

```text
.venv/bin/python -m pytest tests/test_futu_quote.py -q
```

Exact output:

```text
.......................................                                  [100%]
39 passed in 0.37s
```

### Watcher suites

Command:

```text
.venv/bin/python -m pytest tests/test_a_share_trend_watch.py tests/test_market_trend_watch.py -q
```

Exact output:

```text
........................................................................ [ 94%]
....                                                                     [100%]
76 passed in 0.28s
```

### Controller suite

Command:

```text
.venv/bin/python -m pytest tests/test_trend_market_controller.py -q
```

Exact output:

```text
........................................................................ [ 71%]
.............................                                            [100%]
101 passed in 4.01s
```

### Full affected suites, including order/idempotency coverage

Command:

```text
.venv/bin/python -m pytest tests/test_futu_quote.py tests/test_a_share_trend.py tests/test_a_share_trend_watch.py tests/test_market_trend_watch.py tests/test_kelly_order_execution.py tests/test_trend_market_controller.py tests/test_trend_review.py -q
```

Exact output:

```text
........................................................................ [ 11%]
........................................................................ [ 23%]
........................................................................ [ 35%]
........................................................................ [ 46%]
........................................................................ [ 58%]
........................................................................ [ 70%]
........................................................................ [ 81%]
........................................................................ [ 93%]
.........................................                                [100%]
617 passed in 5.77s
```

### Full repository suite

Command:

```text
.venv/bin/python -m pytest -q
```

Exact output:

```text
........................................................................ [  2%]
........................................................................ [  4%]
........................................................................ [  7%]
........................................................................ [  9%]
........................................................................ [ 12%]
........................................................................ [ 14%]
........................................................................ [ 16%]
........................................................................ [ 19%]
........................................................................ [ 21%]
........................................................................ [ 24%]
........................................................................ [ 26%]
........................................................................ [ 28%]
........................................................................ [ 31%]
........................................................................ [ 33%]
........................................................................ [ 36%]
........................................................................ [ 38%]
........................................................................ [ 41%]
........................................................................ [ 43%]
........................................................................ [ 45%]
........................................................................ [ 48%]
........................................................................ [ 50%]
........................................................................ [ 53%]
........................................................................ [ 55%]
........................................................................ [ 57%]
........................................................................ [ 60%]
........................................................................ [ 62%]
........................................................................ [ 65%]
........................................................................ [ 67%]
........................................................................ [ 69%]
........................................................................ [ 72%]
........................................................................ [ 74%]
........................................................................ [ 77%]
........................................................................ [ 79%]
........................................................................ [ 82%]
........................................................................ [ 84%]
........................................................................ [ 86%]
........................................................................ [ 89%]
........................................................................ [ 91%]
........................................................................ [ 94%]
........................................................................ [ 96%]
........................................................................ [ 98%]
...............................                                          [100%]
2983 passed in 75.88s (0:01:15)
```

## Verification boundary

The requested automated and direct in-process regression workflows passed. Live
controller/OpenD/launchd/Screen checks and `make acceptance` were intentionally
not run because this task required all controllers to remain stopped and forbade
live-process mutation.

## Follow-up Important findings

Fixed the two remaining Important findings from the final review:

1. The standalone/default protection account loader now performs the existing
   uncached quote-protocol gate immediately before every new load-created Futu
   account context. Injected controller account loaders remain unchanged.
2. A close-capture `FutuOrderExecutionError` now detaches and closes the failed
   shared account before retry. Quote and unrelated action failures do not take
   this reset path.

Controllers and all live processes remained stopped and untouched.

### RED

Command:

```text
.venv/bin/python -m pytest tests/test_trend_market_controller.py::test_default_protection_loader_gates_each_new_account_context tests/test_trend_market_controller.py::test_controller_close_capture_rebuilds_failed_shared_account_on_retry -q
```

Exact result:

```text
FF                                                                       [100%]
=================================== FAILURES ===================================
________ test_default_protection_loader_gates_each_new_account_context _________

E       Failed: DID NOT RAISE FutuQuoteError

tests/test_trend_market_controller.py:3710: Failed
____ test_controller_close_capture_rebuilds_failed_shared_account_on_retry _____

E       AssertionError: assert ['shared acco... read failed'] == ['shared acco...failed', None]
E
E         At index 2 diff: 'shared account read failed' != None
E         Use -v to get more diff

tests/test_trend_market_controller.py:4020: AssertionError
=========================== short test summary info ============================
FAILED tests/test_trend_market_controller.py::test_default_protection_loader_gates_each_new_account_context
FAILED tests/test_trend_market_controller.py::test_controller_close_capture_rebuilds_failed_shared_account_on_retry
2 failed in 0.64s
```

### Implementation

- Reused `_gate_futu_trade_context` in only `_run_protection_pass`'s default
  account loader, passing through a borrowed quote or allowing the gate to own
  and close its temporary quote.
- Added `reset_account` beside `reset_quote`, detaching the shared reference
  before suppressing close errors, and reused it in the existing account-loader
  failure path.
- Wrapped only controller close capture so `FutuOrderExecutionError` resets the
  borrowed shared account and re-raises the original error.

### Focused GREEN

Command:

```text
.venv/bin/python -m pytest tests/test_trend_market_controller.py::test_default_protection_loader_gates_each_new_account_context tests/test_trend_market_controller.py::test_controller_close_capture_rebuilds_failed_shared_account_on_retry -q
```

Exact output:

```text
..                                                                       [100%]
2 passed in 0.29s
```

### Controller suite

Command:

```text
.venv/bin/python -m pytest tests/test_trend_market_controller.py -q
```

Exact output:

```text
........................................................................ [ 69%]
...............................                                          [100%]
103 passed in 3.95s
```

### Full affected suites

Command:

```text
.venv/bin/python -m pytest tests/test_futu_quote.py tests/test_a_share_trend.py tests/test_a_share_trend_watch.py tests/test_market_trend_watch.py tests/test_kelly_order_execution.py tests/test_trend_market_controller.py tests/test_trend_review.py -q
```

Exact output:

```text
........................................................................ [ 11%]
........................................................................ [ 23%]
........................................................................ [ 34%]
........................................................................ [ 46%]
........................................................................ [ 58%]
........................................................................ [ 69%]
........................................................................ [ 81%]
........................................................................ [ 93%]
...........................................                              [100%]
619 passed in 5.72s
```

### Full repository suite

Command:

```text
.venv/bin/python -m pytest -q
```

Exact output:

```text
........................................................................ [  2%]
........................................................................ [  4%]
........................................................................ [  7%]
........................................................................ [  9%]
........................................................................ [ 12%]
........................................................................ [ 14%]
........................................................................ [ 16%]
........................................................................ [ 19%]
........................................................................ [ 21%]
........................................................................ [ 24%]
........................................................................ [ 26%]
........................................................................ [ 28%]
........................................................................ [ 31%]
........................................................................ [ 33%]
........................................................................ [ 36%]
........................................................................ [ 38%]
........................................................................ [ 41%]
........................................................................ [ 43%]
........................................................................ [ 45%]
........................................................................ [ 48%]
........................................................................ [ 50%]
........................................................................ [ 53%]
........................................................................ [ 55%]
........................................................................ [ 57%]
........................................................................ [ 60%]
........................................................................ [ 62%]
........................................................................ [ 65%]
........................................................................ [ 67%]
........................................................................ [ 69%]
........................................................................ [ 72%]
........................................................................ [ 74%]
........................................................................ [ 77%]
........................................................................ [ 79%]
........................................................................ [ 82%]
........................................................................ [ 84%]
........................................................................ [ 86%]
........................................................................ [ 89%]
........................................................................ [ 91%]
........................................................................ [ 94%]
........................................................................ [ 96%]
........................................................................ [ 98%]
.................................                                        [100%]
2985 passed in 86.61s (0:01:26)
```

### Follow-up verification boundary

The focused controller workflow, controller suite, affected suites, and full
repository suite passed. Live controller/OpenD/launchd/Screen checks and
`make acceptance` remained intentionally out of scope because this task required
controllers to remain stopped and prohibited live-process mutation.
