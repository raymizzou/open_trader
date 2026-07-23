# Task 4 report: corrected trend-report revisions

Status: PASS (independent review complete)

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

## Review fix wave

- Late-buy authorization retries now reuse the original immutable timestamp and
  reject extra/tampered fields instead of blocking a quote-recovery retry.
- A completed correction is reused without generating a new `rN` report, and
  unsupported late market/date combinations fail before writing any revision
  artifacts.
- Controller and Dashboard batch reads validate `report_revision`; legacy base
  batches without that field remain compatible, while normal completed revisions
  use their separate immutable `-rN` batch.
- Added v5 updates to market-report fixtures so the full suite asserts the new
  strategy identity and drawdown key rather than frozen v4 assumptions.

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

PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_trend_market_controller.py tests/test_dashboard_web.py \
  tests/test_market_trend.py -q
405 passed in 35.47s
```

The historical-recovery regression is covered by a revision-3 batch assertion;
normal and explicit corrected executions now pass the selected report revision,
while late dashboard projections still fall back to a valid base batch when no
revision batch exists.

## Remaining integration work

- Independent Task 4 review: PASS; no Critical/Important findings remain.
- The parent integration task still owns the full test suite, direct corrected
  report/simulated-buy workflow, process restart/log checks, and final
  `make acceptance` gate.
