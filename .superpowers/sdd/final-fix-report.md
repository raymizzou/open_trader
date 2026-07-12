# Final fix report: CN cached-close portfolio weights

## Change

- Extracted the existing largest-row rounding-residual algorithm into `recalculate_portfolio_weights`.
- After one or more valid CN cached-close overlays, the dashboard recalculates weights once across every portfolio row, including CN, non-CN, and cash.
- Weight calculation validates every `market_value_hkd` and the complete positive total before mutating any row. On validation failure, the dashboard preserves all existing weights.
- Added a multi-CN/non-CN/cash API-state regression that verifies refreshed summary/value consistency, exact two-decimal weights totaling `100.00%`, and no mutation of `portfolio.csv`.

## TDD RED

Command:

```text
/Users/ray/projects/open_trader/.venv/bin/pytest -q tests/test_dashboard.py -k refreshes_all_weights_after_cn_cached_closes
```

Exact result before implementation:

```text
F                                                                        [100%]
E       AssertionError: assert {'600001': '1...SH': '30.00%'} == {'600001': '2...ASH': '9.97%'}
1 failed, 55 deselected in 0.44s
```

The four rows retained their stale `10.00%`, `20.00%`, `40.00%`, and `30.00%` weights after CN values changed.

## TDD GREEN

Command and exact output:

```text
$ /Users/ray/projects/open_trader/.venv/bin/pytest -q tests/test_dashboard.py -k refreshes_all_weights_after_cn_cached_closes
.                                                                        [100%]
1 passed, 55 deselected in 0.64s
```

## Focused and portfolio verification

Command and exact output:

```text
$ /Users/ray/projects/open_trader/.venv/bin/pytest -q tests/test_dashboard.py tests/test_portfolio.py
........................................................................ [ 80%]
.................                                                        [100%]
89 passed in 0.43s
```

## Full suite

The raw `pytest` executable first failed during collection because the worktree `tests` package was absent from `sys.path`:

```text
ModuleNotFoundError: No module named 'tests'
1 error in 0.73s
```

Rerun through the repository interpreter with the worktree root on `sys.path`:

```text
$ /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q
........................................................................ [  5%]
........................................................................ [ 11%]
........................................................................ [ 17%]
........................................................................ [ 23%]
........................................................................ [ 29%]
........................................................................ [ 35%]
........................................................................ [ 41%]
........................................................................ [ 47%]
........................................................................ [ 53%]
........................................................................ [ 59%]
........................................................................ [ 65%]
........................................................................ [ 71%]
........................................................................ [ 77%]
........................................................................ [ 83%]
........................................................................ [ 89%]
........................................................................ [ 95%]
.........................................................                [100%]
1209 passed in 23.28s
```
