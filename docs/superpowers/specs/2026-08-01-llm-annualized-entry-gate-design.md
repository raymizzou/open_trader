# LLM Hedge Annualized Entry Gate Design

**Date:** 2026-08-01

## Goal

Require an existing Polymarket LLM threshold-hedge opportunity to have a
simple annualized yield of at least `15%` before Open Trader may call it
actionable, notify the operator, or admit it to order preview.

Positive opportunities below `15%` remain visible and persisted for
observation and distribution analysis.

## Scope

This change applies only to opportunities with
`market_type="threshold_hedge"`.

It does not:

- change `simple_annualized_yield()` or create a second yield calculation;
- change acquisition-cost, fee, payout, or capital-duration inputs;
- add a Treasury-rate feed, settlement buffer, or early-exit model;
- change standard same-condition YES/NO arbitrage;
- add Negative Risk execution, Kalshi, or another venue;
- change order sizing, the `$20` normal-cost cap, or the `$2` remediation cap.

## Existing Calculation

Reuse the current domain calculation without alteration:

```text
simple_annualized_yield =
  minimum_profit
  / total_max_cost
  * 365
  / remaining_days_to_resolution
```

The value remains a decimal ratio: `0.15` means `15%`.

The calculation returns unavailable when the resolution time is not in the
future or maximum cost is not positive. An unavailable annualized yield must
never pass admission.

## Admission Rule

Define one server-owned threshold:

```text
MIN_THRESHOLD_ANNUALIZED_YIELD = Decimal("0.15")
```

For an otherwise positive threshold-hedge candidate:

```text
annualized_yield is unavailable
=> visible observation
=> eligibility_reason = "annualized_yield_unavailable"
=> actionable = false

annualized_yield < 0.15
=> visible observation
=> eligibility_reason = "annualized_yield_below_minimum"
=> actionable = false

annualized_yield >= 0.15
=> annualized gate passes
=> existing LLM, deterministic-rule, book-freshness, remediation,
   readiness, balance, and allowance checks remain required
```

The `15%` comparison is inclusive. A value exactly equal to `0.15` passes
this gate but still must pass every existing safety check.

This rule does not reorder or skip existing LLM validation. When several
checks fail, the Dashboard preserves the existing upstream rule or data
failure reason. `annualized_yield_below_minimum` is the visible reason only
when annualized yield is the admission blocker that would otherwise allow the
candidate to proceed.

The annualized floor is an admission condition, not a reason to close the
positive signal episode. Below-floor observations continue contributing to
the current, 7-day, and 30-day annualized distributions.

## Shared Enforcement

The monitor applies the rule before setting `actionable=true`. Existing
notification scheduling already requires `actionable=true`, so below-floor
observations must not create Feishu notification attempts.

The execution service also validates the fresh server-owned opportunity's
annualized yield during the shared preview/notification admission check. It
must reject a missing, malformed, non-finite, or below-floor value even if a
caller supplies an inconsistent `actionable=true` field.

Preview and final confirmation continue to refresh the opportunity. If yield
falls below `15%` before either step, admission fails with
`annualized_yield_below_minimum`; no order is signed or submitted.

## Dashboard Behavior

The existing LLM candidate view continues to display theoretical profit,
remaining days, and simple annualized yield.

Below-floor candidates remain visible with the reason
`年化低于 15% 入场门槛`. An unavailable value displays the existing
unavailable state and the reason `年化无法计算，禁止入场`.

The history and distribution models remain unchanged. No new panel, table,
column, or interaction is required.

## Notification Behavior

Only a currently actionable threshold hedge may notify. Therefore:

- positive but below `15%`: persisted, visible, not notified;
- unavailable annualized yield: persisted when otherwise observable, not
  notified;
- at least `15%` and all existing checks pass: current one-time notification
  behavior remains unchanged.

No live test notification is required. Verification uses deterministic
notification-path assertions.

## Failure Behavior

The gate fails closed:

- missing or invalid annualized yield never falls back to zero or a sample
  value;
- a stale stored yield cannot override the freshly recomputed opportunity;
- an execution request cannot bypass the gate through browser payloads;
- source/API failures do not remove prior history or fabricate actionability.

## Verification

Focused automated coverage must prove:

1. the existing annualized calculation is unchanged;
2. `0.149999...` remains visible but is not actionable;
3. `0.15` passes this gate and continues to later admission checks;
4. unavailable, malformed, and non-finite yields fail closed;
5. below-floor observations remain in signal history and distributions;
6. below-floor observations do not schedule or send notifications;
7. preview and notification admission reject an inconsistent or newly
   below-floor opportunity;
8. standard binary arbitrage behavior is unchanged;
9. the Dashboard shows the server-owned rejection reason.

Implementation verification follows the repository gates: focused tests and
direct workflow checks during development, then `make acceptance` once as the
final Dashboard gate. Only `PASS` is review-ready. After `PASS`, redeploy the
exact accepted SHA and verify PID, working directory, SHA, fresh logs, and
HTTP 200 before handoff.
