# Task 14 report — truthful Predict execution readiness

## Changed files

- `src/open_trader/predict_source.py`
- `src/open_trader/predict_cross_venue.py`
- `src/open_trader/prediction_arbitrage_execution.py`
- `src/open_trader/dashboard_web.py`
- `tests/test_predict_source.py`
- `tests/test_predict_cross_venue.py`
- `tests/test_prediction_arbitrage_execution.py`
- `tests/test_dashboard_web.py`

No dashboard cache, queue, polling job, abstraction, dependency, live order, allowance mutation, cleanup, transfer, redemption, or real notification was added or run. `make acceptance` was not run.

## Red command and output

Command:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest tests/test_predict_source.py tests/test_predict_cross_venue.py tests/test_prediction_arbitrage_execution.py tests/test_dashboard_web.py -k 'stale_funnel or complete_empty_scan or predict_account_projection or official_market_url or gas_notification or allowance_incident' -q
```

Output:

```text
FFFFFFF                                                                  [100%]
=================================== FAILURES ===================================
______ test_stale_funnel_retains_last_success_stages_and_disables_actions ______
E       AssertionError: assert {'matched_pai...airs': 0, ...} == {'matched_pai...airs': 1, ...}
E         Differing items:
E         {'arbitrage_space_pairs': 0} != {'arbitrage_space_pairs': 1}

__________ test_complete_empty_scan_no_v1_market_is_ready_not_blocked __________
E       KeyError: 'empty_state'

____________ test_official_market_url_uses_only_validated_api_slugs ____________
E       TypeError: PredictMarket.__init__() got an unexpected keyword argument 'market_slug'

____________ test_official_market_url_omits_missing_or_unsafe_slugs ____________
E       TypeError: PredictMarket.__init__() got an unexpected keyword argument 'market_slug'

________ test_gas_notification_only_for_blocked_stage_5_signal_episode _________
E       AssertionError: assert {'state': 'fa...insufficient'} == {'state': 'se...fficient_bnb'}
E         Differing items:
E         {'reason': 'account_insufficient'} != {'reason': 'insufficient_bnb'}
E         {'state': 'failed'} != {'state': 'sent'}

_____________ test_allowance_incident_notifies_once_per_generation _____________
E       assert 2 == 1
E        +  where 2 = <test_prediction_arbitrage_execution.ChannelNotifier object at ...>.calls

____ test_predict_account_projection_labels_account_gas_and_masks_addresses ____
E       KeyError: 'account'

=========================== short test summary info ============================
FAILED tests/test_predict_cross_venue.py::test_stale_funnel_retains_last_success_stages_and_disables_actions
FAILED tests/test_predict_cross_venue.py::test_complete_empty_scan_no_v1_market_is_ready_not_blocked
FAILED tests/test_predict_cross_venue.py::test_official_market_url_uses_only_validated_api_slugs
FAILED tests/test_predict_cross_venue.py::test_official_market_url_omits_missing_or_unsafe_slugs
FAILED tests/test_prediction_arbitrage_execution.py::test_gas_notification_only_for_blocked_stage_5_signal_episode
FAILED tests/test_prediction_arbitrage_execution.py::test_allowance_incident_notifies_once_per_generation
FAILED tests/test_dashboard_web.py::test_predict_account_projection_labels_account_gas_and_masks_addresses
7 failed, 621 deselected, 1 warning in 2.30s
```

## Green commands and outputs

Focused command:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest tests/test_predict_source.py tests/test_predict_cross_venue.py tests/test_prediction_arbitrage_execution.py tests/test_dashboard_web.py -k 'stale_funnel or complete_empty_scan or predict_account_projection or official_market_url or gas_notification or allowance_incident' -q
```

Output:

```text
.......                                                                  [100%]
=============================== warnings summary ===============================
../../.venv/lib/python3.12/site-packages/websockets/legacy/__init__.py:6
  /Users/ray/projects/open_trader/.venv/lib/python3.12/site-packages/websockets/legacy/__init__.py:6: DeprecationWarning: websockets.legacy is deprecated; see https://websockets.readthedocs.io/en/stable/howto/upgrade.html for upgrade instructions
    warnings.warn(  # deprecated in 14.0 - 2024-11-09

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
7 passed, 621 deselected, 1 warning in 1.49s
```

Complete affected command:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest tests/test_predict_source.py tests/test_predict_cross_venue.py tests/test_prediction_arbitrage_execution.py tests/test_dashboard_web.py -q
```

Output:

```text
........................................................................ [ 11%]
........................................................................ [ 22%]
........................................................................ [ 34%]
........................................................................ [ 45%]
........................................................................ [ 57%]
........................................................................ [ 68%]
........................................................................ [ 80%]
........................................................................ [ 91%]
....................................................                     [100%]
=============================== warnings summary ===============================
../../.venv/lib/python3.12/site-packages/websockets/legacy/__init__.py:6
  /Users/ray/projects/open_trader/.venv/lib/python3.12/site-packages/websockets/legacy/__init__.py:6: DeprecationWarning: websockets.legacy is deprecated; see https://websockets.readthedocs.io/en/stable/howto/upgrade.html for upgrade instructions
    warnings.warn(  # deprecated in 14.0 - 2024-11-09

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
628 passed, 1 warning in 49.88s
```

Whitespace check:

```bash
git diff --check
```

Output: no output, exit 0.

## Design choices

- Stale/degraded Predict source state keeps one in-memory last-success funnel/timestamp on `PredictCrossVenueMonitor`; it does not add a dashboard cache. Stage 5 remains live-only and is forced to zero when stale.
- A successful scan with no open Predict V1 markets reports `empty_state=complete_scan_no_v1_market` with all funnel counts at zero and no blocker.
- Predict market slugs and Polymarket event slugs flow from API payloads into server-owned candidate evidence. URLs are emitted only for conservative slug values:
  - Predict: `https://predict.fun/market/<slug>`
  - Polymarket: `https://polymarket.com/event/<slug>`
- Candidate evidence keeps full native IDs; dashboard venue projection masks Predict Account and Privy gas signer addresses separately.
- Predict Account facts and gas signer facts are projected separately: allowance/USDT/reservation/unsettled/canary stay under account/reservation/canary; BNB/required/top-up stays under gas.
- Zero allowance remains healthy. Insufficient BNB is read-only and produces an operational signal notification only for a blocked stage-5 signal episode. Residual allowance is a breaker/read-only state.
- Gas-blocked stage-5 notification reuses the existing signal notification lease. Residual allowance and cleanup failure reuse incident persistence; duplicate notification for the same open incident generation is suppressed.

## Self-review

- Standards: diff stays inside the Task 14 files plus this report. No new dependencies, queue, polling loop, live mutation, or acceptance run. `git diff --check` is clean.
- Spec: all binding constraints are represented in focused tests except live acceptance doubles beyond the existing notifier doubles; no real Feishu delivery is asserted.
- Safety check: added an identity guard before sending gas-blocked notifications so a stale signal cannot receive a gas alert for changed opportunity evidence.
- Ponytail check: used existing monitor snapshot state, signal notification leases, incident persistence, address masking helpers, and notifier doubles. No new framework or abstraction.

## Concerns

- The monitor stores last-success funnel/timestamps in memory only. That matches the brief’s “reuse existing monitor snapshots” and avoids a dashboard cache, but process restart naturally loses the stale remembered funnel.
- The gas-blocked notification message is intentionally short and operational; it is not a full dashboard card.
- The only warning left is the existing `websockets.legacy` deprecation warning from the environment.

## Fix round 1 evidence

### Findings addressed

1. `snapshot()` / `_snapshot_funnel()` is now read-only. Last-success funnel capture happens via `_record_successful_funnel()` at successful discovery/book-confirmation transitions.
2. Polymarket official URLs now use only Gamma `eventSlug` / `event_slug`. Generic `slug` no longer produces a Polymarket event URL.

### Red command and output

Command:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest tests/test_predict_source.py tests/test_predict_cross_venue.py tests/test_prediction_arbitrage_execution.py tests/test_dashboard_web.py -k 'stale_funnel or complete_empty_scan or predict_account_projection or official_market_url or gas_notification or allowance_incident' -q
```

Output:

```text
F...F...                                                                 [100%]
=================================== FAILURES ===================================
______ test_stale_funnel_retains_last_success_stages_and_disables_actions ______
E       AssertionError: assert '2026-01-01T00:00:03+00:00' == '2026-01-01T00:00:00+00:00'
E         - 2026-01-01T00:00:00+00:00
E         ?                   ^
E         + 2026-01-01T00:00:03+00:00
E         ?                   ^

___________ test_official_market_url_ignores_generic_polymarket_slug ___________
E       AssertionError: assert 'market_url' not in {'market_id': 'poly-market-1', 'condition_id': 'poly-condition', 'yes_token_id': 'poly-yes-1', 'no_token_id': 'poly-no-1', ...}

=========================== short test summary info ============================
FAILED tests/test_predict_cross_venue.py::test_stale_funnel_retains_last_success_stages_and_disables_actions
FAILED tests/test_predict_cross_venue.py::test_official_market_url_ignores_generic_polymarket_slug
2 failed, 6 passed, 621 deselected, 1 warning in 1.25s
```

### Green commands and outputs

Focused command:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest tests/test_predict_source.py tests/test_predict_cross_venue.py tests/test_prediction_arbitrage_execution.py tests/test_dashboard_web.py -k 'stale_funnel or complete_empty_scan or predict_account_projection or official_market_url or gas_notification or allowance_incident' -q
```

Output:

```text
........                                                                 [100%]
=============================== warnings summary ===============================
../../.venv/lib/python3.12/site-packages/websockets/legacy/__init__.py:6
  /Users/ray/projects/open_trader/.venv/lib/python3.12/site-packages/websockets/legacy/__init__.py:6: DeprecationWarning: websockets.legacy is deprecated; see https://websockets.readthedocs.io/en/stable/howto/upgrade.html for upgrade instructions
    warnings.warn(  # deprecated in 14.0 - 2024-11-09

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
8 passed, 621 deselected, 1 warning in 1.09s
```

Complete affected command:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest tests/test_predict_source.py tests/test_predict_cross_venue.py tests/test_prediction_arbitrage_execution.py tests/test_dashboard_web.py -q
```

Output:

```text
........................................................................ [ 11%]
........................................................................ [ 22%]
........................................................................ [ 34%]
........................................................................ [ 45%]
........................................................................ [ 57%]
........................................................................ [ 68%]
....................................................................     [ 80%]
........................................................................ [ 91%]
.....................................................                    [100%]
=============================== warnings summary ===============================
../../.venv/lib/python3.12/site-packages/websockets/legacy/__init__.py:6
  /Users/ray/projects/open_trader/.venv/lib/python3.12/site-packages/websockets/legacy/__init__.py:6: DeprecationWarning: websockets.legacy is deprecated; see https://websockets.readthedocs.io/en/stable/howto/upgrade.html for upgrade instructions
    warnings.warn(  # deprecated in 14.0 - 2024-11-09

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
629 passed, 1 warning in 50.00s
```

Whitespace check:

```bash
git diff --check
```

Output: no output, exit 0.

### Self-review

- `snapshot()` and `_snapshot_funnel()` now only assemble values; they do not write last-success or stale fields.
- Stale REST/WS still forces stage 5 to zero while retaining the exact prior stage 1-4 counts and timestamp.
- Polymarket `slug` alone is intentionally ignored for official URLs; only `eventSlug` / `event_slug` may produce `https://polymarket.com/event/<slug>`.
- Zero-market successful scan and the prior Task 14 focused tests remain green.
