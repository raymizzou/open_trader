# Standard Polymarket WebSocket Eligibility Gate

**Date:** 2026-08-10

**Status:** User-approved design

**Scope:** Standard same-market YES/NO watcher inside the Top 20 event universe

## Goal

Reduce Hong Kong proxy traffic while preserving second-level discovery for every
opportunity that the current standard YES/NO execution policy can admit.

## Current Behavior

- Every five minutes, the watcher fetches the 20 highest-volume active events.
- Every active, open, order-accepting binary market with an enabled order book
  remains visible in the Top 20 snapshot.
- Both YES and NO token IDs from every visible market enter the WebSocket
  subscription, before fee and negative-risk eligibility is considered.
- A WebSocket update is only a discovery trigger. The watcher rereads both books
  through REST before exposing an actionable opportunity.
- REST confirmation rejects a market unless `fees_enabled is False`, and also
  rejects a market when `neg_risk is True`.

As a result, markets that cannot pass the current execution policy still
generate continuous WebSocket traffic.

## Decisions

### 1. Separate the display universe from the real-time standard pool

Keep every currently normalized market in the Top 20 event snapshot. The
Dashboard event and market universe does not shrink.

Publish a standard market's YES and NO token IDs into the WebSocket map only
when the existing metadata proves both conditions:

```text
fees_enabled is False
neg_risk is not True
```

This predicate must be the single standard-market WebSocket admission rule
wherever market metadata can rebuild or update the token map.

Do not add a profit-distance, liquidity, book-availability, volume, or market
count cap. An eligible market stays subscribed even when it currently has no
threshold candidate or its initial book read fails, so a later book update can
still trigger second-level discovery.

### 2. Keep executable discovery second-level

For a subscribed standard market, preserve the existing flow:

1. receive a WebSocket update for either token;
2. identify the affected market;
3. reread both YES and NO books through REST;
4. rebuild the pair intent;
5. require all existing freshness, fee, negative-risk, readiness, balance,
   allowance, size, tick, and profit gates before exposing an opportunity.

WebSocket data remains a trigger and never becomes executable truth.

The five-minute timer remains the Top 20 universe and metadata refresh timer; it
does not replace WebSocket quote monitoring for eligible markets.

### 3. Promote and demote from refreshed metadata

Each successful universe refresh rebuilds the display universe and the eligible
standard token map from the same normalized market rows.

- A market newly proven eligible enters the real-time pool with both tokens.
- A market no longer proven eligible leaves the standard real-time pool.
- A market with missing or ambiguous fee metadata stays visible but does not
  enter the real-time pool, matching the current fail-closed execution policy.
- The combined WebSocket subscription is recreated only when the final union of
  standard, relation, and cross-venue tokens changes.

Any targeted metadata-refresh path that changes standard market token ownership
must apply the same predicate and mark the subscription dirty only when the
combined token union changes.

### 4. Preserve the other subscription layers

This change does not alter:

- APR-aware relation selection or its two buy-leg subscriptions;
- the 60-second relation activity scan;
- cross-venue token selection;
- the 15% annualized-yield entry gate;
- notification, execution, readiness, or account policy.

The existing set union continues to deduplicate tokens shared by multiple
layers.

## Capability Boundary

The current execution policy cannot admit fee-unverified, fee-enabled, or
negative-risk standard markets. Removing their continuous book subscriptions
therefore does not remove an opportunity the current policy could execute.

Metadata and Top 20 membership already refresh on the five-minute universe
cycle. A market whose metadata changes between refreshes cannot pass the current
cached metadata gate until that refresh, even when its books remain subscribed.
This design does not add a new eligibility delay.

The accepted trade-off is limited to monitor-only display freshness: an
ineligible market's diagnostic book fields, such as gross upper bound, refresh
with the universe scan instead of every WebSocket tick. This work does not
promise second-level diagnostic prices for markets the strategy cannot execute.

## Failure Behavior

- Do not let the new eligibility gate clear the prior standard token map when
  fetching or normalizing the replacement universe fails.
- A book-confirmation failure does not demote an otherwise metadata-eligible
  market.
- Unknown fee state remains fail-closed and display-only.
- If replacement WebSocket creation fails, keep the previous stream and leave
  the subscription dirty for the existing retry path.
- Establish a replacement WebSocket stream before closing the previous stream,
  preserving the existing handover behavior.

## Observability

Add only the missing subscription audit count; do not add a Dashboard surface:

- the Top 20 snapshot continues to expose the full event and market universe;
- WebSocket state exposes `standard_subscribed_tokens`, the size of the gated
  standard token map before union with relation and cross-venue tokens;
- relation activity continues to expose relation-only subscription counts;
- WebSocket state continues to expose the deduplicated combined subscription.

No Dashboard layout, copy, or interaction change is part of this work.

## Verification

Automated checks must cover:

1. fee-free, non-negative-risk markets publish both tokens;
2. fee-enabled, fee-unknown, and negative-risk markets remain visible but do not
   publish standard WebSocket tokens;
3. `no_threshold_candidate` and temporarily unavailable-book markets remain
   subscribed when their metadata is eligible;
4. a WebSocket update for an eligible market still performs paired REST
   confirmation and all existing admission checks;
5. a successful metadata refresh promotes and demotes markets correctly;
6. an unchanged combined token union does not reconnect the stream;
7. relation and cross-venue tokens remain in the combined subscription;
8. a failed universe fetch does not clear the last successful subscription;
9. monitor-only rows remain present and receive their periodic diagnostic book
   refresh;
10. WebSocket state reports the gated standard token count separately from the
    combined subscription count.

Before completion:

- run focused monitor tests;
- run the affected monitor workflow directly and inspect the live snapshot;
- restart any long-running process holding old code;
- verify fresh PID, working directory, accepted Git SHA, logs, and HTTP 200;
- run `make acceptance` only as the final Dashboard gate;
- redeploy the exact accepted SHA;
- observe the Hong Kong route after deployment without setting a numerical
  traffic-reduction acceptance target.

## Non-Goals

- Changing the five-minute Top 20 universe refresh interval.
- Adding a faster eligibility poll in the initial implementation.
- Subscribing only one side of a standard YES/NO market.
- Selecting standard subscriptions by current profitability.
- Supporting execution in fee-enabled, fee-unverified, or negative-risk
  standard markets.
- Changing relation discovery, APR selection, or cross-venue monitoring.
- Adding configuration, a Dashboard control, or a new persistence model.
- Guaranteeing discovery outside the existing Top 20 universe.
