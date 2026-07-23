# Task 4 report: corrected trend-report revisions

Status: DONE_WITH_CONCERNS (pending independent review)

## Implemented

- Execution batches now carry an immutable `report_revision`; revision `0`
  keeps the existing `<execution_date>.json` path and corrected reports use
  `<execution_date>-rN.json`.
- The controller accepts v5 pending market-data actions while retaining strict
  v1-v4 report validation. Normal controller revisions continue to use the
  legacy base batch; the explicit corrected flow locks the matching revision
  batch, so neither path can overwrite the other.
- Added `run_corrected_trend_report` and the CLI command:
  `trend-market correct --market --actor --reason [--allow-late-buys]`.
- The only late-buy exception is bound to CN, report date `2026-07-22`,
  execution date `2026-07-23`, an open CN session, execute mode, explicit
  actor/reason, and a one-shot immutable report-hash authorization artifact.
  Replays with changed authorization or report content fail closed.
- Dashboard execution-batch projection selects the report's revision suffix and
  validates the batch against that revision.
- Added focused regression coverage for revision-batch immutability, v5
  pending-report validation, CLI parsing, and late authorization rules.

## Verification

```text
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_trend_market_controller.py::test_revision_targets_invalid_historical_cycle_then_recovers_next_revision \
  -q
1 passed

PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_trend_review.py tests/test_trend_market_controller.py \
  tests/test_dashboard.py tests/test_trend_market_cli.py -q
539 passed in 7.22s

git diff --check
passed
```

The historical-recovery regression was fixed by making the normal controller
pass revision `0`, while the explicit corrected command passes the report's
revision and `_execution_completed` falls back to the base batch until that
revision batch exists.

## Remaining integration work

- Run the independent Task 4 review and address any Critical/Important
  findings.
- The parent integration task still owns the full test suite, direct corrected
  report/simulated-buy workflow, process restart/log checks, and final
  `make acceptance` gate.
