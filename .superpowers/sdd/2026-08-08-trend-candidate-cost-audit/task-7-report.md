# Task 7 report — no-submit three-market revision publisher

## Scope

- Added `scripts/regenerate_trend_reports_no_submit.py`.
- Added `tests/test_trend_report_regeneration.py`.
- The publisher stages only `trend_a_share`, `trend_hk_phillips`, and
  `trend_us_tiger` under a temporary reports root, calls the existing
  generators directly with `revision=True` and `NullNotifier`, validates the
  expected current versions and API-cost fields, then optionally publishes all
  six artifacts with `O_CREAT | O_EXCL`.
- CN/HK/US are all validated before any real report path is touched. Prior
  report bytes are checked before staging completes and are never overwritten.
  The returned manifest records per-market old/new SHA-256 values, revisions,
  estimated/actual costs, publication state, and `submitted_orders: 0`.
- The module does not import the trend controller or call order submission.
- The publisher reads the allocation controller's immutable terminal reference
  (or accepts a test-provided reference) and passes it to every generator;
  this is required for the generators to emit CN `v13` and HK/US `v11` rather
  than their legacy no-allocation versions.

## Red/green evidence

Initial test-first run before the publisher existed:

```text
FileNotFoundError: .../scripts/regenerate_trend_reports_no_submit.py
!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
```

Focused green run after implementation:

```text
...                                                                      [100%]
3 passed in 0.38s
```

After the allocation-reference fix, rerun with the feature worktree explicitly
on `PYTHONPATH`:

```text
...                                                                      [100%]
3 passed in 0.37s
```

Syntax check:

```text
/Users/ray/projects/open_trader/.venv/bin/python -m py_compile \
  scripts/regenerate_trend_reports_no_submit.py
```

The tests cover:

1. all three generators receive `revision=True` and `NullNotifier`, while a
   dry stage leaves the real report tree unchanged;
2. an HK generation failure prevents every publication; and
3. successful publication creates six immutable files, keeps prior JSON/MD
   bytes unchanged, records hashes/costs, and reports zero submitted orders.

No live reports were regenerated or published, and `make acceptance` was not
run for this task.
