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

## Fix round 1 (review findings)

- Excluded every enriched real-only holding `tmId` from staged candidate IDs
  and passed simulated plus real holding symbols to the staged gate and the
  new-version context collector. Complete holding requests therefore remain
  a deduplicated simulated/real-only snapshot.
- Restored legacy HK/US (and CN) industry-temperature estimate accounting to
  multiply requested `industry_ids`, even when the temperature request errors
  and returns no rows.
- Removed the standalone new-version industry fact that duplicated the staged
  temperature request. Staged API facts now contain exact field names and
  exact `tmId` lists; replay evidence retains the complete trace.
- Added deterministic allocation-backed runner ledger fixtures for CN and a
  parameterized HK/US runner fixture. Each asserts complete IDs, staged IDs,
  zero eligible-industry component/member/state calls, individual-global
  ordering, and exact field/ID cost arithmetic. The US fixture also freezes
  the 2026-08-07 estimate at or below `Decimal("2.852")`.
- Added a legacy error-path assertion proving HK/US use the requested
  industry ID cost when the temperature response is empty.

### Fix-round verification

```text
/Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
  tests/test_a_share_trend.py::test_current_cn_runner_ledger_excludes_real_only_candidates \
  tests/test_market_trend.py::test_allocation_market_runner_ledger_excludes_real_only_candidates \
  tests/test_market_trend.py::test_corrupt_statistics_do_not_weaken_current_market_industry_gate
6 passed in 0.58s

/Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
  tests/test_a_share_trend.py tests/test_market_trend.py tests/test_trend_industry_context.py \
  -k 'not build_report_upgrades_exact_repository_legacy_snapshot'
554 passed, 1 deselected in 2.09s

/Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
  tests/test_a_share_trend.py -k '(runner or paid_scope or cost) and not build_report_upgrades_exact_repository_legacy_snapshot'
52 passed, 428 deselected in 1.03s

/Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
  tests/test_market_trend.py -k 'report or snapshot or cost'
18 passed, 28 deselected in 0.49s

/Users/ray/projects/open_trader/.venv/bin/python -m compileall -q \
  src/open_trader/a_share_trend.py src/open_trader/market_trend.py \
  src/open_trader/trend_industry_context.py tests/test_a_share_trend.py tests/test_market_trend.py
exit 0

git diff --check
exit 0
```

The brief's combined broad selection (`tests/test_a_share_trend.py -k
'runner or paid_scope or cost' tests/test_market_trend.py -k 'report or
snapshot or cost'`) was also run verbatim: `162 passed, 363 deselected, 1
failed`. The sole failure was the known missing ignored artifact
`data/trend_review/daily/CN/2026-07-16.json`; the changed runner tests all
passed. `ruff` remains unavailable in the shared virtualenv.

The previous concern about missing HK/US ledger fixtures is resolved by the
new parameterized allocation-backed test.

### Evidence-ledger follow-up

The staged current-version evidence query now carries the industry-temperature
fields only inside the exact staged trace; the standalone `industry_fields`
metadata remains on legacy evidence only. This keeps the request ledger
unambiguous without changing legacy replay semantics.

```text
/Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
  tests/test_a_share_trend.py::test_current_cn_runner_ledger_excludes_real_only_candidates \
  tests/test_market_trend.py::test_allocation_market_runner_ledger_excludes_real_only_candidates \
  tests/test_market_trend.py::test_corrupt_statistics_do_not_weaken_current_market_industry_gate
6 passed in 0.54s

git diff --check
exit 0
```
