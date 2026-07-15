# Decision Plan K-line Request Cache Design

## Problem

The daily US premarket run requests history through the calendar run date. Before
the US session completes, Futu correctly returns bars only through the previous
trading day. `_RangeCachingProvider` records the returned bar dates as cache
coverage, so repeated requests ending on the run date miss the cache and exhaust
Futu's limit of 60 history requests per 30 seconds.

## Scope

Fix request reuse inside one decision-plan generation run. Do not change failure
notification counters, add retries, add rate limiting, or add dependencies.

## Design

When `_RangeCachingProvider` successfully fetches a request, record the requested
`start` and `end` as the cache coverage. A later request for the same symbol whose
range is contained by that requested coverage reuses the returned bars and
filters them to the later range.

This is safe because decision-plan generation is a single batch that should use
one consistent market-data snapshot. A new generator run creates a new provider
and fetches fresh data.

## Error Handling

Failed or empty responses are not cached, preserving the existing behavior for
unavailable history. Requests outside cached coverage still reach the underlying
provider and preserve its errors.

## Testing and Verification

Add a regression test where a request ends on July 15 but the provider returns
bars only through July 14. Repeating the same request and requesting a narrower
range must make one underlying provider call and return correctly filtered bars.

Verify the focused test red before the implementation and green afterward, then
run the relevant test file, full automated tests, the real dry-run workflow,
inspect the Futu/OpenD request log, and run `make acceptance`.
