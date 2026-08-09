# Persistent cross-venue auto-submit mode design

Date: 2026-08-09

Status: design approved in conversation; implementation not started

## Goal

Make the Predict.fun/Polymarket cross-venue execution mode durable operator
state. A normal deployment, restart, or another person's commit must not change
that state. Only an explicit local operator command may change the configured
mode or arm automatic submission.

Keep all existing opportunity, sizing, daily-principal, same-pair, readiness,
notification-breaker, reconciliation, and residual-position rules unchanged.

## Current problem

The durable store currently records whether cross-venue automatic submission is
armed, but the configured execution mode comes from
`OPEN_TRADER_CROSS_EXECUTION_MODE`. The launchd installer defaults that value to
`observe_only` and writes it into the service plist. A routine deployment can
therefore preserve `armed=true` in SQLite while replacing the running mode with
`observe_only`.

This creates two authorities for one operator decision. The fix is to remove
the deployment environment from that decision and make the existing SQLite
cross-auto state the sole authority.

## Approaches considered

1. Store the configured mode alongside the existing durable arm state. This is
   selected because it gives the execution path one transactional authority and
   reuses the current store.
2. Keep the mode in a local file outside Git. This avoids source-controlled
   defaults but leaves the file and SQLite as two independent authorities.
3. Have the installer copy the mode from the currently loaded plist. This is
   fragile when the plist is absent, stale, or replaced and still makes
   deployment responsible for trading authority.

## Decisions

### 1. SQLite is the sole execution-authority source

Extend the existing singleton `cross_auto_state` row with
`configured_mode`. Its allowed values are exactly:

- `observe_only`;
- `manual_confirm`;
- `auto_submit`.

The same row continues to hold `armed`, `reason`, and `updated_at`. The store
returns a fail-closed state of `configured_mode=observe_only` and `armed=false`
when the row is absent, invalid, or unreadable.

The monitor snapshot and Dashboard projection read the configured mode from the
store. The execution service also reads the store directly when deciding
whether to claim an automatic attempt; it does not treat a monitor snapshot,
environment variable, plist, or command-line deployment argument as authority.

### 2. Only explicit local commands may mutate the state

The local prediction-arbitrage CLI is the only mode/arm write surface.

- `cross-auto arm` performs the existing readiness checks, then atomically
  persists `configured_mode=auto_submit` and `armed=true`.
- `cross-auto pause` persists `armed=false` and preserves the configured mode.
  Pausing an automatic configuration therefore remains visibly
  `auto_submit`, but its effective mode is `observe_only`.
- `cross-auto mode observe_only` and `cross-auto mode manual_confirm` are
  explicit long-term mode changes and also disarm automatic submission.
- `cross-auto mode auto_submit` records the requested configured mode but does
  not bypass readiness or arm submission; `cross-auto arm` remains required.

There is no web endpoint for arming or changing the configured mode. The
Dashboard keeps only its confirmed, CSRF-protected emergency-pause action.

### 3. Deployment cannot write operator state

Normal launchd installation and restart do not read, default, infer, or write
the execution mode. The rendered plist no longer contains
`OPEN_TRADER_CROSS_EXECUTION_MODE`.

The old installer option `--cross-execution-mode` is rejected with a clear
message directing the operator to the local `cross-auto mode` and
`cross-auto arm` commands. It is not silently ignored: stale automation must
fail visibly without changing the database.

Application startup accepts no mode override from the environment. Removing or
changing source defaults, plists, deployment scripts, branches, or worktrees
therefore cannot alter the persisted trading decision.

### 4. The automatic claim is transactional

The store operation that reserves a one-shot cross-venue automatic attempt must
verify, in the same transaction, all of the following:

- `configured_mode=auto_submit`;
- `armed=true`;
- no existing attempt for the same signal;
- no unsettled execution for the same pair;
- the existing Asia/Shanghai daily-principal limit permits the reservation.

If pause or a mode change commits before that claim, the new attempt is
rejected. If the attempt has already been claimed and venue submission has
started, pause prevents later entries but does not interrupt the current
execution. The current execution must finish its existing reconciliation,
incident notification, and residual-position handling.

There is no retry queue and no replay of rejected opportunities.

### 5. Effective mode is derived, not stored

The configured mode records the operator's durable intent. The effective mode
is calculated for display and execution:

- configured `auto_submit` plus armed and ready: `auto_submit`;
- configured `auto_submit` but paused or not ready: `observe_only`;
- configured `manual_confirm`: `manual_confirm`;
- configured `observe_only`: `observe_only`.

Readiness loss does not rewrite the configured mode. Recovery can restore the
effective mode only when the durable row is still armed and every existing
readiness and safety gate passes.

### 6. Rejections remain explicit and stable

Every refusal to claim an automatic attempt records and exposes a stable reason
code plus Chinese operator facts. At minimum the facts include the current
value, the limiting rule, relevant venue or pair when applicable, observation
time, and the required operator action.

Mode-state examples include:

- `configured_mode_not_auto_submit`: current configured mode and the local
  command required to change it;
- `cross_auto_paused`: configured mode is automatic but `armed=false`, with the
  local arm command and the current readiness blocker;
- existing readiness, daily-limit, same-pair, notification-breaker, sizing,
  minimum-order, and active-execution reasons remain unchanged.

The Dashboard must not show a manual order action merely because an automatic
configuration is paused or temporarily degraded. Switching to
`manual_confirm` is an explicit local configuration change.

## Migration and rollout

The schema migration adds `configured_mode` with a database constraint. Existing
installations migrate fail-closed to `observe_only` and `armed=false`; migration
does not infer automatic authority from a historical arm bit or environment
value.

For the current production installation, the user has explicitly authorized
automatic submission. After the accepted SHA is deployed, the operator runs the
local readiness-checked `cross-auto arm` command once. That writes the durable
`auto_submit + armed` state. Future deployments then preserve it without any
special flag.

If the database is unavailable or corrupt, startup and execution remain
observe-only and report the failure. They do not reconstruct authority from a
plist, environment variable, or previous process.

## Verification

Implementation is accepted only when tests and direct runtime checks prove:

1. A pre-seeded `auto_submit + armed` row survives ordinary installation,
   service restart, and redeployment unchanged.
2. Supplying the retired installer mode option fails without mutating the row.
3. Pause preserves configured `auto_submit`, disarms, and blocks new claims.
4. Explicit non-automatic mode changes disarm.
5. Missing, malformed, or unreadable state fails closed.
6. A pause committed before claim rejects the attempt with complete reason
   facts; a pause after submission begins allows only mandatory reconciliation
   and cleanup.
7. Dashboard status comes from the database, exposes configured and effective
   mode truthfully, redacts secrets, and offers no arm or mode-change action.
8. Existing cross-venue store, monitor, execution, CLI, launchd, and Dashboard
   focused tests pass.
9. The final `make acceptance` gate returns `PASS`.
10. The exact accepted SHA is redeployed, with new PID, working directory, SHA,
    fresh log evidence, HTTP 200, durable configured mode, effective mode, and
    armed state verified before handoff.

The rollout must also compare attempt and execution history before and after
deployment to prove that deployment itself did not submit an order.

## Non-goals

- No change to opportunity discovery or Stage 5 semantics.
- No change to order size, concurrent two-leg submission, reconciliation, or
  existing risk limits.
- No remote or Dashboard arm control.
- No new configuration service, state abstraction, retry queue, or second
  persistence mechanism.
