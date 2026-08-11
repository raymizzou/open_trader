# Prediction Service Read-Load Boundaries

**Issue:** #34
**Status:** Approved design
**Date:** 2026-08-11

## Purpose

Keep browser polling from exhausting the same Prediction Service process that
owns monitoring, reconciliation, and order execution. This is a prerequisite
for packaging and later routing production Prediction traffic to port 8769.

Downtime simplifies a release cutover, but it does not reduce steady-state
polling after the cutover. Read load must therefore fail explicitly before it
can consume unbounded threads, file descriptors, or SQLite connections.

## Evidence

The independent service still uses an unbounded `ThreadingHTTPServer`.
Diagnosis against the actual 8769 request path produced two deterministic
signals:

- 48 deliberately slow history requests created 48 concurrent handlers and
  102 process file descriptors. All requests eventually returned 200, and the
  process returned to one thread and five file descriptors after completion.
  The new service did not show a persistent leak, but it did show unbounded
  concurrent work.
- A read-only backup of the current 156 MB production database contained
  34,085 signals. One four-tab-equivalent wave (four state plus four signals
  history requests) returned all four state responses, but three of four
  history requests exceeded the five-second client timeout. The eight read
  handlers settled 4.36 seconds later.

The root problem is boundedness plus repeated full history projection, not
evidence for a process-wide SQLite pool or a new persistence architecture.

## Goals

- Bound all Prediction HTTP work before a handler thread is created.
- Keep the expected four-tab, eight-request wave within the existing
  five-second client boundary.
- Collapse identical concurrent history reads without changing the current
  history response, filtering, ordering, pagination, or total-count contract.
- Return an explicit, retryable overload response instead of timing out or
  accepting an unbounded queue.
- Leave monitoring, execution, database schema, Gateway, Dashboard, and release
  installation behavior unchanged.

## Non-goals

- No SQLite connection pool or long-lived shared connection.
- No SQL pagination rewrite or history contract change.
- No general cache framework, distributed cache, adaptive capacity, or
  per-route priority system.
- No Gateway, Legacy Dashboard, UI, launchd, release, or production cutover.
- No live order or production-ledger mutation during verification.

## HTTP Concurrency Boundary

The Prediction HTTP server owns one fixed global capacity of eight requests.
Health, state, history, and every mutation share the same capacity.

Before creating a handler thread, the server attempts to reserve one slot
without waiting. If no slot is available, it writes a minimal response directly
to the accepted socket and closes it:

```http
HTTP/1.1 503 Service Unavailable
Content-Type: application/json; charset=utf-8
Retry-After: 1
Connection: close

{"error":"prediction service busy"}
```

Every admitted request releases its slot in `finally`, including parsing,
authentication, response-write, client-disconnect, and handler exceptions.
There is no request queue beyond the operating system's bounded listen backlog.

Eight is a fixed v1 limit, not configuration. It admits the measured normal
four-tab wave while bounding bursts. A later change requires new load evidence.

## History Single-flight Cache

Only successful GET history responses are cached. The cache belongs to one
server instance and uses the validated query tuple `(kind, limit, offset)` as
its key.

- TTL is one second, measured with `time.monotonic()`.
- The first admitted request for a missing or expired key computes the existing
  `prediction_history_payload` unchanged.
- Other admitted requests for the same key wait for that in-flight computation
  and reuse the completed response value for serialization; handlers do not
  mutate it.
- A waiter stops after five seconds and receives a history-unavailable 503.
- A failed computation wakes every waiter, is not cached, and returns a
  history-unavailable 503. An expired success is not served as stale fallback.
- Cache entries are replaced atomically after a successful computation.
- There is no background refresh thread and no persistence.

State, health, and POST mutations are never cached. History may therefore be at
most one second behind the ledger, while execution and control surfaces remain
live.

## Error Semantics

Overload is distinct from runtime unavailability and read failure:

- capacity exhausted: HTTP 503, `prediction service busy`, `Retry-After: 1`;
- history computation failed or single-flight wait exceeded five seconds: HTTP
  503, `prediction history unavailable`;
- invalid history query: existing HTTP 400 contract;
- unavailable Shadow or production runtime: existing HTTP 503 contract;
- mutation authentication, CSRF, validation, idempotency, and audit behavior:
  unchanged.

No overload or read-error path reads a mutation body, claims success, returns a
partial history, or serves an expired value.

## Lightweight Evidence

Successful `/healthz` responses add an `http_load` object:

```json
{
  "limit": 8,
  "active": 1,
  "overload_rejections": 0,
  "history_cache_hits": 0,
  "history_cache_misses": 0
}
```

`active` includes the `/healthz` request that is producing the response.

Counters are process-local integers protected by the server-owned
synchronization primitives used by the capacity/cache state. They are
diagnostic evidence, not a new metrics system or durable audit record.

## Verification

### Automated regression

1. Hold eight admitted handlers and send 40 more requests. At most eight handler
   threads may exist; overflow must return the exact busy 503 with
   `Retry-After: 1`, without entering the handler.
2. Release the eight handlers and prove active count, handler threads, sockets,
   and test SQLite connections return to baseline.
3. Send eight identical history requests. All return the same 200 response and
   the underlying history projection runs once.
4. Before TTL expiry, another identical request is a cache hit. After expiry,
   exactly one new computation occurs.
5. A computation exception and a five-second waiter timeout return the explicit
   history-unavailable 503, wake waiters, cache nothing, and release every slot.
6. Mixed GET and POST requests prove that all methods share the same eight-slot
   capacity.
7. Existing frozen state/history contracts, Shadow mutation rejection, and
   production mutation tests remain unchanged and green.

### Direct 30-minute load run

Use a temporary SQLite backup and fake trading clients; never open the
production ledger for writes or submit an order.

- Every 1.5 seconds, issue the measured four-tab wave: four state and four
  identical signals-history requests.
- Every normal wave must return eight HTTP 200 responses, with no connection
  error and no response exceeding five seconds.
- After each completed wave, server handler threads, process file descriptors,
  and open copied-DB/WAL handles must return to their ready baseline plus at
  most two transient descriptors.
- Inject a 48-request slow burst. No more than eight handlers enter work; the
  remaining requests return the exact busy 503 without timeout.
- After the burst and at the end of 30 minutes, `active` is zero and thread,
  descriptor, and copied-SQLite handle counts return to the same baseline
  bounds.

The direct-run report records command, Git SHA, database size and row counts,
request counts by status, maximum latency, maximum active handlers, baseline and
final thread/FD/SQLite-handle counts, and the final `/healthz` `http_load`
object.

## Scope Guard

Completion of #34 proves that polling cannot consume unbounded 8769 HTTP
resources and that the current production-sized history remains readable under
the expected four-tab load. It does not authorize #44 release installation,
Gateway cutover, Legacy shutdown, or production execution.
