# Project Instructions

This is the always-read entry point. This repository and its linked runbooks
define OpenTrader's workflow on every workstation, including Air and Mini.
Apply compatible global guidance alongside these project rules, subject to
higher-priority instructions. Skills and plugins, including Ponytail, support
implementation; they do not waive the project's approval, TDD, verification,
independent-review, or delivery requirements. Read
[agent-verification.md](docs/operations/agent-verification.md) before selecting
an implementation workflow or running development gates, acceptance, merge, or
deployment; documentation and configuration-only tasks may use the stated exemption.

## Session bootstrap

On the first repository action of each session, silently verify the repository
root, current worktree, branch, HEAD SHA, and working-tree status. Treat the
Development Verification, Deployment Preflight, Host Readiness, Production Smoke,
merge, push, and deployment boundaries in the linked runbook as standing rules.
Do not ask the user to restate or confirm them.

Before claiming PASS, READY, deployed, or healthy, verify evidence for the
exact current SHA. Evidence from another SHA does not transfer. Prior-session
static or CI evidence may be reused after rechecking its SHA, scope, environment,
and recorded result. Refresh host, runtime, and external-state evidence before
current readiness or health claims. If evidence is unavailable, report UNKNOWN;
do not run an expensive or external gate solely to fill the gap unless the current
request requires it.

## Worktree and approval

Start every implementation or repository-change task from freshly fetched
`origin/main` in an isolated branch and worktree. Local `main` only synchronizes
GitHub `main`; never integrate task branches or create delivery commits there.
Do not use an unrelated or dirty checkout. Code and behavior changes require a concrete plan and explicit user
approval; follow the implementation and TDD workflow in the linked runbook after
approval. Existing authorization remains valid within its approved scope.

For workstation setup, dependency drift, or Air/Mini comparisons, read
[dependency-reproducibility.md](docs/operations/dependency-reproducibility.md#workstation-setup-and-comparison).
Use the checked-out project's rules and an explicitly selected development
interpreter. Verify each machine before claiming that their environments match;
pulling this file alone does not synchronize global configuration or dependencies.

## Plans and handoffs

Before requesting plan approval, explain the current problem, intended behavior,
key tradeoff or risk, and focused validation in plain language. Once approved,
continue within that scope without repeated approval requests; material scope
or risk changes and the existing action-specific approval boundaries still apply.

For review and delivery, lead with what behavior changed and why. Tie claims to
the exact SHA and evidence links; distinguish verified, failed, unrun, and unknown
checks. Name any specific next action that needs approval and what it entails.
Use diagrams or interactive explanations only when they materially reduce the
cost of understanding; they supplement, never replace, tests and independent
review. Keep key principles and runbook links here, with detailed procedures in
the linked runbooks rather than repeated across instruction files.

## Writing language

For English output, apply [ASD-STE100 writing principles](https://www.asd-ste100.org/about_STE.html):
short, clear sentences, consistent terms, and explicit actions. Do not claim
strict ASD-STE100 compliance without checking the full standard and dictionary.
For Chinese output, use academic-paper rigor: lead with a concise summary,
define key terms, support conclusions with evidence, and distinguish facts,
inferences, and uncertainty. Adapt STE's clarity, concision, and consistent
terminology to Chinese; do not treat its English dictionary as a Chinese standard.
Use data, comparisons, or figures when they help substantiate the argument.
Never invent evidence or citations. Match length and structure to the task;
simple answers do not need a full paper format or unnecessary formality.

## Verification routing

Local development runs only directly affected or newly added test nodeids, using
an existing Python environment against this worktree's source, for example
`PYTHONPATH=src python -m pytest tests/path.py::test_name`. Test-only changes run
the changed tests; shared test helpers also require their directly affected
consumers. Shared production changes run the union of relevant consumer tests,
not whole service suites. Pure documentation/configuration changes need no local
backend run unless they affect a testable contract. Report missing dependencies
or blocked focused checks rather than claiming success. Docker is optional for
focused diagnosis and is not a prerequisite to pushing a reviewed branch.

GitHub CI runs all four backend services (`gateway`, `legacy`, `account`,
`prediction`) with `TEST_N_LEG=1` on every branch push and every PR targeting
`main`, including the final merged-main push. Documentation-only, LP-only,
trend-only, and missing-diff cases have the same full backend coverage, excluding
`pressure` and `browser`. Push-head and PR-merge runs intentionally test distinct
SHA identities. Before authorized publication, complete focused local checks
and independent review; inspect exact-SHA CI before requesting merge approval.
Deployment Preflight runs only for a selected final GitHub `main` SHA during an
explicitly authorized deployment. It reuses trusted exact-SHA main-push CI and
checks immutable source/lock/runtime identity; it never reruns backend tests.
CI also runs the non-LIVE portable prediction scenarios and proves backend
collection coverage. `candidate-acceptance` and `acceptance` are compatibility
aliases for this lightweight check, not another test stage. Use the supported
forward wrapper in [deployment-preflight.md](docs/operations/deployment-preflight.md),
with fresh Host Readiness before installation and Production Smoke afterward.
Missing or mismatched evidence blocks deployment; no automatic full-test retry.
Existing authorized compatible rollback procedures remain separate.
Exact-SHA evidence, rebase, exception, and deployment rules still apply.

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

Before pre-commit review, update the dated operator-facing entry in `CHANGELOG.md`,
then stage only the exact task files. The reviewer target is `git diff --cached`.
For an existing PR, pin the base and head SHAs and review
`git diff <base>...<head>`; read-only PR review requires no staging or file edits.
After any post-review change, restage only the exact task files, rerun relevant
verification, and obtain a fresh review.
Use a fresh independent reviewer for the initial review; reuse that reviewer
for in-scope repairs after restaging and rerunning relevant verification. A
new reviewer is needed when scope or architecture changes. If GitHub `main`
advances, fetch and rebase task commits onto `origin/main`, then rerun required
worktree checks and review. A rebase or conflict resolution that changes behavior,
scope, risk, or architecture requires a new approved plan. Mechanical resolutions
that preserve these may proceed within approved scope, with required checks and
independent review. Publish only the reviewed tree to the authorized task branch,
open a Draft PR targeting `main`, and inspect the latest exact-SHA CI evidence. Require
the `required` check from GitHub Actions, then obtain explicit user approval
before merging on GitHub. Do not integrate through local `main` or direct push.
See [CI identity](docs/operations/ci.md) and the
[proposed repository protections](docs/operations/repository-protection.md);
documented settings are not evidence that protection is enabled.

## Delivery boundaries

Branch push, Draft PR, GitHub merge, release/tag creation, and deployment are
separate actions requiring their applicable explicit authorization. A merged PR
does not release or deploy. Neither PR-head checks nor GitHub's synthetic PR
merge SHA substitute for trusted main-push CI evidence on the selected final GitHub `main` SHA.

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
