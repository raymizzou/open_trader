# Final whole-branch fix round 1/5 report

Date: 2026-08-04

## Status

- Current phase: implementation and requested verification complete; ready for the requested local commit.
- Blocker: none.
- Baseline: branch `fix/keychain-secret-write` at `3cf94488`, with local `main` `ff2b2f05` already integrated.
- Scope remained limited to the four owned production modules, their directly corresponding tests, and this report.
- Did not run `make acceptance`, deploy, merge, push, capture screenshots, send notifications, or perform any live mutation.

## Implemented safety fixes

1. Predict USDT balance and allowance now publish human decimal strings alongside exact raw integer strings. Exact approval verification uses the raw post-read; economics and projections use human USDT.
2. Both refreshed Predict and Polymarket no-submit preflights run after exact approval and the dual REST refresh, before either concurrent submit. Either failure clears and verifies zero allowance with no venue submit or retry.
3. Predict order construction can reject before the order POST. Every failure after the order POST attempt begins, including invalid JSON, malformed payloads, and missing order IDs, is ambiguous.
4. Confirmed operator cleanup accepts `allowance_breaker` as the reason to clear while retaining active-execution, gas, identity, receipt, and zero-post-read gates.
5. Verified Predict reconciliation publishes adapter-shaped fee and execution proof. Canary graduation consumes matching Predict and Polymarket proofs and requires verified zero allowance.
6. Cross-venue economics use the named deterministic `0.10` USDT gas reserve in total cost, cap, profit, and annualization. Unknown or zero gas is non-actionable.
7. Acceptance fails on any nonzero human allowance, nonzero raw allowance, or true allowance breaker for both market-present and complete-empty paths.
8. Acceptance installs a reversible Predict client and nested-builder mutation guard while preserving reads and signed-no-submit construction.

## TDD red/green evidence

### Adapter units and approval breaker

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_predict_trading.py::test_real_adapter_reports_human_usdt_and_raw_post_approval_units
```

- Red: 1 failed, 1 warning (`available_usdt` was raw `5000000`).
- Green: 1 passed, 1 warning.

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_predict_trading.py::test_approval_facts_report_predict_account_owner_allowance_and_gas tests/test_predict_trading.py::test_real_adapter_reports_human_usdt_and_raw_post_approval_units
```

- Red: 2 failed, 1 warning in 0.60s (real `approval_facts` omitted `allowance_breaker`).
- Green: 2 passed, 1 warning in 0.54s.

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_predict_trading.py::test_real_adapter_reports_human_usdt_and_raw_post_approval_units tests/test_predict_trading.py::test_set_exact_buy_allowance_uses_sdk_set_approval_and_proves_exact_post_read tests/test_predict_trading.py::test_clear_buy_allowance_uses_sdk_revoke_and_proves_zero_post_read
```

- Red: 3 failed, 1 warning in 0.63s (set/clear results omitted the refreshed breaker state).
- Green: 3 passed, 1 warning in 0.62s.

### Predict POST ambiguity and verified reconciliation proof

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_predict_trading.py::test_predict_post_response_failures_are_ambiguous_after_single_attempt
```

- Red: 3 failed, 1 warning.

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_predict_trading.py::test_predict_post_response_failures_are_ambiguous_after_single_attempt tests/test_predict_trading.py::test_submit_posts_once_and_transport_error_is_ambiguous tests/test_predict_trading.py::test_cross_entry_posts_only_the_preflight_bound_order tests/test_predict_trading.py::test_cross_remediation_option_and_submit_bind_a_fresh_predict_buy_quote
```

- Green: 6 passed, 1 warning.

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_predict_trading.py::test_reconcile_verifies_only_full_order_match_activity_and_position_agreement
```

- Red: 1 failed, 1 warning (verified result lacked fee/proof fields).
- Green: 1 passed, 1 warning.

### Refreshed dual preflights and ambiguous empty order ID

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_prediction_arbitrage_execution.py::test_cross_exact_allowance_wraps_current_dual_rest_refresh_before_submit tests/test_prediction_arbitrage_execution.py::test_cross_refreshed_preflight_failure_clears_without_submit_or_retry
```

- Red: 3 failed, 1 warning.
- Green: 3 passed, 1 warning.

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_prediction_arbitrage_execution.py::test_cross_predict_accepted_without_order_id_is_unknown_without_cleanup_or_remediation
```

- Red: 1 failed, 1 warning (`remediation_no_safe_option` instead of reconciliation unknown).
- Green: 1 passed, 1 warning.

### Exact raw approval and operator cleanup

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_prediction_arbitrage_execution.py::test_cross_exact_approval_rejects_mismatched_raw_post_read_without_submit tests/test_prediction_arbitrage_execution.py::test_predict_allowance_cleanup_clears_raw_residual_with_zero_human_projection
```

- Red: 2 failed, 1 warning.
- Green: 2 passed, 1 warning in 1.07s.

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_prediction_arbitrage_execution.py::test_predict_allowance_cleanup_uses_residual_breaker_as_reason_to_clear tests/test_prediction_arbitrage_execution.py::test_predict_allowance_cleanup_rejects_insufficient_bnb_without_mutation
```

- Red: 1 failed, 1 passed, 1 warning.
- Green: 2 passed, 1 warning.

### Gas-inclusive economics

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_predict_trading.py::test_cross_entry_posts_only_the_preflight_bound_order tests/test_predict_trading.py::test_cross_entry_rejects_unknown_zero_gas_without_post
```

- Red: 2 failed, 1 warning in 0.62s.
- Green: 2 passed, 1 warning in 0.75s.

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_predict_cross_venue.py::test_cross_venue_gas_reserve_pushes_total_over_twenty_cap
```

- Red: 1 failed, 1 warning.

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_predict_cross_venue.py::test_cross_venue_gas_reserve_pushes_total_over_twenty_cap tests/test_predict_cross_venue.py::test_cross_venue_gas_reserve_rejects_exact_zero_profit tests/test_predict_cross_venue.py::test_cross_venue_intent_uses_shared_scalar_annualization_with_fee_inclusive_capital
```

- Green: 3 passed, 1 warning.

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_prediction_arbitrage_execution.py::test_cross_venue_preview_requires_named_gas_inside_cost_and_profit
```

- Red: 1 failed, 1 warning.
- Green: 1 passed, 1 warning.

### Acceptance residual allowance and Predict SDK guard

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_prediction_arbitrage_acceptance.py -k 'residual_allowance_signal or predict_guard'
```

- Red: 10 failed, 1 passed, 53 deselected, 1 warning in 0.90s.
- Green: 11 passed, 53 deselected, 1 warning in 0.61s.
- Covered nested `builder.set_approval`, `builder.transfer`, `builder.redemption`, and direct client `submit_order`; every guarded case recorded one attempt and zero real calls.

## Final green verification

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest -q tests/test_predict_trading.py tests/test_predict_cross_venue.py tests/test_prediction_arbitrage_execution.py tests/test_prediction_arbitrage_acceptance.py
```

- 383 passed, 1 warning in 5.45s.

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest -q tests/test_dashboard_web.py
```

- 349 passed, 1 warning in 45.35s.

```bash
PYTHONPATH=src .venv/bin/python -m compileall -q src/open_trader/predict_trading.py src/open_trader/predict_cross_venue.py src/open_trader/prediction_arbitrage_execution.py src/open_trader/prediction_arbitrage_acceptance.py
git diff --check
```

- Both exited 0 with no output.

## Direct no-submit readiness

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m open_trader prediction-arb preflight --no-submit --config config/prediction_arbitrage.json
```

- Exit code: 0
- Result: PASS
- `sdk_version: 0.2.0`
- `signer_match: yes`
- `wallet_match: yes`
- `geoblock: allowed`
- `account_reads: pass`
- `fok_pair_signed_not_submitted: pass`
- `equal_requested_shares: pass`
- `merge_capability: present_not_invoked`
- `relayer_readiness: pass`
- `secret_scan: pass`

## Changed files

- `src/open_trader/predict_trading.py`
- `src/open_trader/prediction_arbitrage_execution.py`
- `src/open_trader/predict_cross_venue.py`
- `src/open_trader/prediction_arbitrage_acceptance.py`
- `tests/test_predict_trading.py`
- `tests/test_prediction_arbitrage_execution.py`
- `tests/test_predict_cross_venue.py`
- `tests/test_prediction_arbitrage_acceptance.py`
- `.superpowers/sdd/2026-08-03-predict-cross-venue-yes-no-execution/final-fix-round-1-report.md`

## Concerns and exclusions

- The deterministic gas policy is intentionally a named `0.10` USDT reserve; no FX feed, collaborator, or configuration subsystem was added.
- The existing `websockets.legacy` dependency deprecation warning appeared throughout and was intentionally not addressed per scope.
- No Dashboard source or test was changed; its complete projection suite was run because the public Predict balance/allowance fields changed semantics.
- No acceptance, deployment, merge, push, screenshot, notification, order submission, approval, transfer, redemption, or other live mutation was performed.
