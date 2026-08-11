# Prediction Service Checkout-Driven Release

**Issue:** #44
**Status:** Approved design
**Date:** 2026-08-11

## Purpose

Package port 8769 as one independently installable local Prediction Service
release without introducing a release archive, registry, dual writer, or
zero-downtime handoff. A release is a clean local Git checkout at one exact
commit. The same installer handles first install, upgrade, and compatible
rollback.

Issue #34 has already bounded the service's steady-state read load. Issue #43
has already moved Preview, confirmation, execution, and startup reconciliation
behind the production-capable 8769 runtime. This design adds release identity,
data-generation compatibility, managed launchd lifecycle, and durable runtime
evidence. It does not route production traffic; that remains Issue #45.

## Decisions

- Planned downtime is allowed and preferred over dual ownership.
- A rollback target is an operator-selected clean checkout at an exact Git SHA.
- The existing `com.open-trader.prediction-service` launchd label and installer
  are extended; no second service manager or release registry is introduced.
- The candidate process, not the installer, performs the authoritative data
  generation check while holding the production owner lock.
- Failure after maintenance begins remains fail-closed. The installer does not
  automatically start another checkout.

## Goals

- Give every candidate a small, validated release manifest containing reader
  and HTTP contract generations.
- Refuse dirty checkouts, wrong SHAs, unknown listeners, unknown owners, and
  incompatible data before production resources are created.
- Preserve one production owner during install, upgrade, rollback, failure,
  and uninstall.
- Record enough exact evidence to identify the active release and let Issue #45
  keep the whole Prediction prefix in maintenance until the service is ready.
- Reuse the current runtime, health endpoint, launchd label, scripts, and
  standard-library SQLite/file primitives.

## Non-goals

- No Gateway route change, Legacy shutdown, or production traffic cutover.
- No real production launchd installation or production order during Issue #44
  verification.
- No shared writable SQLite, dual write, leader election, hot handoff, rolling
  upgrade, or availability guarantee during handoff.
- No release archive, copied application tree, package repository, version
  registry, or automatic rollback search.
- No Prediction API, strategy, solver, execution, notification, or Dashboard
  behavior change.

## Release Identity

The checkout contains `ops/prediction-service-release.json` with exactly:

```json
{
  "schema_version": "open_trader.prediction_service.release.v1",
  "reader_generation": 1,
  "contract_generation": 1
}
```

Both generations are positive integers. The manifest deliberately does not
embed a Git SHA; changing the SHA inside the tracked file would create a
self-referential build. The installer derives the checkout's actual SHA and
source state and combines them with the tracked generations as the release
identity.

The extended `scripts/install_prediction_service_launchd.sh` accepts only a
clean checkout. The launchd plist passes the exact manifest path to the
process. The process validates the same file and exposes
its generations in `/healthz`. The installer accepts ready only when PID, cwd,
Git SHA, clean source state, mode, production ownership, reader generation, and
contract generation all match the candidate checkout.

`contract_generation` identifies the public Prediction HTTP contract. Issue
#44 records it but does not route traffic or negotiate it. Issue #45 will use
the ready health/runtime evidence when switching the Gateway prefix.

## Persistent Reader Compatibility

Prediction SQLite owns one singleton schema-metadata record containing
`minimum_reader_generation`. The first generation-aware release treats either
a missing database or a missing metadata table as baseline generation `1`.
After the compatibility gate, normal store schema creation persists that
baseline record.

A future incompatible migration must raise `minimum_reader_generation` before
committing data that older readers cannot interpret. Issue #44 does not perform
such a migration; it establishes and proves the gate.

The candidate startup sequence is fixed:

1. validate its own release manifest without touching production data;
2. acquire the existing Prediction runtime owner lock non-blockingly;
3. open the Prediction SQLite path once with SQLite URI `mode=ro`, read
   `minimum_reader_generation`, and close that connection;
4. reject when `reader_generation < minimum_reader_generation`;
5. only when compatible, construct `PredictionArbitrageStore`, which may create
   or migrate schema;
6. load configuration and create exchange clients;
7. create execution/monitor resources and run startup reconciliation;
8. restore persisted safety state, start monitors, and enter `RUNNING`;
9. bind 8769 and report ready.

There is no installer-side cached compatibility result, lock-before/lock-after
double check, writable preflight connection, or background compatibility
thread. An incompatible process releases the owner and exits before store,
client, monitor, or execution construction.

Shutdown reverses the owned-resource order: reject new mutations and automatic
submission, stop cross-venue and Polymarket monitors, close execution and
exchange clients, close the Store, then release the production owner last. If
any worker cannot be confirmed stopped, shutdown reports failure and retains
the owner instead of allowing another release to start.

## Runtime Record

The installer atomically maintains
`<runtime-root>/prediction-service-runtime.json`. Its schema is
`open_trader.prediction_service.runtime.v1`. It contains:

- state: `maintenance`, `ready`, `failed`, or `stopped`;
- candidate checkout, Git SHA, source state, reader generation, and contract
  generation;
- previous ready release identity when one exists;
- transition start/update timestamps and failure reason;
- when ready: launchd label, PID, cwd, listener, process start time, health
  schema/module, and log paths.

Writes use a sibling temporary file, flush, and `os.replace`. The record never
claims `ready` from expected values alone; all ready fields come from observed
launchd, process, listener, and HTTP evidence. Failed attempts do not erase the
previous release identity.

Issue #45 will use `maintenance`/`failed` versus `ready` as cutover evidence.
Issue #44 only produces the record and tests its fail-closed transitions.

## Installer Flow

### Preflight

Before changing launchd or the runtime record, the installer:

1. resolves the candidate checkout and manifest to canonical paths;
2. verifies the checkout is clean and resolves its exact Git SHA;
3. validates the manifest's exact schema and positive generations;
4. inspects the launchd label, listener PID, process cwd, health identity, and
   runtime record;
5. refuses any unknown listener, owner, label/PID mismatch, dirty checkout,
   invalid manifest, or requested/observed SHA mismatch.

If the currently ready managed service and runtime record already match the
candidate identity, installation is an idempotent success and does not restart
the service.

### Maintenance and old-owner shutdown

For a real transition, the installer atomically writes `state=maintenance`
while preserving previous ready evidence. It then disables automatic restart
for the known launchd label, stops only the process whose label, PID, cwd, and
8769 listener agree, and verifies:

- the old PID is absent;
- the launchd job is absent;
- 8769 has no listener;
- the Prediction runtime owner lock can be acquired non-blockingly by a probe
  and immediately released;
- no known execution or monitor worker from that managed PID remains.

Incomplete evidence stops the transition. Unknown processes are never killed.

### Candidate startup and ready proof

The installer renders the same launchd label against the candidate checkout in
`production` mode, bootstraps it, and waits within a fixed timeout. Ready
requires the exact health and process evidence defined under Release Identity.
Only then does the installer atomically replace the runtime record with
`state=ready` and print the installed release evidence.

If the candidate exits, loops under KeepAlive, remains not-ready, reports the
wrong identity, or cannot bind 8769, the installer boots out that candidate,
verifies job/PID/listener/owner absence, and writes `state=failed` while
preserving maintenance and previous-release evidence. It does not restart the
previous checkout automatically.

## Upgrade, Rollback, and Uninstall

First install, upgrade, and compatible rollback are the same operation with a
different candidate checkout.

- A compatible rollback passes the candidate runtime's locked read-only
  generation gate and reaches ready normally.
- An incompatible rollback releases the owner and exits before opening a
  writable store or creating clients/threads. The installer removes the failed
  job and leaves the runtime record failed/maintenance.
- A failed rollback never starts both old and new services and never searches
  for another version.

Uninstall stops only the verified managed label, then requires job, PID,
listener, worker, and owner absence before removing the plist. It preserves the
database, configuration, logs, and last runtime evidence, marking the record
`stopped` rather than deleting it. Repeated uninstall is idempotent.

## Error Semantics

Failures are explicit and phase-specific. At minimum the installer/runtime
distinguish:

- invalid or dirty release checkout;
- invalid release manifest or wrong Git SHA;
- unknown launchd job, listener, PID, cwd, or owner;
- old owner/worker shutdown not proven;
- incompatible reader generation;
- schema/store/client construction failure;
- startup reconciliation or safety-state recovery failure;
- monitor startup failure;
- candidate timeout or wrong health identity;
- cleanup/owner-release failure.

Any uncertainty after maintenance begins leaves the record non-ready. No error
path reports installation success, overwrites prior ready evidence with
expected values, kills an unknown process, opens production for traffic, or
submits an order.

## Verification

### Runtime tests

- owner acquisition precedes the one read-only generation check;
- compatible startup then opens Store, clients, reconciliation, and monitors in
  order;
- missing metadata bootstraps generation 1 only after compatibility succeeds;
- incompatible generation creates no writable connection, client, monitor,
  execution service, or background thread and releases owner;
- startup and shutdown failures preserve the existing owner-safety rules;
- health reports exact release generations and identity only when ready.

### Installer tests

Using fake `launchctl`, `lsof`, `curl`, owner probes, and temporary checkouts,
cover:

- side-effect-free dry-run;
- first production install;
- same-SHA idempotent reinstall;
- dirty checkout, wrong SHA, invalid manifest, unknown listener, existing
  unknown owner, and port conflict refusal before shutdown;
- verified old-service stop and candidate start;
- startup/reconciliation failure cleanup;
- compatible rollback;
- incompatible rollback refusal before production resource construction;
- failed rollback remaining single-owner and non-ready;
- uninstall, repeated uninstall, and evidence preservation.

### Isolated direct workflow

Run the actual installer and rollback commands against temporary runtime roots,
temporary SQLite, fake exchange clients, and fake launchd/process commands.
Record exact command, candidate/previous SHAs, generations, state transitions,
PID/cwd/listener evidence, logs, and final owner absence. No production
credential, production writable ledger, real launchd job, or real order is
used.

### Regression

Run Prediction Runtime, Service, launchd, execution, store, and API contract
tests, then the repository's full Python suite. Issue #44 does not alter the
Dashboard, Gateway, browser route, or deployed process, so `make acceptance`
is not part of its completion gate.

## Scope Guard

Completion of Issue #44 means a clean checkout can be safely installed,
identified, stopped, upgraded, or compatibly rolled back as the only local
Prediction Service owner, with incompatible readers rejected before production
resource creation. It does not authorize stopping the live Legacy owner,
installing the production job, routing the Gateway prefix, or accepting real
orders. Those production handoff actions remain Issue #45.
