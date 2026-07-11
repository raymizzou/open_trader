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

## Review Fixes

### RED

- UI contract command: focused three-test run returned `3 failed in 0.50s` because initial capital, custom-date validation/safe errors, accessibility state, and hidden-result behavior were absent.
- Optional-end API command returned `1 failed in 0.35s` because the server required both custom dates.
- The first real DOM click/submit harness run returned `1 failed, 1 passed in 0.37s`, detecting that the harness had not modeled the result section's initial hidden state; the harness was corrected to match the HTML contract.

### GREEN

Command: `../../.venv/bin/python -m pytest tests/test_dashboard_web.py -q`

Exact result: `65 passed in 11.99s`

The Node DOM harness now exercises the lazy options click, holdings/watchlist switch, range choice, populated submit body, adapter absence, initial capital, hidden results, malformed response fallback, custom required/date validation without fetch, and close/reopen persistence.

### Review commit

Commit subject: `fix: harden standard backtest workspace`

## Safe Error Message Follow-up

### RED

Command: `../../.venv/bin/python -m pytest tests/test_dashboard_web.py::test_standard_backtest_custom_dates_and_safe_error_contract -q`

Exact result: `1 failed in 0.35s`; the mixed message `参数 invalid: Internal Server Error` was incorrectly passed through.

### GREEN

Command: `../../.venv/bin/python -m pytest tests/test_dashboard_web.py -q`

Exact result: `65 passed in 11.86s`. `node --check src/open_trader/dashboard_static/dashboard.js` and `git diff --check` also exited 0.

Commit subject: `fix: filter mixed-language backtest errors`

## Any-Latin Error Filter Follow-up

### RED

Focused safe-error test returned `1 failed in 0.36s`; `参数 X 无效` exposed that a single Latin character still passed through.

### GREEN

`../../.venv/bin/python -m pytest tests/test_dashboard_web.py -q` returned `65 passed in 11.93s`. Node syntax and diff checks exited 0.

Commit subject: `fix: reject Latin characters in backtest errors`
