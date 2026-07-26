# Trend Strategy Version Identity Repair

Date: 2026-07-26

## Problem

The current ETF-enabled parameters were published under the existing strategy
identities: CN v9, US v6, and HK v6. Their parameter hashes no longer match the
hashes already audited for those identities, so the drawdown preflight
correctly fails closed with `parameter_mismatch`.

Missing frozen baselines are independently allowed to produce `SKIPPED`.
Futu/calendar failures remain `BLOCKED`, and malformed or inconsistent
artifacts remain `FAIL`.

## Decision

Publish the current ETF-enabled parameters as new strategy identities effective
from 2026-07-27:

- CN v10, inheriting from CN v9.
- US v7, inheriting from US v6.
- HK v7, inheriting from HK v6.

Historical versions remain replayable and unchanged. The new versions do not
change selection, sizing, exits, candidate pools, or risk limits beyond the
parameters already present in the current code.

## State Inheritance

Use the existing approved-predecessor path in the drawdown preflight. For each
market, the new record inherits the predecessor's high-water mark, current
equity, pause state, and other audited drawdown state. It records a normal
`new_strategy_version` bootstrap event.

The preflight must fail closed if the exact approved predecessor is absent or
invalid. It must not fetch a replacement live NAV, reset the high-water mark,
or add a same-version compatibility exception.

## Kelly Samples

Extend the existing explicit Kelly identity map:

- CN v10 accepts the already approved CN v4, v7, v8, and v9 samples, plus new
  v10 samples.
- US v7 accepts US v4, v5, and v6 samples, plus new v7 samples.
- HK v7 accepts HK v4, v5, and v6 samples, plus new v7 samples.

No cross-market or otherwise unapproved samples are admitted.

## Merged Test Reconciliation

The latest `main` added notification tests that use an absent frozen baseline
as their failure trigger. That setup now correctly returns `SKIPPED`. Keep the
notification behavior tests, but make them trigger a genuinely malformed
matching frozen baseline. This changes only the fixture, not production alert
behavior.

## Verification

Add focused tests proving:

- current snapshots publish CN v10 and US/HK v7 from 2026-07-27;
- historical v9/v6 snapshots remain replayable;
- ETF parameters and candidate pools are unchanged in the new versions;
- Kelly inheritance contains exactly the approved identities;
- drawdown records inherit from v9/v6 without rebasing;
- a missing approved predecessor still fails closed;
- alert tests exercise a real failure rather than a skipped baseline.

Then run the focused suites, the full test suite, the real drawdown preflight,
and the final Dashboard `make acceptance` gate. Only `PASS` permits deployment
and review.

## Non-goals

- No same-version parameter-hash override.
- No drawdown reset or new live baseline.
- No new abstraction or configuration layer.
- No strategy-rule changes beyond assigning the already-published parameters
  to new audited identities.
