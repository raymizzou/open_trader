# Task 1 report: Normalize Predict category timing and complete market rules

## Implementation

- Added the official Predict category join using `categorySlug` and cached valid category responses by slug.
- Normalized `startsAt`, `endsAt`, and `resolutionProvider` as `event_start_at`, `event_end_at`, and `resolution_provider`.
- Removed market-level timing/source requirements from normalization; no settlement timestamp or resolution source is synthesized.
- Accepted the official `DEFAULT` market variant spelling while preserving the existing market filters.
- Expanded `rules_fingerprint` to include question, rules, category slug/timing, resolution provider, outcome token identities, and explicit Polymarket candidate IDs.
- Kept optional deprecated `resolution_source`, `close_at`, and `settlement_at` constructor fields only for current direct callers. API-normalized markets leave them empty/`None`.
- Preserved the existing API-key and User-Agent request boundary. No live orders or credentials were used.

## Files

- `src/open_trader/predict_source.py`
- `tests/test_predict_source.py`
- This report file

## TDD evidence

RED command:

```text
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest tests/test_predict_source.py -k 'category or market_normal' -q
```

RED output:

```text
FF.                                                                      [100%]
2 failed, 1 passed, 12 deselected in 0.37s
```

The failures were the expected missing category-join behavior: `get_market` returned `None` and sibling markets were not normalized.

GREEN command:

```text
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest tests/test_predict_source.py tests/test_predict_cross_venue.py -q
```

GREEN output:

```text
......................................................                   [100%]
54 passed in 0.72s
```

## Self-review

- `git diff --check` passed.
- Only the requested source/test files contain implementation changes; the report is the requested artifact.
- Category requests are cached and sibling markets reuse one request.
- Missing or invalid category timing fails closed.
- Empty `polymarketConditionIds` stays empty and no catalog scan was added.
- API key and User-Agent handling is unchanged.
- No live process, order, credential, or unrelated refactor was touched.

## Concerns

- `predict_cross_venue.py` still consumes the legacy timing/source fields by design; Task 2 must migrate it to canonical category timing and raw metadata before normalized Predict markets can feed that path.
- Superseded by the round 1 strict-migration fix below; no deprecated `PredictMarket` constructor shim remains.

## Round 1 fix report

### Changed files

- `src/open_trader/predict_source.py`
- `src/open_trader/predict_cross_venue.py`
- `src/open_trader/schemas/cross_exchange_yes_no_equivalence.json`
- `tests/test_predict_source.py`
- `tests/test_predict_cross_venue.py`

### Fixes

- Removed `resolution_source`, `close_at`, and `settlement_at` from `PredictMarket`; no compatibility fields remain.
- Rejected category windows where `event_end_at <= event_start_at`.
- Added focused coverage for equal/reversed windows, official `DEFAULT`, and each fingerprint input.
- Migrated the Predict cross-venue adapter and current Predict Codex payload/schema to category timing and `resolution_provider` without creating a Predict settlement timestamp.
- Predict legs now carry no fabricated settlement timestamp; Polymarket timing remains unchanged.

### RED/GREEN evidence

RED command after strict-migration tests were added:

```text
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest tests/test_predict_source.py tests/test_predict_cross_venue.py -q
```

RED output:

```text
18 failed, 39 passed in 4.85s
```

Focused intermediate outputs:

```text
tests/test_predict_source.py: 18 passed in 0.32s
tests/test_predict_cross_venue.py: 39 passed in 0.61s
```

Final GREEN command:

```text
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest tests/test_predict_source.py tests/test_predict_cross_venue.py -q
```

Final GREEN output:

```text
.........................................................                [100%]
57 passed in 0.73s
```

### Self-review

- `git diff --check` passed.
- `PredictMarket` has no public legacy timing/source fields; the remaining legacy field references are Polymarket-only or the separate cross-venue internal model.
- Normalized Predict data reaches cross-venue code through `category_slug`, `event_start_at`, `event_end_at`, and `resolution_provider`.
- No Predict settlement timestamp is synthesized; Predict legs expose `settlement_at=None`.
- Existing API-key/User-Agent boundaries, source filters, and no-order/no-credential constraints remain unchanged.

### Concerns

- Task 2 still needs to replace the current mixed v1 Codex admission contract with schema v2 canonical-cutoff/direct-polarity validation and remove the remaining Polymarket-oriented raw `close_at`/`settlement_at` fields from `VenueMarket`.

## Round 2 fix report

### Change

- `_valid_market_pair()` now requires Predict `category_slug`, `resolution_provider`, timezone-aware `event_start_at`, timezone-aware `event_end_at`, and a strictly increasing category window before any payload construction.
- Added a focused regression covering each incomplete canonical Predict metadata field.
- No fabricated settlement timestamp or deferred typed-`None` cleanup was added.

### TDD evidence

RED command:

```text
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest tests/test_predict_cross_venue.py -k 'incomplete_predict_canonical_metadata' -q
```

RED output:

```text
F                                                                        [100%]
1 failed, 39 deselected in 0.40s
```

GREEN focused and affected-suite commands:

```text
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest tests/test_predict_cross_venue.py -k 'incomplete_predict_canonical_metadata' -q
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest tests/test_predict_source.py tests/test_predict_cross_venue.py -q
```

GREEN output:

```text
1 passed, 39 deselected in 0.33s
58 passed in 0.59s
```

### Self-review

- The guard rejects missing category slug/provider, missing start/end, and non-increasing windows before `_equivalence_market_payload()` can call `isoformat()`.
- `git diff --check` is required before commit; no unrelated files or runtime paths are touched.
