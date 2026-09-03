# Project Instructions

## Session Bootstrap

On the first repository action of every new session, without waiting for a user reminder:

- Treat the Docker Dev, Candidate Acceptance, Host Readiness, Production Smoke,
  merge, push, and deployment boundaries in this file as standing project rules.
  Do not ask the user to restate or confirm them, and do not summarize them unless asked.
  This does not replace the required concrete plan and explicit user approval for code or behavior changes.
- Silently verify the repository root, current worktree, branch, HEAD SHA, and
  working-tree status before planning or making current-state claims.
- Before claiming PASS, READY, deployed, or healthy, verify evidence for the exact
  current SHA. Results from another SHA or a previous conversation do not transfer.
  If evidence is unavailable, report UNKNOWN; do not run an expensive or external
  gate solely to fill the gap unless the current request requires it.

## Worktree Baseline

Start every implementation or repository-change task from the current local `main` in an isolated branch and
worktree. Do not derive work from an unrelated or dirty checkout.

## Development Verification

Development is Docker-isolated: `make test` builds only the `dev` target of the
worktree-specific `Dockerfile.dev` image and runs the existing backend pytest
suite with `-m "not pressure and not browser"`. The image includes the Node
runtime and `procps`, but excludes npm, Python/JS Playwright, Chromium/browser
assets, host mounts, network, published ports, Docker socket, home directory,
and credentials.
Pass `TEST='path::test_name'` for a focused selector. Playwright is a host-only
Production Smoke prerequisite; normal development and Candidate Acceptance have
zero browser cost. Do not add dependencies or weaken existing skips/xfails. Pure
docs/config changes do not run `make test`.

Changes confined to the standalone trend-curve collector/backtester/CLI/storage and their dedicated
tests use `make test-trend-curve`; they do not require full `make test`, Candidate
Acceptance, Host Readiness, or Production Smoke. If shared modules/dependencies,
Dashboard/backend runtime, reports, trading behavior, or wider production paths are
touched, the normal gates still apply.

The four gates are separate: Docker dev; `make candidate-acceptance` ending
`Candidate Acceptance: PASS`/`FAIL` (backend-only, with `pressure` and `browser`
tests excluded and only `LIVE-*` scenarios excluded from the portable prediction
suite); host-only `make host-readiness` ending `READY`/`BLOCKED`, including
read-only macOS checks of system Chrome for the five marked Python browser
regressions and the installed repository Playwright runner with cached Chromium;
then an explicit exact-SHA deployment followed by read-only
`make production-smoke EXPECTED_SHA=<40hex> EXPECTED_ROOT=<absolute immutable checkout> EXPECTED_RUNTIME_ROOT=/absolute/path/to/shared-runtime PRE_DEPLOY_SUBMISSION_BASELINE=<captured JSON>`
ending `HEALTHY`/`ROLLBACK`. A missing host runner/browser yields `BLOCKED`.
Production Smoke first runs the five marked Python browser regressions, then
uses the direct cached JS runner against `tests/e2e/production-smoke.spec.ts`;
both run from the validated release root, while the prediction error log is
read from the shared runtime root; the browser blocks non-read-only requests
before navigation and the submission baseline is checked again afterward. It
never downloads a browser or starts the fixture server. `make acceptance` is
only the non-mutating Docker Candidate Acceptance alias. It never installs
launchd, performs an outage check, reads production, or submits orders.

For behavior changes, retain the live-process discipline: when old code can
remain in a background process, inspect the relevant process and service manager
state, stop or restart stale processes, and verify fresh PID/timestamped logs
before claiming live behavior changed.

## Review Staging

Before review, update the dated operator-facing entry in `CHANGELOG.md`, then
stage only the exact task files. The reviewer target is `git diff --cached`.
After any post-review change, restage the exact files, rerun relevant
verification, and obtain a fresh review.

## Rebase and Merge

If local `main` advances, rebase the task commit(s) onto the current local
`main`, then rerun the required worktree tests and reviewer. Conflicts or
behavior changes require a new user-approved plan. Merge into local `main` with
`--ff-only`.

Before merging, the dated `CHANGELOG.md` entry must already be included; do not
merge first and add the log afterward.

## Acceptance and Deployment

The synchronous local-main `--ff-only` merge path does not deploy. Candidate
Acceptance, staged independent review, the dated `CHANGELOG.md`, any required
rebase/reverification, and `--ff-only` remain the local merge gates. Host
Readiness is a separate read-only check; it does not mutate launchd, data, or
production. Pure docs, config, test-only, and unrelated changes remain exempt.

Before the first deployment, production must be moved once manually to a clean,
immutable detached release checkout. This required migration is not performed
by `make` or by any acceptance/readiness/smoke target. Capture a redacted
pre-deploy `current_execution`/`last_execution` JSON baseline before explicit
deployment authorization. Deploy only the exact Candidate-accepted SHA using
the existing release runbook, then run Production Smoke against that detached
checkout. Smoke reads health, process/listener, logs, and current-execution
evidence and never deploys, restarts, rolls back, or submits.

Each result applies only to the exact SHA named by its gate. A Candidate
`FAIL` or Host `BLOCKED` blocks deployment. On a Candidate failure, first
complete one read-only audit of every reported error and its downstream
dependencies. Then make one batched fix-forward and rerun the required focused
checks and Candidate gate; do not mutate production. Stop without edits for
business-rule or architecture decisions, external credentials/services/market/
browser/data blockers, dirty/non-main state, a changed SHA, non-reproducible
failures, or any repair requiring test weakening or scope expansion.

Deployment requires Candidate `PASS`, Host `READY`, the exact SHA, and explicit
user authorization. Production Smoke must report `HEALTHY` for that same SHA;
`ROLLBACK` is an evidence result only, not an automated rollback action.

Local merge, remote push, and remote deployment are separate actions. Never
automate remote push or deployment; both still require explicit user
authorization.

Screenshots remain optional unless explicitly requested.

## Cleanup

Normal task cleanup occurs only after local merge, user confirmation, and a
clean worktree. Never delete a dirty worktree.
