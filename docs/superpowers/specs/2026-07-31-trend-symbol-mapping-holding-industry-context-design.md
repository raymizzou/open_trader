# Trend Symbol Mapping and Holding Industry Context Fix

**Date:** 2026-07-31
**Status:** Approved in conversation
**Markets:** CN, HK, US

## Problem

The trend-report pipeline currently treats Futu symbols and Trend Animals
symbols as two formats of one identifier. Its successful symbol cache stores
only a normalized code and `tmId`, so it loses the exact identifier used by
each provider.

That assumption is false. For example:

- Futu identifies the CN ETF as `SH.515450`.
- Trend Animals identifies the same ETF as `515450`.
- Trend Animals assigns it `tmId=328879`.

The existing conversion produces `515450.SH`, which returns no Trend Animals
search result. The failed lookup leaves the real holding as `MANUAL_REVIEW`
even though Trend Animals has the ETF.

Separately, `collect_industry_contexts(...)` collects breadth only for
industries belonging to hard-gate-eligible buy candidates. Industries that
belong only to current real or simulated holdings are omitted from the frozen
report, so the Dashboard cannot display them.

## Explicit Provider Symbol Mapping

Treat provider identifiers as separate facts joined by an explicit mapping.
Each complete mapping record contains:

```json
{
  "schema_version": "open_trader.trend_symbol_mapping.v1",
  "market": "CN",
  "futu_symbol": "SH.515450",
  "trend_animals_symbol": "515450",
  "trend_animals_tm_id": 328879,
  "asset": "ETF基金"
}
```

The same rule applies to stocks and to every supported market. For example,
the known CN stock mapping is:

```text
Futu SH.600036 <-> Trend Animals 600036.SH <-> tmId 308052
```

The mapping record, rather than a formatting function, is the authority for
subsequent provider calls.

### Storage and indexes

Reuse the existing per-symbol JSON cache pattern. Store one atomic mapping
record at
`trend_animals/cache/symbol_mappings/<MARKET>/<FUTU_SYMBOL>.json`; for example,
the ETF record is `symbol_mappings/CN/SH.515450.json`. Do not add a database or
dependency.

Loading mapping records builds two in-memory indexes:

- `(market, futu_symbol) -> mapping`
- `(market, trend_animals_symbol) -> mapping`

Both directions resolve to the same complete record. A duplicate key with
different provider identifiers or `tmId` is a conflict and must fail closed;
the cache must never silently overwrite either side.

Legacy `symbols/*.json` entries containing only `symbol` and `tmId` are not
complete mappings and cannot be the identity authority. A caller may reuse the
legacy `tmId` for one snapshot request; only a matching authoritative snapshot
row can upgrade it to a complete mapping. Otherwise the entry remains
unresolved and is not copied into the new cache.

### Learning mappings

Whenever a market-aware report path accepts a Trend Animals component, search,
or snapshot row, it records the exact returned `tickerSymbol`, `tmId`, and
asset beside the canonical Futu symbol. Candidate-pool ingestion therefore
learns mappings proactively, so a symbol later becoming a real or simulated
holding normally requires no search.

When a Futu holding has neither a complete mapping nor an upgradeable legacy
`tmId`, perform one market-aware discovery request. The one discovery key is:

- CN: the six-digit security code without an exchange suffix, such as
  `515450` or `600036`;
- HK: the established Trend Animals search form without Futu's leading zero,
  such as `3033.HK` for `HK.03033`;
- US: the ticker without `US.`, with a dot represented as an underscore, such
  as `BRK_B`.

These are discovery keys only. The exact `tickerSymbol` returned by Trend
Animals is authoritative. Accept the result only when exactly one allowed-asset
row maps back to the requested canonical Futu symbol, then persist the complete
mapping.

There is no alternate-format retry. If the single discovery cannot prove one
mapping, retain the holding and mark its trend signal unavailable. Do not try a
second code spelling and do not guess a `tmId`.

The already verified `SH.515450 <-> 515450 <-> 328879` record is seeded into
the complete mapping cache before regenerating the current report. The stale
dated miss for `515450` is removed as obsolete cache data; subsequent runs read
the complete mapping before considering any negative lookup cache.

### Provider use

- Futu requests always use `futu_symbol`.
- Trend Animals identity and audit output always preserve
  `trend_animals_symbol`.
- Trend Animals snapshot requests use the mapped `trend_animals_tm_id`.
- Rows returned later must agree with the cached mapping. A disagreement is a
  visible mapping conflict, not an automatic cache rewrite.

## Holding Industry Context

The frozen `industry_contexts` collection uses the union of:

1. industries belonging to hard-gate-eligible buy candidates;
2. industries present in resolved simulated holding snapshots;
3. industries present in resolved real holding snapshots.

Deduplicate by `industry_tm_id`. Use the existing industry component, member,
state, history, and calculation flow for every selected industry. Do not
calculate industry breadth in the browser.

Candidate ranking and `industry_context_status` remain scoped to eligible
candidate industries only. Adding a holding-only industry cannot change
candidate order, entry eligibility, formal actions, position sizing, risk
limits, Kelly state, or strategy version.

Every collected holding industry remains visible in the frozen report. If a
holding-only industry context is incomplete, render that row with the existing
unavailable values. The Dashboard's report-wide ordering fallback message must
continue to follow `industry_context_status`; an invalid holding-only row must
not falsely claim that candidate ordering fell back.

A resolved stock or ETF snapshot without an `industry_tm_id` remains visible in
the holding table but does not invent an industry mapping or synthetic context.

## Data Flow

```text
Futu holding symbol
  -> complete provider mapping
  -> Trend Animals tmId snapshot
  -> validated holding snapshot
  -> candidate + simulated + real industry union
  -> frozen report industry_contexts
  -> Dashboard rendering
```

Candidate and snapshot rows also flow back into the mapping cache after exact
validation, allowing later holdings to reuse known identities without another
search.

## Compatibility and Safety

- Existing frozen reports are not rewritten.
- Existing report and Dashboard schemas remain valid; `industry_contexts`
  contains more rows of its current shape.
- Unresolved real holdings remain visible as `MANUAL_REVIEW`.
- No provider identifier is inferred from another after a complete mapping is
  stored.
- No symbol-specific production branch is added for `515450`; it is migration
  data exercising the general mapping contract.
- CN, HK, and US use the same mapping and holding-industry semantics.

## Verification

Use red-green tests to prove:

1. A complete record round-trips Futu-to-Trend-Animals and
   Trend-Animals-to-Futu.
2. `SH.515450 <-> 515450 <-> 328879` resolves from cache without a search.
3. A newly discovered mapping performs one request, persists both provider
   codes, and performs no request on the next lookup.
4. Candidate/component ingestion can teach a mapping before the symbol becomes
   a holding.
5. Malformed, duplicate, or conflicting mappings fail closed without
   overwriting the prior record.
6. Existing CN stock, HK stock/ETF, and US stock/ETF resolution remains exact.
7. Industry collection contains the union of eligible candidate, simulated
   holding, and real holding industries without duplicates.
8. A holding-only invalid context does not change candidate ordering status or
   produce a false report-wide fallback message.
9. A missing holding industry ID does not create a synthetic context.
10. Current CN report regeneration resolves `515450` trend data and includes
    the bank and electric-power holding industries.

After focused tests, regenerate CN, HK, and US reports in no-submit/revision
mode and compare formal actions, strategy identity, risk facts, and candidate
ordering with the pre-change reports. Then run `make acceptance` once as the
final gate, redeploy the exact accepted SHA, verify live process ownership and
fresh logs, and capture the affected Dashboard view.

## Out of Scope

- Retry, backoff, or alternate symbol spellings
- A new database, dependency, or symbol service
- ETF-to-industry inference when Trend Animals provides no industry
- Entry, exit, ranking, sizing, risk, Kelly, or execution changes
- Dashboard layout, columns, copy, or interaction changes
- Rewriting historical frozen reports
