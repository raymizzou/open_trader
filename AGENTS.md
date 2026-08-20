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

Never run `make acceptance` in a feature worktree. After worktree tests and
review pass, merge to local `main`, then run `make acceptance` from local `main`
only for Dashboard, Prediction, Trend, or another change covered by the runtime
acceptance suite. Pure docs/config, test-only, and unrelated changes do not run
acceptance.

Acceptance results are authoritative:

- `PASS`: only then may a covered runtime task be described as accepted or
  complete.
- `FAIL`: do not push, deploy, or claim completion. Diagnose, present the exact
  repair plan, obtain user approval, and use a fresh fix-forward worker from
  merged `main`.
- `BLOCKED`: do not push, deploy, or claim completion; report the blocker and do not substitute other evidence.

For `PASS` on a Dashboard or other reviewable runtime task, redeploy the exact
accepted local SHA and verify the new PID, cwd, SHA, fresh logs, and HTTP 200
from the review URL. An exact-SHA restart needs no second acceptance run when
source and data are unchanged.

Local merge, remote push, and remote deployment are separate actions. Never
push or remotely deploy without explicit user authorization.

Screenshots remain optional unless explicitly requested.

## Cleanup

Clean up the task branch, worktree, and brief only after applicable acceptance
and deployment are complete and the user confirms. Never delete a dirty
worktree.
