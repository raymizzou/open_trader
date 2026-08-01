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
- change acquisition-cost, fee, or payout inputs;
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

For a two-leg threshold hedge, both contracts must have the same valid end
time. This shared time is when the complete hedge can be treated as released.
If either end time is missing, invalid, or different, annualized yield is
unavailable and admission fails closed. The relation discovery invariant
already excludes mismatched end dates; the monitor and execution paths keep
the same guard when consuming a relation or refreshed opportunity.

`minimum_profit` already equals minimum payout minus both protected buy costs
and the modeled maximum trading fees. It must be labelled as a theoretical
minimum profit, not profit after every possible real-world cost: funding,
withdrawal, FX, unexpected settlement delay, and an optional early exit are
not modeled.

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

Use the approved compact B layout inside the existing `套利信号` panel. Do not
add another panel or interaction. The signal table uses these columns:

1. `出现时间（HKT）`;
2. `标的`;
3. `资金占用`;
4. `净回报`;
5. `操作`.

For a threshold hedge:

- `标的` shows the complete English `question_a / question_b` as the primary,
  stronger line, followed by its complete Chinese translation in smaller,
  muted text;
- neither language may use ellipsis, line clamping, or another truncation;
  both wrap naturally on desktop and mobile;
- reuse the existing asynchronous cached title translator for the exact
  displayed pair; translation must not block signal discovery or replace the
  English source;
- while translation is pending or unavailable, retain the English and keep a
  small second-line status instead of fabricating or silently truncating a
  Chinese title;
- `资金占用` shows remaining days and the shared contract end date;
- `净回报` groups theoretical minimum profit, simple annualized yield, and
  total maximum cost, with wording that the modeled maximum fee is included;
- `操作` shows `仅观察` plus the short blocking reason when the opportunity is
  not actionable, and retains the existing recheck action only when currently
  admissible.

Standard same-condition YES/NO rows keep their existing execution semantics;
this visual consolidation must not change their eligibility or order flow.

Below-floor candidates remain visible with the reason
`年化低于 15% 入场门槛`. An unavailable value displays the existing
unavailable state and the reason `年化无法计算，禁止入场`.

The history and distribution models remain unchanged. Existing stored
threshold fields and the existing translation cache are projected into the
approved layout; no second annualization or translation service is added.

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
9. the Dashboard shows the server-owned rejection reason;
10. capital duration uses the shared contract end time and fails closed when
    either time is invalid or the two times differ;
11. every threshold target preserves the complete English pair above the
    complete cached Chinese translation without truncation on desktop or
    mobile;
12. profit wording states the modeled-fee boundary and does not claim every
    external cost is deducted.

Implementation verification follows the repository gates: focused tests and
direct workflow checks during development, then `make acceptance` once as the
final Dashboard gate. Only `PASS` is review-ready. After `PASS`, redeploy the
exact accepted SHA and verify PID, working directory, SHA, fresh logs, and
HTTP 200 before handoff.
