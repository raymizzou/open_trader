# Task 2 report

Status: DONE_WITH_CONCERNS

## Change

- Added explicit identity, local-strength, market-cap, temperature, discipline,
  industry-temperature, and expansion field groups.
- Added `StagedCandidateFetch` and `fetch_staged_candidates`.
- The helper merges every response only after exact unique-ID and data-date
  validation; it never falls back to `UNIFIED_TREND_FIELDS`.
- The next request contains only candidates that passed every earlier
  discipline gate.  It resolves bars only for discipline-stage survivors and
  requests industry temperatures once for those industries plus supplied
  holding industries.
- Added a waterfall test with the required exact request sequence and 28
  duplicate/missing/extra/stale fail-closed cases spanning all seven stages.

## Commands and results

| Command | Actual result |
| --- | --- |
| `.venv/bin/python -m pytest -q tests/test_a_share_trend.py -k 'staged_candidate or malformed_stage'` | Could not run because this worktree has no `.venv/bin/python`. |
| `/Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_a_share_trend.py -k 'staged_candidate or malformed_stage'` before implementation | `29 failed, 448 deselected`; failures were the missing staged helper/field sets. |
| Same focused command after implementation | `29 passed, 448 deselected in 0.61s`. |
| `/Users/ray/projects/open_trader/.venv/bin/python -m compileall -q src/open_trader/a_share_trend.py tests/test_a_share_trend.py` | Passed. |
| `git diff --check` | Passed. |
| `/Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_a_share_trend.py` | `476 passed, 1 failed`; unrelated checked-in-artifact test could not find `data/trend_review/daily/CN/2026-07-16.json` in this clean worktree. |

## Concerns for Task 3 wiring

- `CANDIDATE_EXPANSION_FIELDS` deliberately excludes `tmId` and `asOfDate` per
  the approved field definition, while the strict merge contract requires the
  API response to include both identifiers.  The focused fake mirrors that
  API invariant.  Verify it against the live no-submit workflow before
  replacing the old runner path.
- This task intentionally returns empty `industry_contexts` and status.  The
  next task owns replacing the existing breadth-based context construction
  with the new temperature-only projection.
