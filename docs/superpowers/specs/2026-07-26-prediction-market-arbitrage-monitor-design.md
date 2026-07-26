# Prediction Market Arbitrage Monitor Design

## Goal

Add a read-only prediction-market arbitrage monitor to Open Trader that:

- runs continuously on the user's Mac
- discovers Polymarket's 20 highest-volume active events
- watches their binary YES/NO order books in real time
- confirms apparent opportunities with a same-batch public REST read
- records confirmed signals and their lifetimes indefinitely
- shows exactly what is monitored, what can produce a formal signal, what is
  active now, and what appeared in the past
- implements the user-approved Dashboard A prototype exactly

The first release never authenticates, reads a wallet, submits an order, or
sends a notification.

## Product Decisions

The user approved the following decisions:

- Platform rollout: Polymarket first, Kalshi second, Predict.fun third.
- Strategy rollout: within-venue binary bundle monitoring first;
  cross-platform matching only after each venue is stable.
- Universe: top 20 active events by 24-hour volume, refreshed every 5 minutes.
- Detection: public WebSocket updates followed by public REST `/books`
  confirmation.
- Formal signal thresholds:
  - net edge at least `$0.01` per matched YES/NO pair
  - estimated total net profit at least `$1.00`
- Fee policy: markets whose fees are not proven safe remain visible and
  monitored but cannot produce formal signals.
- Persistence: confirmed signals are retained indefinitely in local SQLite;
  raw order-book ticks are not retained.
- Operation: launchd keeps the watcher running; `caffeinate -s` prevents system
  sleep while the Mac is on AC power without preventing display sleep.
- UI: one top-level `预测市场` destination; no bottom navigation.
- Notifications: none in V1.

## Roadmap Position

The external roadmap recommends first building data infrastructure, a unified
API layer, and signal observation before dry-run or execution. This design takes
the smallest useful slice:

1. public live data
2. one auditable within-market bundle rule
3. durable signal history
4. operational monitoring in the existing Dashboard

It does not install a generic multi-exchange framework or an execution-capable
sidecar. A unified venue interface is deferred until the second venue actually
exists.

The paper's broader logical and combinatorial arbitrage taxonomy remains useful
research context. V1 implements only the elementary binary bundle invariant:
one YES share plus one NO share settles to `$1`.

## Approved UI Contract

The approved prototype is:

- branch: `prototype/prediction-market-ui`
- commit: `193fac7`
- local source:
  `src/open_trader/dashboard_static/prediction-market-prototype.html`
- selected layout: Variant A, “运营控制台”

The prototype is disposable design evidence and must not be merged. Production
HTML, CSS, and JavaScript must recreate the approved A layout in the existing
Dashboard shell.

### Top Navigation

The header contains these destinations in this order:

1. `持仓`
2. `预测市场`
3. `策略回测`
4. `凯利实验室`

`预测市场` is the active item while the prediction workspace is open. The
navigation remains at the top on mobile and may wrap. There is no bottom
navigation, floating mobile navigation, or prototype state controller.

### Prediction Workspace

The workspace contains, in order:

1. title `预测市场套利`
2. subtitle explaining that the user first sees what is monitored, then current
   and historical signals
3. watcher health and last heartbeat
4. five summary cards:
   - current formal signals
   - monitored events
   - markets / tokens
   - WebSocket state and venue
   - confirmed signals in the selected history window
5. the fee-policy warning
6. two-column desktop content:
   - left: monitored event list
   - right: current signals above historical signals
7. one-column mobile content in the same reading order

The UI uses the existing warm-ledger Dashboard palette and interaction styles.

### Monitored Event Rows

Every monitored event row visibly includes:

- event title
- number of included binary markets
- rank
- explicit `24h 成交量` label and value
- either `可参与信号` or `仅监控 · 费用待核验`
- either `最高预计净利润` or `毛利润上限`

Rows are expandable. Their details list the included markets and whether each
is subscribed and signal-eligible.

The event ordering is a product rule, not presentation-only sorting:

1. events containing at least one signal-eligible market first
2. within the same eligibility group, profit descending
   - eligible events use highest estimated net profit from eligible markets
   - ineligible events use highest gross-profit upper bound
3. equal profit uses 24-hour event volume descending
4. a stable event ID tie-break makes output deterministic

Missing profit sorts below a finite profit in the same eligibility group. It is
shown as `—`, never silently converted to zero.

### Current and Historical Signals

The current section shows only REST-confirmed, fee-safe formal signals. Each
card includes:

- event and market question
- venue
- fee status
- confirmation state and active duration
- YES ask and NO ask
- net edge
- executable matched size
- estimated net profit

History offers `24 小时`, `7 天`, and `全部`. Rows include start time, market,
duration, peak net edge, executable size, and peak estimated net profit.
No-history is a valid state and must have explicit Chinese copy.

### Four Runtime States

- `loading`: client has not received the first prediction API response.
- `live`: watcher is healthy and at least one formal signal is active.
- `quiet`: watcher is healthy and no formal signal is active.
- `degraded`: watcher heartbeat, WebSocket, universe refresh, store, or API is
  stale/unavailable.

On a refresh failure after successful loading, the UI retains last-known rows,
marks them stale, and shows the degraded warning. It never presents stale data
as live.

## Monitoring Universe

Every five minutes the watcher calls the public Gamma events endpoint with:

```text
active=true
closed=false
limit=20
order=volume24hr
ascending=false
```

An event is retained only when its ID, title, and finite non-negative
`volume24hr` are valid. Its nested markets are monitored only when:

- the event and market are active and not closed
- `acceptingOrders` and `enableOrderBook` are true
- outcomes are exactly YES and NO, case-insensitively
- exactly two non-empty CLOB token IDs map to YES and NO
- market ID, condition ID, question, and slug are present

Malformed markets are counted and skipped; they do not abort the universe.
The event remains visible when at least one valid binary market remains.

The watcher compares the new token set to the current set and sends documented
subscribe/unsubscribe frames on the existing WebSocket. If the subscription
update fails, it reconnects with the full current token set.

## Data Flow

```text
Gamma top-20 active events (every 5 minutes)
  -> normalize binary markets
  -> classify explicit Gamma zero-fee markets; fail closed on missing/conflicting fields
  -> subscribe/unsubscribe public token IDs

Polymarket market WebSocket
  -> maintain current best asks in memory
  -> calculate gross/eligible candidate values with Decimal
  -> re-verify candidate fee/minimum-size facts from live CLOB market details
  -> when thresholds appear reachable, POST both token IDs to /books
  -> validate same-batch books and recalculate
  -> open/update/close a formal signal in SQLite
  -> publish a throttled runtime snapshot

Dashboard API
  -> read SQLite
  -> return runtime, sorted monitored events, current signals, and history
  -> render approved A workspace
```

## Modules

The implementation uses five focused modules and existing entry points:

### `prediction_arbitrage.py`

Owns immutable normalized values and pure `Decimal` calculations:

- market/event identity
- best ask
- bundle candidate
- formal signal threshold check
- approved event sort key

It has no network, SQLite, process, CLI, or Dashboard code.

### `polymarket_public.py`

Uses the Python standard library for public HTTPS:

- Gamma top-event discovery
- CLOB market details used for fee/minimum-size verification
- same-batch `POST /books` confirmation

URLs are constants, timeouts are finite, responses are size/type validated, and
no credential field exists.

### `polymarket_stream.py`

Uses the directly declared `websockets` dependency and the synchronous client
API. It owns:

- connection to the public market WebSocket
- initial token subscription with `custom_feature_enabled`
- application `PING` every 10 seconds and `PONG` tracking
- dynamic subscribe/unsubscribe frames
- parsing `book`, `price_change`, and `best_bid_ask`
- reconnect with capped exponential backoff

It yields normalized top-of-book updates and contains no strategy or storage
logic.

### `prediction_arbitrage_store.py`

Uses stdlib `sqlite3` in WAL mode. It owns schema creation, one current runtime
snapshot, formal signal lifecycle records, and history queries.

### `prediction_arbitrage_watch.py`

Owns the continuous orchestration loop:

- five-minute universe refresh
- in-memory paired books
- candidate episode deduplication
- REST confirmation
- formal signal lifecycle
- ten-second heartbeat
- throttled snapshot writes
- startup and disconnect cleanup

### Existing Entry Points

- `cli.py`: add `prediction-arb watch` and `prediction-arb status`.
- `dashboard_web.py`: add
  `GET /api/prediction-arbitrage?window=24h|7d|all`.
- existing Dashboard static files: add the approved workspace.
- `dashboard_acceptance.py`: add live watcher, API, ordering, and exact UI
  acceptance.

No exchange interface, strategy base class, factory, event bus, ORM, task
queue, or configuration framework is added.

## Fee Policy

Fee handling fails closed.

A market is provisionally signal-eligible in the monitored list only when:

- Gamma explicitly reports fees disabled and taker base fee exactly zero
- no Gamma fee field is missing, malformed, or contradictory

Before a candidate becomes a formal signal, the watcher rechecks live CLOB
market details and requires:

- the taker base fee is exactly zero
- the fee curve is absent, disabled, or has an exactly zero rate
- the market-level fee and minimum-order facts are internally consistent

Any missing, malformed, non-zero, or contradictory fee field produces
`fee_unverified`. The market remains subscribed and its gross-profit upper bound
is shown, but it cannot become a formal signal. A CLOB contradiction immediately
downgrades a previously Gamma-eligible market.

V1 does not implement non-zero fee curves. This is deliberate: current fee
categories and parameters can change, and a wrong fee calculation would create
a false arbitrage alert. A later separately tested change may promote
fee-enabled markets to `fee_verified`.

## Bundle Calculation and Confirmation

For YES ask `(p_yes, s_yes)` and NO ask `(p_no, s_no)`:

```text
executable_size = min(s_yes, s_no)
gross_unit_edge = 1 - p_yes - p_no
gross_profit_upper_bound = executable_size * gross_unit_edge
```

For V1 signal-eligible fee-free markets:

```text
net_unit_edge = gross_unit_edge
estimated_net_profit = gross_profit_upper_bound
```

All values use `Decimal`; finite prices must be in `(0, 1)` and sizes must be
positive. The opportunity must also satisfy the CLOB minimum order size.

A WebSocket candidate is not a formal signal. The watcher sends exactly the
YES and NO token IDs in one public `/books` request and:

- matches responses by `asset_id`, never array position
- requires both requested books exactly once
- selects the minimum valid ask price in each book
- requires finite positive size
- requires book timestamps to be no older than 10 seconds
- requires timestamp skew no greater than 2 seconds
- recalculates from the REST books
- requires net unit edge at least `$0.01`
- requires estimated net profit at least `$1.00`

While the WebSocket candidate remains active, failed confirmation may retry at
most once per second. This is an observation, not an atomic execution
guarantee.

## Signal Lifecycle

A formal signal is unique per active market episode:

- `open`: first qualifying REST confirmation
- `update`: later qualifying confirmation raises peak edge/profit facts
- `close`: WebSocket price/size falls below threshold, the market leaves the
  universe, the stream becomes stale, or the watcher restarts

Startup closes any previously open signal as `watcher_restarted` before
creating a new connection. Disconnect closes open signals as `stream_stale`.
The next qualifying REST confirmation starts a new episode.

No notification or order is emitted at any lifecycle step.

## Persistence

Database:

```text
data/prediction_arbitrage/prediction_arbitrage.sqlite3
```

Minimal schema:

```text
runtime_snapshot
  singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1)
  updated_at TEXT NOT NULL
  payload_json TEXT NOT NULL

signals
  signal_id TEXT PRIMARY KEY
  market_id TEXT NOT NULL
  started_at TEXT NOT NULL
  ended_at TEXT
  peak_net_unit_edge TEXT NOT NULL
  peak_estimated_profit TEXT NOT NULL
  payload_json TEXT NOT NULL
```

Indexes support active-signal and `started_at` history queries. A partial unique
index prevents two open signals for one market. JSON retains immutable event,
market, venue, quote, size, and close-reason facts without adding speculative
tables.

Raw WebSocket frames and raw order-book ticks are not stored.

## Dashboard API

`GET /api/prediction-arbitrage?window=24h|7d|all` returns:

```json
{
  "schema_version": "open_trader.prediction_arbitrage.dashboard.v1",
  "generated_at": "2026-07-26T18:00:00+08:00",
  "window": "24h",
  "status": "quiet",
  "runtime": {
    "pid": 123,
    "working_directory": "/path/to/worktree",
    "git_sha": "40-char-sha",
    "heartbeat_at": "2026-07-26T18:00:00+08:00",
    "universe_refreshed_at": "2026-07-26T17:58:00+08:00",
    "websocket": "connected",
    "last_pong_at": "2026-07-26T17:59:59+08:00",
    "reconnects": 0,
    "blocker": null
  },
  "summary": {
    "current_signals": 0,
    "events": 20,
    "markets": 331,
    "tokens": 662,
    "history_signals": 3
  },
  "fee_policy": {
    "message": "费用待核验市场仍会监控，但不会产生正式信号"
  },
  "events": [],
  "current_signals": [],
  "history": []
}
```

The API derives `degraded` when the heartbeat or PONG is older than 30 seconds,
the universe is older than 10 minutes, or the stored blocker is non-empty.
Otherwise it returns `live` when current signals exist and `quiet` when they do
not. `loading` remains a client-only pre-response state.

Invalid history windows return HTTP 400. A missing database returns a valid
degraded payload rather than breaking the rest of the Dashboard.

## Reliability and Failure Handling

- One watcher process holds an existing-style file lock; a second watcher
  exits with code `2`.
- Startup records `starting` before network work.
- Gamma, CLOB, WebSocket, parsing, and SQLite failures update the blocker and
  heartbeat instead of killing the launchd process.
- Reconnect delay starts at 1 second and caps at 60 seconds.
- Runtime snapshot writes occur at most once per second plus the ten-second
  heartbeat.
- SQLite uses a busy timeout so a Dashboard read cannot fail a watcher write.
- Market text is escaped by the existing Dashboard `escapeHtml` helper.
- Signal history survives watcher and Dashboard restarts.
- No stale snapshot is described as live.

## Mac Deployment and Cost

Launchd label:

```text
com.open-trader.prediction-arbitrage
```

The agent runs:

```text
/usr/bin/caffeinate -s <repo>/.venv/bin/python -m open_trader \
  prediction-arb watch --data-dir <shared-data-dir>
```

It uses `RunAtLoad` and `KeepAlive`, writes dedicated stdout/stderr logs, and
starts from the exact deployed worktree. The installer verifies the loaded
label, live watcher PID, working directory, Git SHA, fresh heartbeat, and fresh
logs.

Expected recurring software/API cost is `$0`: all V1 endpoints are public and
there is no cloud host. Keeping the existing Mac awake is expected to add about
`2.9–5.8 kWh/month` at a 4–8 W continuous draw, so a practical Shanghai
electricity budget is approximately `¥2–4/month`. Actual marginal cost can be
lower when the Mac would already be on.

## Security and Scope Boundaries

- No wallet, private key, API key, signer, user channel, authenticated endpoint,
  or order endpoint.
- No environment variable containing prediction-market credentials.
- No user-controlled URL.
- Public HTTP and WebSocket payloads are untrusted and validated.
- The Dashboard says “预计利润” and “信号”, never “已实现利润”.
- Signal discovery does not authorize execution.

## Acceptance Contract

This is a Dashboard and long-running-process change. `make acceptance` is the
only completion gate and must include all of the following.

### Automated Behavior

- normalization and Decimal bundle calculations
- exact formal thresholds and equality behavior
- fee-free eligibility and fail-closed fee uncertainty
- REST book matching by token ID and freshness/skew rules
- candidate deduplication and complete signal lifecycle
- SQLite restart/history behavior and deterministic event sorting
- four UI render states: live, quiet, degraded, loading
- history filters and explicit zero states
- visible `24h 成交量` label/value on every event row
- exact approved navigation order and absence of bottom navigation

### Live Process and Data

- launchd label is loaded
- watcher PID is alive
- watcher working directory and Git SHA match the accepted checkout
- heartbeat, universe refresh, PONG, and logs are fresh
- public Gamma returns real active events
- public CLOB returns real paired order books for a monitored market
- Dashboard prediction API returns real watcher data
- zero live signals is accepted as truthful, not treated as an error

### Live Browser Matrix

At 1920×1080, 1440×1000, 760×1000, and 375×844:

- open `预测市场` from the top navigation
- verify the approved A sections and reading order
- verify the active top-navigation state and no bottom navigation
- verify event count, market/token count, current signals, and history agree
  with the live API
- verify DOM event order exactly matches the API order
- independently verify the API order obeys eligibility, profit, volume, and ID
  tie-break rules
- verify every event visibly labels `24h 成交量`
- expand an event and verify market detail/state rows
- exercise `24 小时`, `7 天`, and `全部`
- verify either real history rows or the explicit zero-history state
- verify no horizontal page overflow, no browser/HTTP errors, keyboard focus,
  and mobile controls at least 44 px high
- capture a fresh full-page prediction-market screenshot at every viewport

The test suite deterministically covers all four UI states. The live browser
uses only the real watcher state; it does not fabricate a market signal.

After `make acceptance` returns `PASS`, redeploy the exact accepted Git SHA and
verify the new Dashboard and watcher PIDs, working directories, Git SHAs, fresh
logs, fresh heartbeat, and HTTP 200 review URL before asking the user to review.

## Deferred Work

1. Non-zero Polymarket fee curves after live formula verification.
2. Polymarket NegRisk multi-outcome bundles.
3. Kalshi within-venue monitor.
4. Predict.fun within-venue monitor.
5. Cross-platform market identity matching and dry-run.
6. Notifications, wallet access, and execution only after separate UI, risk,
   and safety approval.

## References

- [Prediction Market Arbitrage Compendium roadmap](https://github.com/Oceanjackson1/Prediction-Market-Arbitrage-Compendium/blob/main/analysis/implementation-roadmap.md)
- [Polymarket market discovery](https://docs.polymarket.com/market-data/discover-markets)
- [Polymarket real-time market stream](https://docs.polymarket.com/market-data/realtime-data)
- [Polymarket batch order books](https://docs.polymarket.com/api-reference/market-data/get-order-books-request-body)
- [Polymarket CLOB market info](https://docs.polymarket.com/api-reference/markets/get-clob-market-info)
- [Polymarket fees](https://docs.polymarket.com/trading/fees)
- [Unravelling the Probabilistic Forest: Arbitrage in Prediction Markets](https://arxiv.org/abs/2508.03474)
