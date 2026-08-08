# Task 3 report: staged runner wiring and industry breadth removal

## Delivered

- Kept legacy strategy versions on their original complete candidate snapshot,
  industry breadth, cost, and report-evidence paths.
- Routed allocation-backed current versions (`CN v13`, `HK/US v11`) through
  `fetch_staged_candidates`: complete snapshots are now limited to simulated
  and real-only holdings; non-held candidates use the staged request path.
- Passed staged industry-temperature rows into a temperature-only context.
  It has no member breadth or required industry strength and reports
  `ordering_mode=individual_global`.
- Removed eligible-industry component/member/state requests and their cost
  from the current-version branch. The estimate is complete holding rows plus
  real-only holding rows plus the staged trace estimate.
- Recorded staged fields/IDs in `api_facts` and replay evidence. Preserved
  the previous evidence shape for legacy versions.

## Tests run

```text
/Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
  tests/test_a_share_trend.py tests/test_market_trend.py tests/test_trend_industry_context.py \
  -k 'not build_report_upgrades_exact_repository_legacy_snapshot'
551 passed, 1 deselected in 2.01s

/Users/ray/projects/open_trader/.venv/bin/python -m compileall -q \
  src/open_trader/a_share_trend.py src/open_trader/market_trend.py \
  src/open_trader/trend_industry_context.py
exit 0

git diff --check
exit 0
```

The unfiltered three-file pytest invocation had one unrelated failure before
the exclusion: `test_build_report_upgrades_exact_repository_legacy_snapshot`
expects the ignored local artifact
`data/trend_review/daily/CN/2026-07-16.json`, which is not present in this
clean worktree. It did not execute the changed runner code.

## Concerns for review

- The current-version runner ledger regression test is direct for CN. The
  shared HK/US runner follows the same gated branch and all 44 existing market
  tests pass, but there is not yet a separate allocation-backed HK/US request
  ledger fixture in this task. Add one in review if three-market ledger proof
  must be test-local rather than supplied by the final offline/live gate.
- `ruff` is not installed in the shared virtualenv (`No module named ruff`).
