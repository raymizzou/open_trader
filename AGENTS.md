# Project Instructions

## Worktree Baseline

Start every implementation or repository-change task from the current local `main` in an isolated branch and
worktree. Do not derive work from an unrelated or dirty checkout.

## Development Verification

While developing, run focused tests and direct non-destructive workflow checks
when practical. For any code or test change, `make test` must exit 0 in the
prepared worktree before review. Existing declared skips and xfails may remain;
adding or weakening one requires explicit user approval. Pure docs/config
changes do not run `make test`.

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

The synchronous local-main merge/completion path does not run `make acceptance`.
Focused checks, `make test` for code/test changes, staged independent review, the
dated `CHANGELOG.md`, any required rebase/reverification, and `--ff-only` remain
the local merge gates. `make acceptance` is not a prerequisite for merging to
local `main` or describing the repository change as complete.

A bounded `Open Trader acceptance guardian` cron job runs the existing full
`make acceptance` command on clean local `main` every two hours. It remains
runtime-mutating: it may install/restart local Account, Dashboard, and Trend
`launchd` services and perform the controlled Account outage check.

Each result applies only to the exact start SHA. `PASS` may be silent. If the
SHA changes during the run, the result is stale and forbids repair from that
run.
`FAIL` or `BLOCKED` for the current SHA is reported to Feishu and blocks
push/deploy, but does not roll back or block later local-main development.

Standing automated repair authorization is narrow and belongs exclusively to
the `Open Trader acceptance guardian` cron job. A deterministic, reproducible,
repository-owned acceptance regression may be repaired from current clean local
`main` in a fresh isolated worktree, using the existing failing public seam as
RED and adding at most a narrow regression when needed. The cron job must use
the smallest fix, may not weaken tests or add skips/xfails, must run focused
verification and `make test`, update `CHANGELOG.md`, obtain independent staged
review, rebase and reverify if `main` moved, merge with `--ff-only` to local
`main`, and rerun acceptance. It stops and reports without edits for
business-rule or architecture decisions, external credentials/services/market/
browser/data blockers, dirty/non-main state, a changed SHA, non-reproducible
failures, or any repair requiring test weakening or scope expansion.

Runtime deployment requires `PASS` for the exact SHA plus explicit user
authorization. Redeploy that exact local SHA and verify the new PID, cwd, SHA,
fresh logs, and HTTP 200 from the review URL. An exact-SHA restart needs no
second acceptance run when source and data are unchanged.

Local merge, remote push, and remote deployment are separate actions. Never
automate remote push or deployment; both still require explicit user
authorization.

Screenshots remain optional unless explicitly requested.

## Cleanup

Normal task cleanup does not wait for the periodic acceptance cycle. Clean up
the task branch, worktree, and brief only after local merge, user confirmation,
and a clean worktree. Never delete a dirty worktree.
