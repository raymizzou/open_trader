# Task 4 report

Status: PASS

## Scope

- Added a dedicated eastmoney acceptance branch for the explanatory CN audit table.
- Kept the existing US/HK audit checks unchanged.
- Updated acceptance fixtures and helper tests for the comparison-table structure and mobile controls.

## Verification

RED:

    .venv/bin/python -m pytest -q tests/test_dashboard_acceptance.py -k "trend_audit or mobile_report"
    1 failed, 1 passed, 266 deselected

    Failure was the old eastmoney three-section expectation after the acceptance fixture was updated to the new two-section/table structure.

GREEN:

    .venv/bin/python -m pytest -q tests/test_dashboard_acceptance.py -k "trend_audit or mobile_report"
    2 passed, 266 deselected in 0.21s

Full related regression:

    .venv/bin/python -m pytest -q tests/test_dashboard.py tests/test_dashboard_web.py tests/test_dashboard_acceptance.py
    720 passed in 29.22s

Additional checks:

    python -m compileall -q src/open_trader/dashboard_acceptance.py
    git diff --check

## Self-review

- eastmoney validates the audit table, exact headers, API candidate row count, identity, status, reason count/content, unknown reason codes, expandable field summaries, industry concentration, sources, and API cost.
- Mobile eastmoney validation checks real locator overflow and 44px summary targets through the supplied browser page.
- US/HK continue through the original legacy audit assertions.
- The standalone “排除项” heading is explicitly rejected for eastmoney.

Concerns: none.

## Review fix: reason-count summary

- Added rendered CN reason-label normalization using the existing TREND_REASON_LABELS mapping with field-label aliases and raw-code fallback.
- The eastmoney checker now counts every excluded_reasons label and requires a matching label/count pair in the rendered audit text.
- The focused fake now includes 趋势强度 1 and a regression case proves a missing count is rejected.

Verification after the fix:

    .venv/bin/python -m pytest -q tests/test_dashboard_acceptance.py -k "trend_audit or mobile_report"
    2 passed, 266 deselected in 0.29s

    .venv/bin/python -m pytest -q tests/test_dashboard.py tests/test_dashboard_web.py tests/test_dashboard_acceptance.py
    720 passed in 29.41s
