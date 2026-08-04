# Account Current Valuation Design

**Status:** Approved after grill; Issue #21 merged and implementation is planned
**Date:** 2026-08-04

## Context

Issue #21 moves browser Account reads to
`GET /api/v1/account/snapshot` through Frontend Gateway. This follow-up fixes two
valuation gaps after that cutover:

- quoteable Eastmoney and Phillips real positions currently keep statement or
  account-close prices because the quote universe excludes statement-only
  holdings and the projection only overlays quotes for live broker sources;
- `market_value_usd` intentionally remains empty for non-USD positions, while
  Trend simulated positions publish no USD equivalent at all.

Direct OpenD reads already return prices for the affected CN and HK symbols.
The missing data is therefore an ownership and projection defect, not an OpenD
capability gap.

This work starts from the `main` commit that contains the accepted Issue #21
cutover. It does not modify or invalidate Issue #21's frozen baseline and
cutover checkpoints.

## Goals

- Use accepted OpenD quotes for every quoteable US, HK, and CN real position,
  regardless of whether its broker source is live or statement.
- Publish complete USD and HKD display values for every quoteable US, HK, and
  CN real position.
- Publish the same complete display valuation shape for the three Trend
  simulated accounts.
- Keep Account Sync Worker as the sole Account writer, Account API as the
  Account read model, Frontend Gateway as a transparent router, and the browser
  as a renderer.
- Remain compatible with the frozen Account v1 contract and its independent
  rollback behavior.

## Non-goals

- No Legacy Dashboard Account-price patch or browser call to `/api/quotes`.
- No Gateway aggregation, transformation, valuation, FX calculation, or
  fallback.
- No Account v2 and no semantic change to an existing v1 field.
- No new quote endpoint, daemon, database, cache, queue, dependency, WebSocket,
  or configurable valuation engine.
- No migration of Trend simulated positions into Account Module.
- No Dashboard layout, strategy, report, allocation, execution, refresh-cadence,
  or broker-adapter redesign. The only interaction change is the approved
  transient valuation-update highlight described below.
- No change to cash-balance valuation.
- No new valuation object or USD display value for `cash` or
  `money_market_fund` positions.

## Chosen Approach

The Account Sync Worker derives its quote universe from accepted Account
positions, applies each accepted quote before producing the Account projection,
and publishes one complete valuation per position. Account API validates and
serves that publication unchanged. The browser renders the owner-published
values.

Trend remains the owner of simulated positions. Its existing OpenD-backed
simulate-position service publishes the same small valuation object from its
account snapshot and existing FX inputs.

### Rejected alternatives

- Patching Legacy or restoring the browser quote loop would recreate the
  Account authority that Issue #21 removes.
- Repurposing `market_value_usd` as a cross-currency equivalent would change a
  frozen v1 field whose contract says it is empty when not applicable.
- Account v2 is unnecessary because v1 permits optional additive fields.
- Client-side FX would make displayed Account facts depend on browser state and
  could disagree with Account summaries and weights.

## Account Ownership And Data Flow

```text
accepted Account positions --+
                              +--> Account Sync Worker --> publications
OpenD quotes -----------------+           |
accepted FX ------------------+           v
                                      Account API
                                           |
                                  transparent Gateway
                                           |
                                        browser
```

The Worker remains the only process that calls account/quote adapters and
writes Account publications. Account API does not call OpenD, calculate FX, or
repair values. Gateway does not inspect the snapshot body. Browser Account rows
do not calculate price, market value, or currency conversion.

## Quote Universe

The Worker builds the quote universe only from the current accepted Account
positions. `data/latest/portfolio.csv` is not a production fallback and the
quote service requires the Account-derived universe explicitly. Positions are
deduplicated by canonical Futu symbol before an OpenD request and mapped back
to every matching broker position afterward.

A position is quoteable when all of these hold:

- market is `US`, `HK`, or `CN`;
- asset class is `stock`, `etf`, `fund`, `option`, or `unknown`;
- quantity is non-zero; and
- market and symbol form a valid canonical Futu symbol.

Broker source kind is not an exclusion. In particular, Eastmoney and Phillips
positions are eligible. `cash` and `money_market_fund` remain outside both the
OpenD quote request and the new `current_valuation` object; their existing flat
account or statement facts remain unchanged.

Invalid or unsupported identities are explicit skipped rows in quote
diagnostics. They are never synthesized or silently converted into a different
symbol.

## Price Precedence

For a quoteable position, one quote source supplies all current
price-dependent fields:

1. a valid accepted quote from the current OpenD refresh;
2. the last accepted quote retained by the existing quote publication.

A valid OpenD quote wins regardless of the position's broker source kind. Its
price, session kind, and quote time replace the position's displayed
`last_price`, `price_kind`, and `price_as_of`; native market value is recalculated
with the existing quantity and instrument-multiplier rules. Non-quoteable
positions keep their truthful accepted `account_snapshot` or `statement` facts.

For HK and CN, `price_as_of` uses OpenD's actual `update_time`, normalized to an
offset-aware market timestamp. It uses the quote refresh time only when OpenD
does not provide an update time. This prevents a closed-market price from being
stamped as if it traded at the later polling time.

Unknown, non-finite, zero, or negative quote prices do not overwrite an
accepted value. Zero is never used to mean unavailable.

Using an accepted OpenD quote for a statement position does not change v1 field
semantics: the existing fields already represent the selected price and its
declared kind. It corrects which eligible positions participate in the existing
quote overlay. Account summaries, broker summaries, weights, P/L, and existing
HKD values continue to be derived from those same selected position values.

## Additive Account v1 Field

Each quoteable `US`, `HK`, or `CN` position may add this optional v1 object:

```json
{
  "current_valuation": {
    "price": "40.36",
    "price_kind": "live",
    "price_as_of": "2026-08-04T10:30:05+08:00",
    "market_value_usd": "558.43",
    "market_value_hkd": "4356.34"
  }
}
```

The object is optional at the public v1 compatibility boundary so the browser
can continue reading the accepted Issue #21 Account release during an
independent rollback. In a release that implements this feature, publication is
all-or-nothing: every in-scope position contains the object and all five child
fields are non-empty and valid.

The child fields mean:

| Field | Meaning |
| --- | --- |
| `price` | selected native-currency unit price |
| `price_kind` | existing Account price-kind vocabulary |
| `price_as_of` | selected quote, account-snapshot, or statement time |
| `market_value_usd` | selected native market value converted to USD |
| `market_value_hkd` | selected native market value converted to HKD |

When present, `price`, `price_kind`, `price_as_of`, and `market_value_hkd` must
equal their existing position-field counterparts. Existing
`market_value_usd` keeps its frozen meaning and remains empty for non-USD
positions; the nested value is the new cross-currency display equivalent.

Because the object is part of each position, it participates in the existing
`account_generation`, `snapshot_generation`, response bytes, and ETag rules.
No new generation is added.

## FX And Valuation Rules

The Worker reuses its accepted currency-to-HKD rates and existing deterministic
rounding. It does not add an FX provider. Eastmoney and Phillips currently
publish no FX rows, so their existing deterministic statement fallback remains
`USD/HKD = 7.8`; Futu and Tiger continue using their accepted account rates.

For the selected native market value:

```text
market_value_hkd = native_market_value * native_currency_to_hkd
market_value_usd = market_value_hkd / usd_to_hkd
```

`HKD` converts to HKD at `1`. The accepted USD-to-HKD rate is required even for
a non-USD position because it is the denominator of the USD equivalent. Values
are finite decimal strings rounded with the existing money rule.

USD and HKD outputs are rounded independently under that rule. A USD-native
position may therefore differ from a round-trip HKD-to-USD calculation by a
small currency-unit rounding amount; no additional equality invariant is
introduced.

If the selected quote changes native market value, existing Account summaries,
weights, and P/L use that same value. The nested HKD result must therefore equal
the position's existing `market_value_hkd`; the browser cannot display one
valuation while Account totals use another.

## Truthful Failure And Stale Behavior

- If a current quote refresh fails, the existing retained quote publication
  and Account stale rules remain authoritative.
- If a required OpenD quote is unavailable but a previously accepted quote
  exists, the existing retained-publication path serves it as stale.
- If no accepted quote exists for a required instrument, Account v1 keeps its
  frozen fail-closed behavior and returns `503`; an account snapshot or
  statement price is not promoted to a current quote.
- If an in-scope position lacks a valid selected price, native value, required
  FX, or either converted value, the Worker does not replace the last accepted
  complete Account projection.
- With a previous complete publication, Account API serves that publication as
  `200 stale` according to the existing contract. Without one, it returns the
  existing contract-shaped `503`; it does not emit a partially blank `200`.
- Retained accepted quotes gain no new hard age limit in this follow-up. The
  existing stale status and action-disable behavior remain authoritative.
- When a newly accepted position appears before its first quote refresh, a
  brief fail-closed `503` window is allowed. The browser retains its previous
  snapshot and the next successful quote cycle restores the Account response;
  this follow-up does not add cross-file transactional publication.
- Error reasons contain stable machine codes and no upstream response, account
  identifier, credential, or absolute path.

## Trend Simulated Positions

Trend's existing simulate-position service continues reading each dedicated
OpenD simulated account. Each non-cash simulated position adds the same
`current_valuation` object:

- `price` comes from the OpenD account snapshot's accepted position price;
- `price_kind` is `account_snapshot`;
- `price_as_of` is the simulated snapshot sync time;
- HKD uses the service's existing currency-to-HKD input; and
- USD is derived through the same USD-to-HKD denominator rule.

The Account Worker and Account API do not read, store, or serve simulated
positions. Sharing the JSON shape does not create a cross-domain service.

The simulated-position response is also all-or-nothing. Missing or invalid
price, native value, or FX makes that broker's simulated response unavailable
through its existing error state instead of returning rows with blank USD or
HKD values.

## Browser Behavior And Compatibility

For both real and simulated rows, the browser prefers
`current_valuation.price`, `market_value_usd`, and `market_value_hkd`. It does no
FX or market-value calculation.

If the optional object is absent because the Account pair was independently
rolled back to the accepted Issue #21 release, the browser falls back to the
existing flat fields. Missing optional data is a compatibility state, not a
reason to query Legacy, `/api/quotes`, or raw files.

Within a feature-enabled Account release, partial presence is rejected before
publication, so a normal browser refresh cannot intermittently lose one of the
two display currencies.

The existing Account-table column remains labelled `实时价`. No per-row OpenD
badge is added. Provenance is enforced by the Account contract, accepted quote
publication, quote time, and release acceptance rather than extra UI copy.

When a successful Account poll replaces an already-rendered quoteable position
with a different accepted `current_valuation`, the existing `实时价`, `美元市值`,
and `港元市值` cells briefly fade from the existing success tint back to the
normal table background. This is feedback that a new accepted valuation reached
the page, not a data-source label. It does not run on first load, a `304`, a
failed/`503` poll, tab or filter changes, or cash/money-market rows, and it is
disabled by `prefers-reduced-motion`. No layout space, permanent copy, badge, or
row height is added.

## Release And Rollback Sequence

1. Finish, accept, merge, and deploy Issue #21 without this behavior change.
2. Start implementation from the resulting local `main`.
3. Update Worker publication and Account API validation together so they ship
   with the same Account release SHA.
4. Add the Browser and Trend simulated-position consumers without changing
   Gateway routing or ownership.
5. Run focused tests and direct real OpenD/Worker/API checks, then run
   `make acceptance` once as the final Dashboard gate.
6. After `PASS`, redeploy the exact accepted SHA and verify PID, cwd, SHA,
   release match, fresh logs, and HTTP 200 at `http://127.0.0.1:8766/`.

Rollback uses the accepted Issue #21 release. A whole-stack rollback restores
its browser and Account pair. An Account-only rollback may leave the newer
browser running; the optional-field fallback keeps it compatible without
restoring Legacy Account ownership.

## Verification

Focused automated checks must prove:

- accepted statement-only CN/HK positions enter the OpenD quote universe;
- production quote refresh has no `portfolio.csv` or no-argument fallback;
- cash and money-market positions remain excluded from quote requests;
- one instrument held at multiple brokers is requested once and applied to all
  matching positions;
- a valid OpenD quote overrides statement price and updates dependent Account
  values, summaries, weights, and P/L consistently;
- invalid/missing quotes retain an accepted quote or fail closed and never
  produce zero or partial valuation objects;
- all quoteable real US/HK/CN positions publish complete USD and HKD
  equivalents, while cash and money-market positions remain unchanged;
- HK/CN price time uses OpenD `update_time` and only falls back to refresh time
  when the upstream value is absent;
- existing flat v1 fields keep their types and semantics;
- ETag and generation change when a visible current valuation changes;
- simulated US/HK/CN positions publish the complete object, and missing FX
  returns the existing unavailable response;
- browser renderers prefer the object, tolerate its complete absence, reject no
  Account state based on Legacy fields, and perform no `/api/quotes` request or
  client FX calculation.

Direct verification must compare every quoteable Account position with the
same stable accepted OpenD quote publication used by the Worker, then verify
the corresponding Account API/Gateway response and browser-rendered price, USD
value, and HKD value. It must also check one US, one HK, and one CN
simulated-position response. The current pre-implementation probe returned
valid OpenD prices for all 35 accepted quoteable positions.

The final `make acceptance` result is the only Dashboard review-readiness gate.
Only `PASS` permits deployment and operator review; `FAIL` is fixed and rerun,
and `BLOCKED` is reported without substituting fixtures or screenshots. A
release candidate may not pass this gate using only retained stale prices: the
candidate Worker must complete a fresh OpenD refresh covering every quoteable
position. OpenD unavailability at that gate is `BLOCKED`.
