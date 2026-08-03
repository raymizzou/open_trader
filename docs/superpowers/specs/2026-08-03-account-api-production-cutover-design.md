# Account API Production Cutover Design

## Goal

Issue #21 promotes the Account API from an isolated shadow reader to the
production read interface and moves the browser's Account views to
`GET /api/v1/account/snapshot` through Frontend Gateway.

After the cutover:

- Account Sync Worker remains the only Account writer;
- Account API and Account Sync Worker run the same Account release SHA;
- Frontend Gateway routes only the Account snapshot path to Account API and
  keeps every other existing API route on Legacy Dashboard;
- the browser never reads or falls back to the Account fields in
  `/api/dashboard`;
- Account and Legacy failures are isolated in both directions; and
- rollback changes the Account release, not the browser back to Legacy
  Account ownership.

The stable browser and operator URL remains `http://127.0.0.1:8766/`.

## Chosen Approach

Frontend Gateway becomes a two-upstream path router:

```text
Browser
  |-- /api/v1/account/snapshot --> Frontend Gateway --> Account API :8768
  `-- every other /api/*         --> Frontend Gateway --> Legacy :8767
```

The Account route is an exact path match after URL parsing. Its query string,
request headers allowed by the existing trust boundary, status, response
headers, ETag, and body are forwarded without business transformation.
Frontend Gateway does not parse or aggregate an Account snapshot and does not
read Account files.

The browser owns the presentation composition. It keeps Account state separate
from the remaining Dashboard state and joins Account positions to
Trend/Research enrichment by stable opaque IDs.

### Rejected alternatives

- Legacy Dashboard proxying Account API would leave Legacy in the Account read
  path and would not complete R3.
- A new BFF or aggregation service would duplicate Frontend Gateway and create
  another release and failure boundary without solving a current need.
- Automatic or per-request fallback to `/api/dashboard`, `/api/quotes`, or raw
  publications would hide migration failures and restore a second Account
  authority.

## Account API Production Mode

Account API gains an explicit `shadow` or `production` runtime mode. The CLI
and installer default remains `shadow`, so a checkout cannot become a
production Account source by omission.

Both `/healthz` and the runtime log expose the selected mode. Production
installation succeeds only when all of the following agree:

- expected mode is `production`;
- the launchd PID is the sole `127.0.0.1:8768` listener;
- process cwd is the immutable release checkout;
- API Git SHA equals the expected release SHA;
- Worker heartbeat Git SHA equals the same release SHA; and
- snapshot publication is complete and contract-valid.

### Shadow traffic guard

Frontend Gateway removes any caller-provided internal route marker and sets:

```http
X-Open-Trader-Account-Route: production
```

when it forwards the Account snapshot request. An Account API running in
`shadow` mode rejects a request carrying that marker with a contract-safe
`503 account_api_shadow_only` using the frozen v1 unavailable envelope. A
production Account API accepts it.

The marker is not authentication. It is a fail-closed mode guard that prevents
a shadow process accidentally installed on `8768` from serving browser
traffic. Direct parity and operator diagnostics omit the marker and remain
available in both modes.

The frozen Account v1 snapshot schema, ETag calculation, publication reads,
freshness rules, broker selection, quote semantics, and Worker cadence do not
change in R3.

## Frontend Gateway Boundary

`FrontendGatewayConfig` gains a loopback-only Account upstream, defaulting to
`127.0.0.1:8768`. The existing Legacy upstream remains
`127.0.0.1:8767`.

The Account route reuses the existing proxy implementation and preserves:

- loopback validation;
- same-origin and Origin handling;
- request body limits;
- Host and Referer rewriting;
- hop-by-hop header removal;
- query strings;
- multiple response headers;
- response status, reason, body, and ETag; and
- broken-client handling.

If Account API returns a response, including a contract `503`, Gateway passes
that response through unchanged. Only connection, timeout, or HTTP protocol
failure before an Account response causes Gateway to synthesize:

```json
{
  "schema_version": "open_trader.frontend_gateway.error.v1",
  "code": "account_module_unavailable",
  "message": "Account Module is unavailable"
}
```

with HTTP 503. It never retries against Legacy or raw files.

### Gateway liveness and deployment readiness

Gateway `/healthz` remains process/config liveness and returns HTTP 200 while
Gateway can serve requests. It preserves the existing `upstream_status` field
for compatibility and additionally reports Legacy and Account upstream status
separately. Account is `ok` only when its health payload identifies Account
API in `production` mode; shadow, malformed, unreachable, or non-200 health is
reported as unavailable/non-production without making Gateway itself exit.

Runtime failure of either upstream therefore does not trigger launchd restart
of Gateway. The R3 installer has a stricter readiness gate: it succeeds only
when Legacy is healthy and Account is healthy in production mode.

## Browser State And Data Flow

The static shell loads independently of either upstream. On startup the
browser begins two independent requests:

- `/api/dashboard` for the remaining Legacy-owned Trend, Research, Prediction,
  Kelly, backtest, and transitional statement surfaces; and
- `/api/v1/account/snapshot` for Account-owned summary, broker summaries,
  positions, cash, prices, valuation, weights, and source status.

Failure isolation is bidirectional:

- Account failure does not clear or block healthy Trend, Research, Prediction,
  or other Legacy-owned state.
- Legacy `/api/dashboard` failure does not clear or block a healthy Account
  snapshot.
- Shared page containers degrade by data owner rather than treating the whole
  container as one module. For example, Account real-position views can be
  unavailable while Trend simulation/report views in the same panel remain
  usable.

The browser keeps dedicated Account snapshot, ETag, transport error,
in-flight, and polling state. Account renderers read only that state. Although
`/api/dashboard` temporarily continues to carry Account fields for unmigrated
production consumers, browser code must ignore those fields in success,
failure, and initial-load paths.

### Polling and conditional requests

The Account poll has these fixed semantics:

- start immediately and repeat every five seconds;
- allow at most one request in flight;
- abort a request after four seconds;
- send the last successful ETag as `If-None-Match`;
- do not use exponential backoff for the loopback service; and
- let native browser background throttling handle hidden tabs.

Responses update state as follows:

- `200 healthy`: atomically replace the snapshot and ETag, clear transport
  error, render current Account data, and allow Account-dependent actions.
- `200 stale`: atomically replace the snapshot and ETag, show the server's
  stale sources/timestamps/reasons, and disable actions that require healthy
  Account facts.
- `304`: keep the cached snapshot and its original `healthy` or `stale`
  domain state, clear any later transport/unavailable error, and treat the
  Account connection as recovered.
- contract `503`, Gateway `503`, timeout, or network failure after a previous
  success: preserve the last snapshot only as visibly frozen historical
  context, show Account unavailable with its last accepted time, and disable
  Account-dependent actions.
- failure before the first successful snapshot: show an empty Account
  unavailable state. Never fill it from Dashboard fields.

The next `200` or `304` recovers automatically without a full-page reload.

### Remove the browser quote reload loop

The browser stops requesting `/api/quotes`. It also removes the current chain
where a quote poll reloads the entire `/api/dashboard` payload. Account
positions already carry `last_price`, `price_kind`, `price_as_of`, market
values, and weights, so no new top-level `quotes` field or client quote
calculation is needed.

The Legacy `/api/quotes` endpoint remains temporarily available for unmigrated
production consumers and is removed with other Legacy Account reads in #23.

## Stable-ID Composition

The current browser joins Account positions to enriched Dashboard holdings by
broker, market, symbol, and array index. R3 removes this guessed identity.

Account Module exposes its existing deterministic `instrument_id` construction
as a reusable public function. The transitional Legacy holdings projection
adds the resulting opaque `instrument_id` to each non-Account enrichment row;
it does not copy Account amounts, status, price, valuation, or weight.

The browser then:

- uses `position_id` as the stable key for each Account row;
- joins Account positions to Trend/Research enrichment by `instrument_id`;
- treats IDs as opaque and never parses them; and
- refuses to fall back to market/symbol matching when an ID is missing or
  ambiguous.

When enrichment cannot be joined uniquely, the browser still displays the
Account-owned position facts and marks the non-Account detail unavailable.

## Transitional Statement Boundary

Statement import remains on its existing Legacy command for R3. Its HTTP
route, parsing, publication behavior, and Trend coupling are migrated in #22.

Because the command changes Account publication, its browser control is
enabled only when the current Account snapshot is `200 healthy`. It is disabled
before first Account load and for `stale`, `503`, timeout, or transport failure.
This is a presentation safety gate, not a change to the statement command.

## Two-SHA Release Checkpoint

The first production Account cutover has no earlier production Account release
to return to because R2 is explicitly shadow-only. R3 therefore creates two
immutable commits in the same branch.

### Baseline SHA

The baseline commit contains every Account-side R3 change:

- production mode;
- production installer/readiness behavior;
- the shadow traffic guard; and
- the public stable-ID construction helper.

It changes no Account v1 representation or Worker publication behavior.
Gateway and browser remain uncut while this SHA is deployed as the first
production Account release.

The baseline checkpoint is machine-gated rather than a separate operator
review. Its exact SHA, PID, cwd, lock transition, Worker heartbeat, successful
post-start Account refresh, successful post-start quote publication, Account
generation, quote timestamp, API health, and logs are recorded. Any failed
check stops R3 before routing traffic.

### Cutover SHA

The cutover commit adds only Gateway routing, the transitional holdings
`instrument_id` projection, browser composition/polling, Dashboard acceptance
coverage, and related documentation. Between baseline and cutover:

- Worker publication schema and cadence are identical;
- Account API contract and persistence behavior are identical; and
- broker, price, valuation, and freshness semantics are identical.

If implementation discovers that Account-side behavior must change after the
baseline is frozen, that baseline is discarded. A new baseline is created and
fully proven before Gateway cutover.

Normal final deployment runs Gateway, Legacy, Account API, and Worker from the
accepted cutover SHA for simple whole-system evidence. The safety invariant is
narrower: Account API and Worker must match each other. During the independent
rollback drill, Gateway/Legacy intentionally remain on the cutover SHA while
Account API/Worker run the baseline SHA.

## Production Cutover Sequence

Each SHA is deployed from an immutable checkout. A checkout used by a running
process is never switched in place.

1. Record the current R2 API and Worker PID, cwd, SHA, lock owner, Account
   publication timestamps, quote publication timestamp, and listener.
2. Preflight the baseline CLI, plist, installer, API mode, and contract without
   routing browser traffic.
3. Stop the old Worker. Confirm its PID is gone and the writer lock is released.
4. Start the baseline Worker. Confirm its heartbeat SHA and PID, then wait for
   at least one successful Account refresh and one successful quote publication
   after the new process start. Success is proven by accepted timestamps; the
   content hash need not change when broker facts are unchanged.
5. Replace the old shadow API with baseline Account API in production mode.
   Confirm the sole listener, release match, healthy snapshot, ETag/304, and
   parity with the same publication.
6. Record the baseline evidence and freeze it as the Account rollback target.
7. Preflight the complete cutover Gateway and browser on temporary ports against
   the production Account API. Do not modify the live `8766` route yet.
8. Stop the baseline Worker, confirm PID removal and lock release, start the
   cutover Worker, and wait for post-start Account and quote success.
9. Start cutover Account API, then prove production mode, API/Worker SHA match,
   publication agreement, ETag/304, and fresh logs.
10. Deploy cutover Gateway/Legacy/static assets only after the Account pair is
    healthy. Confirm `8766` routes Account to `8768` and other APIs to `8767`.

A brief mismatch/unavailable window while only one half of an Account release
has changed is allowed and must remain visible. At no point may two Workers own
the writer lock.

## Rollback And Re-Cutover Drill

Rollback never changes the browser to Legacy Account fields:

1. Leave cutover Gateway, Legacy, and browser assets running.
2. Stop the cutover Worker and confirm PID removal and writer-lock release.
3. Start the baseline Worker and wait for successful post-start Account and
   quote publication.
4. Replace cutover Account API with baseline Account API from the same baseline
   SHA and verify production health, release match, snapshot, ETag, and logs.
5. Confirm the browser recovers through `/api/v1/account/snapshot`; no
   `/api/quotes` or Legacy Account read is used.
6. Repeat the safe sequence to return Account Worker/API to the cutover SHA
   before final acceptance.

Mixed Gateway/Account SHAs during this drill are expected and are not
`account_release_mismatch`. Only API/Worker mismatch within Account fails.

## Fail-Closed Dashboard Installation

The current Dashboard stack installer automatically restores the single-process
Legacy Dashboard when Gateway readiness fails. R3 removes that automatic
fallback from stack installation because it would silently restore Legacy
Account ownership at `8766`.

Candidate Gateway and routing are validated on temporary ports before live
replacement. A live installation failure returns nonzero and remains visible;
it does not claim success or invoke single-process mode.

The explicit `--mode single` command remains temporarily as a documented
break-glass tool until #23 removes Legacy ownership. R3 deployment, rollback,
tests, and acceptance must not invoke it.

## Verification

### Automated checks

Focused tests prove:

- production and shadow Account modes, mode health, and the shadow route guard;
- Account API launchd mode/PID/cwd/SHA/listener readiness;
- exact Gateway route selection and unchanged handling of every other route;
- transparent Account `200`, `304`, contract `503`, ETag, body, and headers;
- synthesized `account_module_unavailable` only for transport failure;
- caller cannot inject or override the internal production marker;
- Gateway liveness reports two upstreams while installer readiness requires a
  production Account API;
- Dashboard stack failure does not auto-restore single-process Legacy;
- browser five-second polling, four-second timeout, single in-flight request,
  ETag reuse, `304` recovery, and `200 stale`/`503` state transitions;
- first-load and later Account failures never consume Legacy Account fields;
- divergent test Account values in `/api/dashboard` cannot affect rendered
  Account values;
- Legacy failure leaves Account usable and Account failure leaves other modules
  usable;
- Trend simulation/report views survive Account failure in shared containers;
- stable-ID composition and fail-closed missing/ambiguous enrichment;
- statement controls require `200 healthy` Account state; and
- browser source and requests no longer use `/api/quotes`.

The focused suites are followed by the complete pytest suite. No acceptance run
occurs after intermediate changes.

### Direct runtime checks

Before the final gate, direct checks prove:

- baseline-to-cutover and cutover-to-baseline-to-cutover writer safety;
- post-start Account and quote publication for each Worker release;
- one Worker PID and one lock owner at every step;
- Account API/Worker SHA agreement and production mode;
- Account parity, ETag, and 304 behavior through Gateway;
- Legacy and other module routes still reach their current owner; and
- all long-running processes use the intended immutable cwd/SHA with fresh
  logs and no startup errors.

A controlled live fault check stops Account API only. It verifies that Gateway
returns explicit Account 503, the browser freezes/marks Account data, and
Legacy-owned modules continue. Restarting the same candidate Account API must
recover the browser on the next `304` or `200`. The runtime is returned to a
healthy stable state before final acceptance.

### Final Dashboard gate

`make acceptance` is the final verification command. It must observe the real
production Account route, a live Account/quote refresh, browser conditional
polling, process versions, logs, desktop/mobile flows, and the existing
Dashboard behaviors.

- `PASS`: redeploy the exact accepted SHA and continue to handoff.
- `FAIL`: diagnose and fix, then repeat the required verification and final
  gate.
- `BLOCKED`: report the environment blocker and do not substitute curl,
  fixtures, mocks, or screenshots.

After PASS, deploy the unchanged accepted SHA again. Verify new PIDs, cwd, SHA,
Account release match, fresh logs, the three listeners, and HTTP 200 from
`http://127.0.0.1:8766/` before asking for operator review.

## Documentation And Review Boundary

Implementation updates the Account/Gateway operations references and adds the
dated operator-facing `CHANGELOG.md` entry before any merge. The runbook records
both immutable SHAs and exact cutover/rollback commands.

Final evidence is posted to Issue #21 after the target is restated and checked
for secrets. Issue #21 remains at the operator-review gate; #22 is not unlocked
until the operator explicitly accepts R3.

## Non-Goals

- No Dashboard layout, interaction, visual-language, strategy, report,
  execution, broker-selection, price, valuation, or sync-cadence redesign.
- No Account v2, new quote endpoint, WebSocket, delta API, event stream, cache,
  database, queue, service discovery, container, or new dependency.
- No removal of Legacy `/api/dashboard` Account fields, `/api/quotes`, raw-file
  reads, or other production consumers; #23 owns that cleanup.
- No migration of statement HTTP or domain semantics; #22 owns it.
- No migration of Trend, Research, Prediction, Kelly, or backtest modules.
- No browser connection directly to port `8768`.
- No automatic fallback, dual writer, or silent fake-healthy state.
