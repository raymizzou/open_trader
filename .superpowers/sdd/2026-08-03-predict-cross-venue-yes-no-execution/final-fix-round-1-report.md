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
- `sdk_version: 0.0.22`
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

---

# Final whole-branch fix round 2/5

Date: 2026-08-04

Phase: implementation and final verification complete with no blocker; this report is included in the pending round-2 commit.

## Result

Addressed exactly the three open Important findings:

- A. Canary graduation now consumes the real adapter shapes: Predict direct order/trade refs and Polymarket direct `matched_refs`. Each venue must prove its identity, verified order and trade refs, agreeing actual/proof fee, and `filled_quantity == position_quantity == exact intent quantity`. Dust, residual, and remediation holdings cannot graduate because only the exact full two-leg path supplies the expected quantity. Polymarket reconciliation derives `actual_fee` and proof `fee` only from independently matched `TAKER` trade rows with validated size, price, and fee rate. It uses the documented `C * feeRate * p * (1-p)` fee curve (`https://docs.polymarket.com/trading/fees`); missing or malformed fee evidence leaves those fields absent and therefore retains the 5-USDT canary cap. It never substitutes `maximum_fee` for actual fee.
- B. The reversible Predict SDK guard now covers installed `convert_positions(_async)` and `run_approvals(_async)` surfaces in addition to inherited cancel, merge, redeem, split, and set prefixes. Existing make/build/sign/check/balance/get/validate and signed-no-submit reads remain allowed.
- C. Once `builder.set_approval` starts, an exception, ambiguous/malformed/unknown receipt, non-conclusive transaction status, allowance mismatch, or failed post-read returns `possible_mutation=true`. Only pre-call rejection or status-0 failure with independently re-read initial-zero/post-zero allowance is conclusively non-mutating. The execution service opens the breaker/incident, retains the reservation, and submits neither venue for possible mutation.

Installed `predict_sdk.order_builder.OrderBuilder` public mutation surface inspected in the pinned environment:

- `cancel_orders`, `cancel_orders_async`
- `convert_positions`, `convert_positions_async`
- `merge_positions`, `merge_positions_async`
- `redeem_positions`, `redeem_positions_async`
- `run_approvals`, `run_approvals_async`
- `set_approval`, `set_approval_async`, `set_approvals`, `set_approvals_async`
- `set_ctf_exchange_allowance`, `set_ctf_exchange_allowance_async`
- `set_ctf_exchange_approval`, `set_ctf_exchange_approval_async`
- `set_neg_risk_adapter_approval`, `set_neg_risk_adapter_approval_async`
- `split_positions`, `split_positions_async`

## TDD red/green evidence

### Real canary proof and Polymarket fee evidence

```bash
source /Users/ray/projects/open_trader/.venv/bin/activate && pytest -q tests/test_polymarket_trading.py::test_cross_leg_reconciliation_uses_order_trade_and_position_proof tests/test_polymarket_trading.py::test_cross_leg_reconciliation_does_not_invent_actual_fee_without_trade_fee_evidence tests/test_prediction_arbitrage_execution.py::test_cross_canary_cap_stays_five_until_exact_zero_allowance_success_is_verified 'tests/test_prediction_arbitrage_execution.py::test_cross_canary_cap_stays_five_after_non_graduating_outcomes[equal_but_below_expected_quantity-<lambda>]' 'tests/test_prediction_arbitrage_execution.py::test_cross_canary_cap_stays_five_after_non_graduating_outcomes[fee_disagreement-<lambda>]'
```

- Red: 3 failed, 2 passed, 1 warning in 1.75s. Failures exposed absent real Polymarket fee, rejection of direct adapter `matched_refs`, and acceptance of disagreeing fees.
- Green: 5 passed, 1 warning in 0.97s.

```bash
source /Users/ray/projects/open_trader/.venv/bin/activate && pytest -q tests/test_polymarket_trading.py::test_cross_leg_reconciliation_does_not_invent_actual_fee_without_trade_fee_evidence
```

- Red: 1 failed, 1 warning in 1.24s when fee rate/price existed but the matched row did not prove the account was the taker.

```bash
source /Users/ray/projects/open_trader/.venv/bin/activate && pytest -q tests/test_polymarket_trading.py::test_cross_leg_reconciliation_uses_order_trade_and_position_proof tests/test_polymarket_trading.py::test_cross_leg_reconciliation_does_not_invent_actual_fee_without_trade_fee_evidence
```

- Green: 2 passed, 1 warning in 0.93s.

```bash
source /Users/ray/projects/open_trader/.venv/bin/activate && pytest -q tests/test_prediction_arbitrage_execution.py::test_cross_canary_cap_stays_five_until_exact_zero_allowance_success_is_verified tests/test_prediction_arbitrage_execution.py::test_cross_canary_requires_each_adapter_quantity_to_equal_the_exact_intent tests/test_prediction_arbitrage_execution.py::test_cross_venue_uses_one_fresh_bounded_completion_only_below_emergency_limit tests/test_prediction_arbitrage_execution.py::test_cross_remediation_completes_from_fresh_bound_option_within_limit tests/test_prediction_arbitrage_execution.py::test_cross_venue_reconciliation_contains_independent_outcomes
```

- Green boundary sweep: 15 passed, 1 warning in 1.37s. This includes all four per-venue fill/position inequalities plus dust and remediation non-graduation.

### Installed Predict SDK mutation guard

```bash
source /Users/ray/projects/open_trader/.venv/bin/activate && pytest -q 'tests/test_prediction_arbitrage_acceptance.py::test_predict_guard_blocks_client_and_nested_builder_mutations_without_real_call[builder-convert_positions]' 'tests/test_prediction_arbitrage_acceptance.py::test_predict_guard_blocks_client_and_nested_builder_mutations_without_real_call[builder-convert_positions_async]' 'tests/test_prediction_arbitrage_acceptance.py::test_predict_guard_blocks_client_and_nested_builder_mutations_without_real_call[builder-run_approvals]' 'tests/test_prediction_arbitrage_acceptance.py::test_predict_guard_blocks_client_and_nested_builder_mutations_without_real_call[builder-run_approvals_async]'
```

- Red: 4 failed, 1 warning in 0.74s; readiness incorrectly passed and the underlying sentinels ran.
- Green: 4 passed, 1 warning in 0.43s; each case records one blocked mutation attempt, zero real calls, and restores the original nested builder.
- The complete parametrization also covers installed cancel/merge/redeem/split/set variants plus existing nested transfer/redemption and direct client submit attempts.

### Ambiguous exact approval

```bash
source /Users/ray/projects/open_trader/.venv/bin/activate && pytest -q tests/test_predict_trading.py::test_set_exact_buy_allowance_marks_builder_exception_as_possible_mutation tests/test_predict_trading.py::test_set_exact_buy_allowance_only_clears_possible_mutation_for_proven_zero_failed_receipt tests/test_prediction_arbitrage_execution.py::test_cross_ambiguous_exact_approval_holds_reservation_and_opens_incident_without_submit
```

- Red: 8 failed, 1 warning in 1.22s.
- Green: 8 passed, 1 warning in 0.75s.

```bash
source /Users/ray/projects/open_trader/.venv/bin/activate && pytest -q tests/test_predict_trading.py::test_set_exact_buy_allowance_marks_unverifiable_post_read_as_possible_mutation
```

- Red: 1 failed, 1 warning in 0.49s because a non-`RuntimeError` RPC post-read failure escaped the adapter.
- Green: 1 passed, 1 warning in 0.42s; the result is redacted and marked possible mutation after exactly one SDK approval call.

## Complete affected verification

```bash
source /Users/ray/projects/open_trader/.venv/bin/activate && pytest -q tests/test_predict_trading.py tests/test_predict_cross_venue.py tests/test_polymarket_trading.py tests/test_prediction_arbitrage_execution.py tests/test_prediction_arbitrage_acceptance.py
```

- Final rerun: 485 passed, 1 warning in 6.49s.
- `tests/test_dashboard_web.py` was not run in round 2 because no Dashboard projection or public account field changed.

```bash
source /Users/ray/projects/open_trader/.venv/bin/activate && python -m compileall -q src/open_trader/predict_trading.py src/open_trader/polymarket_trading.py src/open_trader/prediction_arbitrage_execution.py src/open_trader/prediction_arbitrage_acceptance.py && git diff --check
```

- Exit code: 0 with no output.

## Direct no-submit readiness

```bash
source /Users/ray/projects/open_trader/.venv/bin/activate && PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" python -m open_trader prediction-arb preflight --no-submit --config config/prediction_arbitrage.json
```

- Exit code: 0
- Result: PASS
- `sdk_version: 0.0.22`
- `signer_match: yes`
- `wallet_match: yes`
- `geoblock: allowed`
- `account_reads: pass`
- `fok_pair_signed_not_submitted: pass`
- `equal_requested_shares: pass`
- `merge_capability: present_not_invoked`
- `relayer_readiness: pass`
- `secret_scan: pass`

## Changed files in round 2

- `src/open_trader/polymarket_trading.py`
- `src/open_trader/predict_trading.py`
- `src/open_trader/prediction_arbitrage_acceptance.py`
- `src/open_trader/prediction_arbitrage_execution.py`
- `tests/test_polymarket_trading.py`
- `tests/test_predict_trading.py`
- `tests/test_prediction_arbitrage_acceptance.py`
- `tests/test_prediction_arbitrage_execution.py`
- `.superpowers/sdd/2026-08-03-predict-cross-venue-yes-no-execution/final-fix-round-1-report.md`

## Final review and concerns

- Standards review: no documented-standard violation or material code smell found. The changes extend existing adapters, guard proxy, execution state machine, and evidence rather than adding a parallel subsystem.
- Specification review: all three round-2 findings are covered; no acceptance, deployment, merge, push, screenshot, live notification, approval, order, transfer, redemption, or other live mutation was performed.
- Missing or malformed Polymarket taker-fee evidence deliberately keeps a successfully reconciled holding at the 5-USDT canary cap; it does not make the holding itself unverified.
- The existing `websockets.legacy` deprecation warning remains and was intentionally not addressed per scope.

# Final Fix Round 3 — Strict Predict Receipt Status

## Root cause and SDK contract

The installed `predict_sdk` 0.0.22 waits for a Web3 transaction receipt and compares `receipt["status"]` directly with integer `1`. Web3 normalizes the RPC receipt status to an integer before the SDK returns it. The adapter therefore accepts only exact Python integers `0` and `1`; booleans, floats, `Decimal` values, strings, out-of-range integers, arbitrary numeric-looking objects, and missing or malformed values remain ambiguous.

## TDD red/green evidence

```bash
source /Users/ray/projects/open_trader/.venv/bin/activate && pytest -q tests/test_predict_trading.py::test_set_exact_buy_allowance_rejects_noncanonical_receipt_status_as_ambiguous tests/test_prediction_arbitrage_execution.py::test_cross_malformed_success_like_receipt_opens_approval_incident_without_submit
```

- Red: 14 failed, 3 passed, 1 warning in 3.16s. Broad `int()` coercion treated floats, `Decimal`, strings, booleans, and numeric-looking objects as conclusive; the production-shaped service case advanced to `holding_to_resolution` instead of opening an approval incident.
- Green: 17 passed, 1 warning in 2.85s. Every noncanonical status is `receipt_ambiguous` with `possible_mutation=True`; the service holds the 4.80-USDT reservation, opens the breaker/incident, and submits zero venue legs.

```bash
source /Users/ray/projects/open_trader/.venv/bin/activate && pytest -q tests/test_predict_trading.py::test_set_exact_buy_allowance_uses_sdk_set_approval_and_proves_exact_post_read tests/test_predict_trading.py::test_set_exact_buy_allowance_only_clears_possible_mutation_for_proven_zero_failed_receipt
```

- Canonical boundary green: 7 passed, 1 warning in 0.54s. Exact integer `1` still requires the exact allowance post-read before confirmation; exact integer `0` remains conclusive only under the existing proven zero/unchanged allowance rules.

## Focused and complete affected verification

```bash
source /Users/ray/projects/open_trader/.venv/bin/activate && pytest -q tests/test_predict_trading.py tests/test_prediction_arbitrage_execution.py
```

- Focused result: 258 passed, 1 warning in 4.29s.

```bash
source /Users/ray/projects/open_trader/.venv/bin/activate && pytest -q tests/test_predict_trading.py tests/test_predict_cross_venue.py tests/test_polymarket_trading.py tests/test_prediction_arbitrage_execution.py tests/test_prediction_arbitrage_acceptance.py
```

- Complete owned affected result: 502 passed, 1 warning in 5.90s.
- `tests/test_dashboard_web.py` was not run because round 3 changes no public field or Dashboard projection.

```bash
source /Users/ray/projects/open_trader/.venv/bin/activate && python -m compileall -q src/open_trader/predict_trading.py && git diff --check
```

- Exit code: 0 with no output.

## Direct no-submit readiness

```bash
source /Users/ray/projects/open_trader/.venv/bin/activate && PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" python -m open_trader prediction-arb preflight --no-submit --config config/prediction_arbitrage.json
```

- Exit code: 0
- Result: PASS
- `sdk_version: 0.0.22`
- `signer_match: yes`
- `wallet_match: yes`
- `geoblock: allowed`
- `account_reads: pass`
- `fok_pair_signed_not_submitted: pass`
- `equal_requested_shares: pass`
- `merge_capability: present_not_invoked`
- `relayer_readiness: pass`
- `secret_scan: pass`

## Changed files in round 3

- `src/open_trader/predict_trading.py`
- `tests/test_predict_trading.py`
- `tests/test_prediction_arbitrage_execution.py`
- `.superpowers/sdd/2026-08-03-predict-cross-venue-yes-no-execution/final-fix-round-1-report.md`

## Final review and concerns

- Current phase: round 3 implementation and verification complete; no blocker.
- The production change is limited to replacing broad numeric coercion with an exact type-and-value check.
- No acceptance, deployment, merge, push, screenshot, approval, order, transfer, redemption, or other live mutation was performed.
- The existing `websockets.legacy` deprecation warning remains and is outside this round's scope.
