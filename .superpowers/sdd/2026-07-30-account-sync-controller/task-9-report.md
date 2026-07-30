# Task 9 report — read-only account sync health UI

## Scope and state boundary

Implemented only the approved static Dashboard UI and its acceptance coverage.

- Replaced the `刷新账户与行情` control with `#account-sync-status` and controller-heartbeat text.
- Kept the existing five-second quote poll read-only: it fetches `/api/quotes`, then rereads `/api/dashboard` with `preserveOnError: true`; it has no account-sync response branch, disabled/loading control, or simulated-position refresh.
- Removed `accountSyncReloadNeeded`.
- Read `account_sync` and accepted `broker_positions` only from the published Dashboard payload. No server endpoint, refresh writer, controller ownership, or API ownership changed.

## UI and acceptance coverage

- Broker cards, source rows, selected-account metadata, and row actions all project `account_sync.brokers[broker]` through one status helper.
- Normal, failed, stale, and unknown states render text plus semantic status classes. The header renders `同步正常` or `同步异常` and the controller heartbeat.
- Failed, stale, and unknown broker data render an action-paused banner and replace executable `做T` with non-executable `人工复核`.
- Account rows now start from accepted `broker_positions`, then enrich from an aggregate holding when one matches. Therefore accepted rows remain visible without a matching `portfolio.csv` aggregate row.
- Mobile places sync status before assets, preserves status text, retains the existing row-card layout, and is covered for no page-level horizontal overflow and 14 accepted Tiger rows.
- Acceptance now rejects a remaining refresh element or its old text, unhealthy/missing account-sync controller evidence, non-normal sources in the normal flow, mismatched accepted counts, missing unsafe-state banner/review action, executable `做T` in unsafe states, and mobile overflow.

## TDD evidence

RED tests were added before the implementation: static assertions failed while the refresh control and `accountSyncReloadNeeded` still existed, and the renderer test initially failed with `ReferenceError: renderAccountSyncStatus is not defined`. The acceptance contract test also initially lacked account-sync validation. The implementation then made those tests green.

During final focused verification, five legacy browser-test doubles still expected `#refresh-quotes`. They were reconciled with the approved `#account-sync-status` contract; the isolated rerun passed:

```text
5 passed in 0.68s
```

## Verification

The brief's literal focused command could not collect this repository's test package because it omits the repository root from `PYTHONPATH`:

```text
$ PYTHONPATH=src .venv/bin/pytest -q tests/test_dashboard_web.py tests/test_dashboard_acceptance.py
ModuleNotFoundError: No module named 'tests'
```

The compatible invocation includes the repository root as well as `src`:

```text
$ PYTHONPATH=.:src .venv/bin/pytest -q tests/test_dashboard_web.py tests/test_dashboard_acceptance.py
558 passed in 37.82s
exit=0
```

`git diff --check` completed with no output.

## Optional Playwright check: BLOCKED

Playwright was invoked as requested:

```text
$ npx playwright test tests/e2e/dashboard-warm-ledger.spec.ts
Error: http://127.0.0.1:18766 is already used, make sure that nothing is running on the port/url or set reuseExistingServer:true in config.webServer.
```

The required fixture server is configured with `reuseExistingServer: false`. Port `127.0.0.1:18766` is owned by an existing user dashboard process, so it was not stopped:

```text
PID 6264
/opt/homebrew/.../Python -m open_trader dashboard --host 127.0.0.1 --port 18766 ...
```

This blocks only the optional browser check. Focused static, renderer, and acceptance tests are green. No live deployment or `make acceptance` run was performed for this static/acceptance task.

## Commit scope

The commit stages only the eight Task 9 implementation/test files plus this requested Task 9 report. Existing uncommitted edits to `README.md` and `docs/monthly_portfolio_import.md` are preserved and excluded.
