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
