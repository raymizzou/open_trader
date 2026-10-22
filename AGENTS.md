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
`origin/main` in an isolated branch and worktree at a durable path. Keep task
evidence durable too; `/tmp` must not be its only location. Local `main` only synchronizes
GitHub `main`; never integrate task branches or create delivery commits there.
Do not use an unrelated or dirty checkout. Code and behavior changes require a concrete plan and explicit user
approval; follow the implementation and TDD workflow in the linked runbook after
approval. Existing authorization remains valid within its approved scope.

If local `main` has divergent local commits, preserve that branch, its worktree,
and its local changes. Create the task from freshly fetched `origin/main`; do not
reset, force-update, or merge divergent `main` during task setup. Reconciliation
is a separate scoped action.

Read the task checkout's `AGENTS.md` together with its linked runbooks. Copying
`AGENTS.md` alone into an older checkout does not migrate source or workflow;
use the checkout's own files and do not duplicate the existing workstation guide.

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

Main owns investigation, planning, acceptance, Git and publication. Before
implementation, include one concise [impact map](docs/operations/agent-verification.md#implementation-impact-map)
in the approved plan: changed behavior, production seams/direct consumers, exact
test nodeids, old expectations to migrate and assertions to preserve. Update that
map as evidence changes; it does not add a verification gate or full suite.

When modules/services, behavior, risk or expected effort/time materially exceed
the approved plan, pause expanded repairs. Main may classify causes and check
representative cases read-only, then present impact, repair batches, dependencies,
verification, exclusions, uncertainty and continue/narrow/split options. Obtain
approval before expanded implementation; failure counts are not defect counts.
Follow [scope reassessment](docs/operations/agent-verification.md#scope-expansion-and-reassessment).
The worker autonomously batches routine in-scope repairs, preserving the contract
and protected assertions; do not require another release instruction per test or
small correction. Keep the worker for the same target and use the required fresh
initial reviewer. See [worker batches](docs/operations/agent-verification.md#approved-worker-batches).

Keep one compact [task state and execution record](docs/operations/agent-verification.md#task-state-and-recovery),
updating state after a batch or commit and linking original evidence at handoff.
On interruption, verify actual worktree, edits, active commands and live session
ownership before resuming the original session/model. Actual reads, edits or tool
execution establish progress; a `working` badge alone does not. After 10 continuous
minutes without actual progress, Main reports the blocker and waiting/model-change
options. The user decides model changes; this does not authorize model changes for other roles,
set a test/CI timeout or authorize stopping active commands.

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
focused diagnosis and is not a prerequisite to task-branch publication.

GitHub CI runs all four backend services (`gateway`, `legacy`, `account`,
`prediction`) with `TEST_N_LEG=0` and the explicit permanent N-leg retirement
manifest on every final GitHub `main` push. Feature pushes, PR events and tag
pushes do not trigger this workflow. No workflow concurrency group cancels or
replaces another main SHA's run. Documentation-only, LP-only,
trend-only, and missing-diff cases have the same active backend coverage, excluding
`pressure` and `browser`. Before first publication, complete applicable focused
local checks; independent review follows publication and continues alongside
delivery. After merge, Main records the final SHA and actual CI run, attempt and
result. Merge success is not CI success. Failed, cancelled or missing CI stays
visible; Main diagnoses it and proposes a scoped repair/revert through a new PR.
No automatic main write, revert, retry-until-green or deployment is authorized.
Deployment Preflight runs only for a selected final GitHub `main` SHA during an
explicitly authorized deployment. It reuses trusted exact-SHA main-push CI and
checks immutable source/lock/runtime identity; it never reruns backend tests.
CI also runs the non-LIVE portable prediction scenarios and proves that executed
required nodeids plus the explicit optional collection and declared retired nodeids
cover the complete backend collection without overlap. The sole optional node is
`tests/test_lp_d2_service_lifecycle.py::test_supported_rollback_uses_current_api_without_disposable_record_audit`,
declared in `scripts/ci_optional_tests.json`. Its unchanged regression runs serially
in `Rollback advisory`, automatically only on main pushes, with a manual entry.
Its genuine failure can be deferred; required CI and Deployment Preflight do not
wait for it. Optional classification is not execution or success. Other active
tests remain required. Retired tests are recorded as not executed, never passed. See
[the retirement policy](docs/operations/ci-nleg-retirement.md). `candidate-acceptance` and `acceptance` are compatibility
aliases for this lightweight check, not another test stage. Use the supported
forward wrapper in [deployment-preflight.md](docs/operations/deployment-preflight.md),
with fresh Host Readiness before installation and Production Smoke afterward.
Missing or mismatched evidence blocks deployment; no automatic full-test retry.
Existing authorized compatible rollback procedures remain separate.
Exact-SHA evidence, conflict-resolution, exception and deployment rules still apply.

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

When a conflict only combines independent added `CHANGELOG.md` entries, preserve
both entries, dates, and meaning and resolve it directly without a new permission
question. Continue required checks and independent review. For this mechanical
resolution on an already-published task branch, Main may synchronize `origin/main`
into that same task branch with a normal merge commit and ordinary push, without
rewriting history. This does not authorize merging the PR into GitHub `main`,
force-pushing, deployment, or choosing between contradictory release claims;
meaning-changing conflicts still require the existing plan approval.

Before first publication, update the dated operator-facing entry in `CHANGELOG.md`,
complete applicable focused/security checks, then Main stages and commits only
the exact task files. For an unpublished local-only review, stage those files
and use `git diff --cached`. After publication, pin the base and head SHAs and review
`git diff <base>...<head>`; read-only PR review requires no staging or file edits.
After any post-review change, rerun relevant verification; Main stages, commits
and pushes only the task changes to the same open PR, then refresh independent review.
After merge, repairs require a new isolated branch and PR, with the existing
publication authorization and separate user authorization for that PR's merge.
For unpublished local-only work, restage only task files and refresh staged review
without publishing.
Use a fresh independent reviewer for the initial review; reuse that reviewer
for in-scope repairs against the updated published head/base. A
new reviewer is needed when scope or architecture changes. A newer GitHub `main`
alone does not require a rebase, merge back into the task branch, push or rerun
solely for CI freshness. Resolve actual conflicts within scope. A rebase or
conflict resolution that changes behavior, scope, risk or architecture requires
a new approved plan. Mechanical resolutions
that preserve these may proceed within approved scope, with required checks and
independent review. Under the standing 2026-10-09 user authorization scoped to
`raymizzou/open_trader`, Main must promptly push the task branch and create a
normal (non-draft) PR targeting `main`, or update its existing PR, once the
[first reviewable implementation](docs/operations/agent-verification.md#first-reviewable-implementation)
passes new/changed and known directly affected tests (the relevant consumer union
for shared production changes), plus applicable focused/security checks. Known
confirmed affected failures must be repaired before publication; new core tests
alone do not meet that condition. Full CI supplements local checks by finding
previously unrecognized impacts. Documentation-only work uses its
verification exemption. Do not ask again for task-branch push or PR permission.
Explicit local-only, no-push, do-not-publish or draft instructions override this
default. This standing authorization supersedes older pre-publication review
and separate push/PR permission requirements. Publish before independent review
completion, dots feedback, repair
closure, final acceptance or merge; an unavailable reviewer does not delay it.
Report actual publication/access failures. Verify the remote PR URL and exact
head/base, and record checks and pending review status. Publication is a review
handoff, not approval or readiness; claim dots has started only with observed evidence.
Immediately start a separate Herdr independent reviewer pane after publication;
verify actual startup and keep review and post-merge CI status separate. Pin issue scope, PR
URL, head/base SHAs and verification; unavailable prerequisites are blockers,
not completed reviews. Follow the
[authorized PR delivery loop](docs/operations/agent-verification.md#authorized-pr-delivery-loop)
for reviewer comment authority, feedback repairs and worker handoff.
Main remains the delivery owner. After every PR update (including pushes) and
before requesting or executing an authorized merge, freshly fetch all reviews,
inline threads and conversation comments against the latest PR/head/base, with
complete pagination and fresh CI/mergeability. Record thread resolved/unresolved
state and comment dispositions; report outstanding feedback with URL, blocking
decision and owner. Keep ownership until the defined handoff or recorded stopping
condition. Completed review and repaired blockers remain Main's responsibility;
report unfinished review, feedback and verification accurately. After explicit
authorization for that PR's merge, verify repository, base `main`, open state,
expected head SHA and absence of conflicts, then merge through GitHub with the
expected head. Unknown mergeability needs a bounded metadata refresh; an actual
conflict needs scoped resolution. Do not wait for feature-push/PR-merge CI,
pending independent review/dots or an old review-ready/full-acceptance label.
These ongoing duties do not create a replacement premerge wait. Do not integrate
through local `main` or direct push.
See [CI identity](docs/operations/ci.md) and the
[repository protections](docs/operations/repository-protection.md); use dated
read-back evidence for settings. Old feature branches can still carry workflows
that trigger extra runs during migration; do not synchronize or push solely to
remove those runs, or wait for them.

## OpenTrader PR review publication

The standing user authorization dated 2026-10-09 applies to
`raymizzou/open_trader` across machines and checkouts, including Air and Mini.
An authorized PR creation/update or review of an already published PR includes
the assigned independent reviewer's own result comment on that same PR; see the
[publication procedure](docs/operations/agent-verification.md#open-trader-pr-review-publication).
Explicit local-only or do-not-post instructions override it, and
pre-publication staged reviews stay local.

## Delivery boundaries

Task-branch commits/pushes and PR creation/updates, including in-scope repair
commits to the same open PR, have the standing authorization above. After merge,
repairs use the new-PR route described above. Workers and
reviewers retain their Git boundaries; Main owns publication. This grants no
force-push, direct `main` push, automatic merge or deployment authority.
GitHub merge, release/tag creation, and deployment remain separate actions
requiring their applicable explicit authorization. A merged PR
does not release or deploy. Neither PR-head checks nor GitHub's synthetic PR
merge SHA substitute for trusted main-push CI evidence on the selected final GitHub `main` SHA.
A failed main SHA cannot be deployed and does not change running production.
An older main ancestor needs its own valid evidence when selected for deployment.

Screenshots are optional unless requested. Clean up only after GitHub merge,
user confirmation, and a clean worktree; never delete a dirty worktree.
Ending a worker session is separate from deleting its worktree or branch. End
development workers only with saved work, completed assigned review/repair work
or an accepted handoff, and a recorded owner for remaining responsibilities;
see [worker lifecycle](docs/operations/agent-verification.md#worker-lifecycle).

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
