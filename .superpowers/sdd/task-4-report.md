# Task 4 Report: Self-Reconciling Trend Market Controller

## Outcome

Implemented Task 4 on `feat/trend-market-controller-spec` from baseline
`2aaa22d`.

- Added one per-market reconciliation controller for CN, HK, and US.
- Added deterministic filesystem/state-transition coverage for recovery,
  revision, execution, protection, close capture, read-only mode, and status.
- Kept the implementation behind the seven direct monkeypatch seams required by
  the task brief; no service container or mutable controller-state class was
  introduced.
- Did not wire the CLI, launchd, Dashboard, or a live Futu deployment; those
  remain later plan tasks.

## Implementation

### Durable, fact-driven reconciliation

- Added `ControllerCycle`, `BUY_WINDOWS`, `run_trend_market_controller(...)`,
  and `load_trend_market_status(...)`.
- Derives report and execution dates from the existing Futu calendar/session
  helpers and preserves the logical target of a running or failed report task
  when wall-clock time advances.
- On a restart after close, the next morning, or over a weekend, first
  reconciles an unfinished prior execution date, then advances to the current
  cycle only after terminal action facts exist.
- Uses one supervised `ThreadPoolExecutor(max_workers=1)` report future and a
  named immutable `ReportTask` target while continuing active-session
  protection passes.
- Retries calendar, report, broker, and close failures with bounded exponential
  backoff. It publishes a heartbeat before each reconciliation tick; dependency
  calls return control to the controller rather than promising a wall-clock
  heartbeat interval while external I/O is still running.

### Report freeze, validation, and revision safety

- Strictly validates frozen report schema, dates, filename/revision identity,
  market/broker, account freshness and identity, strategy metadata, complete
  action collections, and positive finite BUY fields before execution.
- Missing reports are generated for the same logical dates until a valid frozen
  report exists. Frozen delivery recovery delegates to the existing report
  runner and does not rebuild expensive content.
- Delivery recovery uses the public strict receipt reader and binds its stem,
  embedded JSON/Markdown, declared hashes, canonical selected-report SHA, and
  declared replay-evidence path/SHA to the exact frozen artifact. Recovery mode
  comes from that artifact's revision suffix, not from ambient request state.
- Malformed or invalid frozen reports fail closed and require an explicit
  revision; a pending revision may replace that artifact before execution.
- Revision request creation and first batch lock share a per-execution-date
  gate, closing the request-versus-batch TOCTOU race.
- Every revision request freezes its baseline path, byte SHA, and revision
  number. Only a higher revision with a strictly bound delivery receipt
  satisfies it: an r1 present before the request requires r2, while an r1
  frozen after an r0-baseline request can be recovered and completed after a
  crash. A newer report without its receipt remains incomplete.
- A later revision cannot replace an already locked batch and produces one
  deduplicated anomaly notification.

### Execution, protection, and close recovery

- Reuses Task 2/3 immutable batch/action execution and guarded Futu clients.
- Quote acquisition is best effort: missing BUY quotes remain per-action
  pending/missed facts, while SELL reconciliation is still allowed to run.
- Runs the existing CN/HK/US protection watchers in `once=True` mode and routes
  protection exits through the shared stable sell action protocol. One-pass
  client/calendar/account/quote/reconnect/lock failures return structured
  `abnormal` results immediately and never sleep; persistent standalone watcher
  mode retains reconnect behavior.
- Adds an immutable close-completion cursor. A crash after the daily close fact
  but before projection completion rebuilds the projection once; a completed
  close is not repeatedly mutated every heartbeat.
- Treats a batch as selection only, not completion. Catch-up validates the
  locked report and requires durable `filled`, `missed`, position-zero, or
  terminal operator-resolution facts for its actions. Empty action lists
  complete naturally; there is no global execution-noop escape hatch.
- Scans forward from actual durable report/batch cycles (ten transitions per
  tick) and selects the oldest unfinished cycle. Each recovered report becomes
  the next scan seed, so longer outages progress across ticks without an
  unbounded historical scan.
- Recovers a missing prior-session close fact and projection on next-morning,
  weekend, and active-session restarts, after the current protection pass.

### Mode, status, and notification boundaries

- Exact executor-host mismatch returns an in-memory `readonly` status before
  creating files, reports, broker clients, close facts, or notifications.
- Execute-mode status is written atomically with the required v1 schema,
  heartbeat, PID, working directory, Git SHA, success, blocker, and next check.
- Controller, calendar, uncertainty/conflict, missed-window, and revision
  anomaly notifications use immutable success-only receipts.
- Refreshes the heartbeat before calendar I/O on every tick and runs protection
  from the best-effort local market date before calendar derivation, backoff,
  and any opening-report load or repair.
- Added one shared Task 3 audit loader used by completion checks. It validates
  action identity, timezone, strict resolution attempt identity, and terminal
  `filled`, `missed`, and position-zero evidence before a ledger action can be
  considered complete.

## TDD Evidence

### Initial RED

The required controller test module was created first. Its initial collection
failed because `open_trader.trend_market_controller` did not exist.

### Safety regression RED/GREEN

Independent review findings were reproduced with focused tests before fixes:

- restart-after-close lost the prior logical cycle;
- invalid/malformed frozen reports could bypass the intended revision/blocker
  behavior;
- revision request and batch locking had a TOCTOU window;
- broker and close failures ignored bounded backoff;
- an existing close fact could suppress projection recovery;
- quote acquisition failure prevented missed/SELL reconciliation;
- a running or failed report future could drift to a newly derived cycle.

Each regression failed before its targeted fix and passed afterward. Later
spec review found recovery and fail-closed gaps around delivery receipts,
client-free expired BUYs, protection ordering/results, revision identity,
global no-op completion, and strict terminal action evidence. The focused
controller selection first ran red with `13 failed, 1 passed`; the forged
broker evidence selections each ran red before the strict loader was added.
Two final durability regressions also ran red before their fixes: a fresh-zero
SELL appended a second broker observation on restart, and an abnormal
protection result allowed BUY when the calendar cycle said the market was not
open. That review stage ended with 50 state-transition test cases.

The next spec review added 20 focused RED cases: six initial real watcher
client/calendar/snapshot failures, five further account/lock/client-close
failures, eight receipt/revision/replay binding cases (including separate JSON
and Markdown mismatch cases), and one observation filename-digest mismatch.
Each failed against the prior implementation before
the targeted change and passed afterward. Existing persistent watcher
recovery, valid delivery recovery, post-request revision recovery, and strict
audit paths were retained as controls.

### Final automated verification

Controller tests:

```text
58 passed in 3.91s
```

Task-required controller/report/watcher/ledger suite:

```text
521 passed in 5.42s
```

Full repository suite:

```text
2852 passed in 49.87s
```

Static checks:

- Python byte-compilation passed for production and test modules.
- `git diff --check` passed.

### Direct safe workflow

A direct `run_trend_market_controller(config, "CN", once=True)` call used a
temporary directory and a deliberately non-matching executor hostname. It
returned:

```text
{'phase': 'readonly', 'effective_mode': 'readonly', 'filesystem_unchanged': True}
```

No temporary data or reports directory was created.

## Self-Review

- Consolidated the report future's logical cycle and request-completion
  responsibility into the named immutable `ReportTask` value; generator mode
  remains a local decision derived from the selected artifact/request facts.
- Centralized timezone normalization, retry calculation, and status
  construction plus atomic publication.
- Kept the seven task-specified adapter seams directly patchable.
- Removed the execution-noop protocol and the intermediate parallel
  `broker_facts` directory. Terminal observations now live in the existing
  immutable open ledger and are SHA-bound from the action event.
- The two remediation rounds are a net 832 production lines relative to the
  original Task 4 commit: 290 in the controller, 465 in Task 3 review/audit
  validation, and 77 in watcher one-pass recovery. The bulk is explicit
  fail-closed validation at persistence and process boundaries, not a new
  scheduler, state machine, service layer, or alternate execution path.
- Trust boundary: the designated local executor and immutable-ledger writer are
  trusted. Strict audit validation targets partial, corrupt, missing, stale, or
  mismatched artifacts and fails closed on them. Coherent malicious rewriting
  of the complete report/batch/intent/result/observation/event chain and all
  hashes is out of scope; no crypto attestation or historical broker-status
  re-read was added.
- Deliberately left the temporarily duplicated legacy CLI validation/execution
  routes unchanged because Task 5 removes those operational branches.

## Concerns / Limits

- No live Futu/OpenD workflow was run because it could place simulated broker
  orders. Automated tests exercised broker reconciliation and duplicate
  prevention through controlled clients.
- Read-only process inspection found the existing US watcher (PID 77982) still
  running from the `dashboard-main-merge` worktree. The HK and CN launchd
  watchers were loaded but not running, both with last exit code 0. No existing
  watcher was stopped or restarted in Task 4; the fenced job migration belongs
  to Task 8.
- No Task 4 controller process is deployed yet, so fresh controller PID/log
  verification and the Dashboard acceptance gate are not applicable at this
  stage. Completion of the overall Dashboard task must still pass the later
  live migration and final `make acceptance` gate.
