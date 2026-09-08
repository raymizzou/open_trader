# Project Instructions

This is the always-read entry point. Global agent instructions still govern
approval, worker delegation, TDD, and independent review. Read
[agent-verification.md](docs/operations/agent-verification.md) before selecting
or running development gates, acceptance, merge, or deployment; documentation
and configuration-only tasks may use the stated exemption.

## Session bootstrap

On the first repository action of each session, silently verify the repository
root, current worktree, branch, HEAD SHA, and working-tree status. Treat the
Docker Dev, Candidate Acceptance, Host Readiness, Production Smoke, merge,
push, and deployment boundaries in the linked runbook as standing rules.
Do not ask the user to restate or confirm them.

Before claiming PASS, READY, deployed, or healthy, verify evidence for the
exact current SHA. Evidence from another SHA or an earlier session does not
transfer. If evidence is unavailable, report UNKNOWN; do not run an expensive
or external gate solely to fill the gap unless the current request requires it.

## Worktree and approval

Start every implementation or repository-change task from the current local
`main` in an isolated branch and worktree. Do not use an unrelated or dirty
checkout. Code and behavior changes require a concrete plan and explicit user
approval; follow the global worker and TDD contract after approval.

## Verification routing

Pure documentation and configuration changes do not run `make test`. Standalone
trend-curve collector, backtester, CLI, storage, and dedicated-test changes use
`make test-trend-curve` and do not require the full suite, Candidate Acceptance,
Host Readiness, or Production Smoke; shared modules/dependencies,
Dashboard/backend runtime, reports, trading, or wider production paths use the
normal gates. Read the linked runbook before selecting a non-exempt gate.

## Review and merge

Before review, update the dated operator-facing entry in `CHANGELOG.md`, then
stage only the exact task files. The reviewer target is `git diff --cached`.
After any post-review change, restage only the exact task files, rerun relevant
verification, and obtain a fresh review.
Use a fresh independent reviewer for the initial review; reuse that reviewer
for in-scope repairs after restaging and rerunning relevant verification. A
new reviewer is needed when scope or architecture changes. If local `main`
advances, rebase task commits onto it and rerun the required worktree checks
and review. Conflicts or behavior changes require a new approved plan. Merge
into local `main` with `--ff-only`, only after the dated changelog entry is
included.

## Delivery boundaries

Local merge, remote push, and remote deployment are separate actions. Local
merge does not deploy. Never automate push or deployment; each requires
explicit user authorization and the exact gates in the linked runbook.

Screenshots are optional unless requested. Clean up only after local merge,
user confirmation, and a clean worktree; never delete a dirty worktree.
