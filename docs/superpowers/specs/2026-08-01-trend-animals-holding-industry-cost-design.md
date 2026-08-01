# Trend Animals Holding-Industry Cost Reduction Design

**Date:** 2026-08-01
**Status:** Approved in conversation; pending written-spec review
**Markets:** CN, HK, US

## Goal

Reduce normal trading-day Trend Animals cost without changing entry gates,
exit rules, risk controls, candidate-industry breadth, current candidate
ordering, formal actions, or the Dashboard's holding-industry metrics.

The accepted trade-off is that an industry collected only because it is held
will no longer keep an exact local member-breadth history. If that industry
later becomes an eligible candidate industry, its first report may use
`context_current_only` ordering instead of historical-context ordering. This
can change candidate order, but never lets a candidate bypass a hard gate.

For identical same-day inputs, this trade-off is the only permitted strategy
exception. Candidate inclusion and exclusion, candidate order, formal actions,
risk results, holding decisions, and every Dashboard field and status other
than the displayed API-cost value must remain unchanged. That cost value is
expected to decrease.

## Current Cost Cause

`collect_industry_contexts()` currently expands every holding-only industry
into all component instruments and requests `tradableFlag` plus
`isTrendRightSide` for every member. Those rows do not affect the current
candidate decision because candidate ordering uses a context map built before
holding-only contexts are appended.

For the 2026-07-31 reports, holding-only industries caused about 3,610 member
snapshots. At the current field prices this is 10.83 balance units, plus about
18 component calls. The expected saving is approximately 13 balance units per
normal reporting day; the exact amount varies with holdings and industry size.

## Design

Keep the existing candidate-industry path unchanged:

1. Fetch each eligible candidate industry's components.
2. Fetch the union of candidate-industry members with
   `INDUSTRY_MEMBER_FIELDS`.
3. Calculate exact local breadth and use it in candidate ordering.

Change only the holding-only path:

1. Do not call `get_components()` for holding-only industries.
2. Do not include holding-only members in `INDUSTRY_MEMBER_FIELDS` snapshots.
3. Continue fetching `INDUSTRY_STATE_FIELDS` for holding-only industry IDs.
4. Keep holding-only context rows, temperature, direction, strength,
   warm-to-hot count, supplier aggregate right-count ratio, supplier aggregate
   right-market-cap ratio, and strength-based display order.
5. Store local member counts as zero and local `right_share` as unavailable.
   Member-coverage validation does not invalidate a deliberately state-only
   holding context; missing or invalid state fields still do.
6. Add `member_breadth_collected` to `IndustryContext`. Candidate contexts and
   old artifacts default to `true`; new holding-only contexts explicitly store
   `false`. Do not migrate or rewrite old artifacts.

The Dashboard industry table already renders the supplier aggregate ratios,
not the local member numerator and denominator, so its visible holding-industry
metrics remain unchanged. Buy rows continue to use exact local breadth because
only eligible candidate industries appear there.

`member_breadth_collected` is an audit field only. The Dashboard gains no new
column, label, badge, fallback status, or unavailable-state marker from this
change.

API audit facts will report component requests, component rows, member IDs,
and member rows for candidate industries only. Context and state IDs will
continue to include holding-only industries. No report-schema version or new
configuration option is required.

## Error and History Behavior

- A holding-industry state lookup failure keeps that holding industry visible
  with unavailable state values and a recorded holding error.
- A failed holding-industry state lookup never falls back to component or
  member queries and never triggers a paid breadth retry.
- Holding-only failures never make an otherwise valid candidate context fall
  back to legacy ordering.
- State-only holding contexts remain in dated history for temperature and
  supplier aggregate-ratio comparisons.
- Because their local `right_share` is unavailable, a later transition from
  holding-only to eligible candidate can make historical context incomplete;
  the existing conservative `context_current_only` mode handles that case.

## Verification

TDD will first add a focused regression test proving that component and member
calls contain eligible candidate industries only, holding-only industry state
is still queried, holding-only rows and aggregate metrics remain present and
sorted, and candidate ordering status remains unchanged.

Before deployment, an offline before/after ledger will run against the frozen
2026-07-31 CN, HK, and US evidence. It must prove:

- industry component calls fall from 22 to 4, a reduction of 18;
- member snapshots fall from 4,821 to 1,211, a reduction of 3,610; and
- the priced member fields fall by exactly 10.830 balance units.

The additional component-call saving is reported as an estimate because that
endpoint has no stable published unit price. The total expected saving remains
approximately 13 balance units, but exact currency savings are not an
acceptance requirement.

For identical inputs, the comparison permits differences only in holding-only
local breadth values, `member_breadth_collected`, API-call facts, cost values,
evidence hashes, and generation timestamps. Candidate inclusion and exclusion,
candidate order, formal actions, risk results, holding decisions, and rendered
Dashboard fields and statuses must match. Any other difference fails
verification.

Then run the relevant trend-report tests, a frozen-data workflow check, and the
repository's final `make acceptance` gate. After `PASS`, deploy the exact
accepted SHA and verify PID, working directory, Git SHA, fresh logs, and HTTP
200 before handoff.

## Out of Scope

- Changing candidate breadth or any strategy/risk/execution rule.
- Removing other paid snapshot fields.
- Adding caches, refresh schedules, feature flags, or new abstractions.
- Changing weekend or holiday behavior; it already reuses the last trading-day
  report without paid Trend Animals refetches.
