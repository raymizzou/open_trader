# Task 4 report

## Root cause

The account-grouped dashboard removed the broker filter controls and their event
binding in `3a5f036`. Market filters remain as `[data-market]` buttons, while
broker summary cards now link to the four account sections with
`href="#account-<broker>"`.

`_browser_check` still clicked the removed
`button[data-broker="eastmoney"]`. The permissive browser fake accepted every
selector, so unit tests missed the Playwright timeout seen on desktop and mobile.

## TDD evidence

- RED: the focused browser test failed because the obsolete Eastmoney button
  selector was still requested.
- GREEN: removed that one click. The flow still clicks the CN market filter and
  verifies `#visible-count` against `expected_cn`; the fake now asserts the old
  selector is never requested.

## Verification

- Focused browser test: `1 passed in 0.03s`
- `tests/test_dashboard_acceptance.py tests/test_dashboard_web.py`:
  `142 passed in 14.54s`
- `make acceptance` intentionally not run; the controller owns the live process
  restart and final acceptance run.
