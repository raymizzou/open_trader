# Task 2 report — simplify broker source status panel

## Scope

Implemented the approved Dashboard-only UI and acceptance change.

- Reduced the header source panel to the grouped `#source-status-list` mount.
- Removed the global quote-status, account-sync-status, and last-refresh DOM and renderer paths.
- Preserved quote fetch/polling, `state.quotePayload`, holding-price rendering, connection diagnostics, page-error state, broker sync status, and account-sync safety classes.
- Added the `.source-status-group` label token and kept broker source rows two-column on mobile.
- Updated browser acceptance to validate grouped source labels and per-broker source times.
- No backend projection, controller, API, schema, or data-file changes.

## Rendering and acceptance behavior

- `refreshQuotes()` now relies on the normal dashboard render after successful quote loads and renders connection diagnostics after a quote-fetch failure.
- Removed `renderAccountSyncStatus()`, `renderAccountSyncStatusIntoHeader()`, `renderQuoteStatus()`, `quoteRefreshText()`, and `quoteStatusText()`.
- `_check_source_status_panel()` validates `实时账户` / `券商结单`, rejects deleted global copy, and checks source-specific live or statement timestamps for all four brokers.
- `_check_session_prices()` now validates only the compact per-row US session price contract.

## TDD evidence

The four requested web contract tests were changed before the production edits. They failed first because the three global elements and dead renderer paths still existed, the group-label style was absent, and mobile stacked source rows. After the minimal implementation, all four focused tests passed.

## Verification

The literal brief command using `PYTHONPATH=src` could not collect this worktree because `tests` is not importable without the repository root:

```text
ModuleNotFoundError: No module named 'tests'
```

The compatible worktree invocation (`PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src"`) passed the focused web contract tests:

```text
4 passed in 0.92s
```

Focused acceptance coverage passed:

```text
10 passed in 0.73s
```

The complete acceptance module passed:

```text
283 passed in 1.13s
```

The complete Dashboard-focused suite passed:

```text
828 passed in 36.87s
```

`node --check src/open_trader/dashboard_static/dashboard.js` and `git diff --check` completed without output. The commit hook completed successfully.

## Commit

`1c4f9fa feat: simplify broker source status panel`
