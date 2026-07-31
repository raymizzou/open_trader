# Task 1 report — grouped broker source times

## Scope

Implemented the approved source-list-only Dashboard change.

- Added the fixed `实时账户` (`futu`, `tiger`) and `券商结单` (`phillips`, `eastmoney`) groups.
- Preserved `brokerSyncStatus()` unchanged for broker cards and account sections.
- Added `brokerSourceTime()` and `brokerSourceStatus()` for source-list-specific timestamps and display text.
- Removed the controller row from `renderSourceStatusList()`; controller/account-sync status remains available through its existing header and account-section paths.
- Kept the change limited to `dashboard.js` and `test_dashboard_web.py`; no HTML, CSS, schema, or acceptance changes.

## Rendering behavior

- Live brokers display time-of-day from `data_as_of`, falling back to `last_success_at` when needed.
- Statement brokers display the `MM-DD` portion of `data_as_of`.
- Healthy rows show their source time; failed, stale, and unknown rows use the required unsafe/fallback wording.
- Source rows remain escaped and retain broker-specific status classes and `data-broker` attributes.

## TDD evidence

The specified renderer test was added before production changes. It failed first because the existing renderer had no group labels, still rendered the controller row, and omitted per-broker source times. After the minimal implementation, the renderer test passed. The neighboring account-sync test was updated only for the removed controller status return and the changed live-broker failed wording.

## Verification

The literal brief command could not collect this worktree because `tests` is not a package and `PYTHONPATH=src` omits the repository root:

```text
ModuleNotFoundError: No module named 'tests'
```

The compatible worktree invocation passed both focused tests:

```text
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
  tests/test_dashboard_web.py::test_dashboard_groups_broker_sources_and_shows_each_source_time \
  tests/test_dashboard_web.py::test_dashboard_renders_file_backed_account_sync_health_and_accepted_positions
2 passed in 1.02s
```

The complete Dashboard web test module also passed:

```text
279 passed in 36.95s
```

`git diff --check` and `git show --check` completed without output. The commit hook reported no uncommitted files after the implementation commit.

## Commit

`dba33a4 feat: show broker source timestamps`
