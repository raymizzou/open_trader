# Trend Protection Holiday Status Design

## Context

On a non-trading day, the controller's time-of-day check can still run the
protection pass. The pass correctly returns `holiday` with zero exceptions and
zero unknown quotes, but `_protection_blocker` currently accepts only
`completed`, so the CN controller publishes a false blocking state.

## Decision

Treat `holiday` like `completed` only when both `exception_count` and
`unknown_quote_count` are valid integers equal to zero. Any missing count,
exception, unknown quote, or other status remains blocking.

This change belongs in `_protection_blocker`, the shared boundary that converts
protection results into controller health. It does not change market-calendar
derivation, polling cadence, report generation, order execution, or the
protection watcher itself.

## Verification

- A regression test proves a clean `holiday` result is non-blocking while a
  `holiday` result with an unknown quote remains blocking.
- Focused controller tests and the full repository suite pass.
- The CN/HK/US controllers are restarted from the final SHA and publish fresh,
  matching status.
- `make acceptance` returns `PASS` before merge and deployment handoff.
