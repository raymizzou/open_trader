# Task 4 Report: version continuity without resetting samples

## Status

Implemented and committed as `382dafe feat: version contextual trend selection`.
Dashboard compatibility follow-up was committed as
`b379658 fix: keep dashboard trend versions compatible`.

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
- Dashboard risk/drawdown validation recognizes v8/v5 as strict v4-contract versions, including Kelly and drawdown constraints. Integrated Dashboard acceptance accepts the current CN v8 and US/HK v5 defaults while retaining the approved historical replay versions (CN v4/v6/v7 and US/HK v4).

Runner collection and cost-calculation code were not changed. `trend_api_stats` already emits a stat row for every contributing opening identity, so no schema/display change was needed.

## Dashboard compatibility follow-up

The parent review found two remaining Dashboard backend version allowlists. Tests
were added before the implementation:

```text
PYTHONPATH=src .venv/bin/python -m pytest tests/test_dashboard.py -k 'current_live_risk' -q
2 failed, 229 deselected in 0.46s

PYTHONPATH=src .venv/bin/python -m pytest tests/test_dashboard_acceptance.py -k 'integrated_templates_and_three_market_reports' -q
1 failed, 296 deselected in 0.65s
```

The implementation then added v5/v8 to the Dashboard's strict risk/drawdown
contract and Kelly/drawdown constraint sets, and made integrated acceptance use
the report's version only when it is in the explicit per-market compatibility
allowlist. This keeps historical replay validation while accepting current live
defaults.

Focused Dashboard suites:

```text
PYTHONPATH=src .venv/bin/python -m pytest tests/test_dashboard.py tests/test_dashboard_acceptance.py -q
528 passed in 1.27s
```

Full repository suite after the follow-up:

```text
make test
3436 passed in 78.79s (0:01:18)
```

Compile and whitespace checks also completed successfully.

## Concerns

The task brief did not list `src/open_trader/trend_review.py`, but its snapshot
normalization and replay allowlists must accept v8/v5 or the new frozen reports
cannot be validated/rebuilt. No live background process or Dashboard acceptance
gate was run; the parent task owns that final gate. The acceptance allowlist
retains approved historical versions deliberately; it rejects versions outside
the market-specific set.
