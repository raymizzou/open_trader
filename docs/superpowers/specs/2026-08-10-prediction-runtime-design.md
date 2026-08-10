# Prediction Runtime in Legacy Design

## Status

Approved during the #40 plan review on 2026-08-10. This document records the
implementation boundary before code changes.

## Goal

Collect the existing Prediction monitor, trading-client, ledger, and execution
lifecycle behind one independently startable and stoppable `PredictionRuntime`
inside the Legacy Dashboard. Legacy remains the only long-lived production
Prediction owner; this ticket does not create the future 8769 service.

## Scope and non-goals

In scope:

- one side-effect-free Runtime constructor and explicit `start()`/`stop()`;
- lifecycle ownership lock beside the Prediction data directory;
- deterministic dependency/start/stop order;
- fail-closed partial initialization, duplicate start, and shutdown errors;
- graceful `SIGTERM`/`SIGINT` handoff into the existing Legacy `finally` path;
- focused lifecycle and real cross-process lock tests;
- preserving the #39 HTTP/API, notification, signal, execution, and SQLite
  semantics.

Out of scope:

- no `127.0.0.1:8769` listener or Prediction Service process;
- no Frontend Gateway route change;
- no API-handler rewrite or new API fields;
- no SQLite schema, strategy, solver, proof, mode, or order behavior change;
- no migration of one-shot `prediction-arb cross-auto` CLI commands;
- no execution-thread drain protocol. The production handoff checks that no
  execution is active before the one-time stop/start switch; a future service
  migration owns the complete execution-drain protocol.

## Boundary and dependencies

`PredictionRuntime` does not import or accept `DashboardConfig`. Legacy extracts
the small set of Prediction values it already owns and passes them explicitly:

- `data_dir`;
- `prediction_config_path`;
- resolved `dashboard_url`;
- `prediction_notifier`;
- optional injected cross-venue monitor used by tests.

The Runtime exposes the existing handles needed by `create_dashboard_server`:
the Prediction store, primary monitor, cross-venue monitor/runtime, and
execution service. It owns their construction and lifecycle but does not own
the HTTP handlers or their session/CSRF tokens.

The existing cross-venue event-loop wrapper and cross-venue monitor construction
move with the lifecycle code. No generic factory, plugin interface, or second
configuration hierarchy is introduced.

## Lifecycle

The Runtime has these internal states:

```text
NEW -> STARTING -> RUNNING
                  \-> NOT_READY
NEW/STARTING -> FAILED
RUNNING/NOT_READY/FAILED -> STOPPING -> STOPPED
```

- `start()` is the only method that acquires resources.
- A duplicate `start()` raises a clear duplicate-start error and creates no
  second resource set. A failed instance is terminal and cannot be restarted.
- `stop()` is idempotent. It attempts all reverse-order cleanup steps and
  records the first/combined failure. If a background thread cannot be
  confirmed stopped, the live process does not release the ownership lock.

### Ownership lock

The Runtime opens `<data_dir>/prediction_arbitrage/runtime.lock` and acquires
an OS advisory lock before opening the ledger. The file is data-directory
scoped, so isolated test/shadow data can run independently. The lock is a
process-lifetime owner lock; the existing `execution.lock` remains per-trade
mutual exclusion.

If the lock is unavailable, Prediction stays unavailable/locked while the
non-Prediction Legacy Dashboard continues to serve. One-shot CLI status/mode/
arm commands remain the explicitly documented current exception until the
later mutation-ownership migration.

### Start order

1. Acquire `runtime.lock`.
2. Open `PredictionArbitrageStore` and load the trading configuration.
3. Construct Polymarket and optional Predict trading clients.
4. Construct relation/title validators, the Polymarket monitor, and
   `PredictionExecutionService`.
5. Wire monitor observers and construct the cross-venue monitor/runtime. A
   Predict/cross-venue construction failure degrades only that source.
6. Run `reconcile_startup()` before any public monitor heartbeat.
7. Start the Polymarket monitor, then the cross-venue runtime when available.
8. Mark `RUNNING` and log the lifecycle metadata.

If a core construction step fails, every already-created resource is cleaned,
the lock is released, and the instance becomes terminal `FAILED`. If startup
reconciliation raises or returns `state=locked` after the owner is acquired,
monitors do not start; the Runtime remains `NOT_READY` and holds the lock until
explicit `stop()`.

### Stop order

1. Stop the cross-venue runtime.
2. Stop the Polymarket monitor.
3. Close the execution/client/store resources when they expose `close()`.
4. Release `runtime.lock` last, only after cleanup and thread state are known.

Shutdown errors do not skip later cleanup attempts. A `SIGTERM` or `SIGINT`
causes `serve_dashboard()` to leave `serve_forever()` and execute this same
`finally` path. If the process is forcibly killed, the OS releases the
advisory lock and the next startup reconciliation remains the safety boundary.

## Legacy integration

`serve_dashboard()` creates at most one Runtime before constructing the HTTP
server, passes Runtime-owned handles to `create_dashboard_server()`, and calls
`runtime.stop()` in its existing `finally` block. No Dashboard page or ordinary
request constructs a monitor or a second Runtime.

When Prediction configuration is absent, Legacy keeps the current unavailable
surface and does not create a no-op Runtime. When Prediction is unavailable,
the existing #39 state/mutation behavior remains unchanged.

## Verification

The implementation must add focused tests for:

- constructor side-effect freedom;
- start dependency order and reverse stop order;
- duplicate start and idempotent stop;
- core initialization failure cleanup;
- reconciliation failure holding the owner lock and not starting monitors;
- source-level cross-venue degradation;
- shutdown failure continuing cleanup without releasing an uncertain owner;
- real second-process lock exclusion and lock release after process exit;
- SIGTERM reaching the Runtime cleanup path;
- existing `tests/test_prediction_api_contract.py` and direct Legacy workflow.

Before production handoff, verify no `active_execution` is present, stop the
old Legacy owner once, start the new SHA once, and check PID, cwd, SHA, fresh
logs, HTTP 200, and the absence of a 8769 listener. Do not perform a deliberate
production rollback round trip; retain the old SHA for fail-closed rollback.
