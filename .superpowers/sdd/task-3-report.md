# Task 3 Report

## Status

Implemented compatible statement selection, explicit latest promotion, password prompting, and broker-safe Eastmoney replacement without changing `PORTFOLIO_FIELDNAMES` or its order.

## TDD evidence

- RED: `PYTHONPATH=.:src /Users/ray/projects/open_trader/.venv/bin/pytest tests/test_pipeline.py tests/test_parsers_text.py tests/test_portfolio.py -q` produced 12 expected feature failures and 78 passes.
- Rounding RED: equal-valued combined rows produced `99.99%`, proving the two-decimal total regression test.
- Missing-FX RED: a non-HKD row with blank `fx_to_hkd` was accepted before validation was added.
- GREEN focused: `92 passed in 0.40s`.
- Full suite: `PYTHONPATH=.:src /Users/ray/projects/open_trader/.venv/bin/pytest -q` -> `1162 passed in 20.68s`.
- Static check: `git diff --check` passed. Ruff was unavailable in the repository virtual environment.

## Compatibility invariants checked

- `PORTFOLIO_FIELDNAMES` and field order are unchanged.
- Phillips-only CLI mode retains legacy portfolio construction; Phillips and Eastmoney inputs are mutually exclusive so no parsed statement is ignored.
- Eastmoney mode removes only rows whose broker set is exactly `{eastmoney}` and preserves all other broker and cash row values except recalculated weight.
- Mixed Eastmoney broker rows and preserved/new `(market, symbol, currency)` identity collisions fail closed.
- Every combined row requires a finite `market_value_hkd`; every non-HKD row requires a finite positive `fx_to_hkd`.
- Combined weights use the entire valid HKD total and a minimal rounding residual so displayed two-decimal weights total exactly `100.00%` when the portfolio total is nonzero.
- The dated run is always promoted; latest is touched only when `update_latest=True`. Direct Python callers retain the `True` default, while CLI promotion requires `--update-latest`.
- Eastmoney passwords are obtained only through `getpass`; no plaintext password CLI argument or console output exists.
- Combined non-cash holding count remains derivable from the complete merged portfolio rows; no stale count field or schema extension was introduced.

## Blocking review fixes

### RED

Command:

`PYTHONPATH=.:src /Users/ray/projects/open_trader/.venv/bin/pytest tests/test_portfolio.py tests/test_pipeline.py tests/test_parsers_text.py -q`

Output: `8 failed, 88 passed in 0.55s`.

The failures directly demonstrated stale `market_value_hkd` use, retained stale cost/P&L fields, acceptance of zero/negative combined totals, acceptance of missing/non-finite source `market_value`, and an inaccurate Eastmoney combined holding count. One count fixture initially lacked the USD rate required by its preserved USD row; after correcting that fixture, the count assertion remained RED until the pipeline result logic changed.

### GREEN

Covering command:

`PYTHONPATH=.:src /Users/ray/projects/open_trader/.venv/bin/pytest tests/test_portfolio.py tests/test_pipeline.py tests/test_parsers_text.py -q`

Output: `96 passed in 0.67s`.

Full-suite command:

`PYTHONPATH=.:src /Users/ray/projects/open_trader/.venv/bin/pytest -q`

Output: `1166 passed in 20.16s`.

`git diff --check` also passed before the full suite.

### Rechecked invariants

- Every combined row now requires finite source `market_value`; `market_value_hkd` is recomputed from source value and effective FX, with HKD fixed to rate 1.
- Non-cash rows with finite cost recompute HKD cost and local-currency unrealized P&L/percentage; missing cost clears all derived cost/P&L fields and forces `data_check`.
- Cash rows retain blank cost/P&L fields.
- Combined HKD totals of zero or less fail closed.
- Eastmoney `positions_count` and CLI `positions:` output now represent every combined non-cash portfolio row; Phillips-only counting remains unchanged.
- Self-review RED: the missing-cost regression with `avg_cost_price="stale"` failed (`1 failed in 0.08s`); the shared clear helper now also blanks this cost-derived field.
- Self-review GREEN: covering tests `96 passed in 0.47s`; full suite `1166 passed in 23.75s`.
