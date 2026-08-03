# Task 12 report — bind cross confirmations to durable signal episodes without a TTL

Date: 2026-08-03

Commit message:

`feat: bind cross confirmations to signal episodes`

## Scope

Touched only the Task 12-owned files:

- `src/open_trader/predict_cross_venue.py`
- `src/open_trader/prediction_arbitrage_store.py`
- `src/open_trader/prediction_arbitrage_execution.py`
- `tests/test_predict_cross_venue.py`
- `tests/test_prediction_arbitrage_store.py`
- `tests/test_prediction_arbitrage_execution.py`

I did not touch dashboard, acceptance, CHANGELOG, plan, or the SDD ledger.

## TDD red step

Focused red command:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest \
  tests/test_predict_cross_venue.py \
  tests/test_prediction_arbitrage_store.py \
  tests/test_prediction_arbitrage_execution.py \
  -k 'signal_episode or cross_preview or no_ttl or canary_quantity or preview_matches' -q
```

Observed intended failures before implementation:

- cross opportunities and refreshed payloads exposed no `signal_episode_id`
- valid expired cross previews still failed with `preview_expired`
- malformed expired cross previews were rejected as `preview_expired` instead of fail-closed payload validation
- cross previews still exposed `expires_at`
- confirmation from old cross previews returned no execution because TTL was still enforced
- `_cross_preview_matches()` still accepted changes in signal episode, exact quantity, cost envelope, and annualized yield
- cross canary preview still used the larger normal-monitor quantity instead of the execution-only <= 5 USDT smallest quantity

Red result:

- `11 failed, 2 passed, 279 deselected`

## Implementation

### `predict_cross_venue.py`

- Extended cross intent rebuilding with optional execution-only controls:
  - `target_quantity`
  - `max_total_cost`
  - `prefer_smallest`
- Preserved default monitoring behavior; the new knobs are optional and only change behavior when explicitly requested.
- Added exact-quantity enforcement when `target_quantity` is provided.
- Added durable in-memory `opportunity_id -> signal_id` tracking in `PredictCrossVenueMonitor`.
- Attached `signal_episode_id` to live and refreshed cross opportunity payloads.
- Repopulated `signal_episode_id` from open persisted signals when needed.
- Cleared signal-episode ownership when an opportunity closes.

### `prediction_arbitrage_store.py`

- Preserved the legacy `expires_at` column and legacy TTL behavior.
- Added cross-preview payload validation so only valid `cross_venue_yes_no` payloads bypass the legacy preview TTL.
- Required a non-empty `signal_episode_id` plus a cross intent envelope before ignoring the expiry column.
- Kept cross execution reservation behavior unchanged once the payload is valid.

### `prediction_arbitrage_execution.py`

- Cross preview now requests execution-only canary sizing:
  - `max_total_cost=Decimal("5")`
  - `prefer_smallest=True`
- Cross confirmation/final validation now refreshes with `target_quantity=<confirmed quantity>` so quantity cannot drift upward on a deeper book.
- Cross preview payload now freezes and carries `signal_episode_id`.
- Cross preview responses no longer expose `expires_at`; legacy previews still do.
- Tightened `_cross_preview_matches()` to reject changes in:
  - signal episode
  - requested/net quantity
  - per-leg max cost ceiling
  - total max cost ceiling
  - minimum payout
  - minimum profit
  - annualized yield floor
- Kept existing checks for direction, native identity, cutoff, cache key, rules fingerprints, and approved candidates.

## Verification

Focused Task 12 slice after implementation:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest \
  tests/test_predict_cross_venue.py \
  tests/test_prediction_arbitrage_store.py \
  tests/test_prediction_arbitrage_execution.py \
  -k 'signal_episode or cross_preview or no_ttl or canary_quantity or preview_matches' -q
```

Result:

- `13 passed, 279 deselected`

Full affected test command:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest \
  tests/test_predict_cross_venue.py \
  tests/test_prediction_arbitrage_store.py \
  tests/test_prediction_arbitrage_execution.py -q
```

Result:

- `292 passed, 1 warning`

Warning observed:

- upstream `websockets.legacy` deprecation warning from the installed dependency set; unrelated to this task

## Notes / boundaries kept

- No acceptance run
- No dashboard work
- No live approval / order / transfer
- No schema split, second store, queue, or exchange framework
- Legacy preview TTL semantics remain intact for non-cross payloads
- Cross TTL bypass is fail-closed and only applies after valid cross payload decoding

## Fix round 1 — review findings

Review date: August 3, 2026

### Findings addressed

1. Strengthened store-local fail-closed validation so expired cross previews bypass legacy TTL only when they carry the complete frozen cross envelope required by Task 12.
2. Extended signal episode / notification identity to include canonical approved-candidate native IDs so candidate rotation closes the old episode and creates a new signal ID even when pair/direction/fingerprints stay constant.

### TDD red step for review round

Tight red command for the two findings:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest \
  tests/test_prediction_arbitrage_store.py \
  tests/test_predict_cross_venue.py \
  -k 'expired_incomplete_cross_preview or cross_preview_no_ttl_rejects_invalid_cross_payload_before_reserving or candidate_identity_rotation or notifies_only_first_cross_stage_5_per_dedupe_identity' -q
```

Initial red result:

- `18 failed, 1 passed, 131 deselected, 1 warning`

Observed intended failures:

- expired incomplete cross payloads were still treated as no-TTL-valid and did not stay `preview_expired`
- persisted notification identity still omitted approved-candidate native IDs
- changing approved candidate IDs did not rotate the signal episode

Intermediate rerun after the first implementation pass exposed two concrete follow-up issues:

- `_valid_cross_preview_payload()` had the wrong classmethod signature
- persisted notification identity token-ID fields were dropped by SQLite payload sanitization

Intermediate result:

- `19 failed, 131 deselected, 1 warning`

### Review-fix verification

Targeted review-fix slice after both fixes:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest \
  tests/test_prediction_arbitrage_store.py \
  tests/test_predict_cross_venue.py \
  -k 'expired_incomplete_cross_preview or cross_preview_no_ttl_rejects_invalid_cross_payload_before_reserving or candidate_identity_rotation or notifies_only_first_cross_stage_5_per_dedupe_identity' -q
```

Result:

- `19 passed`

Broader Task 12 focused slice after review fixes:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest \
  tests/test_predict_cross_venue.py \
  tests/test_prediction_arbitrage_store.py \
  tests/test_prediction_arbitrage_execution.py \
  -k 'signal_episode or cross_preview or no_ttl or canary_quantity or preview_matches' -q
```

Result:

- `29 passed, 280 deselected, 1 warning`

Full affected suite after review fixes:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest \
  tests/test_predict_cross_venue.py \
  tests/test_prediction_arbitrage_store.py \
  tests/test_prediction_arbitrage_execution.py -q
```

Result:

- `309 passed, 1 warning`

### Review-round implementation details

- `prediction_arbitrage_store.py`
  - strengthened store-local cross preview validation to require the complete frozen envelope before bypassing TTL
  - invalid unexpired cross payloads fail with `cross_preview_invalid`
  - invalid expired cross payloads fall back to legacy `preview_expired`
  - preserved legacy preview TTL behavior for non-cross payloads
- `predict_cross_venue.py`
  - extended persisted notification/episode identity with approved-candidate market/condition/token IDs when present
- `prediction_arbitrage_store.py` sanitization
  - allowed the persisted notification identity to retain the approved-candidate token-ID fields needed for durable episode rotation evidence
- tests
  - added regression coverage for incomplete expired cross payloads across the critical frozen-envelope parts
  - added regression coverage for candidate-ID-driven signal episode rotation
  - updated the existing execution notification fixture to the new persisted identity contract

## Fix round 2 — re-review finding 1

Review date: August 3, 2026

### Remaining finding addressed

1. `_valid_cross_preview_payload()` still accepted expired cross previews that were missing intent-side fields required by `_intent_from_payload()` for `CrossVenueIntent`, allowing the TTL bypass to remain open for incomplete frozen envelopes.

### TDD red step for re-review round

First red command after adding the new intent-envelope regressions:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest \
  tests/test_prediction_arbitrage_store.py \
  -k 'cross_preview_no_ttl_rejects_invalid_cross_payload_before_reserving or expired_incomplete_cross_preview' -q
```

Red result:

- `6 failed, 17 passed, 47 deselected`

Observed intended failures:

- expired cross previews still bypassed TTL when the frozen intent was missing `quantity`
- expired cross previews still bypassed TTL when the frozen intent was missing `calculable_gas`
- expired cross previews still bypassed TTL when the frozen intent was missing `maximum_fee`
- expired cross previews still bypassed TTL when the frozen intent was missing `resolution_at`
- expired cross previews still bypassed TTL when the frozen intent was missing `actionable`
- expired cross previews still bypassed TTL when the frozen intent was missing `quote_available`

### Re-review implementation details

- `prediction_arbitrage_store.py`
  - expanded the store-local cross-preview validator to require the full frozen intent envelope consumed by `_intent_from_payload()`
  - validated intent-side `pair_id`, `direction`, `quantity`, `calculable_gas`, `total_max_cost`, `maximum_fee`, `minimum_payout`, `minimum_profit`, `annualized_yield`, `canonical_cutoff`, `resolution_at`, `actionable`, and `quote_available`
  - validated each cross leg’s native exchange/market/condition/token/outcome plus settlement asset, requested/net quantity, max price/cost, maximum fee, fee asset, book timestamp, settlement-at field presence, and minimum order size
  - rejected malformed and non-finite numeric values before the TTL bypass
  - kept the check store-local and fail-closed, with no import back into execution code
  - required the duplicated frozen envelope values relied on by reservation and execution (`pair_id`, `direction`, `canonical_cutoff`, `total_max_cost`, `minimum_payout`, `minimum_profit`, `annualized_yield`) to agree across the preview payload and intent payload
- tests
  - enriched the valid frozen cross-preview fixture to include the full Task 12 intent envelope and leg payload
  - added explicit regressions for missing intent `quantity`, `calculable_gas`, `maximum_fee`, `resolution_at`, `actionable`, and `quote_available`

### Re-review verification

Focused store-only green rerun after the validator hardening:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest \
  tests/test_prediction_arbitrage_store.py \
  -k 'cross_preview_no_ttl_rejects_invalid_cross_payload_before_reserving or expired_incomplete_cross_preview' -q
```

Result:

- `23 passed, 47 deselected`

Focused review-fix slice:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest \
  tests/test_prediction_arbitrage_store.py \
  tests/test_predict_cross_venue.py \
  -k 'expired_incomplete_cross_preview or cross_preview_no_ttl_rejects_invalid_cross_payload_before_reserving or candidate_identity_rotation or notifies_only_first_cross_stage_5_per_dedupe_identity' -q
```

Result:

- `25 passed, 131 deselected, 1 warning`

Compatibility check for the three execution-path regressions exposed by the stricter store gate:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest \
  tests/test_prediction_arbitrage_execution.py \
  -k 'cross_preview_no_ttl_accepts_same_episode_better_prices_after_elapsed_window or cross_preview_canary_quantity_requests_smallest_and_freezes_exact_quantity' -q
```

Result:

- `3 passed, 156 deselected, 1 warning`

Task 12 focused slice:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest \
  tests/test_predict_cross_venue.py \
  tests/test_prediction_arbitrage_store.py \
  tests/test_prediction_arbitrage_execution.py \
  -k 'signal_episode or cross_preview or no_ttl or canary_quantity or preview_matches' -q
```

Result:

- `35 passed, 280 deselected, 1 warning`

Full affected backend suite:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest \
  tests/test_predict_cross_venue.py \
  tests/test_prediction_arbitrage_store.py \
  tests/test_prediction_arbitrage_execution.py -q
```

Result:

- `315 passed, 1 warning`
