# Task 13 report: exact allowance, cleanup, and durable canary mode

## Status

Implemented in the current isolated worktree. No live approval, order, transfer, or acceptance run was performed.

## Files changed

- `src/open_trader/prediction_arbitrage_execution.py`
- `src/open_trader/dashboard_web.py`
- `tests/test_prediction_arbitrage_execution.py`
- `tests/test_dashboard_web.py`

## What changed

- Cross execution now keeps the existing execution service/store/incident/lock path and adds exact Predict allowance inside `_run_cross_venue_execution()`.
- The submit sequence is:
  1. current cross refresh and preflight checks
  2. `set_exact_buy_allowance()` with the frozen Predict max debit in USDT base units
  3. post-approval account read proving the exact allowance
  4. post-approval cross refresh using the frozen target quantity
  5. concurrent Predict + Polymarket FOK submits
  6. independent REST reconciliation
  7. allowance clear with post-read zero proof before conclusive close/hold paths
- Post-approval refresh cancellation clears allowance before returning `both_rejected` with `未下单 · 授权已清零`.
- Approval failure posts zero venue submits and returns `未下单`.
- Unknown submission/reconciliation remains fail-closed: it opens the cross breaker and does not clear or release capacity while state is ambiguous.
- Residual Predict allowance at startup with no active execution opens the cross breaker, records one existing incident path, and makes zero mutation calls.
- Added `cleanup_predict_allowance(confirm=True)` as an operator-confirmed local action. It only calls `clear_buy_allowance()` after fresh account/gas/allowance identity checks, proves zero with a post-read, reports `usdt_moved: false`, and rejects unsafe/no-op cases.
- Added `POST /api/prediction-arbitrage/predict-allowance/cleanup` behind the existing local mutation/session/CSRF path. The schema is exactly `{"confirm": true}`; owner/spender/amount from clients are rejected.
- Canary mode uses existing execution evidence, not a new table. A matching compatibility fingerprint graduates from 5 USDT to 20 USDT only after equal two-leg holding evidence plus zero Predict allowance proof under the same Predict SDK/account/gas signer/chain/approval-step identity.

## TDD evidence

Red command from the brief, before implementation:

```text
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest \
  tests/test_prediction_arbitrage_execution.py \
  tests/test_dashboard_web.py \
  -k 'exact_allowance or post_approval or residual_allowance or allowance_cleanup or cross_canary' -q
```

Initial red result:

```text
5 failed, 491 deselected, 1 warning
```

Focused green after implementation and cleanup-boundary additions:

```text
9 passed, 491 deselected, 1 warning in 1.85s
```

Full affected green:

```text
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest \
  tests/test_predict_trading.py \
  tests/test_prediction_arbitrage_store.py \
  tests/test_prediction_arbitrage_execution.py \
  tests/test_dashboard_web.py -q

599 passed, 1 warning in 44.92s
```

Extra syntax/diff checks:

```text
git diff --check
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m py_compile \
  src/open_trader/prediction_arbitrage_execution.py src/open_trader/dashboard_web.py
```

Both exited 0.

## Coverage added

- Exact approval amount is `2_300_000` for the frozen `2.30` USDT Predict max debit.
- Approval failure: no venue submits, no blind retry.
- Post-approval refresh breach: no venue submits, allowance clears, `未下单 · 授权已清零`.
- Cleanup failure: cross breaker opens and exactly one incident is persisted.
- Both rejected: allowance clears and proves zero before reservation release.
- Both fills/holding: allowance clears and proves zero before `holding_to_resolution`.
- Unknown submit/reconcile: allowance is not cleared, breaker stays open, capacity is not released.
- Residual startup allowance: locked residual state, incident, no mutation.
- Operator cleanup rejects `confirm=False`, active execution, insufficient BNB, already-zero allowance, and changed identity after clear.
- Cleanup Dashboard API rejects missing/false/extra owner/spender/amount fields and accepts only `{"confirm": true}`.
- Canary cap remains 5 until verified proof exists; same fingerprint moves to 20; changed signer returns to 5.

## Concerns

- The full affected suite reports one existing `websockets.legacy` deprecation warning from the environment; no test failure.
- No live approval/order/transfer/acceptance was run, by task constraint.

## Fix round 1/5

### Reviewer findings addressed

- Post-approval envelope breach with cleanup receipt/post-read failure now routes through the existing immediate incident path with reason `predict_allowance_cleanup_failed`. It opens the cross breaker, persists exactly one incident, submits zero venue orders, and does not release the cross reservation through the no-submit close path.
- Confirmed approval with unavailable or mismatched exact-allowance post-read is now distinguished from no-mutation approval failure. The possible-mutation path fails closed with `predict_allowance_approval_unverified`, opens the breaker, persists one incident, and submits zero venue orders.
- Durable canary verification no longer self-certifies `fees_verified` or `balances_verified`. `canary_verified=true` is only written when existing reconciliation evidence proves:
  - both venues verified fills and positions,
  - concrete order and trade references,
  - actual fee facts,
  - post-fill balance baseline,
  - zero Predict allowance proof,
  - matching compatibility fingerprint.
- Added negative canary coverage for cancellation, both rejected, one-leg incident, cleanup failure, partial reconciliation, and missing fee proof; all remain on the 5 USDT canary cap.
- Added cleanup route-specific mutation-security tests for Host, Origin, session cookie, CSRF, and loopback-address rejection before body parsing. Schema tests continue to reject client owner/spender/amount and accept only `{"confirm": true}`.

### Round 1 TDD evidence

Focused red command:

```text
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest \
  tests/test_prediction_arbitrage_execution.py \
  tests/test_dashboard_web.py \
  -k 'approval_unverified or unverified_post_read or post_approval_breach_cleanup or cross_canary or allowance_cleanup' -q
```

Red result before fix:

```text
4 failed, 15 passed, 494 deselected, 1 warning
```

Focused green after fix:

```text
20 passed, 493 deselected, 1 warning in 4.86s
```

Full affected green:

```text
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest \
  tests/test_predict_trading.py \
  tests/test_prediction_arbitrage_store.py \
  tests/test_prediction_arbitrage_execution.py \
  tests/test_dashboard_web.py -q

612 passed, 1 warning in 48.43s
```

Extra checks:

```text
git diff --check
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m py_compile \
  src/open_trader/prediction_arbitrage_execution.py src/open_trader/dashboard_web.py
```

Both exited 0.

### Round 1 concerns

- The only warning remains the existing `websockets.legacy` deprecation warning from the test environment.
- No live approval/order/transfer was run.
- `make acceptance` was not run per instruction.

## Fix round 2/5

### Reviewer finding addressed

- `_proof_has_order_refs()` no longer accepts refs from any `matched_refs` entry. Direct proof refs still count, but matched refs must now be under the requested venue key; nested outcome/leg refs are accepted only inside that venue-bound entry. Wrong-venue refs no longer graduate the first cross canary.

### Round 2 TDD evidence

Focused red command:

```text
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest \
  tests/test_prediction_arbitrage_execution.py -k 'cross_canary' -q
```

Red result before fix:

```text
2 failed, 7 passed, 173 deselected, 1 warning in 3.20s
```

Focused green after fix:

```text
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest \
  tests/test_prediction_arbitrage_execution.py -k 'cross_canary' -q

9 passed, 173 deselected, 1 warning in 3.15s
```

Full affected green:

```text
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest \
  tests/test_predict_trading.py \
  tests/test_prediction_arbitrage_store.py \
  tests/test_prediction_arbitrage_execution.py \
  tests/test_dashboard_web.py -q

614 passed, 1 warning in 48.60s
```

Extra checks:

```text
git diff --check
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m py_compile \
  src/open_trader/prediction_arbitrage_execution.py src/open_trader/dashboard_web.py
```

Both exited 0.

### Round 2 concerns

- The only warning remains the existing `websockets.legacy` deprecation warning from the test environment.
- No live approval/order/transfer was run.
- `make acceptance` was not run per instruction.

## Fix round 3/5

### Reviewer finding addressed

- `_proof_has_order_refs()` now requires `proof["venue"]` to exactly match the expected venue before accepting either top-level direct `order_ids`/`trade_ids` or venue-bound `matched_refs`. Missing, malformed, or mismatched venue identity fails closed. Correct direct refs for matching venues still graduate canary, and wrong-venue matched refs remain rejected.

### Round 3 TDD evidence

Focused red command:

```text
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest \
  tests/test_prediction_arbitrage_execution.py -k 'cross_canary' -q
```

Red result before fix:

```text
2 failed, 9 passed, 173 deselected, 1 warning in 1.63s
```

Focused green after fix:

```text
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest \
  tests/test_prediction_arbitrage_execution.py -k 'cross_canary' -q

11 passed, 173 deselected, 1 warning in 1.12s
```

Full affected green:

```text
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest \
  tests/test_predict_trading.py \
  tests/test_prediction_arbitrage_store.py \
  tests/test_prediction_arbitrage_execution.py \
  tests/test_dashboard_web.py -q

616 passed, 1 warning in 49.62s
```

Extra checks:

```text
git diff --check
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m py_compile \
  src/open_trader/prediction_arbitrage_execution.py src/open_trader/dashboard_web.py
```

Both exited 0.

### Round 3 concerns

- The only warning remains the existing `websockets.legacy` deprecation warning from the test environment.
- No live approval/order/transfer was run.
- `make acceptance` was not run per instruction.
