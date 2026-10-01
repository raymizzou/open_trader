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

Start every implementation or repository-change task from freshly fetched
`origin/main` in an isolated branch and worktree. Local `main` only synchronizes
GitHub `main`; never integrate task branches or create delivery commits there.
Do not use an unrelated or dirty checkout. Code and behavior changes require a concrete plan and explicit user
approval; follow the global worker and TDD contract after approval.

## Verification routing

Pure documentation and configuration changes do not run `make test`. Standalone
trend-curve collector, backtester, CLI, storage, and dedicated-test changes use
`make test-trend-curve` and do not require the full suite, Candidate Acceptance,
Host Readiness, or Production Smoke; shared modules/dependencies,
Dashboard/backend runtime, reports, trading, or wider production paths use the
normal gates. Read the linked runbook before selecting a non-exempt gate.

For production-code changes covered by the normal gates, run
`make test SERVICE=<gateway|legacy|account|prediction>` for the affected backend
service. Use multiple service names for shared changes and `TEST=...` for a
narrower seam. Do not run the full backend suite during development. After
known repairs and any required rebase, complete affected-service checks and
independent review before publishing the task branch and opening a Draft PR.
Candidate Acceptance runs only for the final GitHub `main` SHA selected for an
explicitly authorized deployment, after GitHub PR merge. Do not run it during
development, review, before merge, or automatically after merge. It is not a PR
merge gate. If a deployment candidate fails, audit every reported failure and
its downstream dependencies, batch the in-scope repairs on an isolated branch,
then repeat focused checks, review, Draft PR, CI, and authorized GitHub merge
before rerunning Candidate Acceptance for the new final deployment SHA.
Exact-SHA evidence, rebase, exception, and deployment rules still apply as
described in the runbook.

## Test design and stability

All tests must preserve the intended contract and fail for incorrect behavior,
without relying on machine speed or accidental scheduling. Use controlled
clocks for business deadlines and explicit events/barriers for synchronization,
with independent real-time watchdogs and isolated clock overrides. Keep real
integration, timeout, cancellation, cleanup, and performance coverage when
those behaviors are the subject; fake time is not mandatory everywhere.
Diagnose failures from evidence before calling them flaky. Stability repairs
must retain negative assertions, prove the regression still catches wrong
behavior, and repeat relevant serial/concurrent runs with controlled scheduling
delays. Do not hide failures by skipping, weakening assertions, inflating
timeouts, or retrying until green; report every failed attempt. Contract changes
require explicit user approval. Follow the detailed
[test stability guidance](docs/operations/agent-verification.md#test-design-and-stability).

## Review and merge

Before review, update the dated operator-facing entry in `CHANGELOG.md`, then
stage only the exact task files. The reviewer target is `git diff --cached`.
After any post-review change, restage only the exact task files, rerun relevant
verification, and obtain a fresh review.
Use a fresh independent reviewer for the initial review; reuse that reviewer
for in-scope repairs after restaging and rerunning relevant verification. A
new reviewer is needed when scope or architecture changes. If GitHub `main`
advances, fetch and rebase task commits onto `origin/main`, then rerun required
worktree checks and review. Conflicts or behavior changes require a new approved
plan. Publish only the reviewed tree to the authorized task branch, open a
Draft PR targeting `main`, and inspect the latest exact-SHA CI evidence. Require
the `required` check from GitHub Actions, then obtain explicit user approval
before merging on GitHub. Do not integrate through local `main` or direct push.
See [CI identity](docs/operations/ci.md) and the
[proposed repository protections](docs/operations/repository-protection.md);
documented settings are not evidence that protection is enabled.

## Delivery boundaries

Branch push, Draft PR, GitHub merge, release/tag creation, and deployment are
separate actions requiring their applicable explicit authorization. A merged PR
does not release or deploy. Neither PR-head checks nor GitHub's synthetic PR
merge SHA substitute for Candidate evidence on the final GitHub `main` SHA.

Screenshots are optional unless requested. Clean up only after GitHub merge,
user confirmation, and a clean worktree; never delete a dirty worktree.

## Agent skills

### Issue tracker

Issues and specs live in GitHub Issues for `raymizzou/open_trader`.
See `docs/agents/issue-tracker.md`.

### Triage labels

Use the five default triage labels.
See `docs/agents/triage-labels.md`.

### Domain docs

Use the existing multi-context layout through `CONTEXT-MAP.md`.
See `docs/agents/domain.md`.
