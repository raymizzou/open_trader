# Task 6 Report

## Scope

Task 6 turns the selected trend report into a market-neutral, frozen-fact
workspace. Strategy lifecycle cards read only `strategy_parameter_rows`, the
header shows the frozen strategy version and API-cost label, and industry
earning-effect context is displayed beside the urgent action stage. Existing
buy, sell, review, hold, risk, simulation, and audit surfaces remain in the
workspace, with frozen industry confirmation added to buy rows.

Historical reports keep their own lifecycle facts; no current strategy rule is
calculated or substituted in the browser. Invalid industry context is labelled
and explains the legacy-ordering fallback.

## TDD evidence

### RED

The frozen lifecycle/industry test was added before the renderer existed:

```text
PYTHONPATH=.:src .venv/bin/pytest \
  tests/test_dashboard_web.py -k 'discipline or lifecycle or historical' -q
1 failed, 5 passed, 236 deselected
```

The failure was expected: the old renderer had no market-neutral lifecycle
cards and no frozen industry context.

## Implementation

- Added pure frozen-row lifecycle classification for entry gates, deterministic
  sorting, execution, holding, exit, and unknown discipline facts. Cumulative
  drawdown remains in the existing risk summary rather than being duplicated.
- Rendered native `<details class="trend-discipline-card">` cards with compact
  frozen facts, affected-row counts, escaped labels/values, and full expanded
  rows. The legacy CN-only cards remain only for payloads that predate the
  frozen row projection.
- Added frozen API-cost, version/status, and industry-context rendering,
  including breadth ratios, prior share/percentage-point changes, and explicit
  invalid-context reasons.
- Reordered the integrated workspace and added responsive five-column/one-column
  lifecycle grids, 44px summary targets, mobile ordering, wrapping, focus
  states, and no page-level horizontal overflow.
- Added acceptance checks for current and historical frozen rows, keyboardable
  lifecycle summaries, cost labels, industry breadth facts, and invalid-context
  fallback copy.

## GREEN

Focused lifecycle tests:

```text
6 passed, 236 deselected
```

Dashboard web and acceptance suites:

```text
tests/test_dashboard_web.py       242 passed
tests/test_dashboard_acceptance.py 298 passed
```

Backend projections and industry-context tests:

```text
tests/test_dashboard.py tests/test_trend_industry_context.py
254 passed
```

Repository-wide verification:

```text
make test
3460 passed in 74.43s (0:01:14)
```

Static JavaScript validation:

```text
node --check src/open_trader/dashboard_static/dashboard.js
exit 0
```

`make acceptance` was intentionally not run; it remains the parent Dashboard
task's final gate.
