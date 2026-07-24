# Task 4 Report: version continuity without resetting samples

## Status

Implemented and committed as `382dafe feat: version contextual trend selection`.

## TDD evidence

### RED

Added default-version, identity-map, Kelly sample, snapshot, and API-stats tests first.

Command:

```text
PYTHONPATH=src .venv/bin/python -m pytest tests/test_a_share_trend.py -k 'strategy_snapshot' tests/test_market_trend.py -k 'strategy_snapshot or live_strategy' tests/test_trend_kelly.py tests/test_trend_api_stats.py -k 'identity or version or inherits' -v
```

Exact result before implementation:

```text
8 failed, 39 passed, 396 deselected in 0.73s
```

The failures were the new CN v8 / US v5 / HK v5 defaults, the four explicit
identity maps, CN v8 sample selection, and CN v8 API-stat attribution.

### GREEN

Focused version/identity command:

```text
PYTHONPATH=src .venv/bin/python -m pytest tests/test_a_share_trend.py -k 'strategy_snapshot or generated_report or report_runner_uses_cn_simulation_account' tests/test_market_trend.py -k 'strategy_snapshot or live_strategy' tests/test_trend_kelly.py -k 'identity or inherits' tests/test_trend_api_stats.py -k 'identity or inherits' -q
36 passed, 407 deselected in 0.69s
```

Affected strategy/report/replay suites:

```text
PYTHONPATH=src .venv/bin/python -m pytest tests/test_a_share_trend.py tests/test_market_trend.py tests/test_trend_kelly.py tests/test_trend_api_stats.py tests/test_trend_review.py -q
663 passed in 3.10s
```

Full repository suite:

```text
make test
3433 passed in 78.73s (0:01:18)
```

Additional checks:

```text
PYTHONPATH=src .venv/bin/python -m compileall -q src/open_trader/a_share_trend.py src/open_trader/trend_kelly.py src/open_trader/trend_review.py
git diff --check
```

Both completed with exit status 0 and no output.

## Changes

- Live defaults are CN v8 and US/HK v5; CN v4/v6/v7 and US/HK v4 remain valid replay versions.
- Kelly matching uses explicit non-recursive maps: CN v7 ← CN v4/v7, CN v8 ← CN v4/v7/v8, US v5 ← US v4/v5, HK v5 ← HK v4/v5. Cross-market, wrong strategy IDs, CN v5/v6, and older unapproved identities do not match.
- v8/v5 snapshots list approved sample identities, contextual ordering keys/fallback, industry member/state fields, and fee semantics. Existing v4/v6/v7 frozen rows remain unchanged.
- Risk, Kelly, snapshot normalization, and report-evidence replay allowlists treat v8/v5 like the existing v4/v6/v7 machinery.
- `纪律.md` is v2 and records the contextual ordering, whole-report fallback/omission rules, exact fee labels/unit, and approved sample continuity.

No Dashboard, runner collection, or cost-calculation code was changed. `trend_api_stats` already emits a stat row for every contributing opening identity, so no schema/display change was needed.

## Concerns

The task brief did not list `src/open_trader/trend_review.py`, but its snapshot
normalization and replay allowlists must accept v8/v5 or the new frozen reports
cannot be validated/rebuilt. No live background process or Dashboard acceptance
gate was run; the parent task owns that final gate.
