# Project Instructions

## Worktree Baseline

Start every implementation or repository-change task from the current local `main` in an isolated branch and
worktree. Do not derive work from an unrelated or dirty checkout.

## Development Verification

While developing, run focused tests and direct non-destructive workflow checks
when practical. For covered runtime changes, run `make test` once before review
or merge, then run a task-specific non-destructive preflight against copies or
read-only current artifacts before merge. After merge, run one runtime-only
`make acceptance` from clean local `main`. Existing declared skips and xfails
may remain; adding or weakening one requires explicit user approval. Pure
docs/config changes do not run `make test`.

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

The synchronous local-main `--ff-only` merge path does not run `make acceptance`.
Focused checks, `make test` for code/test changes, staged independent review, the
dated `CHANGELOG.md`, any required rebase/reverification, and `--ff-only` remain
the local merge gates. `make acceptance` is not a prerequisite for merging to
local `main`; covered runtime changes require exactly one post-merge runtime-only
`make acceptance` result of `PASS` before they are described as accepted or
complete. Pure docs, config, test-only, and unrelated changes remain exempt.

Covered runtime changes use one worktree `make test` and one task-specific
non-destructive preflight against copies or read-only current artifacts before
review/merge. After merge, run one runtime-only `make acceptance` from clean
local `main`; it remains runtime-mutating and may install/restart local
Account, Dashboard, and Trend `launchd` services and perform the controlled
Account outage check. The full Python suite is owned by the pre-merge gate and
is not repeated by acceptance.

Each result applies only to the exact start SHA. `PASS` may be silent. If the
SHA changes during the run, the result is stale and forbids repair from that
run.
`FAIL` or `BLOCKED` for the current SHA is reported to Feishu and blocks
push/deploy, but does not roll back or block later local-main development. On
an acceptance failure, first complete one read-only audit of every reported
error and its downstream dependencies. Then make one batched fix-forward and
rerun the required focused checks and pre-merge gates; do not rerun acceptance
between individual fixes. Stop without edits for business-rule or
architecture decisions, external credentials/services/market/browser/data
blockers, dirty/non-main state, a changed SHA, non-reproducible failures, or
any repair requiring test weakening or scope expansion.

Runtime deployment requires `PASS` for the exact SHA plus explicit user
authorization. Redeploy that exact local SHA and verify the new PID, cwd, SHA,
fresh logs, and HTTP 200 from the review URL. An exact-SHA restart needs no
second acceptance run when source and data are unchanged.

Local merge, remote push, and remote deployment are separate actions. Never
automate remote push or deployment; both still require explicit user
authorization.

Screenshots remain optional unless explicitly requested.

## Cleanup

Normal task cleanup occurs only after local merge, user confirmation, and a
clean worktree. Never delete a dirty worktree.
