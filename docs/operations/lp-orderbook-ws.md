# LP order books over WebSocket (#487 A+B)

The default is `off`. This change provides the switch and local verification;
it does not authorize deployment, a Tokyo trial, or enabling `use`.
Subscriptions contain only exact current trial/income pool token identities.
Candidate-only markets, unrelated manual orders and position-only tokens do not
expand the subscription. Arbitrage monitoring is unchanged.

| Mode | Protection book | New-entry pricing | WS |
| --- | --- | --- | --- |
| `off` | Original REST path | REST | Stopped; actual cleanup retains ownership |
| `shadow` | REST; compare the copied existing queue decision | REST | Maintain and audit books |
| `use` | Synchronized WS for eligible current BUYs; otherwise REST | REST, including detached income reads and fresh manual previews | Maintain and audit books |

Use the existing authenticated, loopback-only operator command on the selected
host. A mode-only change uses the existing settings CAS, does not enable/pause
LP, and does not change budgets or the trading configuration version:

```sh
open-trader prediction-arb lp-auto config --orderbook-mode shadow --url http://127.0.0.1:8769 --json
open-trader prediction-arb lp-auto status --url http://127.0.0.1:8769 --json
```

These are operator procedures, not executed production actions. Substitute the
existing service's verified loopback port if different. Preserve the current
session/CSRF authentication and host access controls. Invalid modes are rejected.
Legacy settings without the field mean `off`. An absent mode in a legacy status
projection also means `off`; runtime diagnostics publish the effective mode.

## Two-hour shadow evidence

After separately approved deployment and shadow activation, retain the service's
`prediction_lp_orderbook_shadow` records for at least 7,200 comparable seconds.
The records contain only decision booleans, fixed reasons, a UTC observation time
and coverage; no account/order payload or credentials. They use the existing
bounded best-effort diagnostic queue, so a broken or blocked sink cannot delay a
real REST cancellation. Lost or missing evidence is not a passing trial.

The existing status JSON publishes
`state.diagnostics.async_reads.orderbook`. Save **only that subtree** to a
host-local evidence file, for example `lp487-shadow-summary.json`; the full LP
status can contain account/order information. Record the selected code identity,
trial start/end and restart/mode boundaries alongside this sanitized file.
The subtree contains bounded per-token routes, latest fallback reason/time,
reconciliation mismatch counts, connection state, comparison counts/reasons,
and arrival-to-update / wake-to-decision latency distributions (last 256 values).
Retry diagnostics also include `failure_streak`, `next_retry_monotonic` and
`retry_reason`. A status read copies memory and does not issue network requests.

Review all of the following:

- The unchanged rule is `front_estimate / level_total <= 0.5`.
  **REST can cancel ⇒ WS can cancel** is mandatory. With baseline 100 and own
  remaining 20, REST total 30 gives front 10 and ratio 1/3; WS total 120 gives
  front 100 and ratio 5/6 and must be recorded as a mismatch.
- UNKNOWN on either side is **not comparable**, never consistent. No samples
  produce `UNKNOWN`, not PASS. Any mismatch or not-comparable sample produces
  FAIL for the current process report. Start a fresh, explicitly managed trial
  after investigating failures; do not hide old logs or automatically retry.
- Comparable samples must be no more than ten seconds apart to build continuous
  coverage. The report records the longest continuous segment. A 7,200-second
  segment alone does not prove every token or market condition was exercised;
  inspect current-token coverage and the retained logs, including intervals
  without active protected orders. A process restart resets in-memory coverage.
- Review full-book reconciliation differences, fallback reasons and measured
  latencies. Local controlled-clock tests do not establish Tokyo latency or a
  two-hour production result. No performance number is promised.

Only after Ray reviews the actual evidence and separately authorizes the live
switch, run the same command with `--orderbook-mode use`. Shadow→use keeps the
connection and latest synchronized data; it requires no restart. The diagnostic
report never changes the mode automatically.

Rollback is the same command with `--orderbook-mode off`. It stops WS and restores
the original REST path, without changing the running/paused control or budgets.
Read back committed/effective settings and runtime diagnostics. Pending cleanup
must retain the service owner; an accepted setting is not proof of socket drain.
Do not restart, deploy or change trading controls as an implied rollback action.

## Data and ownership contract

There is one explicitly owned WS connection on the existing `lp-market-io` loop,
using direct `websockets==15.0.1` and the existing proxy cleanup compatibility
patch. It sends text `PING` every ten seconds. Actual received data or `PONG`
proves liveness. Processing a queued message does not renew that proof. A quiet
synchronized book is usable through ten seconds since proof; fifteen seconds of
silence invalidates the connection. Reads and diagnostics never renew timestamps.
Reconnect delays grow from 0.25 seconds through 0.5, 1, 2, 4, 8 and 16 to a
30-second cap, with 0–20% positive jitter capped at 30 seconds. Only validated
book data (including row-level price changes after REST synchronization) or text
`PONG` resets the failure streak; handshake success and malformed
messages do not. Off and close interrupt a pending retry wait.

Full books replace all levels. BUY/SELL changes replace absolute quantities;
zero removes a level. Identity, timestamp, Decimal validity, crossing and supplied
best prices are checked. Invalid updates invalidate only their token. A disconnect,
silence or full application queue invalidates the entire connection generation.
The application queue and each pre-seed delta buffer hold at most 256 events;
overflow never drops through into a supposedly synchronized book. Retired
subscription deliveries and prior-connection buffers cannot seed a new owner.
The 20 ms consumer poll never cancels an outstanding queue getter. A dequeued
message is consumed once or its connection generation is invalidated; delayed
consumption does not renew receive proof.

Synchronization accepts a full WS book or an identity/timestamp-validated REST
snapshot at T followed by buffered updates at or after T in receive order. A
pending REST completion cannot overwrite a newer full WS book or subscription.
Synchronized tokens are audited against REST every 60 seconds, deduplicated by
token. Compare every bid/ask level only when REST is at least as new. A mismatch
replaces the book and increments diagnostics; an older REST book cannot replace
new WS data or leak into the accepted audit result. Shadow ordinary REST reads do
not repeatedly reseed an already synchronized WS book.

Unavailable WS uses the unchanged five-second start cadence, five-second request
deadline, provider/account Retry-After and actual response cleanup. Metadata and
reward reference caching retain their original cadence and timestamps. Eligible
WS results pass through the original exact MarketAttempt identity, watermark,
freshness and UNKNOWN acceptance. Losing an accepted WS version lowers only its
market confidence; it does not create another HTTP attempt or change real read
failure counts. An invalidation cannot clear a later REST fact. Complete pending
REST mailboxes retain their original provenance and timestamps during WS outages.
Actual HTTP failures and timeouts still follow the original failure accounting.
A failed required metadata/reward refresh or
expired attempt discards the local-completion reference template; subsequent
book-only changes cannot report a full success or clear its read failure. Only
a valid complete ordinary refresh restores that template. They cannot restore funding facts or replay a
native action. A qualifying bid-depth update at or above an own BUY price requests
one outstanding token check; protection collects the latest local result first.

WS runner, receiver, transient queue waiter and close work are accounted for in
runtime diagnostics and the existing ownership drain. Backoff awaits its stop
event in the owned resident runner, with no separate `Event.wait` task. The HTTP pool count is
unchanged; an owned WS connection adds one to the declared connection limit.
No additional I/O thread or logging thread is created by this feature. `off`
retains the original resource counts. Close remains false until both WS and REST
cleanup actually end.
