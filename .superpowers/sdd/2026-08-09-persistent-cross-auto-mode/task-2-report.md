# Task 2 report: durable cross-auto authority

## Commit

- Implementation: `f66e372c feat: read cross auto mode from durable state`

## Verification

The worktree has no `.venv/bin/python`; commands used the existing project
interpreter at `/Users/ray/projects/open_trader/.venv/bin/python`.

1. Red (before implementation):

   ```text
   PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_predict_cross_venue.py tests/test_prediction_arbitrage_execution.py -k 'store or pause or configured_mode or cross_auto_status'
   5 failed, 4 passed, 335 deselected, 1 warning in 1.46s
   ```

   The failures were the expected missing durable monitor snapshot field,
   monitor-derived service mode, pause-before-claim behavior, and automatic
   submission after durable arm.

2. Focused green:

   ```text
   ... -k 'store or pause or configured_mode or cross_auto_status'
   9 passed, 335 deselected, 1 warning in 1.14s
   ```

3. Requested cross-venue suites:

   ```text
   PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_predict_cross_venue.py tests/test_prediction_arbitrage_execution.py
   344 passed, 1 warning in 5.50s
   ```

4. Store regression suite:

   ```text
   PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_prediction_arbitrage_store.py
   88 passed in 0.80s
   ```

5. Final combined gate and whitespace check:

   ```text
   git diff --check
   PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_predict_cross_venue.py tests/test_prediction_arbitrage_execution.py tests/test_prediction_arbitrage_store.py
   432 passed, 1 warning in 6.22s
   ```

## Self-review

- The monitor and service read `configured_mode` only from the durable store;
  dashboard construction no longer imports an environment execution mode.
- The automatic path claims inside the preview-consumption SQLite transaction,
  before creating its execution/reservation and alongside pair, daily-principal,
  unsettled-principal, duplicate, and active-execution protections. A paused
  durable state returns `cross_auto_paused` and the existing rejection writer
  persists its operator facts.
- A pause after `confirm()` has made the execution durable is not consulted by
  reconciliation, so it cannot interrupt the submitted pair.

## Concerns

- No Dashboard, CLI, launchd, plist, or live-process check was run: this task
  deliberately does not change those surfaces.
- The final suite emitted the existing third-party `websockets.legacy`
  deprecation warning.
