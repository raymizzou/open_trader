# Task 9 review-fix report

## Fixes

- Dashboard acceptance now reads `account_sync/controller_status.json` from
  `_project_data_dir(root)`, matching the shared runtime data directory used by
  the Makefile and the rest of acceptance checks.
- Accepted broker positions without an aggregate holding now normalize
  `market_value` with `fx_to_hkd` into HKD value and retain accepted quantity,
  price, P/L, and accepted weight fields for account-row rendering.
- Existing detail-level P/L percentage calculation remains authoritative when
  a quote is unavailable.

## Regression coverage

- Acceptance regression proves a worktree-local status file is not required when
  the shared project data directory contains the live controller status.
- Dashboard JavaScript regression covers an orphan accepted position and checks
  HKD value, quantity, cost/last price, P/L, and account/portfolio weights.

## Verification

```text
PYTHONSAFEPATH=1 PYTHONPATH=.:src .venv/bin/pytest -q tests/test_dashboard_web.py tests/test_dashboard_acceptance.py
560 passed in 36.80s

node --check src/open_trader/dashboard_static/dashboard.js
PASS

git diff --check
PASS
```
