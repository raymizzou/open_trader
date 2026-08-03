# Account API Shadow Runtime Design

**Issue:** #20
**Status:** Approved design, awaiting written-spec review
**Date:** 2026-08-03

## Context

R1 froze the Account v1 public contract and renamed the sole publisher to
Account Sync Worker. R2 deploys a read-only Account API shadow runtime over the
existing file-backed publication. It proves the new HTTP representation is
equivalent to the writer-owned Account facts before any Gateway cutover.

The normative response schema, IDs, generations, freshness rules, HTTP status
semantics, and evolution policy remain defined by
`docs/superpowers/specs/2026-08-03-account-v1-contract.md`. This document fixes
how R2 reads, serves, operates, and validates that contract.

## Goals

- Serve the real Account v1 snapshot from `127.0.0.1:8768` in shadow mode.
- Keep Account Sync Worker as the only writer and broker-connected process.
- Detect publication read races instead of combining different publication
  moments.
- Prove live parity directly against the raw Account publication.
- Operate Account API independently from Dashboard, Frontend Gateway, and
  other modules.
- Leave a reviewable launchd runtime with exact PID, cwd, SHA, logs, and HTTP
  evidence.

## Non-goals

- No Gateway route, Dashboard call, browser flow, CORS, or public exposure.
- No broker adapter calls, synchronization, file writes, or second writer.
- No change to the current persistence schema or its `dashboard_projection`
  compatibility key.
- No Trend or Research fields, recommendation, risk decision, or global
  `actionable` value.
- No WebSocket, delta endpoint, event stream, service discovery,
  operator-configurable runtime port, database, queue, cache, or new
  dependency.
- No production-mode switch in R2.

## Architecture

### Account Sync Worker

Account Sync Worker remains the sole writer. It continues publishing:

- `data/latest/account_sync_state.json`;
- `data/latest/quotes.json`;
- `data/account_sync/controller_status.json`.

R2 does not add another output file or change the Worker refresh loops.

### Account Snapshot Module

`src/open_trader/account_snapshot.py` is the deep read module. Its small
interface accepts the data directory, API release SHA, and current time, and
returns one of:

- a complete v1 snapshot with its strong ETag;
- a contract-shaped `503` envelope;
- an internal unstable-publication result used by HTTP and parity callers.

The implementation owns stable file reading, persistence validation, status
mapping, safe errors, opaque IDs, deterministic ordering, generation hashing,
and ETag construction. It reuses the existing Account persistence validation
and projection rules rather than copying broker models.

The historical name `dashboard_projection` is an internal R2 compatibility
input only. It is not exposed in the v1 response or the module interface.

### Account API Module

`src/open_trader/account_api.py` is the thin stdlib HTTP transport. It owns
loopback validation, route dispatch, response headers, runtime metadata, and
server lifecycle. It does not import broker adapters or Dashboard/Gateway
modules.

The CLI command is:

```text
open-trader account-api --data-dir data
```

The operator command fixes host `127.0.0.1`, port `8768`, and mode `shadow`.
The internal server factory may accept port `0` for isolated tests, but the
operator CLI exposes no host, port, or production-mode override.

## Stable Publication Read

Each snapshot attempt performs this sequence:

1. Read the exact bytes of Account publication as `A1`.
2. Read the exact bytes of Quotes publication as `Q1`.
3. Read the Worker heartbeat for schema and release SHA.
4. Read Account publication again as `A2`.
5. Read Quotes publication again as `Q2`.

The attempt is accepted only when:

- `A1 == A2` byte-for-byte;
- `Q1 == Q2` byte-for-byte;
- both accepted byte sequences parse and validate;
- `account.dashboard_projection.quote_as_of` equals
  `quotes.last_success_at`;
- the heartbeat schema is supported and contains a valid Worker Git SHA.

Atomic writer replacement plus byte equality pins one complete Account and
Quotes pair without adding locks. The reader retries immediately up to three
attempts. Three unstable attempts produce retryable
`503 account_publication_unstable`. The parity workflow reports the same case
as `BLOCKED`, because it cannot prove or disprove equivalence while the source
keeps changing.

The heartbeat is not compared byte-for-byte because its heartbeat timestamp
changes independently. Only its schema and Worker release SHA participate in
the snapshot contract.

## Snapshot Construction

The accepted `dashboard_projection` supplies the Account-owned summary, broker
summaries, position values, cash values, `generated_at`, and `quote_as_of`.
Accepted broker sources supply source kind, accepted data time, latest success,
and refresh status.

R2 then applies the R1 contract:

- add deterministic `instrument_id` and `position_id` values;
- rename `broker_positions` to `positions` and `cash_details` to
  `cash_balances`;
- sort broker summaries, positions, cash balances, and source broker keys by
  the contract rules;
- derive `account_generation` from accepted Account facts and timestamps;
- derive `snapshot_generation` from the complete visible response;
- construct the strong `account-v1-<hex>` ETag.

`generated_at` is the projection publication time. It is never replaced with
the HTTP request time, so an unchanged publication produces byte-identical
snapshot content and the same ETag.

## Freshness and Errors

The response follows the R1 status rules:

- a valid latest refresh is `200 healthy`;
- a failed refresh with a complete last accepted publication is `200 stale`;
- missing, malformed, unsupported, incomplete, or never-valid publication is
  `503`;
- API and Worker release SHA mismatch is
  `503 account_release_mismatch`;
- a missing, invalid, or SHA-less Worker heartbeat cannot prove a release
  match and is also `503 account_release_mismatch`;
- statement sources do not become stale from wall-clock age alone;
- closed-market quotes do not become stale from age alone;
- quote `partial` with zero missing instruments remains healthy.

Stable error codes identify the failure class. Messages are fixed
operator-safe text and never forward paths, account numbers, credentials, or
upstream responses. A stale broker source uses code `broker_refresh_failed`
with the normalized broker as `source`; stale quotes use
`quotes_refresh_failed`. A healthy response has no errors.

The existing R1 unavailable codes remain required. R2 adds only
`account_publication_unstable` for a stable-read race that exhausts all three
attempts.

## HTTP Interface

R2 exposes only:

```text
GET /healthz
GET /api/v1/account/snapshot
```

Unknown paths return a JSON `404`. No write method or static route is added.
The server emits no CORS headers, and Frontend Gateway receives no Account
route in R2.

`GET /api/v1/account/snapshot` returns:

- `200` with the contract body and `ETag` for healthy or stale data;
- `304` with the same `ETag` and an empty body when `If-None-Match` exactly
  matches;
- `503` with the R1 unavailable envelope when the snapshot cannot be served.

`GET /healthz` reports liveness only and always returns `200` while the process
can answer HTTP. Its additive fields are:

- schema `open_trader.account_api.health.v1`;
- module `account_api`;
- status `ok`;
- mode `shadow`;
- PID and process start time;
- API and Worker Git SHA;
- `release_match`;
- source `account_sync_worker_publication`.

Publication failure or release mismatch is visible in health metadata but does
not turn liveness into a non-200 response. If the heartbeat cannot provide a
valid Worker SHA, health reports an empty `worker_git_sha` and
`release_match: false`.

## Live Shadow Parity

The operator command is:

```text
open-trader account-api-parity --data-dir data
```

The parity workflow uses the raw Account publication as its only truth source.
It does not call or import Legacy Dashboard and does not compare Trend,
Research, DOM, PID, or process start time.

For each attempt it pins raw Account and Quotes bytes around one HTTP snapshot
request. When the raw publication stays unchanged, it compares:

- `generated_at` and `quote_as_of`;
- summary and broker summaries;
- every common position and cash field;
- source kinds and accepted source timestamps;
- independently computed opaque IDs;
- the response generation and ETag invariants.

The result vocabulary and process exits are fixed:

- `PASS`, exit `0`: live Account facts are equivalent;
- `FAIL`, exit `1`: a deterministic mismatch or HTTP contract failure exists;
- `BLOCKED`, exit `2`: three attempts could not pin one publication.

Fixtures may test parity logic but cannot replace the final live proof.

## launchd Operations

R2 adds an independent launchd job with label
`com.open-trader.account-api`, plus dedicated install, uninstall, stdout, and
stderr paths. Its installer follows the existing project pattern:

- boot out only the Account API label;
- wait until that label is absent;
- install and start only Account API;
- verify the new PID, cwd, Git SHA, health response, and loopback listener;
- leave Worker, Gateway, Legacy Dashboard, and all other jobs untouched.

The Account API installer does not restart Worker. During final review
deployment, the existing Worker installer is run separately from the same
accepted Git SHA so the release-match contract can produce a live `200`
snapshot.

## Verification and Review Gate

Implementation uses TDD for:

- field mapping, ordering, IDs, generations, and ETag;
- healthy, stale, unavailable, release-mismatch, and unstable states;
- stable-read retry behavior;
- HTTP `200`, `304`, `404`, `503`, and health behavior;
- parity `PASS`, `FAIL`, and `BLOCKED`;
- loopback enforcement and independent launchd lifecycle.

Direct workflow verification must additionally prove:

- repeated GET requests do not change publication hashes or mtimes;
- no broker adapter is called or imported by the Account API path;
- only `127.0.0.1:8768` listens for Account API;
- API and Worker run the same Git SHA;
- current PID, cwd, SHA, fresh logs, health `200`, snapshot `200`, ETag `304`,
  and live parity `PASS` are all observed.

Focused tests and the full pytest suite are required. R2 changes no Dashboard
or Gateway behavior, so `make acceptance` and screenshots are outside this
task. Before merge, the branch must contain a dated operator-facing
`CHANGELOG.md` entry.

The shadow API remains running for operator review. #20 stays open and R3
stays locked until the operator confirms the evidence.

## Rejected Alternatives

### Worker publishes a second v1 snapshot file

Rejected because it changes the writer and creates a second derived
publication whose consistency and recovery would need independent ownership.

### Account API reads Legacy `/api/dashboard`

Rejected because it makes the new module depend on a mixed-domain consumer,
can import Trend/Research enrichment, and prevents raw publication from being
the parity truth source.

### Shared HTTP or launchd framework

Rejected for R2 because stdlib HTTP and the existing small scripts already
cover the two routes and one job. Add a shared framework only after repeated
production requirements demonstrate real common behavior.
