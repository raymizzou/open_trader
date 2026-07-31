# Trend Symbol Mapping, Simulated Execution, and Holding Industry Context

**Date:** 2026-07-31

**Status:** Approved after grill

**Markets:** CN, HK, US

## Problem

The trend pipeline currently treats Futu and Trend Animals identifiers as
convertible formats of one code. The successful lookup cache stores only a
normalized symbol and `tmId`, so it loses the exact identifier returned by
each provider.

That assumption fails for the CN ETF in the current real holdings:

```text
Futu:         SH.515450
Trend query:  515450
Trend result: 515450
Trend tmId:   328879
```

The current conversion queries `515450.SH`, gets no result, and leaves 红利50
as `MANUAL_REVIEW`. The same conversion is also used when formal trend actions
are turned into Futu simulated orders, so execution must use the verified Futu
identifier rather than reconstructing one from a short report symbol.

Separately, the frozen `industry_contexts` list contains only industries for
eligible buy candidates. Industries represented only by current simulated or
real holdings are absent from the Dashboard.

## Complete Provider Identity

A complete identity record is an immutable fact joining all three provider
keys:

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

The same record preserves a stock's different provider forms, for example:

```text
Futu SH.600036 <-> Trend Animals 600036.SH <-> tmId 308052
```

Store one atomic JSON record at:

```text
trend_animals/cache/symbol_mappings/<MARKET>/<FUTU_SYMBOL>.json
```

Loading records builds three market-scoped indexes:

- `(market, futu_symbol) -> mapping`
- `(market, trend_animals_symbol) -> mapping`
- `(market, trend_animals_tm_id) -> mapping`

Each key must identify the same record. A disagreement on any of the three
keys is a conflict. The prior record is preserved, the affected symbol is
marked `MANUAL_REVIEW`, and its reason is displayed as `趋势代码映射异常`.
Nothing is silently overwritten.

Successful mappings do not expire. They survive trading dates, report
revisions, controller restarts, and deployments through the shared runtime
data directory. There is no symbol-specific branch or tracked mapping catalog.

## How a Mapping Is Established

A provider row cannot create the other provider's identifier by formatting.
A mapping is written only after both sides have independently succeeded.

### Futu holding to Trend Animals

For an unmapped Futu holding:

1. Preserve the exact Futu account code, such as `SH.515450`.
2. Derive one discovery keyword from the security token. The keyword is not
   treated as the Trend Animals canonical code.
3. Call `searchTicker` once.
4. Keep results whose market, security token, and allowed asset type agree.
5. Require exactly one identity.
6. Preserve the exact returned `tickerSymbol` and `tmId`, then write the
   complete record.

The one discovery keyword per market is:

- CN: the six-digit token, for example `515450` or `600036`;
- HK: the existing Trend search token without Futu's leading zero, for example
  `3033.HK` for `HK.03033`;
- US: the ticker without `US.`, with a dot represented as an underscore, for
  example `BRK_B`.

There is no alternate spelling, backoff, or retry.

### Trend candidate to Futu simulated order

A Trend Animals candidate already carries the exact Trend code and `tmId`.
The report path currently derives one Futu code and must successfully request
Futu daily K-line data before the candidate can produce ATR and a BUY. Reuse
that existing successful Futu call as the independent Futu confirmation; do
not add another quote request.

Only after that Futu success may the report path store the complete identity.
If the mapping is missing or conflicts, the candidate may remain visible but
cannot produce a simulated BUY. Record `symbol_mapping_unavailable` in the
existing skip/reason surface. Never fall back to string reconstruction for a
new order.

### Legacy cache upgrade

Legacy `symbols/*.json` files contain only a known Futu security token and
`tmId`. They are not complete mappings. They may be upgraded without a new
search only when an authoritative current snapshot returns the same `tmId`, a
market-valid exact `tickerSymbol`, and an allowed asset. That combination
provides the previously known Futu side and the returned Trend side without
guessing.

An arbitrary candidate, component, or snapshot row without an independently
known Futu side cannot create a complete mapping. It may only validate an
existing mapping.

## Permanent Failure Cache

A failed discovery creates no mapping. Cache the failure by:

```text
market + futu_symbol + exact discovery query + discovery rule version
```

The failure is permanent for that key. Controller restarts, later trading
dates, and report revisions must not repeat the request. A new request is
allowed only when an operator clears the failure, supplies a verified mapping,
or the discovery rule version changes.

The complete success mapping is checked before the failure cache. The old
dated miss files are legacy data and are ignored by the new permanent-miss
contract.

The already observed successful result
`SH.515450 <-> 515450 <-> 328879 <-> ETF基金` is initialized through the same
mapping validator before deployment. Initialization performs no network call.

## Futu Simulated Execution

New reports freeze the exact `futu_symbol` in every formal action:

- BUY: the verified Futu side of the candidate mapping;
- SELL: the exact code returned by the Futu simulated account position.

The report metadata marks that the new mapping contract is active. The
simulated executor uses the frozen field for quote lookup, action keys, audit
events, and `place_order`. A new-contract report without a valid
`futu_symbol` fails closed.

Historical frozen reports without the mapping-contract marker retain the
existing conversion fallback so immutable execution history remains readable
and reconcilable. Real broker holdings remain read-only: resolving `515450`
may change its advisory decision from `MANUAL_REVIEW` to the computed holding
signal, but it does not add the real holding to `formal_actions`.

## Holding Industry Context

The frozen context list is the deduplicated union of:

1. hard-gate-eligible candidate industries;
2. resolved simulated-holding industries;
3. resolved real-holding industries.

Deduplicate by `industry_tm_id`. A holding snapshot without an explicit
`industry_tm_id` remains visible but creates no guessed or synthetic industry.

Use the existing component, member, state, history, and calculation flow.
Candidate ranking and `industry_context_status` continue to inspect eligible
candidate industries only. Holding-only contexts are display data: they never
change candidate order, entry rules, concentration gates, size, risk, Kelly
state, or strategy version.

A holding-only query failure or incomplete context cannot fail the whole
report. Keep the industry row and show its existing unavailable details. It
must not trigger the report-wide `已回退旧排序` message. Candidate-industry
errors retain their existing strict behavior.

Sort the frozen `industry_contexts` list by:

1. valid numeric `strength`, descending;
2. missing or invalid strength last;
3. `industry_tm_id`, ascending, as the stable tie-breaker.

The Dashboard renders this frozen order and does not sort again. The broader
union intentionally adds daily first-run Trend Animals member/state cost;
same-day revisions continue to use the existing response cache, and reports
continue to show estimated and actual balance usage.

## Data Flows

```text
Futu holding
  -> complete mapping or one permanent discovery decision
  -> Trend tmId snapshot
  -> real/simulated holding evaluation

Trend candidate
  -> existing required Futu K-line success
  -> complete mapping
  -> BUY action with frozen futu_symbol
  -> Futu simulated order

eligible candidate industries + simulated holding industries + real holding industries
  -> frozen contexts sorted by strength
  -> Dashboard renders frozen order
```

## Compatibility and Change Boundary

- CN, HK, and US share the same rules.
- Existing historical reports are not rewritten.
- New formal actions add `futu_symbol`; historical actions use the legacy
  fallback only when the new mapping marker is absent.
- No database, dependency, background symbol service, retry scheduler, or
  manual mapping UI is added.
- No scoring formula, threshold, ranking rule, sizing rule, risk rule, Kelly
  rule, or strategy version changes.
- The intentional behavior changes are: verified simulated-order routing,
  fail-closed BUY when mapping is unavailable, resolved real-holding advice,
  added holding-industry rows, strength-sorted context display, and explicit
  per-symbol mapping anomaly copy.
- Dashboard columns, layout, and interaction remain unchanged.

## Verification

Red-green tests must prove:

1. All three identity keys round-trip to one complete record.
2. Conflicts on Futu code, Trend code, or `tmId` preserve the prior record and
   produce a per-symbol mapping anomaly.
3. One successful discovery persists the exact returned Trend code; a second
   client performs no request.
4. One failed discovery creates a permanent exact-query miss; later dates do
   not retry it.
5. The verified `515450` mapping initializes and loads without network access.
6. Legacy Futu/`tmId` data upgrades only after a matching authoritative
   snapshot.
7. Candidate rows alone cannot invent Futu identity; a successful existing
   Futu K-line request can complete it.
8. New BUY and SELL actions freeze the exact Futu code, and the simulated
   executor submits that field across CN/HK/US.
9. A missing/conflicting mapping leaves a candidate visible but prevents BUY.
10. Industry contexts contain the candidate/simulated/real union, sort by
    strength, and put missing strength last.
11. A holding-only error stays row-local and does not change candidate status
    or produce a false global fallback message.
12. A missing holding industry ID creates no synthetic context.
13. The current CN revision resolves `515450`, contains 银行 `339103` and 电力
    `621693`, and preserves formal action semantics apart from frozen
    `futu_symbol` fields.

After focused and full tests, regenerate current CN/HK/US revisions from the
candidate controllers. Compare strategy identity, candidate order, side,
symbol, quantity, risk, and Kelly facts. Allow the expected additive
`futu_symbol`, resolved real-holding decision, context membership/order,
billing/cache facts, and revision hashes. Then run `make acceptance` as the
final gate, redeploy the exact accepted SHA, verify live ownership/logs, and
capture the affected Dashboard view.

## Out of Scope

- Alternate code spellings or automatic retries
- Automatic conflict repair or overwrite
- Real-money order execution changes
- ETF-to-industry inference
- New Dashboard columns, layout, or interaction
- Historical report rewrites
