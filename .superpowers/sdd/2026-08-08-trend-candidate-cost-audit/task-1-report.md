# Task 1 report

## Changes

- Added allocation strategy versions CN v13 and HK/US v11, keeping the prior allocation versions readable.
- Added the three-version individual global-strength rank gate. New versions sort by global strength, industry temperature on an exact strength tie, days, amount, then symbol; predecessor versions retain the industry-context ordering path.
- Published only the approved new rank rows for the three new versions and preserved existing risk, exit, and sizing contracts.
- Added drawdown predecessors and replay/normalization recognition for all three versions.
- Added the required Kelly identities. A target identity matches itself even when its inherited historical sample list deliberately excludes the newly opened version.

## Verification

Passed:

```text
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_a_share_trend.py -k 'new_versions_rank or predecessor_versions' tests/test_strategy_drawdown.py tests/test_trend_kelly.py tests/test_trend_review.py -k 'strategy_version or normalization'
20 passed, 827 deselected in 0.62s

PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_trend_review.py -k projection_tolerates_daily_allocation_identity_changes
1 passed, 297 deselected in 0.78s
```

The version/risk guard `rg` audit and `git diff --check` also passed.

## Concerns

- The shared virtualenv is editable-installed against another worktree. Tests must use `PYTHONPATH=src`; without it they execute that other worktree's source.
- Full run of the four affected test files reached `840 passed`; seven repository-fixture tests failed only because this isolated worktree has no ignored `data/trend_review/daily/*/2026-07-16.json` artifacts.
- `src/open_trader/trend_kelly.py` was necessarily changed although omitted from the brief's file list: it owns the Kelly identity registry required by the stated inheritance contract.

## Review fix

- Added CN v13 and HK/US v11 to Dashboard acceptance's allowed report-version registry and the protection-line reason label guard.

Passed:

```text
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_dashboard_acceptance.py -k 'dashboard_acceptance_allows_current_market_versions or protection_reason_label_accepts_current_rank_versions'
4 passed, 348 deselected, 1 warning in 0.78s

PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_dashboard_acceptance.py
352 passed, 1 warning in 2.46s
```
