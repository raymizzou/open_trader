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
