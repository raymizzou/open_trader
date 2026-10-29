# LP order books over WebSocket (#487 A+B)

The default is `off`. This change provides the switch and local verification;
it does not authorize deployment, a Tokyo trial, or enabling `use`.
Subscriptions contain only exact current trial/income pool token identities.
Candidate-only markets, unrelated manual orders and position-only tokens do not
expand the subscription. Arbitrage monitoring is unchanged.

| Mode | Protection book | New-entry pricing | WS |
| --- | --- | --- | --- |
| `off` | Original REST path | REST | Stopped; actual cleanup retains ownership |
| `shadow` | REST; compare the same frozen bid-level decision | REST | Maintain and audit books |
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

Deployment, a Tokyo shadow run and any mode activation need separate approval.
Local controlled-clock tests do not establish actual two-hour coverage, latency,
CI success or deployment readiness. Local boundary tests include a frame actually
received at the inclusive two-second deadline and applied later, with a separate
2.001-second negative case. The original deadline remains fixed: only relevant
already-received evidence can hold a comparison pending application.

After separately approved deployment and shadow activation, retain the service's
`prediction_lp_orderbook_shadow` records and the selected code identity, trial
start/end, restart and mode boundaries. The existing status JSON publishes
`state.diagnostics.async_reads.orderbook`. Save **only that subtree**, for example
to a host-local `lp487-shadow-summary.json`; full LP status can include private
account/order facts. Status and owner drains only copy existing observations.
They do not perform a new comparison, fetch a book or renew source timestamps.

Each comparison starts at an actual eligible BUY REST completion on the existing
market I/O loop. It captures the exact order/terms, process/cycle, baseline,
condition/token, local acceptance cutoff, own remaining quantity and fixed N.
The owner later reads that observation by source/context identity. A cached REST
book cannot become a fresh sample through repeated owner evaluation.

Two separate checks apply:

- **Public book check:** sort both bid vectors by price descending, then select
  the first N public price/quantity levels. Compare against immutable independent
  WS versions in the recent two-second history, including a quiet active anchor,
  and allow a matching future WS receipt through REST completion +2 seconds.
  Levels below N do not change the compared prefix. REST synchronization alone
  is not independent WS evidence. Missing continuity or retained history is
  reasoned not-comparable evidence, never an assumed match.
- **Cancellation check:** evaluate the existing #488 fixed-level rule with the
  same frozen inputs. REST cancellation requires an earlier WS cancellation or
  one received within two seconds, inclusive. Signed timing is WS receipt minus
  REST completion: negative means WS led, positive means WS lagged. A later
  successful frame cannot erase a missed cancellation. Earlier or transient
  WS-only changes are report-only counters, not failures by themselves. REST
  remains the real cancellation owner; shadow sends no native cancellation.

The newest connection proof and the earliest qualified cancellation evidence are
separate facts. A later PONG keeps liveness current but cannot retime a valid
queued frame. Each frozen context retains its first qualifying confirmation for
that token synchronization identity; a validated frame can also supply its own
original message confirmation. An older quiet book still needs a real proof
strictly after local acceptance. A first qualifying proof after the fixed
cancellation deadline remains late, even when the book itself arrived earlier.
Temporary freshness expiry keeps this captured proof only while the actual
connection, member and synchronization era remain unchanged. The unavailable
book remains UNKNOWN and cannot authorize use. Invalidation, reconnect or member
retirement clears the proof, including when the book was already unavailable.

A removed order's frozen comparison context and subscription remain observed for
two seconds. A new order cannot inherit that context. The current limits are
64 history versions per token, 64 active plus 64 retired comparison contexts,
256 pending comparison windows, 512 remembered cancel source identities and
256 retained timing values. Capacity eviction, continuity gaps, close and mode
changes produce explicit unavailable/incomplete reasons instead of fabricated
agreement. Pending/incomplete evidence prevents PASS. Close still waits for
actual existing WS/REST cleanup.

Receipt metadata belongs to the existing application queue and its single queue
waiter, capped at queue capacity +1 (257 for the normal 256-event queue). It holds
only token/continuity and receive-time metadata, not an extra replay payload.
Receipt alone cannot validate a book or cancellation. At equality an empty real
consumer poll accounts for receive progress; already queued eligible evidence
keeps the window and its retired context pending until actual application or
explicit invalidation. Later-than-deadline receipts cannot hold that window.
Disconnect, member retirement, invalid updates, overflow and close clear the
relevant markers; a newly registered context does not wait for older markers.
Read-only status exposes `pending_receipts` and `receipt_capacity` without
advancing deadlines or consuming a frame. Normal initial synchronization may promote
same-connection/member queued markers from unsynchronized to synchronized within
the same synchronization era. An invalid frame advances that era even if no
full book had synchronized yet. Reconnects and member changes remain separate
identities; their old markers cannot hold or qualify a new observation.

An eligible actual REST completion with no registered baseline comparison context
increments `eligible_rest_reads`, `context_missing_reads` and the finite reason
`missing_comparison_context` once. It does not increment `samples`, invent a REST
cancel observation or claim a book match. A later actual read with a context can
be compared; repeated status/drain calls cannot repair or recount the earlier
gap. The UNKNOWN sample denominator is `samples + context_missing_reads`; its
numerator is `comparison_unknown_samples + context_missing_reads`. Per-context
samples and actual eligible reads are published separately because a REST read
can serve more than one frozen context.

The process report requires all of the following:

- At least 7,200 continuous seconds in shadow in the same process, observed at
  intervals no greater than ten seconds, plus at least 30 distinct actual REST
  cancel source observations and at least one comparison sample. Coverage is
  maintained independently of status reads and order turnover. Process restart,
  mode change or an observation gap breaks continuity; historical maximum
  coverage is diagnostic and cannot qualify a new segment.
- Zero book mismatches, audit mismatches, missed WS cancellations or unexplained
  WS UNKNOWN. UNKNOWN is never agreement. Explained UNKNOWN samples must be at
  most 2% by the published denominator, with no continuous unavailable segment
  longer than 15 seconds. Exactly 2% and 15 seconds are inclusive boundaries.
- No unresolved book/cancel windows or incomplete observations. No more than six
  unexpected disconnects in any observed one-hour window; exceeding six latches
  a failure for investigation even after the events leave the short display tail.
  Voluntary close has its own counter and does not count as an unexpected outage.

Mode changes and coverage restarts do not erase hard failures or original logs.
The report is `PENDING` until its prerequisites are met, `FAIL` for a hard failure
(or a qualified UNKNOWN fraction above the limit), and `PASS` only when all
implemented gates hold. A pristine unused diagnostic is `UNKNOWN`. No report
automatically enables use; inspect actual token coverage and retained evidence,
including unavailable intervals and times without active protected orders.

Connection logs publish connected/disconnected time, a finite reason, optional
numeric close code and sanitized exception class. Full cumulative counts remain
available after the bounded latest-16 event tail rolls over. Reasons distinguish
server close, invalid JSON, silence, queue overflow, send failure, other exception
and voluntary close. Audits publish attempts, compared/equal/different,
REST-older-skipped and unavailable counts. An attempt is not a completed match;
older REST data is skipped and never replaces a newer WS book.

Comparison logs contain decision booleans, token prefix, finite reason/outcome,
public first-N bid vectors, REST exchange/read times, WS exchange/confirmation/
actual receipt times, version and signed timing where available. They exclude
account/order/request IDs, credentials, quantities from private account state,
raw socket bodies and raw exception messages. The records use only the existing
bounded Queue32 best-effort diagnostic sink. A throwing or blocked sink cannot
block the real REST cancellation; missing logs are not successful trial evidence.

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
patch. It sends text `PING` every five seconds. Actual received data or `PONG`
proves liveness. Processing a queued message does not renew that proof. A quiet
synchronized book is usable through ten seconds since proof; fifteen seconds of
silence invalidates the connection. Reads and diagnostics never renew timestamps.
Reconnect delays grow from 0.25 seconds through 0.5, 1, 2, 4, 8 and 16 to a
30-second cap, with 0–20% positive jitter capped at 30 seconds. Only validated
book data (including row-level price changes after REST synchronization) or text
`PONG` resets the failure streak; handshake success and malformed
messages do not. Off and close interrupt a pending retry wait.

At the LP delivery boundary, `timestamp` is an aware UTC `datetime` for the
exchange time, matching REST. Internal ordering remains Decimal milliseconds;
`source_timestamp` retains the original numeric precision. Conversion uses no
float and floors sub-microsecond fractions, so an older source cannot round up
past a local acceptance cutoff. Missing, nonfinite or out-of-range exchange times
invalidate the token; local arrival time never substitutes for exchange time.

For BUYs accepted in the current process, use, first-baseline capture and shadow
share exact condition/token and captured local acceptance identity. REST keeps
its inclusive exchange-time check (timestamp >= acceptance). WS instead needs a
synchronized current connection/token identity plus genuine received message or
PONG confirmation strictly after acceptance. Quiet unchanged books can qualify;
an older exchange change time alone does not make them unusable. Conversely,
handshake, sent PING, cached owner reads and processing time cannot supply that
confirmation. The exchange `timestamp` and raw `source_timestamp` remain unchanged.

Until confirmation qualifies, eligible use falls back to ordinary REST without
inventing an HTTP failure or replacing a synchronized WS book with that fallback.
A later real PONG may qualify the existing quiet book and wake the existing
market-result path; normal healthy use adds no REST dual insurance between
60-second audits. Recovered orders with no local acceptance cutoff retain their
existing behavior. Invalid/untrusted sources remain UNKNOWN; captured cutoffs
never call back into future mutable owner state.

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
