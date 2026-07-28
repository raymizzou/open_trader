# Prediction-Market Arbitrage Monitoring and Execution Design

**Date:** 2026-07-26

**Status:** Approved for implementation

**Target:** Open Trader Dashboard on the user's Mac
**First venue:** Polymarket

## 1. Goal

Add a continuously running Polymarket arbitrage workspace to Open Trader that:

- monitors the 20 highest-volume active events
- detects executable binary YES/NO bundle opportunities
- makes the monitored universe and all past confirmed signals visible
- labels an opportunity `可参与` only after every trade-safety check passes
- lets the user preview and confirm one arbitrage execution inside Open Trader
- submits exactly one equal-share YES/NO FOK pair for each confirmed click
- automatically merges a successfully filled pair back into pUSD
- fails closed, alerts, and locks trading after any one-leg or merge incident
- survives restart without trusting stale local execution state
- runs continuously through the existing macOS operating model

The production implementation must reproduce the user-approved execution
prototype and satisfy every scenario in Section 16.

## 2. Locked Product Decisions

### 2.1 Rollout

- V1: Polymarket, within-market standard binary YES/NO bundles.
- V2: Kalshi.
- V3: Predict.fun.
- Cross-venue matching is deferred until each individual venue is stable.
- V1 does not introduce a generic multi-venue abstraction.

### 2.2 Monitoring

- Universe: top 20 active Polymarket events by 24-hour volume.
- Universe refresh: every 5 minutes.
- Order-book updates: public Polymarket WebSocket.
- Candidate confirmation: same-batch public REST `/books` request.
- Confirmed-signal thresholds:
  - net edge at least `$0.01` for one equal-share YES/NO pair
  - estimated total net profit at least `$1.00`
- Raw order-book ticks are not retained.
- Confirmed signal episodes are retained indefinitely in SQLite.

### 2.3 Meaning of `可参与`

`可参与` is a user-facing execution promise, not merely a mathematical signal.
It means the backend has most recently verified all of the following:

- a fresh, same-batch pair of executable books exists
- equal YES and NO shares can be bought
- total normal-order cost is at most `$20.00`
- estimated net profit is at least `$1.00`
- net edge per pair is at least `$0.01`
- depth, tick size, and minimum-order constraints are satisfied
- the market is standard binary, not NegRisk
- the market is explicitly verified fee-free
- the selected wallet has sufficient balance and allowance
- signing and relayer readiness checks pass
- Polymarket's geoblock check explicitly allows trading
- the global execution lock and circuit breaker are both open
- the data and readiness checks are fresh

The backend may perform more checks than the UI exposes. The user does not need
to understand intermediate notions such as “REST-confirmed formal signal.”

`可参与` is not a price guarantee. Opening the confirmation modal and pressing
`确认下单` each trigger fresh validation. If the guaranteed net profit has
fallen below `$1.00`, or any safety fact has changed, the request is rejected
without submitting an order.

### 2.4 Execution

- First click: open an Open Trader confirmation modal.
- Second click: `确认下单` creates one execution request.
- One execution request contains exactly two equal-share FOK BUY legs:
  - one YES leg
  - one NO leg
- The orders are sent as one Polymarket batch request, but the design treats the
  two responses as independently successful or rejected because the venue does
  not guarantee atomic batch execution.
- Only one execution may be in flight across the entire application.
- Other `参与` controls remain disabled until the execution reaches a terminal
  or incident state.
- A successful equal-share pair is immediately merged into pUSD.
- The first live click is always deliberate; there is no automated trading loop
  and no batch submission across multiple opportunity cards.

### 2.5 Risk Limits

- Dedicated low-balance trading wallet.
- Initial wallet funding cap: `$65.00` pUSD.
- No automatic refill.
- Maximum normal cost per confirmed click: `$20.00`, including any applicable
  fees. V1 only enables fee-free markets.
- Emergency authorization: at most `$2.00` estimated loss to neutralize a
  one-leg fill by completing the missing leg or unwinding the filled leg.
- The `$2.00` value is an expected-loss ceiling, not a permission to keep
  chasing prices.
- Any one-leg event, even one automatically neutralized within the ceiling,
  opens the circuit breaker and requires manual acknowledgement.

### 2.6 Alerts and Manual Recovery

Normal signals do not create notifications. The following incidents must create
both an existing macOS notification and an existing Feishu notification:

- one leg fills while the other does not
- automatic one-leg remediation fails
- merge fails or cannot be confirmed
- restart reconciliation finds an open order or unmatched directional position

Every incident also remains visible in the Dashboard until acknowledged.

The only normal way to re-enable trading is the UI action
`我已处理，恢复交易`. The backend accepts that action only after a fresh
reconciliation proves:

- no live open order remains
- no unresolved directional imbalance remains
- no unconfirmed merge remains

### 2.7 Deployment and Cost

- Runs continuously on the user's Mac.
- `launchd` restarts the watcher/Dashboard.
- `caffeinate -s` prevents system sleep while connected to AC power without
  forcing the display to remain awake.
- The live-trading Dashboard binds only to `127.0.0.1`.
- No mobile/LAN access is enabled in V1.
- Expected monitoring API/cloud cost: `$0`.
- Estimated Mac electricity cost: approximately `¥2–4/month`, dependent on the
  user's power tariff and actual machine load.
- Initial working capital: up to `$50` pUSD.
- Maximum normal exposure initiated by one click: `$20`.
- Maximum authorized emergency expected loss: `$2`.
- V1 only trades fee-free markets.
- Merge must use Polymarket's supported gasless relayer path. If gasless merge
  readiness cannot be proven, opportunities remain non-actionable rather than
  silently spending POL.
- Slippage, single-leg remediation, or venue failure can still cause real loss;
  the UI must not describe arbitrage as risk-free.

## 3. Scope and Explicit Deferrals

### In scope

- public market discovery and book streaming
- durable signal, execution, merge, and incident history
- authenticated balance, allowance, order, trade, and position reads
- two-leg FOK submission after explicit confirmation
- bounded one-leg remediation
- gasless merge
- crash/restart reconciliation
- local-only Dashboard controls
- Keychain-backed wallet credentials
- macOS and Feishu incident alerts

### Deferred

- Kalshi and Predict.fun adapters
- cross-venue semantic market matching
- automated unattended order placement
- multi-opportunity or bulk execution
- auto-refill
- NegRisk execution
- fee-enabled execution
- browser entry of private keys
- remote or mobile trade control
- artificial live-money canary orders
- a generic venue/execution framework

## 4. Dependency Compatibility Gate

Polymarket's official Python client ecosystem is changing, and current official
repositories contain open issues involving wallet authentication and FOK price
precision. Therefore implementation starts with a blocking compatibility gate.

On the target Mac, using the selected dedicated wallet type and exact pinned
official client versions, the gate must prove without submitting a live order:

1. derive authenticated API credentials
2. read balance and allowance
3. read open orders, trades, and positions
4. construct two equal-share FOK orders with Decimal-safe tick rounding
5. sign both orders and serialize the batch payload
6. prove the official client's merge capability is present and authenticate to
   the gasless relayer path without invoking the mutate-only merge method
7. exercise merge construction/response handling against the controlled
   execution boundary, because the official SDK exposes merge as a
   construct-and-submit operation rather than a public build-only operation
8. redact all secret material from logs and exceptions

The selected package names and exact versions are recorded only after this gate
passes. No handwritten EIP-712 implementation, experimental CLI fallback, or
unofficial signing library may be substituted merely to bypass a failed gate.

If the official clients cannot safely support the selected wallet, the feature
is `BLOCKED`: monitoring may continue, but the production `参与` control must
not be enabled.

## 5. Approved UI Contract

The approved interactive design evidence is:

- branch: `prototype/prediction-market-ui`
- commit: `e0d5083`
- source:
  `src/open_trader/dashboard_static/prediction-market-execution-prototype.html`
- layout: Variant A, `运营控制台`

The prototype is disposable design evidence and is not merged directly.
Production HTML, CSS, and JavaScript recreate its approved behavior in the
existing Dashboard shell.

All material future UI changes require a design artifact and user confirmation
before production implementation.

### 5.1 Prototype is the mandatory acceptance baseline

Commit `e0d5083` is the UI source of truth, not an illustrative reference.
Before production UI implementation, acceptance fixtures must render that exact
commit into fixed golden screenshots for:

- desktop `1440x1100`
- mobile `375x812`
- ready/actionable
- quiet
- executing
- success/merge
- incident/circuit-breaker
- degraded
- loading
- confirmation modal
- reset modal
- signal, trade/merge, and incident history tabs

Production is checked against those fixtures with the same browser, fonts,
viewport, deterministic data, animation-disabled CSS, and device scale factor.
Acceptance requires:

- the same information hierarchy, component order, dimensions, spacing,
  typography, colors, borders, states, labels, buttons, and responsive behavior
- no unapproved component addition, removal, reordering, restyling, or copy
  change
- a visual diff of at most `0.1%` changed pixels per golden screenshot, with no
  changed pixel caused by a semantic layout, component, color, or copy mismatch
- exact interaction parity for modal, confirmation, execution, history,
  incident detail, and reset flows

Dynamic live market values are tested separately and do not replace the
deterministic golden comparison. The prototype-only scenario selector is test
instrumentation and is the sole approved omission from production UI.

### 5.2 Top navigation

The header contains these destinations in this exact order:

1. `持仓`
2. `预测市场`
3. `策略回测`
4. `凯利实验室`

There is no bottom navigation.

### 5.3 Readiness strip

The prediction workspace begins with a persistent readiness strip showing:

- masked wallet address
- pUSD balance and the `$65` wallet-cap policy
- region-check status
- circuit-breaker/trading status
- first-live-order validation status

The UI never displays a private key, API secret, full credential, or a field
into which one can paste a private key.

Until a real order pair and merge have succeeded, the status remains
`实盘链路尚未完成首单验证`. Deterministic tests and unsigned/unsubmitted
previews do not change that status.

### 5.4 Monitoring workspace

The workspace contains, in order:

1. title `预测市场套利`
2. explanation of what is monitored
3. watcher health and last heartbeat
4. summary cards
5. explicit fee/risk policy
6. monitored-event list
7. current opportunities
8. histories:
   - `信号`
   - `交易与合并`
   - `事故`

Every monitored event row visibly includes:

- event title
- rank
- number of included binary markets
- explicit `24h 成交量` label and formatted value
- current eligibility (`可参与` or a precise non-actionable reason)
- `最高预计净利润` when actionable, otherwise a monitor-only upper bound

Rows may expand to show included markets and their individual eligibility
reason.

### 5.5 Ordering

The server returns events in this deterministic order:

1. events with at least one `可参与` opportunity first
2. within the same participation group, profit descending
3. equal profit: 24-hour volume descending
4. equal volume: stable event ID ascending

For non-actionable events, “profit” means the best observable gross upper bound.
Missing profit sorts below finite profit and displays `—`, never zero.

### 5.6 Confirmation modal

Clicking `参与` opens a modal containing:

- event and market
- the exact equal share quantity
- YES FOK BUY maximum price and estimated cost
- NO FOK BUY maximum price and estimated cost
- total normal cost, guaranteed minimum estimated net profit, and net edge
- explicit statement that the venue's batch is not atomic
- the `$2` one-leg remediation authorization
- `取消`
- `确认下单`

The modal uses fresh server data. On confirmation, the server validates again.
The browser cannot supply prices, quantities, wallet identifiers, or risk
limits as trusted execution inputs.

### 5.7 Execution and incident views

While executing, the card and modal show the current phase without implying a
fill before venue confirmation:

- validating
- submitting both FOK legs
- reconciling independent leg results
- neutralizing one-leg exposure, if necessary
- merging
- complete
- incident/locked

A circuit-breaker banner remains visible until acknowledged. Its detail view
shows:

- both intended legs
- each venue result
- any remediation attempt
- current orders and position reconciliation
- merge state
- alert delivery state
- the reason reset is allowed or denied

## 6. Monitoring Universe and Eligibility

Every five minutes, the watcher requests the top active events:

```text
active=true
closed=false
limit=20
order=volume24hr
ascending=false
```

An event is retained only when its ID, title, and finite non-negative
`volume24hr` are valid. A market is monitored when:

- event and market are active and not closed
- order book is enabled and orders are accepted
- exactly two outcomes map case-insensitively to YES and NO
- exactly two non-empty token IDs map to those outcomes
- market ID, condition ID, question, and slug exist

Malformed markets are counted and skipped without aborting the universe.

A monitored market is execution-eligible only when live market facts also
prove:

- it is not NegRisk
- its fee rate is explicitly zero and internally consistent
- minimum size and tick-size facts are available
- both books are fresh
- all Section 2.3 checks pass

NegRisk, fee-enabled, and fee-unknown markets remain visible and contribute to
monitoring statistics, but they never show an enabled `参与` control.

### 6.1 Freshness budgets

Freshness is objective and server-enforced:

- a displayed actionable book confirmation is no more than 10 seconds old and
  has not been invalidated by a newer WebSocket update
- cached wallet, allowance, geoblock, and relayer readiness used for the
  actionable label are no more than 60 seconds old
- preview and final execution validation refresh all volatile facts regardless
  of cache age
- a preview expires after 10 seconds and is always one-use
- watcher heartbeat older than 30 seconds is degraded
- public WebSocket disconnected/reconnecting for more than 15 seconds is
  degraded
- last successful universe refresh older than 10 minutes is degraded
- a store write failure disables action immediately

An item that exceeds any applicable budget loses `可参与` before the next API
snapshot. Last-known data may remain visible only with a stale marker.

## 7. Arithmetic and Candidate Sizing

All monetary and share arithmetic uses `Decimal`; binary floating point is not
permitted in signal, sizing, risk, or order-price calculations.

For equal share quantity `q`:

```text
yes_cost        = executable cost of q YES shares
no_cost         = executable cost of q NO shares
fees            = verified trading fees; zero for actionable V1 markets
normal_cost     = yes_cost + no_cost + fees
settlement      = q * $1.00
estimated_profit = settlement - normal_cost
net_edge        = estimated_profit / q
```

The backend selects the largest `q` that simultaneously:

- is executable from fresh depth on both books
- conforms to venue tick and minimum-size rules
- produces exactly equal YES and NO requested shares after the pinned official
  SDK's protected-BUY spend and tick rounding
- has `normal_cost <= $20.00`
- has `estimated_profit >= $1.00`
- has `net_edge >= $0.01`
- fits available balance and allowance

Prices and spend amounts are rounded only in the conservative direction. The
final order payload contains cent-denominated spend caps and stable maximum FOK
prices; it never sends an unbounded market order. The no-submit compatibility
gate signs both orders and compares their actual requested amounts before the
feature can become actionable.

## 8. Architecture

The design adds concrete Polymarket capability to the existing process:

```text
Gamma top-20 refresh
  -> normalize standard binary markets
  -> classify monitor-only vs execution-eligible
  -> update public WebSocket subscriptions

Public WebSocket books
  -> maintain current best depth in memory
  -> calculate Decimal candidate
  -> same-batch REST /books confirmation
  -> open/update/close durable signal episode
  -> publish throttled Dashboard snapshot

Dashboard
  -> GET monitoring, readiness, execution, and history state
  -> POST preview (fresh non-mutating validation)
  -> POST execution (consume preview + final validation + two FOK legs)
  -> POST circuit-breaker acknowledgement

Execution state machine
  -> persist intent
  -> submit stable two-leg batch
  -> reconcile both independent outcomes
  -> merge equal pair OR neutralize bounded one-leg exposure
  -> persist terminal result / incident
  -> alert and lock when required
```

### 8.1 Modules

`prediction_arbitrage.py`

- domain types, normalization, Decimal arithmetic, sizing, eligibility, sorting
- no networking, secrets, order submission, or notification side effects

`polymarket_monitor.py`

- public Gamma/CLOB HTTP and WebSocket integration
- universe refresh, subscription lifecycle, same-batch book reads, heartbeat

`prediction_arbitrage_store.py`

- SQLite migrations and narrow durable queries
- signal episodes, executions, legs, merges, incidents, acknowledgement

`polymarket_trading.py`

- the one official-client boundary
- Keychain credential access
- geoblock, balances, allowances, orders, trades, positions
- signing and submitting the two FOK legs
- gasless merge
- never owns business policy

`prediction_arbitrage_execution.py`

- one concrete serialized execution state machine
- preview lifecycle and idempotency
- risk limits, one-leg remediation, merge, restart reconciliation
- circuit breaker and notifications

Existing Dashboard, launcher, and notification modules remain the entry points.
No sidecar, task queue, generic exchange base class, or internal message bus is
introduced.

## 9. Credentials and Local Security

One-time terminal setup uses hidden input (`getpass`) and the native macOS
`security` command to store:

- signing private key
- relayer credential material, if required

Secrets:

- live only in macOS Keychain and process memory
- never appear in repository files, `.env`, command arguments, browser storage,
  SQLite, API responses, logs, tracebacks, or notifications
- are redacted before any exception is logged

A mode-`0600`, gitignored local file may contain only non-secret settings such
as signer address, funder/trading address, signature type, and risk limits. The
signer derived from the Keychain key must match the configured signer, and the
official client must derive the configured funder/trading address for the
selected signature type, before trading is enabled.

The trading Dashboard:

- binds to `127.0.0.1`
- requires a random per-process same-origin session token
- requires a CSRF token on all mutation requests
- validates `Origin` and `Host`
- accepts only server-generated opaque preview IDs as execution input
- never trusts browser-provided price, quantity, address, or limit values

## 10. Preview, Idempotency, and Serialization

### 10.1 Preview

`POST /api/prediction-arbitrage/preview`:

1. accepts a server-issued opportunity ID
2. re-reads both books in one REST batch
3. rechecks market facts, fee status, geoblock, wallet, balance, allowance,
   relayer readiness, execution lock, and breaker
4. recalculates equal-share sizing
5. persists a one-use opaque preview with a 10-second expiry
6. returns display-only leg and risk details

Opening a modal does not submit or sign an order.

### 10.2 Execution

`POST /api/prediction-arbitrage/executions`:

1. atomically consumes the preview ID
2. acquires the process-wide/file-backed execution lock
3. repeats all volatile validation with current data
4. persists the execution intent before any external mutation
5. assigns two stable local leg IDs, signs both FOK legs, and submits them in
   exactly one batch POST
6. reconciles order/trade outcomes before deciding the next transition

The preview cannot be reused, even after a browser retry. A repeated HTTP
request with the same application idempotency key returns the existing
execution; it does not place a second pair.

Stable local leg IDs derive from execution ID plus `YES` or `NO`. The official
Polymarket CLOB order API does not expose an application-supplied client order
ID, so the batch POST is attempted exactly once. If its response is ambiguous,
the service never resubmits it: it queries live orders, trades, and positions
and keeps trading locked until the outcome is proven.

Only one execution can occupy a non-terminal state. A database invariant plus
an OS-level lock prevents multiple Dashboard processes from placing orders.

Order outcomes reconcile at least once per second for up to 30 seconds using
the injected clock in tests. A still-unknown result then becomes an incident;
normal trading stays locked and background reconciliation may continue without
placing a new order.

## 11. Execution State Machine

```text
previewed
  -> final_validating
  -> submitting
  -> reconciling

reconciling
  -> both_rejected                 (terminal, no breaker)
  -> both_filled -> merging
  -> one_leg -> breaker_open -> remediating
  -> ambiguous -> breaker_open -> reconciling

merging
  -> complete                      (terminal)
  -> merge_incident                (breaker remains open)

remediating
  -> neutralized_incident          (breaker remains open)
  -> directional_incident          (breaker remains open)
```

All transitions are committed to SQLite before the next external action. The
state machine records venue response IDs and timestamps but never secrets or
full signed payloads.

### 11.1 One-leg policy

If exactly one leg fills:

1. open the circuit breaker immediately
2. fetch fresh books and current positions
3. estimate both neutralization paths:
   - complete the missing leg with a new FOK order
   - sell/unwind the filled leg with a new FOK order
4. choose the lower estimated-loss action that is executable and has estimated
   loss at most `$2.00`
5. use one FOK attempt with a stable remediation ID
6. reconcile instead of blindly retrying
7. send mandatory alerts and retain the incident

If neither path meets the bound, place no speculative order. Preserve the
directional position, keep trading locked, and raise an urgent incident.

### 11.2 Merge policy

After both equal-share legs are confirmed filled:

1. reconcile actual token balances
2. merge only the equal amount held on both sides
3. submit through the proven gasless relayer path
4. reconcile the transaction and resulting pUSD balance
5. mark complete only after confirmation

An ambiguous or failed merge is an incident. The balanced YES/NO pair remains
locked and visible; the service does not claim realized profit.

The foreground merge-confirmation window is 60 seconds. An unconfirmed
transaction after that window is an ambiguous merge incident, not a failure
proof and not permission to send a second merge blindly.

## 12. Startup and Crash Recovery

Trading always starts locked. Before enabling `参与`, startup reconciliation:

1. loads every non-terminal local execution
2. queries live open orders, trades, token positions, relayer transactions, and
   balances
3. cancels known live open execution orders
4. recognizes already completed orders/transactions idempotently
5. auto-merges a proven equal pair when safe
6. never opens a new directional order merely to “repair” an old unknown state
7. opens an incident for any imbalance or ambiguity
8. leaves the breaker locked until the user acknowledges a clean reconciliation

When there is no unfinished local execution, startup still verifies that the
dedicated wallet has no unexplained open orders or directional positions before
unlocking.

Old processes using pre-change code must be stopped before the accepted build is
started.

## 13. Persistence and History

SQLite stores:

### Signals

- market and event identity
- opened/last-seen/closed timestamps
- peak net edge, size, cost, and estimated profit
- eligibility and close reason

### Executions and legs

- execution/preview/idempotency identity
- immutable server-computed intent
- state transitions and timestamps
- each intended leg and venue result
- remediation attempts
- merge state and transaction reference
- realized or currently estimated result

### Incidents

- incident type and severity
- related execution
- reconciled orders and positions
- breaker state
- macOS/Feishu delivery outcomes
- acknowledgement time and reconciliation evidence

Raw WebSocket ticks and signed order payloads are never persisted.

Histories are retained indefinitely unless the user later approves a retention
change. A restart must not erase or synthesize history.

## 14. API Contract

All routes are under the existing Dashboard server.

- `GET /api/prediction-arbitrage/state`
  - readiness, heartbeat, breaker, masked wallet, balances
  - sorted monitored events and current opportunities
  - current execution/incident summary
- `GET /api/prediction-arbitrage/history?kind=signals|executions|incidents`
  - paginated durable history
- `POST /api/prediction-arbitrage/preview`
  - non-mutating fresh validation; returns one-use preview
- `POST /api/prediction-arbitrage/executions`
  - consumes preview; final revalidation; submits one two-leg request
- `POST /api/prediction-arbitrage/circuit-breaker/reset`
  - acknowledges incident only after live clean reconciliation

Mutation responses return the durable execution or incident ID. Browser
timeouts are resolved by reading that durable state, not by blindly retrying.

## 15. Acceptance Method

`make acceptance` is the only final Dashboard review-readiness gate. It runs
once after focused development tests and direct workflow checks have passed.

It has two evidence phases:

### Phase A: deterministic money-state scenarios

An isolated acceptance server uses:

- scratch SQLite
- deterministic clocks/IDs
- recorded public market-shaped inputs
- a controlled execution boundary that can produce each documented venue
  result
- controlled macOS and Feishu delivery outcomes

This phase is permitted to exercise browser mutations because it cannot sign or
send live orders. It verifies every dangerous state-machine branch, UI state,
durable record, lock, and forbidden side effect.

The UI portion renders the fixed prototype and production pages from the same
deterministic scenarios, captures every Section 5.1 golden state at both
required viewports, and fails on any visual or interaction mismatch outside the
single approved prototype-only control.

### Phase B: live non-mutating integration

The production-shaped process uses:

- real Gamma and CLOB public APIs
- a real WebSocket subscription and heartbeat
- the real dedicated account for authenticated reads
- real Keychain retrieval
- real geoblock, balance, allowance, orders, trades, and positions reads
- real construction and signing of an unsubmitted two-leg preview
- real gasless-relayer authentication/readiness checks without invoking the
  SDK's mutate-only merge method

It must not submit an order or merge merely for acceptance.

### Result semantics

Every scenario prints:

```text
SCENARIO <ID> PASS|FAIL|BLOCKED <short evidence>
```

- `PASS`: every required UI, backend, persistence, and forbidden-behavior
  assertion passed.
- `FAIL`: the system was available but any assertion failed.
- `BLOCKED`: a required browser or external environment was unavailable.

One `FAIL` makes the entire gate `FAIL`. One required `BLOCKED` makes the entire
gate `BLOCKED`. Only an all-`PASS` report produces final `PASS`.

Fixtures, curl output, screenshots, or unit tests cannot substitute for a
blocked required browser/live phase.

After `PASS`, the exact accepted Git SHA is redeployed. The handoff verifies
fresh process PID, working directory, Git SHA, logs, heartbeat, HTTP 200, and
the review URL.

## 16. Detailed Acceptance Scenarios

Every row is a contractual scenario. “Persisted evidence” means assertions read
back from SQLite or fresh logs after the action; an in-memory object alone is
insufficient.

### 16.1 Monitoring and eligibility

| ID | Preconditions and action | Required visible UI | Required backend and persisted evidence | Forbidden behavior |
|---|---|---|---|---|
| `MON-01 Loading` | Start the Dashboard with no prediction snapshot yet; open `预测市场`. | Loading skeleton/status is visible; no zero-valued summary is presented as fact; top navigation order is exact. | API reports loading/not-ready; watcher start is logged with timestamp. | No `可参与` button; no stale row presented as live; no bottom navigation. |
| `MON-02 Ready/actionable` | Feed a fresh standard, fee-free binary market whose same-batch books satisfy both thresholds and all account checks. | Event appears before non-actionable events; row visibly shows `24h 成交量`, `可参与`, profit, and enabled `参与`; health/readiness is green. | Candidate arithmetic uses Decimal; confirmed signal episode and current eligibility are queryable; source timestamps and book batch ID are recorded. | No eligibility based only on WebSocket data; no hidden volume; no order submission from monitoring alone. |
| `MON-03 Ready/quiet` | Feed a healthy top-20 universe with no market satisfying both thresholds. | Health is ready; explicit “当前没有可参与机会” copy; monitored rows and histories remain visible. | Heartbeat and universe refresh advance; no active signal row exists. | Quiet must not be shown as degraded; no fabricated opportunity. |
| `MON-04 Degraded/stale` | After a valid snapshot, exceed one Section 6.1 budget: heartbeat 30 seconds, WebSocket disruption 15 seconds, universe refresh 10 minutes, actionable books 10 seconds, readiness 60 seconds, or cause a store write failure. | Last-known data remains visible but is clearly marked stale; degraded reason and last successful time appear; every `参与` is disabled. | Health records the failing component and measured age; stale state survives API refresh; recovery produces a fresh timestamp. | Stale data must not be labeled live or actionable; no execution preview. |
| `MON-05 Fee-enabled/unknown` | Present an otherwise profitable standard binary market with nonzero, missing, or conflicting fee facts. | Market remains monitored; reason says fee-enabled or fee-unverified; gross upper bound and `24h 成交量` remain visible; no enabled action. | Eligibility reason is stored; no formal actionable episode is opened. | Never assume zero fees; never hide the event solely because it cannot trade. |
| `MON-06 NegRisk` | Present an otherwise profitable NegRisk market. | Market remains visible with `仅监控 · NegRisk`; no enabled `参与`. | `neg_risk=true` and ineligibility reason are returned/persisted. | Never send a NegRisk order or classify it as standard binary. |
| `MON-07 Malformed market` | Include malformed IDs/outcomes/volume alongside valid markets in a refresh. | Valid events still render; diagnostics show skipped market count without exposing a stack trace. | Malformed item is counted/logged; universe refresh completes with valid items. | One malformed item must not abort or erase the whole universe. |
| `MON-08 Sorting` | Provide actionable and non-actionable events with controlled profits, volumes, and equalities. | Actionable first; then profit descending; ties by visible 24h volume descending; final ties stable across refresh. | API order matches the product ordering and stable-ID tiebreak. | Browser-only sorting or alphabetical drift; treating missing profit as zero. |
| `MON-09 Signal lifecycle` | Open, improve, weaken, and close one REST-confirmed signal across multiple updates. | Current card duration and values update; after close it leaves current view and appears once in signal history with peak values. | One signal episode has correct open/last/close times and peaks; restart preserves it. | Duplicate history episodes for uninterrupted signal; raw ticks stored indefinitely. |
| `MON-10 Universe rotation` | Change top-20 membership on a five-minute refresh. | New event appears in correct order; removed event no longer appears current; history remains. | WebSocket subscribe/unsubscribe set equals the new token set; failed incremental update triggers full reconnect. | Orphan subscriptions treated as current; deletion of historical signals. |

### 16.2 Preview and pre-trade rejection

| ID | Preconditions and action | Required visible UI | Required backend and persisted evidence | Forbidden behavior |
|---|---|---|---|---|
| `PRE-01 Open preview` | With `MON-02` state, click `参与`. | Modal shows event, exact equal quantity, both FOK legs, maximum prices, leg/total costs, minimum profit, net edge, non-atomic warning, `$20` cap, and `$2` authorization; focus moves inside modal. | Fresh same-batch books, geoblock, wallet, allowance, balance, relayer, lock, and breaker are checked; one expiring one-use preview is stored. | Opening modal must not sign, submit, merge, or alert; browser values are not trusted inputs. |
| `PRE-02 Cancel` | Open a valid preview, then click `取消` or press Escape before execution. | Modal closes and focus returns to the originating `参与`; opportunity remains available if still fresh. | Preview is canceled/allowed to expire; no execution exists. | No order, signature, balance mutation, breaker, or incident. |
| `PRE-03 Price worsens` | Open a valid preview; before `确认下单`, worsen either book so guaranteed profit is below `$1`; confirm. | Modal/card shows rejected/stale-price explanation and refreshed values; no success animation. | Final validation is recorded; preview is consumed; zero order-submit calls; no execution intent that claims submission. | No threshold lowering, price chasing, partial submit, or reuse of the old preview. |
| `PRE-04 Region blocked` | Geoblock returns blocked during preview or final validation. | Region status is blocked; actions disabled; explicit no-bypass copy. | Rejection reason and geoblock timestamp are logged without sensitive network detail; zero submit calls. | No order, proxy/VPN workaround, cached allow result, or optimistic fallback. |
| `PRE-05 Region unavailable` | Geoblock endpoint times out, errors, or returns malformed data. | Region check shows unavailable and all actions are disabled. | Fail-closed rejection is recorded; later recovery requires a fresh successful check. | No use of a prior allow response beyond freshness; no order. |
| `PRE-06 Keychain/signing unavailable` | Lock/remove required Keychain item or force derived-address mismatch; request preview/confirm. | Wallet readiness is red with safe remediation copy; no secret is displayed; action disabled/rejected. | Redacted error and mismatch category are logged; zero submit calls. | No private key in UI/API/log/SQLite/process arguments; no fallback to `.env`. |
| `PRE-07 Balance/allowance insufficient` | Fresh books qualify but pUSD balance or allowance cannot cover the server-computed amount. | Exact safe reason appears; opportunity is temporarily non-actionable. | Fresh balance/allowance values and rejection reason are recorded; zero submit calls. | No smaller trade unless it still independently satisfies every threshold; no approval transaction silently sent. |
| `PRE-08 Relayer unavailable` | Gasless merge readiness cannot be proven. | Market remains monitored but action is disabled with merge-readiness reason. | Readiness failure is logged; zero order-submit calls. | No paid-gas fallback and no trade that cannot follow the approved merge path. |
| `PRE-09 Preview expiry/reuse` | Advance past the 10-second preview expiry, or submit the same consumed preview twice. | First valid request has one result; subsequent request shows expired/already-used and links to existing execution when applicable. | Exactly one durable execution/idempotency record; external submit count is at most one batch. | No duplicate pair caused by retry, double-click, reload, or network timeout. |

### 16.3 Security boundary

| ID | Preconditions and action | Required visible UI | Required backend and persisted evidence | Forbidden behavior |
|---|---|---|---|---|
| `SEC-01 Local bind` | Start accepted production configuration and attempt local and non-loopback access. | `127.0.0.1` review URL works; no LAN trading URL is offered. | Listening socket is loopback-only; startup log records bind address. | Listening on `0.0.0.0`, LAN IP, or public interface. |
| `SEC-02 Origin/CSRF/session` | Send mutation requests with missing/wrong Origin, Host, session, or CSRF token; then send a valid same-origin request. | Invalid browser request gets a safe refusal; valid flow works normally. | Invalid calls return 4xx and create no preview/execution; security rejection is logged without tokens. | No state mutation, signature, or external call from invalid request; no token in logs. |
| `SEC-03 Browser tampering` | Alter modal DOM/API payload prices, quantity, wallet, `$20`, `$2`, or profit before confirm. | Result reflects server-computed values or rejects as stale; tampered values never appear as authoritative success data. | Server ignores untrusted fields and reconstructs intent from preview/opportunity IDs. | No order using browser-supplied economics or wallet identity. |
| `SEC-04 Secret redaction` | Trigger SDK, signing, relayer, HTTP, and notification exceptions containing seeded secret sentinels. | UI shows only safe error category. | Logs, API bodies, SQLite, notifications, and acceptance artifacts contain zero sentinel matches. | Any full/partial private key, API secret, signed payload, or authorization header leakage. |

### 16.4 Order execution and merge

| ID | Preconditions and action | Required visible UI | Required backend and persisted evidence | Forbidden behavior |
|---|---|---|---|---|
| `EXE-01 Serialization/double-click` | Two actionable cards exist; double-click one confirm and attempt the other while first is in flight. | One execution progresses; all other action buttons are disabled and explain the global lock. | One execution intent, one batch call, exactly two intended legs, stable local leg IDs; second request returns existing/busy state. | More than one batch, four legs, parallel execution, or silent dropped click. |
| `EXE-02 Both FOK rejected` | Controlled venue independently rejects both legs. | Execution shows both rejected, no position, no profit, and returns to ready after reconciliation. | Both leg responses and zero fills are stored; no merge/remediation; breaker remains closed. | Claiming a fill/profit; retrying without a new user confirmation. |
| `EXE-03 Both filled and merged` | Controlled venue fills equal YES/NO shares; relayer confirms merge. | Progress shows submit → reconcile → merge → complete; exact cost, payout, and realized profit appear in `交易与合并`. | Exactly two equal filled legs, one merge request, confirmed pUSD delta, terminal `complete`; no incident. | Marking complete before live fill and merge confirmation; second merge; notification for normal success. |
| `EXE-04 YES-only then complete missing NO` | YES fills, NO rejects; fresh NO completion is executable with lower expected loss and loss `<= $2`; merge succeeds. | Circuit-breaker banner appears immediately; remediation, merge, and final neutral pUSD state are visible; incident persists; reset action is gated. | One bounded NO FOK remediation, reconciled equal pair, exactly one confirmed merge, incident, and both alerts are recorded. | Continuing normal trading; more than one blind remediation; estimated loss over `$2`; hiding the one-leg event after recovery. |
| `EXE-05 NO-only then unwind NO` | NO fills, YES rejects; unwinding NO is the lower executable path with loss `<= $2`. | Same incident/lock visibility; chosen unwind and realized loss are shown. | One bounded NO SELL FOK, zero directional position after reconciliation, incident and alert outcomes stored. | Completing the more expensive leg when unwind is lower loss; auto-reset; loss above limit. |
| `EXE-06 One leg, no safe remedy` | One leg fills; both completion and unwind are unavailable or estimate loss `> $2`. | Urgent incident shows unmatched position and “需要人工处理”; trading stays locked. | No remediation order is sent; live position evidence, attempted estimates, breaker, and alerts are persisted. | Chasing, exceeding `$2`, pretending neutral, or enabling another trade. |
| `EXE-07 Merge rejected/failed` | Both legs fill equally but merge returns a definite failure. | Balanced pair and unmerged amount are visible; no realized-profit claim; breaker/incident/alerts remain. | Merge attempt/failure and equal token balances are stored; trading locked. | Resubmitting indefinitely, spending POL without approval, or marking complete. |
| `EXE-08 Ambiguous submit` | Batch request times out after the venue may have received it. | UI says reconciling, then shows discovered outcome or locked ambiguity; never says both rejected merely from timeout. | Service queries orders/trades using stable IDs before any retry; result/incident is stored. | Blind second batch; treating network timeout as proof of rejection. |
| `EXE-09 Delayed/timeout outcome` | Venue remains delayed/pending through the 30-second reconciliation window. | Execution remains in-flight with elapsed time; other trades stay disabled; at 30 seconds it becomes an incident. | At-least-once-per-second reconciliation evidence and final breaker state are stored; alerts fire at incident transition; safe background reads may continue. | Enabling other trades while terminal state is unknown; invented fill result; second batch submission. |
| `EXE-10 Notification degradation` | Create a mandatory incident while one or both notification channels fail. | Incident remains visible and shows each channel's delivery state; trading remains locked. | Independent delivery attempts, bounded safe retries, and errors are persisted. | Notification failure suppressing the incident, unlocking trading, or blocking risk-neutralization work. |

### 16.5 Restart recovery and reset

| ID | Preconditions and action | Required visible UI | Required backend and persisted evidence | Forbidden behavior |
|---|---|---|---|---|
| `REC-01 Clean restart` | Stop accepted process with no open execution/order/position; restart. | Startup shows locked/reconciling, then ready after fresh checks; histories remain. | Fresh authenticated reads prove clean state; new PID/start time/heartbeat are logged. | Reusing pre-restart readiness without live reconciliation; erasing history. |
| `REC-02 Restart with open orders` | Seed non-terminal execution and live open leg orders; restart. | Locked recovery view lists open-order handling and incident. | Orders are canceled once, terminal outcomes reconciled, incident/alerts stored. | New normal order before cancellation/reconciliation; assuming cancel succeeded without confirmation. |
| `REC-03 Restart with equal pair` | Seed both filled equal positions and no confirmed merge; restart. | Locked recovery shows balanced pair and merge progress/result; acknowledgement remains required because recovery was abnormal. | Actual balances are read; at most one idempotent merge is sent; transaction is reconciled; incident record remains. | Duplicate merge or treating local “submitted” as chain confirmation. |
| `REC-04 Restart with imbalance` | Seed one unmatched token position; restart. | Urgent directional incident and exact current imbalance are visible; action buttons disabled. | No new directional repair order; breaker and alerts persisted from live position evidence. | Automatic speculative repair of old state; unlocking based on stale SQLite. |
| `REC-05 Unknown external state` | Dedicated wallet has an unexplained open order/position absent from SQLite. | Startup remains locked and explains unknown external state. | External identifiers and reconciled balances are stored in a new incident. | Ignoring activity because no local execution matches it. |
| `RST-01 Reset allowed` | Incident has been viewed; fresh reconciliation proves zero open orders, zero imbalance, and no pending merge; click `我已处理，恢复交易`. | Confirmation explains clean evidence; after confirm breaker opens to ready and actions may re-enable. | Acknowledgement user action/time and reconciliation snapshot are stored. | Reset based only on UI state or stale prior check; deleting incident history. |
| `RST-02 Reset denied` | Any open order, imbalance, pending/ambiguous merge, failed readiness, or stale account read remains; request reset. | Modal states the exact blocking fact; breaker remains closed to trading. | Denial and fresh reconciliation evidence are logged; no readiness change. | Force reset, hidden override endpoint, or order placement. |

### 16.6 History and durable audit

| ID | Preconditions and action | Required visible UI | Required backend and persisted evidence | Forbidden behavior |
|---|---|---|---|---|
| `HIS-01 Signal history` | Create closed signal episodes, restart, select signal history and time filters. | Correct rows, duration, peak edge/size/profit and explicit empty state; older rows appear under `全部`. | SQLite rows survive restart and pagination/filter totals agree. | Raw ticks exposed; duplicate/lost episodes; retention truncation. |
| `HIS-02 Trade/merge history` | Complete both-rejected, successful, and incident executions; restart. | `交易与合并` distinguishes rejected, complete, neutralized, directional, and unmerged outcomes; amounts never imply unrealized profit. | Execution, leg, remediation, merge, and transition rows remain linked and ordered. | Signed payload/secret display; rewriting an incident as normal success. |
| `HIS-03 Incident history` | Create acknowledged and unacknowledged incidents with varied notification results. | `事故` shows severity, position/merge state, alerts, breaker, and acknowledgement; unacknowledged incidents are prominent. | Incident evidence survives reset/restart indefinitely. | Acknowledgement deleting/hiding the audit trail. |

### 16.7 UI and accessibility

| ID | Preconditions and action | Required visible UI | Required backend and persisted evidence | Forbidden behavior |
|---|---|---|---|---|
| `UI-01 Desktop prototype parity` | Render every Section 5.1 state with deterministic prototype data in the fixed browser at `1440x1100`; capture production screenshots and exercise every approved interaction. | Production matches the fixed `e0d5083` golden hierarchy, component order, dimensions, spacing, typography, colors, borders, copy, and state behavior; visible volume/sorting are exact; console has no functional errors. | Per-state visual diff is `<=0.1%` changed pixels and contains no semantic mismatch; interaction assertions and screenshot paths are included in the acceptance report. | Any unapproved addition, omission, reorder, restyle, copy drift, bottom nav, clipped control, or prototype-only scenario selector in production. |
| `UI-02 Mobile prototype parity` | Repeat every Section 5.1 state at `375x812` with the same deterministic data and browser configuration. | Production matches the mobile golden screenshots; single-column order, wrapping, modal placement, and all visible content are identical; no horizontal overflow; controls are at least `44x44`. | Per-state visual diff meets the same strict threshold; browser assertions record scroll width, hit targets, interactions, and screenshot paths. | Responsive behavior that differs from the approved prototype, bottom nav, off-screen confirm/reset, hidden volume, or hover-only information. |
| `UI-03 Keyboard modal` | Open confirmation and reset modals using keyboard; Tab/Shift-Tab; press Escape where safe; close. | Initial focus is meaningful, focus is trapped, visible focus exists, cancel closes, focus returns to invoker; executing mutation cannot be accidentally dismissed as canceled. | Browser assertions cover focus order and restored element. | Focus escaping behind modal, background action activation, duplicate submit from Enter. |
| `UI-04 Status semantics` | Exercise every approved prototype state with controlled data. | Colors, text, icons, stale/quiet distinction, `可参与`, and first-live-order status match the corresponding golden state exactly. | API enum-to-copy mapping is exhaustive; every state has a named screenshot comparison. | Color-only meaning, “risk-free” language, success before confirmation, or visual state not present in the approved prototype. |
| `UI-05 Cost disclosure` | Open workspace and modal in actionable state. | Readiness/policy makes `$65` wallet cap, `$20` normal cap, `$2` emergency expected-loss cap, fee-free-only policy, and possible real loss understandable. | Values come from server policy and match enforced configuration. | UI-only limits differing from backend; promise of zero loss or guaranteed profit. |

### 16.8 Live integration and operations

| ID | Preconditions and action | Required visible UI | Required backend and persisted evidence | Forbidden behavior |
|---|---|---|---|---|
| `LIVE-01 Public data` | With network available, start the actual watcher and open the actual Dashboard. | Real top-20 events, explicit 24h volumes, heartbeat, venue, and current eligibility render; data source is not labeled fixture. | Real Gamma response, WebSocket subscription, same-batch CLOB read, timestamps, and SQLite signal read/write all succeed. | Fixture/mock substituted for this scenario; stale data labeled live. |
| `LIVE-02 Authenticated non-mutating preflight` | With dedicated wallet configured, run acceptance preflight. | Only masked wallet, balance, allowance, region, relayer, and readiness appear. | Real Keychain retrieval, derived-address match, geoblock, authenticated account reads, exact two-leg construction/signing without POST, official merge-capability presence, and relayer authentication/readiness pass; controlled execution tests cover merge construction/response handling; logs are secret-clean. | Any live order/merge/approval; calling the SDK's mutate-only merge method during acceptance; secret in output; unsigned mock substituted for the two-leg signing check. |
| `LIVE-03 First-order status` | Run acceptance before any real user trade and exercise the success path only through the controlled executor. | Live Dashboard remains `实盘链路尚未完成首单验证`; controlled success never changes it. | Code path requires durable real venue fill and merge references before the flag can change; the first later user-confirmed live success is verified operationally when it occurs. | Acceptance/test double or manual config setting the live-verified flag; automatic canary order. |
| `OPS-01 launchd continuity` | Install/restart accepted launch configuration and inspect service/process manager. | Dashboard health becomes fresh after restart. | `launchctl` and process inspection show expected PID, loopback bind, working directory, accepted SHA, `caffeinate -s`, and fresh timestamped logs/heartbeat. | Old pre-change process still serving; wrong worktree/SHA; only unit-test evidence. |
| `OPS-02 Crash restart` | Terminate the watcher process without corrupting storage; observe launchd. | Brief degraded/recovering state, then the correct reconciliation outcome. | launchd starts a new PID; SQLite integrity passes; startup reconciliation and fresh heartbeat appear in logs. | Silent death, duplicate concurrent process, readiness before reconciliation. |
| `OPS-03 Exact-SHA deployment readiness` | Before the gate, deploy the committed candidate SHA using the production launchd path and open the review URL. | Review URL returns HTTP 200 and shows the candidate UI/data state. | PID, cwd, candidate SHA, start time, fresh logs, heartbeat, and HTTP 200 are captured; the installer is ready to repeat the exact-SHA restart after PASS. | Dirty source, wrong SHA, stale process, or asking the user to run acceptance. |

## 17. Focused Development Verification

Before the final gate, development uses focused checks:

- domain unit tests for normalization, Decimal math, sizing, sorting, and state
  transitions
- SQLite migration/restart/idempotency tests
- adapter contract tests using controlled official-client responses
- browser tests for each UI state and mutation flow
- direct watcher command against real public data
- direct authenticated non-mutating preflight
- direct launchd/process/log inspection where practical

`make acceptance` is not run after intermediate changes. It is the last gate
before review handoff.

## 18. References

- [Prediction Market Arbitrage Compendium implementation roadmap](https://github.com/Oceanjackson1/Prediction-Market-Arbitrage-Compendium/blob/main/analysis/implementation-roadmap.md)
- [Polymarket place orders and FOK behavior](https://docs.polymarket.com/trading/place-orders)
- [Polymarket authentication](https://docs.polymarket.com/api-reference/authentication)
- [Polymarket clients and SDKs](https://docs.polymarket.com/api-reference/clients-sdks)
- [Polymarket CTF merge](https://docs.polymarket.com/trading/ctf/merge)
- [Polymarket gasless transactions](https://docs.polymarket.com/trading/gasless)
- [Polymarket geoblock endpoint](https://docs.polymarket.com/api-reference/geoblock)
- [Polymarket fee documentation](https://docs.polymarket.com/trading/fees)
- [Official Python CLOB client v2](https://github.com/Polymarket/py-clob-client-v2)
- [Official beta unified Python SDK](https://github.com/Polymarket/py-sdk)
- [Open FOK precision issue](https://github.com/Polymarket/py-clob-client-v2/issues/59)
- [Open deposit-wallet authentication issue](https://github.com/Polymarket/clob-client-v2/issues/65)
