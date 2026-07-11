# Task 5 Report

## RED

Command:

`../../.venv/bin/python -m pytest tests/test_dashboard_web.py::test_dashboard_has_one_global_backtest_entry_and_no_row_entry tests/test_dashboard_web.py::test_standard_backtest_workspace_builds_request_without_adapter -q`

Exact result: `2 failed in 0.36s`

- The static contract failed because `#open-standard-backtest` did not exist.
- The Node harness failed because `state.standardBacktest` did not exist.

## GREEN

Command:

`../../.venv/bin/python -m pytest tests/test_dashboard_web.py -q`

Exact result: `61 passed in 11.93s`

Additional checks:

- `node --check src/open_trader/dashboard_static/dashboard.js` exited 0.
- `git diff --check` exited 0.

## Commit

Commit subject: `feat: add global standard backtest workspace`

## Concerns

- The task scope uses the existing static/Node harness and API tests; no live dashboard service was restarted because this worktree task did not request deployment.
- Backtrader remains intentionally absent from visible controls and from the posted request body.
