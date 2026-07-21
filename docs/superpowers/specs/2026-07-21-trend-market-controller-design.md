# Trend Market Controller Design

## Problem Statement

Trend automation is split across six independent report and watcher jobs for
CN, HK, and US markets. Each job depends on a particular wall-clock trigger and
on artifacts produced by another job. A deployment, restart, or missed trigger
can therefore leave a valid operation list waiting while no process is alive to
execute it. The Dashboard shows the downstream symptom, but not the missing
process or prerequisite that caused it.

Running the same deployment on another machine adds a second safety problem.
Local ledgers alone cannot prevent duplicate simulated orders across machines,
and a copied configuration must not silently create another executor.

## Solution

Replace the separate trend report and watcher schedules with one persistent,
self-reconciling market controller for each of CN, HK, and US. All three jobs
use the same controller module and differ only through their market adapters.
`launchd` owns process availability through `RunAtLoad` and `KeepAlive`; the
controller owns business sequencing by inspecting the current market session,
frozen reports, immutable execution ledgers, and Futu orders whenever it starts
or wakes.

The controller exposes one external interface:

`run_trend_market_controller(config, market)`

Its implementation reuses the existing report generation, execution,
protection monitoring, close capture, calendar, notification, and broker
functions. It does not add a second implementation of those rules.

The operational success criterion is not that external dependencies can never
fail. It is that a started strategy self-recovers from safe failures, persists
every transition, never silently loses a step, never duplicates an ambiguous
order, and explicitly records an unavoidable missed window.

Ordinary premarket automation is unchanged by this work.

## User Stories

1. As the operator, I want each market to have one automation process, so that I do not need to reason about separate report and watcher schedules.
2. As the operator, I want a controller to reconcile immediately after deployment, so that a deployment after the original trigger does not defer work until the next day.
3. As the operator, I want a restarted controller to continue from durable facts, so that process memory is never required for recovery.
4. As the operator, I want reports generated once when their prerequisites are ready, so that retries do not produce duplicate formal artifacts.
5. As the operator, I want an operation list validated before execution, so that stale, incomplete, wrong-market, or wrong-date reports cannot place orders.
6. As the operator, I want execution restricted to the strategy's valid market window, so that recovery never turns into a late order.
7. As the operator, I want a missed window recorded explicitly, so that a non-executed action is not left indefinitely as merely pending.
8. As the operator, I want every action to have a stable identity independent of report revision, so that regenerating a report cannot create a duplicate order.
9. As the operator, I want the controller to query Futu before every submission, so that a new machine can recognize an order created by the prior machine.
10. As the operator, I want an accepted order recovered from Futu when the local result write was interrupted, so that recovery does not submit it again.
11. As the operator, I want ambiguous execution to fail closed, so that avoiding duplicate exposure takes priority over automatic completion.
12. As the operator, I want errors known to occur before submission retried within the valid window, so that harmless transient failures can self-heal.
13. As the operator, I want execution uncertainty notified only once, so that the alert is actionable rather than noisy.
14. As the operator, I want only one named machine permitted to execute, so that copying the deployment to another machine does not create an active-active race.
15. As the operator, I want non-matching machines to be read-only automatically, so that safety does not depend on remembering a per-machine boolean.
16. As the operator, I want read-only machines not to generate reports, so that secondary deployments neither consume report inputs nor publish conflicting artifacts.
17. As the operator, I want read-only machines not to run trend controllers or send trend task notifications, so that they remain operationally silent.
18. As the operator, I want direct execution entry points guarded by the same host rule, so that bypassing the controller cannot bypass read-only mode.
19. As the operator, I want the Dashboard to show effective mode and executor host, so that I can see which machine is authorized.
20. As the operator, I want the Dashboard to show controller PID, Git SHA, phase, latest success, blocker, and next check, so that failures are attributable.
21. As the operator, I want a missing executor heartbeat shown as a blocking error, so that a dead controller is not confused with a pending order.
22. As the operator, I want deployment verification to inspect the live controller processes and fresh logs, so that passing unit tests are not mistaken for a working scheduler.
23. As the operator, I want a missing report generated even after a controller restart, so that report generation cannot be lost with its original trigger.
24. As the operator, I want report recovery to continue without interrupting protection monitoring, so that an existing holding is never left unprotected while a new report is built.
25. As the operator, I want the execution batch to lock one report at opening, so that a later revision cannot change a partially executed plan.
26. As the operator, I want partially filled buys completed within the valid window and frozen risk limits, so that the actual opening matches the strategy as closely as possible.
27. As the operator, I want simultaneous exit reasons merged into one close action, so that danger and protection signals cannot create duplicate sells.
28. As the operator, I want a partially filled sell completed only after the prior order is conclusively terminal, so that the remaining position closes without overlapping orders.
29. As the operator, I want one trend-market command namespace for run, status, and resolution, so that the operational interface is not split again.
30. As the operator, I want an auditable manual resolution for uncertain orders, so that recovery decisions never require deleting or rewriting ledger files.

## Implementation Decisions

### Controller ownership

- Install exactly one persistent trend controller for each enabled market on the designated executor machine.
- Replace the separate trend report and watcher jobs during a fenced migration; old and new executors are never active together.
- Use `RunAtLoad` and `KeepAlive` with restart throttling. Calendar triggers no longer sequence business steps.
- On every start and wake, derive the next action from durable facts rather than a mutable in-process state machine.
- The controller lifecycle is: ensure a valid frozen report, reconcile eligible opening actions, monitor active protection lines during the session, and capture the close. The controller may enter at any point in this lifecycle after a restart.
- Existing market calendars and window rules remain authoritative. CN and HK retain the 09:30–10:00 opening window; US retains its regular-session rule.
- Protection monitoring has priority during an active session. If a report must be recovered during the session, the controller runs report generation as a supervised child task while monitoring continues; no new opening action is eligible until that report is frozen and validated.
- Replace the old operational commands with one `trend-market` namespace containing `run`, `status`, and `resolve`. `launchd` calls `run`; an operator may call `run --revision` before execution begins to make an explicit report correction; `status` is read-only; `resolve` records explicit operator decisions. Existing business functions remain internal implementation.

### Execution mode

- Add `OPEN_TRADER_TREND_EXECUTOR_HOST` as the single executor designation.
- Compare it with the local hostname. An exact match produces effective mode `execute`; an absent or non-matching value produces `readonly`.
- Only the executor deployment installs and runs trend controllers.
- Read-only mode does not generate reports, execute or modify orders, run protection automation, capture closes, or send trend task notifications. It may display already available data and perform explicitly read-only queries.
- The order-writing adapter checks effective mode immediately before every broker mutation. Controller routing is not the only guard.
- Automatic failover is not provided. Promoting another machine requires an explicit executor-host configuration change after the previous executor is stopped.

### Report lifecycle

- The executor is the only machine allowed to generate trend reports.
- A missing report must be generated whenever the controller discovers it, including recovery during a trading session.
- Report work remains one logical report for its data date and execution date. A failure before atomic freeze discards temporary output and recomputes that same report identity; it does not create a revision.
- Once a report is frozen, delivery failure retries delivery only and never recomputes the report.
- Delivery recovery uses the report runner's strict receipt reader. The receipt
  stem, embedded JSON and Markdown, declared content hashes, and any declared
  replay-evidence path/SHA must match the selected frozen artifacts before the
  controller may recover or execute them. The receipt's standalone protection
  state, the state embedded in its report JSON, and the state in the selected
  frozen report must also be identical; a self-consistent receipt hash does not
  excuse a cross-artifact state mismatch.
- An explicit correction may create a revision before execution begins. At the first eligible execution check, atomically lock the latest valid report SHA as the day's execution batch.
- A revision request immutably records the latest report path, byte SHA, and
  revision number visible when it is written. Only a later revision number can
  satisfy that request, and it is not eligible until its strict delivery
  receipt binds the exact frozen JSON/Markdown and any declared replay
  evidence. A revision frozen after the request is recovered in place after a
  crash; a revision already present before the request is the baseline and
  therefore cannot satisfy it. A newer report with no receipt remains pending.
- Explicit revision resolves the same oldest unfinished cycle as normal
  reconciliation before publishing its request. A malformed historical report
  or batch therefore remains the blocking catch-up target instead of silently
  redirecting the correction to the current cycle. If that historical cycle
  already has an execution batch, revision is rejected even when validation of
  the batch or its report fails. The only exception is an explicitly authorized,
  SHA-bound legacy cutover for an expired cycle that cannot be replayed; it
  records a no-backfill audit skip rather than pretending to complete a revision.
- Revision baseline capture and immutable request publication hold the same
  per-market report lock used by the real report runner. Lock order is revision
  gate followed by report lock, and request creation occurs before the
  controller's long-lived process lock. A missing or r0 baseline still requires
  at least r1: candidates and completions must have a revision number greater
  than `max(0, baseline_revision)`.
- A revision appearing after the execution batch is locked is an anomaly. Display and notify the difference, but do not add, remove, resize, or submit actions from it automatically.
- If a recovered report freezes inside the valid window, it may execute after all validation passes. If it freezes after the window, preserve it for audit and mark its opening actions `missed`; never roll them into the next trading day.

### Action identity and duplicate prevention

- Identify an opening action by market, execution date, symbol, and side. Report hashes and action indexes remain evidence but are not order identity.
- Give each broker attempt a monotonic number under that stable action identity. Derive its Futu remark from the action identity and attempt number so it is identical after restart, report revision, or sequential machine migration.
- Before any submission, query the designated Futu simulated account for every remark already belonging to the action and for the proposed attempt remark.
- If an exact broker order exists, write or repair the immutable local result and do not submit.
- If the remark exists but symbol, side, or quantity conflicts, record `conflict`, notify once, and do not submit.
- If a local result exists, treat the action as reconciled and do not submit.
- If a local intent exists without a result, reconcile against Futu. If no broker fact resolves the ambiguity, record `uncertain` and never resubmit automatically.
- Only when the proposed numbered attempt has neither a local execution fact nor a broker order, and the cumulative action is still eligible, may the controller atomically write its intent and submit once.
- A later report revision with the same action identity cannot create a second order. An existing intent locks the submitted request facts.
- Multiple exit reasons for the same open position lifecycle merge into one `SELL_ALL` action. Signal event IDs are attached as audit reasons; they are not separate order identities.
- While a sell order is active or partially filled, submit no overlapping sell. After Futu conclusively reports cancellation or rejection, re-read the remaining position and create a numbered retry for only that quantity. An ambiguous state becomes `uncertain`.
- A partially filled buy remains one cumulative opening action. Wait while an attempt is active. After it is conclusively terminal, aggregate confirmed fills and, while the window remains open, create a numbered attempt for the remaining target.
- A buy retry may not make cumulative shares exceed the frozen plan quantity or cumulative notional exceed the frozen amount limit. Recalculate the remaining affordable quantity from confirmed fills, remaining budget, current quote, lot size, cash, and the existing risk limits.
- When the buy window closes, retain the partial position and stop retrying the unfilled remainder.

### Trust boundary and artifact integrity

- The designated local executor process and the code that writes its immutable
  ledger are trusted components. Local filesystem permissions and the exact
  executor-host fence are the security boundary for these artifacts.
- The strict audit loader is designed to fail closed on partial writes,
  malformed JSON, missing artifacts, stale or mismatched report/action
  identities, forged standalone terminal events, and inconsistent
  intent/result/order/observation facts.
- An adversary able to coherently rewrite the frozen report, batch, intents,
  results, broker observations, terminal events, and all matching hashes is
  outside this design's threat model. The ledger is not a cryptographic
  attestation system, and historical audit loading does not re-query broker
  status solely to prove authenticity.
- Futu remains the cross-machine source checked before every possible order
  submission. Broker responses and account snapshots used for terminal local
  facts are captured by the trusted executor and bound to the exact frozen
  action and immutable attempt facts.

### Validation and retry policy

- Validate market, execution date, report freshness and completeness, strategy version, action fields, account identity, positions, cash/NAV, quote validity, quantity, and trading window before writing an intent.
- Failures before intent creation may retry with bounded backoff while the action remains eligible.
- Failures after intent creation are never treated as safe retries without a conclusive broker reconciliation.
- When a valid window expires, write one durable `missed` fact and stop attempting the action.
- Missing reports trigger generation with bounded backoff until the report freezes, even if the execution window later closes. Invalid frozen reports block execution and require an explicit correction; they are never silently replaced.
- Trend data or Futu being unavailable for an entire valid window is an unavoidable `missed` outcome. The controller continues the audit/report work but never violates the strategy window to force a fill.
- The controller invokes protection watchers in one-pass mode. A one-pass
  watcher returns a structured `abnormal` result immediately on client,
  calendar, account-snapshot, quote-snapshot, reconnect, or lock failure; it
  never performs an internal reconnect sleep. The controller can therefore
  publish its next heartbeat, disable every new BUY, and continue safe
  SELL/reconciliation work before its own bounded retry.
- One-pass watcher shutdown is part of its outcome. If client close fails after
  an otherwise normal, holiday, closed, or no-comparable result, the watcher
  returns structured `abnormal` instead of allowing the close exception to
  replace the result. Persistent watcher mode retains its existing exception
  contract.

### Manual resolution

- `uncertain` has exactly three explicit operator resolutions: confirm submitted with a Futu order ID, confirm not submitted and authorize one retry, or abandon the action.
- The `trend-market resolve` command requires the market, stable action identity, resolution, actor, reason, and any required broker order ID.
- Resolution writes a new immutable audit fact. It never edits or deletes an intent, result, broker fact, or earlier resolution.
- Only an explicit “confirm not submitted and authorize retry” resolution may advance an ambiguous action to another numbered attempt.

### Status and observability

- Maintain one atomic, non-authoritative controller status document per executor market.
- Include effective mode, executor host, local host, PID, working directory, Git SHA, phase, heartbeat time, last successful transition, current blocker, and next check time.
- Frozen reports, immutable execution ledgers, and broker facts remain authoritative; the status document is disposable observability data.
- Deduplicate failure and uncertainty notifications by market, trading date, action identity, and reason.
- The Dashboard distinguishes `pending`, `submitted`, `uncertain`, `conflict`, `missed`, and controller-unavailable states.
- A read-only Dashboard shows the effective read-only reason and does not require a local controller heartbeat.

### Deployment

- The installer determines effective mode before changing jobs.
- On the executor host, install or reload the three enabled controllers and verify that each has a fresh PID and heartbeat.
- On a read-only host, unload any existing trend automation jobs and install none.
- Deployment must report effective mode, local hostname, configured executor host, and every job it installed or removed.
- Migration never runs old and new executors concurrently. Stop the old report/watcher jobs, verify their PIDs are absent, install the controller, reconcile existing Futu orders and ledgers, and only then enable execution.
- A rollback must not directly restart the old watcher. Deploy the old source with trend automation stopped, reconcile all intents and Futu orders, and only then explicitly restore the old automation if it is still safe.

### Legacy cutover for unreplayable expired cycles

- A report created before replay evidence and strict account-date binding existed
  may be impossible to revise safely after its execution window has expired.
  The controller must not substitute current Trend Animals or account data for
  that historical date and must not weaken normal report validation.
- After explicit operator authorization, deployment may record one immutable
  legacy cutover fact per affected cycle. The fact binds the market, as-of and
  execution dates, latest frozen report path and SHA-256, pending revision
  request path and SHA-256, actor, reason, and authorization timestamp.
- A cutover is valid only after the execution window, before any execution
  batch exists, and while every bound artifact still has the recorded hash.
  Missing, malformed, conflicting, or changed facts fail closed.
- A valid cutover means only “audit this expired cycle as skipped and never
  backfill its orders.” It creates no report, batch, action result, broker
  request, notification, or retrospective submission. The original report and
  revision request remain immutable and visible for audit.
- Cutover recording is a one-time deployment migration helper, not another
  public operational command. Normal operation remains the single
  `trend-market` namespace.

## Testing Decisions

- Test through the controller interface. Clock, sleep, market data, broker, notifier, and filesystem dependencies are internal adapters supplied by the controller implementation and replaced in tests.
- Prefer end-to-end state-transition tests over new tests for pass-through scheduling helpers.
- Preserve focused tests for the existing report, risk, execution, and protection modules; delete obsolete tests that only assert the removed launchd split.
- Verify that report completion followed by restart does not regenerate the report.
- Verify that failure before report freeze regenerates the same logical report, while delivery failure retries delivery without recomputing report content.
- Verify that receipt/report mismatches fail closed, revision delivery recovery
  uses the selected artifact's suffix, and revision requests accept only an
  artifact newer than their frozen baseline.
- Verify CN/HK/US prepared recovery rejects a protection state that differs
  between the receipt, embedded report JSON, and selected frozen report.
- Verify a revision request waits for the real CN/HK/US report lock, captures a
  report frozen by the prior holder as its baseline, and can only complete with
  r1 or later.
- Verify an invalid historical rN remains the oldest unfinished catch-up cycle,
  `run --revision` publishes a request for that historical identity while the
  controller lock is held, and the persistent loop later completes it only
  with a strictly receipt-bound rN+1. Verify no request is published for the
  current cycle and no revision is allowed once a historical batch exists when
  no authorized legacy cutover applies.
- Verify an explicitly authorized legacy cutover skips only its exact expired
  cycle, validates the report and revision-request hashes, rejects an existing
  batch or an open execution window, and performs no broker/report/action
  mutation. Verify a missing or tampered cutover remains blocked.
- Verify that a report recovered during an active session does not interrupt protection monitoring.
- Verify real CN/HK/US one-pass watcher failures return `abnormal` without
  sleeping, while persistent standalone watcher mode retains reconnect
  behavior.
- Verify one-pass normal, holiday, and no-comparable exits become `abnormal`
  when client close fails, while persistent close exceptions still propagate.
- Verify that a report recovered inside the window executes after validation, while one recovered after the window is preserved with `missed` actions.
- Verify that the report SHA locked at opening remains the complete execution batch and that a later revision changes no automatic action.
- Verify that broker acceptance followed by a crash before result persistence reconciles without a second submission.
- Verify that an unresolved intent becomes `uncertain` and produces zero additional submissions.
- Verify all three manual uncertainty resolutions through immutable audit facts, including that only the explicit retry resolution permits another attempt.
- Verify that a broker order with the same remark but conflicting request facts becomes `conflict` and produces zero additional submissions.
- Verify that report revisions with an unchanged action identity do not produce another order.
- Verify that a partial buy waits for an active order, then submits only the remaining risk-limited quantity after a conclusive terminal state and before the window closes.
- Verify that a partial buy is retained without further submission after the window closes.
- Verify that simultaneous exit signals merge into one close action and that a sell retry covers only the confirmed remaining position after the prior order is terminal.
- Verify that actions outside their valid window become `missed` and never submit.
- Verify that hostname mismatch prevents report generation, controller installation, notifications, and every broker write.
- Verify sequential migration by starting with no local ledger and an existing matching Futu order; the new executor must reconstruct the result without submitting.
- Verify the installer renders exactly one controller job per enabled market on the executor and no trend jobs on a read-only host.
- Verify restart recovery and heartbeat updates with launchd-focused integration tests.
- Verify the operational CLI exposes only the `trend-market` run/status/resolve namespace and that removed report/watcher execution commands cannot be invoked.
- Verify migration and rollback scripts never leave old and new trend executors active at the same time.
- Before completion, run focused automated tests, the complete test suite, direct controller workflows that do not create unsafe live orders, process and launchd inspection, fresh-log inspection, and the final Dashboard `make acceptance` gate.
- After an acceptance `PASS`, redeploy the exact accepted Git SHA and verify PID, working directory, Git SHA, heartbeat, fresh logs, and HTTP 200 from the review URL.

## Out of Scope

- Ordinary HK and US premarket automation.
- A single global controller coordinating all markets.
- Active-active execution or an automatic distributed leader election protocol.
- Automatic promotion of a read-only machine when the executor fails.
- Cross-machine report or ledger synchronization for read-only deployments.
- Real-money order placement.
- Changes to trend selection, sizing, protection, Kelly, or review rules.
- Retrospective submission after a strategy window has closed.
- Protection against a malicious actor that already controls the trusted
  executor or can coherently rewrite the entire local ledger and its hashes.

## Further Notes

Futu is the cross-machine execution fact source, while immutable local intents
preserve auditability. Futu does not provide a repository-wide distributed
transaction with the local ledger, so ambiguous post-intent states deliberately
require human resolution. This is the smallest design that prioritizes no
duplicate exposure without adding a database, queue, or leader-election system.
