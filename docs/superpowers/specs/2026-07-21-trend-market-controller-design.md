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

## Implementation Decisions

### Controller ownership

- Install exactly one persistent trend controller for each enabled market on the designated executor machine.
- Remove the separate trend report and watcher jobs after the corresponding controller is verified.
- Use `RunAtLoad` and `KeepAlive` with restart throttling. Calendar triggers no longer sequence business steps.
- On every start and wake, derive the next action from durable facts rather than a mutable in-process state machine.
- The controller lifecycle is: ensure a valid frozen report, reconcile eligible opening actions, monitor active protection lines during the session, and capture the close. The controller may enter at any point in this lifecycle after a restart.
- Existing market calendars and window rules remain authoritative. CN and HK retain the 09:30–10:00 opening window; US retains its regular-session rule.

### Execution mode

- Add `OPEN_TRADER_TREND_EXECUTOR_HOST` as the single executor designation.
- Compare it with the local hostname. An exact match produces effective mode `execute`; an absent or non-matching value produces `readonly`.
- Only the executor deployment installs and runs trend controllers.
- Read-only mode does not generate reports, execute or modify orders, run protection automation, capture closes, or send trend task notifications. It may display already available data and perform explicitly read-only queries.
- The order-writing adapter checks effective mode immediately before every broker mutation. Controller routing is not the only guard.
- Automatic failover is not provided. Promoting another machine requires an explicit executor-host configuration change after the previous executor is stopped.

### Action identity and duplicate prevention

- Identify an opening action by market, execution date, symbol, and side. Report hashes and action indexes remain evidence but are not order identity.
- Derive the Futu remark from that stable action identity so it is identical after restart, report revision, or sequential machine migration.
- Before any submission, query the designated Futu simulated account for that remark.
- If an exact broker order exists, write or repair the immutable local result and do not submit.
- If the remark exists but symbol, side, or quantity conflicts, record `conflict`, notify once, and do not submit.
- If a local result exists, treat the action as reconciled and do not submit.
- If a local intent exists without a result, reconcile against Futu. If no broker fact resolves the ambiguity, record `uncertain` and never resubmit automatically.
- Only when neither a local execution fact nor a broker order exists may the controller atomically write a new intent and submit once.
- A later report revision with the same action identity cannot create a second order. An existing intent locks the submitted request facts.
- Protection exits retain their stable event identity and use the same reconciliation policy.

### Validation and retry policy

- Validate market, execution date, report freshness and completeness, strategy version, action fields, account identity, positions, cash/NAV, quote validity, quantity, and trading window before writing an intent.
- Failures before intent creation may retry with bounded backoff while the action remains eligible.
- Failures after intent creation are never treated as safe retries without a conclusive broker reconciliation.
- When a valid window expires, write one durable `missed` fact and stop attempting the action.
- Missing or invalid reports block execution. The controller may retry report generation or validation while the window remains open.

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
- The migration removes old report/watcher jobs only after the replacement controller for that market passes live verification.

## Testing Decisions

- Test through the controller interface. Clock, sleep, market data, broker, notifier, and filesystem dependencies are internal adapters supplied by the controller implementation and replaced in tests.
- Prefer end-to-end state-transition tests over new tests for pass-through scheduling helpers.
- Preserve focused tests for the existing report, risk, execution, and protection modules; delete obsolete tests that only assert the removed launchd split.
- Verify that report completion followed by restart does not regenerate the report.
- Verify that broker acceptance followed by a crash before result persistence reconciles without a second submission.
- Verify that an unresolved intent becomes `uncertain` and produces zero additional submissions.
- Verify that a broker order with the same remark but conflicting request facts becomes `conflict` and produces zero additional submissions.
- Verify that report revisions with an unchanged action identity do not produce another order.
- Verify that actions outside their valid window become `missed` and never submit.
- Verify that hostname mismatch prevents report generation, controller installation, notifications, and every broker write.
- Verify sequential migration by starting with no local ledger and an existing matching Futu order; the new executor must reconstruct the result without submitting.
- Verify the installer renders exactly one controller job per enabled market on the executor and no trend jobs on a read-only host.
- Verify restart recovery and heartbeat updates with launchd-focused integration tests.
- Before completion, run focused automated tests, the complete test suite, direct controller workflows that do not create unsafe live orders, process and launchd inspection, fresh-log inspection, and the final Dashboard `make acceptance` gate.
- After an acceptance `PASS`, redeploy the exact accepted Git SHA and verify PID, working directory, Git SHA, heartbeat, fresh logs, and HTTP 200 from the review URL.

## Out of Scope

- Ordinary HK and US premarket automation.
- A single global controller coordinating all markets.
- Active-active execution or an automatic distributed leader election protocol.
- Automatic promotion of a read-only machine when the executor fails.
- Real-money order placement.
- Changes to trend selection, sizing, protection, Kelly, or review rules.
- Retrospective submission after a strategy window has closed.

## Further Notes

Futu is the cross-machine execution fact source, while immutable local intents
preserve auditability. Futu does not provide a repository-wide distributed
transaction with the local ledger, so ambiguous post-intent states deliberately
require human resolution. This is the smallest design that prioritizes no
duplicate exposure without adding a database, queue, or leader-election system.
