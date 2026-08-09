# APR-Aware Polymarket Relation WebSocket Pool

**Date:** 2026-08-09

**Revised:** 2026-08-10

**Status:** Grill-approved design

**Scope:** Polymarket same-venue threshold relations only

## Goal

Reduce Hong Kong proxy traffic without replacing second-level monitoring with a
five-minute snapshot scanner. The real-time pool must be tied directly to the
existing `15%` simple annualized-yield entry gate instead of an arbitrary raw
net-edge threshold.

## Current Behavior

- The relation catalog is rediscovered every 24 hours.
- Every 60 seconds, all persisted relations are evaluated from refreshed public
  order books.
- Any relation with `net_edge >= -5%` enters the active pool.
- Every active relation publishes all four YES/NO token IDs from its two markets
  into the Polymarket WebSocket subscription map.
- A WebSocket update triggers a fresh REST book confirmation and re-evaluation
  of the affected relation.

The `-5%` activity pool currently contains roughly 296 relations and is the main
source of avoidable WebSocket traffic. It is a useful diagnostic funnel but is
too broad to be the real-time subscription policy.

## Decisions

### 1. Keep the 60-second activity scan

The activity scan remains at 60 seconds. A five-minute-only scanner can miss an
opportunity whose profitable window starts and ends between scans.

The 24-hour catalog scan, five-minute Top 20 market-universe refresh, and
Predict.fun/Polymarket cross-venue monitoring remain unchanged.

### 2. Select the real-time pool by distance to the 15% APR gate

For every relation that produces a valid activity candidate and a valid common
future resolution timestamp, calculate simple annualized yield using the
existing domain formula and constant:

```text
annualized_yield = minimum_profit / total_max_cost * 365 / remaining_days
target = MIN_THRESHOLD_ANNUALIZED_YIELD = 0.15
```

The real-time relation set is the union of:

1. every non-rejected relation whose current annualized yield is at least
   `15%`; and
2. up to 100 non-rejected relations below `15%` with the highest current
   annualized yield.

The second group is the APR prewarm pool. Ranking is deterministic:

1. annualized yield descending;
2. net edge descending;
3. relation ID ascending.

Do not add another raw net-edge or absolute-profit threshold for WebSocket
selection. The prewarm pool is the closest 100 among the existing valid
activity candidates, even when some of those 100 currently have negative or
small absolute profit. This change does not alter trading economics.

If fewer than 100 below-target relations have calculable APR, subscribe only the
available relations. Relations with pending or temporarily unavailable Codex
validation remain eligible until explicitly rejected. The 100-row prewarm limit
does not share a quota with relations at or above the entry gate; the at-target
set has the separate anomaly guard defined below.

Do not impose per-event quotas. The prewarm pool may concentrate in one or a few
events when those relations are economically closest to the APR gate. Shared
tokens are deduplicated by the subscription union.

Relations with missing, expired, or mismatched resolution timestamps remain in
background diagnostics but cannot enter the APR real-time pool. They cannot pass
the existing annualized-yield execution gate, so continuous subscription would
not make them actionable.

The existing `net_edge >= -5%` classification remains as the
`relations_within_5pct` diagnostic. It no longer decides WebSocket membership.

The activity scan takes materially longer than the execution freshness window,
so the APR ranking is an approximate subscription priority rather than an
atomic market snapshot. It must never be accepted as order evidence. The
existing REST confirmation and ten-second book-freshness gate remain decisive.

### 3. Subscribe only the two hedge buy legs

For each selected relation, publish only:

- `relation.buy_leg_a.token_id`; and
- `relation.buy_leg_b.token_id`.

Do not publish the unused complementary YES/NO tokens. The current opportunity
cost, depth, and emergency-unwind checks all use the two selected buy-leg order
books. Token IDs already needed by the Top 20 watcher or cross-venue monitor
remain deduplicated in the combined subscription set.

Rebuild the local token-to-relation map after every successful pool selection,
but create a new WebSocket subscription only when the final token union of Top
20, relation, and cross-venue monitoring actually changes. A relation-ID change
that leaves this token union unchanged must not reconnect the stream. When the
union changes, establish the new stream before closing the previous stream.

### 4. Preserve execution safety

WebSocket data remains a discovery trigger, not executable truth. On an update
to a subscribed token, the monitor continues to:

1. identify only affected relations;
2. refresh their public order books through REST;
3. rebuild the candidate;
4. require positive profit, safe unwind, fresh books, approved relation rules,
   fresh readiness, sufficient balance and allowance, and APR of at least 15%;
5. notify or expose an actionable opportunity only after every existing gate
   passes.

No execution, notification, LLM-validation, readiness, or account policy is
relaxed by this change.

## State and Observability

Keep the existing activity fields and their meanings, including
`relations_within_5pct` and `positive_candidates`.

The snapshot must make the new subscription policy auditable with these counts:

- `apr_target_relations`: calculable, non-rejected relations currently at or
  above 15%;
- `apr_target_limit`: `100`;
- `apr_prewarm_relations`: below-target relations selected into the top-100 pool;
- `apr_prewarm_limit`: `100`;
- `subscribed_relations`: actual relation count in the APR real-time pool;
- `relation_subscribed_tokens`: actual relation-only token count after
  deduplication.

The existing combined WebSocket state may continue to report the union of Top
20, relation, and cross-venue tokens. No Dashboard layout or copy change is part
of this work.

## Failure Behavior

- Build the next APR pool completely before replacing the current token map.
- If an activity scan fails, keep the last successful real-time pool and mark
  activity degraded; do not replace it with an empty subscription set.
- If a relation falls out of the prewarm top 100, it may be removed on the next
  successful 60-second scan.
- A relation at or above 15% must not be removed by the prewarm size cap.
- Existing Codex or deterministic rejection continues to exclude a relation
  from WebSocket subscription.
- If more than 100 non-rejected relations simultaneously report APR of at least
  15%, treat the scan as anomalous: keep the last successful subscription pool,
  mark relation activity degraded, and fail closed for new relation orders until
  a later normal scan succeeds.
- Pending and temporarily unavailable validation states are not rejection and
  remain eligible for the pool.

## Expected Subscription Bound

Before token deduplication, the relation layer is bounded by:

```text
2 * (apr_target_relations + apr_prewarm_relations)
```

During a normal scan, both `apr_target_relations` and
`apr_prewarm_relations` are at most 100. The relation layer therefore has a
normal pre-deduplication ceiling of 400 token references. The Top 20 and
cross-venue token sets are unaffected, so proxy behavior must still be observed
after deployment rather than inferred from token count alone. Completion does
not promise a fixed traffic-reduction percentage.

## Verification

Automated checks must cover:

1. all calculable, non-rejected relations at or above 15% are selected while the
   at-target anomaly limit is not exceeded;
2. up to 100 closest below-target relations are selected with deterministic
   tie-breaking, and exactly 100 are selected when enough are eligible;
3. remaining duration changes APR ranking for otherwise comparable relations;
4. missing or mismatched resolution timestamps are not selected;
5. rejected relations are not selected;
6. relation subscriptions contain only the two buy-leg token IDs;
7. a failed scan preserves the last successful subscription pool;
8. a WebSocket tick still performs REST confirmation and all existing admission
   gates;
9. unchanged combined token unions do not resubscribe even when relation IDs
   rotate;
10. more than 100 at-target relations degrade the scan, preserve the previous
    pool, and block new relation admission from the anomalous scan;
11. pending validation remains eligible while explicit rejection does not;
12. APR ranking may concentrate within one event and does not add an absolute
    profit gate.

Before completion:

- run focused relation-monitor tests;
- run the affected monitor workflow directly and inspect its snapshot;
- restart any long-running process holding old code;
- verify fresh PID, working directory, accepted Git SHA, logs, and HTTP 200;
- run `make acceptance` only as the final Dashboard gate;
- observe the post-deploy Hong Kong route against its prior behavior and report
  whether the optimization worked, without a numerical acceptance target.

## Non-Goals

- Changing the 15% APR entry threshold.
- Changing trade size, balance, allowance, unwind, or readiness gates.
- Changing relation discovery or Codex rule validation.
- Changing the 13-pair cross-venue monitor.
- Changing Dashboard layout, copy, or interaction.
- Adding a runtime configuration or Dashboard control for the prewarm limit.
- Changing the 60-second REST activity scan, its concurrency, or its book scope.
- Enforcing a numerical traffic-reduction acceptance target.
- Guaranteeing detection of a relation that jumps from outside the APR prewarm
  pool to profitable and disappears within one 60-second scan interval. Avoiding
  that risk entirely would require retaining broad real-time subscriptions.
