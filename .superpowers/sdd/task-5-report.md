# Task 5 Report

## Scope

Task 5 makes Trend Animals API-cost semantics canonical across the frozen JSON,
Markdown, and Feishu report surfaces, then projects frozen cost, industry
context, and strategy parameter rows through the Dashboard backend. Legacy raw
cost fields and payloads without the new facts remain readable.

## TDD evidence

### RED

Formatter/render tests were added first:

```text
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_a_share_trend.py -k 'api_cost_label or report_cost_label' -q
5 failed, 329 deselected
```

The expected failure was the missing `trend_api_cost_label` and the old
Markdown cost lines.

Dashboard projection tests were then run before implementation:

```text
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_dashboard.py -k \
  'frozen_cost_contexts or malformed_frozen_cost or legacy_projection' -q
3 failed, 565 deselected
```

The expected failures were missing projected frozen fields and no fail-closed
validation for malformed API cost facts.

## Implementation

- Added `trend_api_cost_label()` with actual-first, complete-estimate, and
  honest incomplete-estimate branches; zero actual cost renders as `0`.
- Kept non-finite balances invalid and rejected negative Trend Animals balances
  before they can be used to derive report-attributed actual cost.
- Reused the formatter in `_report_payload`, `render_markdown`, and
  `render_trend_feishu_text`; JSON now carries the canonical label plus raw
  actual/estimated values, completeness, and unit.
- Added strict Dashboard validation for new API-cost, industry-context, and
  parameter-row facts. Invalid new facts are unavailable; legacy payloads
  retain raw audit costs and receive empty new projections.
- Projected `api_cost`, `industry_context_status`, `industry_contexts`, and
  frozen `strategy_parameter_rows` from the selected report artifact.

## GREEN

Focused formatter/render tests:

```text
6 passed, 329 deselected
```

Focused Dashboard projection tests:

```text
6 passed, 231 deselected
```

Affected suites:

```text
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_a_share_trend.py tests/test_market_trend.py tests/test_dashboard.py -q
603 passed
```

The full `make test` result is recorded below after the repository-wide run.

```text
make test
3457 passed in 88.96s (0:01:28)
```

The Dashboard validator accepts the pre-Task-5 four-field `api_cost` object
while requiring the label on the current five-field object, so existing frozen
reports are not silently discarded.

No Dashboard process, external API, account, or notification service was
started or restarted by this task. `make acceptance` remains the parent
Dashboard task's final gate.
