# Prediction Market Arbitrage Scanner Design

## Goal

Add the first prediction-market arbitrage capability to Open Trader: a
strictly read-only, one-shot Polymarket scanner that finds executable binary
bundle opportunities from live public order books.

The first slice answers one operational question:

> Can the same number of YES and NO shares be bought now for less than their
> guaranteed $1 settlement value, after current taker fees and a configured
> safety threshold?

It must not load wallet credentials, authenticate to Polymarket, submit orders,
run continuously, notify users, or change the dashboard.

## Roadmap Position

This slice implements the useful core of Phase 1 from the external roadmap:

- live public market discovery
- normalized market and order-book data
- an observable arbitrage signal
- durable local scan artifacts

It intentionally does not install every project named by the roadmap.
Cross-platform APIs, historical ingestion, persistent watchers, LLM dependency
discovery, dry-run execution, and risk-managed trading are separate milestones.

## Approaches Considered

### A. Official REST APIs directly - selected

Use the public Gamma API for market discovery and the public CLOB `/books`
endpoint for order books. Implement the small HTTP boundary with the Python
standard library already used by Open Trader.

Advantages:

- no wallet, API key, or authentication path
- no new dependency
- fewest moving parts
- exact control over validation, fee treatment, and fail-closed behavior
- fits Open Trader's existing client -> pure rule -> artifact -> CLI shape

Cost:

- a later Kalshi integration will need a second client or a unified adapter

### B. Add `dr-manhattan`

Use its CCXT-style Polymarket and Kalshi abstraction immediately.

Advantages:

- multi-platform interface exists up front
- WebSocket and order execution paths are available for later work

Costs:

- brings a large dependency and credential-capable surface into a read-only
  first slice
- Open Trader would depend on a fast-moving third-party normalization layer
- no current need for most of the package

Reconsider this when the first cross-platform milestone begins.

### C. Run an existing arbitrage bot as a sidecar

Adopt one of the roadmap's Python or Rust bots and ingest its logs.

Advantages:

- quickest path to a broad demo

Costs:

- duplicates process, configuration, persistence, and risk behavior already
  present in Open Trader
- makes signal semantics and auditability depend on another application
- introduces execution-capable code before execution is authorized

This approach is rejected.

## User Experience

Add one nested CLI command:

```bash
.venv/bin/python -m open_trader prediction-arb scan \
  --max-events 100 \
  --min-net-edge 0.01 \
  --max-book-age-seconds 10 \
  --max-book-skew-seconds 2 \
  --data-dir data
```

The command performs one scan and exits. It prints:

```text
status: ok
events_scanned: 100
markets_eligible: 241
markets_scanned: 241
opportunities: 2
artifact: data/runs/2026-07-26/prediction_arbitrage/opportunities.json
latest: data/latest/prediction_arbitrage/opportunities.json
```

Each opportunity is also printed in one compact line containing the market
question, YES ask, NO ask, executable size, fee-adjusted unit edge, and
estimated net profit.

`--max-events` limits API work, not opportunity count. Its default is `100`.
`--min-net-edge` is a dollar amount per matched YES/NO share pair and defaults
to `0.01`. Decimal CLI values must be finite and non-negative.

## Architecture

The scanner follows existing Open Trader boundaries without extending
`MarketScope`, which remains specific to `CN`, `HK`, and `US` securities.

```text
Polymarket Gamma events/keyset
-> validate and normalize active binary markets
-> Polymarket CLOB /books in bounded batches
-> validate paired fresh order books
-> pure Decimal bundle calculation
-> dated atomic JSON artifact
-> latest atomic promotion only after a complete scan
-> CLI summary
```

The implementation is split into four responsibilities:

### `prediction_arbitrage.py`

Owns immutable market, book, opportunity, and scan-result models plus the pure
bundle calculation. It has no network, filesystem, CLI, or notification code.

### `polymarket_public.py`

Owns the two public HTTP calls:

- `GET https://gamma-api.polymarket.com/events/keyset`
- `POST https://clob.polymarket.com/books`

The POST is a public read operation whose body contains only token IDs. The
client must never accept or read credentials.

Discovery uses keyset pagination because the older offset endpoint is
deprecated. Events are requested in descending `volume24hr` order and scanning
stops after `max_events`. CLOB requests are chunked so one request never
contains more than 100 token IDs.

### `prediction_arbitrage_store.py`

Writes one versioned JSON payload with the project's existing temporary-file
plus atomic-replace pattern.

### `cli.py`

Validates arguments, invokes one scan, prints the summary, and converts expected
network or data failures into concise stderr plus exit code `2`.

No factory, exchange interface, strategy base class, background service, or
configuration file is added.

## Eligible Markets

A Gamma market is eligible only when all of the following are true:

- its parent event is active and not closed
- the market is active and not closed
- `acceptingOrders` is true
- `enableOrderBook` is true
- `outcomes` parses to exactly `["Yes", "No"]`, case-insensitively
- `clobTokenIds` parses to exactly two non-empty token IDs in YES, NO order
- its condition ID, market ID, question, and slug are present

Closed or malformed nested markets are skipped even when their parent event is
active. Skip counts are recorded by reason.

NegRisk aggregation is not part of this slice. A binary market may carry a
NegRisk flag because it belongs to a larger event; it is still eligible for its
own YES/NO bundle calculation.

## Order-Book Validation

The scanner must not trust response-array ordering. For every token book:

- best ask is the valid positive price with the minimum value in `asks`
- best bid is the valid positive price with the maximum value in `bids`
- prices must be within `(0, 1)`
- sizes must be finite and positive
- timestamp must parse as Unix milliseconds
- `asset_id` must match a requested token ID

A market is skipped unless both token books:

- are present in the same batch response
- contain a best ask
- are no older than `max_book_age_seconds`
- differ in timestamp by no more than `max_book_skew_seconds`

Duplicate, unknown, or missing asset IDs make that market invalid. They do not
get silently paired by array position.

## Arbitrage Calculation

All money and share calculations use `Decimal`.

For best asks `p_yes`, `p_no` and sizes `s_yes`, `s_no`:

```text
executable_size = min(s_yes, s_no)
gross_unit_edge = 1 - p_yes - p_no
```

The opportunity is executable only when:

```text
executable_size >= max(yes_min_order_size, no_min_order_size)
```

When `feesEnabled` is false, the taker fee is zero. When it is true, the market
must provide a valid fee schedule. The fee per share on each leg uses the
current Polymarket formula:

```text
fee_per_share(p) = fee_rate * p * (1 - p)
```

The scanner calculates:

```text
taker_fee_per_pair =
  fee_per_share(p_yes) + fee_per_share(p_no)

net_unit_edge =
  gross_unit_edge - taker_fee_per_pair

estimated_net_profit =
  executable_size * net_unit_edge
```

An opportunity is emitted only when `net_unit_edge >= min_net_edge`.

This deliberately uses only the size resting at the best ask on both legs.
There is no depth walk and no speculative slippage model. The minimum net edge
is the safety margin for latency and rounding; it is not presented as a
guarantee that both non-atomic orders will fill.

If fees are enabled but the schedule is missing or malformed, the market is
skipped. The scanner never assumes zero fees.

## Artifact

Dated artifact:

```text
data/runs/<YYYY-MM-DD>/prediction_arbitrage/opportunities.json
```

Latest artifact:

```text
data/latest/prediction_arbitrage/opportunities.json
```

Top-level schema:

```json
{
  "schema_version": "open_trader.prediction_arbitrage_scan.v1",
  "generated_at": "2026-07-26T16:30:00+08:00",
  "status": "ok",
  "source": "polymarket",
  "warnings": [],
  "filters": {
    "max_events": 100,
    "min_net_edge": "0.01",
    "max_book_age_seconds": 10,
    "max_book_skew_seconds": 2
  },
  "summary": {
    "events_scanned": 100,
    "markets_eligible": 241,
    "markets_scanned": 241,
    "opportunities": 2,
    "skipped": {
      "closed": 4,
      "malformed_market": 1,
      "missing_book": 3,
      "stale_book": 2,
      "unknown_fee_schedule": 0
    }
  },
  "opportunities": []
}
```

Each opportunity contains:

- stable type `binary_bundle_long`
- event ID, title, and slug
- market ID, condition ID, question, slug, and Polymarket URL
- YES and NO token IDs
- YES and NO best-ask price and size
- both order-book timestamps
- executable size and minimum order size
- fee-enabled flag and applied fee rate
- gross unit edge, taker fee per pair, net unit edge
- estimated net profit

All Decimal values are serialized as strings. Opportunities are sorted by net
unit edge descending, then estimated net profit descending, then condition ID
for deterministic output.

## Failure Handling

### Complete failure

Gamma discovery failure, a malformed top-level response, or failure of every
CLOB batch causes:

- concise stderr without a traceback
- exit code `2`
- no dated artifact
- no latest promotion

### Partial failure

If at least one CLOB batch succeeds and another fails:

- write a dated artifact with `status=partial` and warnings
- print the artifact path and failed-batch count
- exit code `2`
- preserve the previous latest artifact

### Complete scan

An `ok` scan writes the dated artifact first and then promotes the exact payload
to latest. Zero opportunities is a successful result with exit code `0`.

Malformed individual markets or books are counted and skipped. They do not
abort an otherwise complete scan.

## Security and Safety

- No private-key, wallet, API-key, or signer fields exist in this feature.
- No authenticated endpoint or order endpoint is called.
- No environment variable containing Polymarket credentials is read.
- URLs are fixed constants, not user-controlled request targets.
- HTTP calls have finite timeouts.
- Market text is treated as untrusted display data and never executed.
- The CLI describes results as observed non-atomic opportunities, not
  guaranteed realized profits.

## Testing

Unit and CLI tests use fake HTTP responses. They cover:

- keyset pagination stops exactly at `max_events`
- active parent events still exclude closed nested markets
- JSON-encoded `outcomes` and `clobTokenIds` normalize correctly
- malformed or non-binary markets are skipped
- unsorted bid and ask arrays produce the correct extrema
- token books are matched by asset ID, not response order
- missing, stale, and timestamp-skewed books are skipped
- fee-free and fee-enabled net edge calculations
- unknown fee schedules fail closed
- insufficient best-level size is not emitted
- threshold equality is included
- Decimal output is deterministic
- complete scans atomically update dated and latest artifacts
- partial scans preserve the previous latest artifact
- zero-opportunity scans succeed
- CLI arguments, output, and expected errors
- no credential is required or accepted

Final verification must include:

1. focused tests for the new modules and CLI
2. the full existing test suite
3. one real public-API scan
4. inspection of the generated artifact against the live order books

This is not a Dashboard task, so `make acceptance` is not required by the
Dashboard acceptance gate.

## Definition of Done

- The command scans current Polymarket markets without credentials.
- Every reported opportunity is based on paired, fresh best asks and executable
  size at those price levels.
- Dynamic taker fees are applied or the market is skipped.
- A complete scan writes valid dated and latest artifacts atomically.
- Partial and failed scans cannot replace a known-good latest artifact.
- Tests and one real scan pass.
- Documentation states that execution is non-atomic and remains out of scope.

## Deferred Milestones

1. Persistent watcher, deduplicated notifications, and live dashboard display.
2. NegRisk multi-outcome rebalancing using all required YES legs.
3. Historical scan storage and opportunity-lifetime statistics.
4. Kalshi market matching and cross-platform dry-run; reconsider
   `dr-manhattan` at this boundary.
5. LLM-assisted logical dependency discovery for combinatorial arbitrage.
6. Wallet, execution, position reconciliation, and circuit breakers only after
   a separately approved execution design.

## References

- External roadmap:
  <https://github.com/Oceanjackson1/Prediction-Market-Arbitrage-Compendium/blob/main/analysis/implementation-roadmap.md>
- Polymarket API overview:
  <https://docs.polymarket.com/api-reference/introduction>
- Polymarket market discovery:
  <https://docs.polymarket.com/market-data/fetching-markets>
- Polymarket batch order books:
  <https://docs.polymarket.com/api-reference/market-data/get-order-books-request-body>
- Polymarket fees:
  <https://docs.polymarket.com/trading/fees>
