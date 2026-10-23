# Agent Verification and Delivery

This runbook defines the project workflow with `AGENTS.md`; compatible global
guidance also applies, subject to higher-priority instructions.
Read it before selecting or running development gates, Deployment Preflight,
merge, or deployment. Documentation and configuration-only work does not run
`make test`; other exemptions below remain scope-specific.

## Implementation and TDD

After plan approval, implement within the approved scope in the isolated task
worktree. For new or changed behavior, including bug fixes, first add a focused
test that fails for the intended missing or incorrect behavior. Record the RED
result, make the smallest implementation change, then run that test and the
directly affected consumers. A failure caused only by missing dependencies does
not establish RED. For behavior-preserving refactors, run the existing focused
contract tests before and after the change. Documentation and configuration
exemptions below remain valid; a skill does not add another approval requirement
when the project already grants an exemption.

Delegate implementation only when the user or applicable instructions authorize
it. Give the worker the approved scope, worktree, and verification contract.
The required independent review remains separate from implementation: the author
does not serve as their own reviewer. Follow the publication, review and repair
ordering in `AGENTS.md`; external dots/Opus PR review follows first publication.
Do not launch, resume, replace or wait for a local Codex/Herdr reviewer unless the
user explicitly requests one again. This applies to workflow/documentation PRs too.

When the user selects Herdr for implementation, start new Codex implementation
workers with `gpt-6.1-sol` and `high` reasoning unless the user chooses otherwise:

```sh
herdr agent start <worker-name> --kind codex --pane <available-pane-id> -- -m gpt-6.1-sol -c 'model_reasoning_effort="high"'
```

Before assigning work, use `herdr agent read <worker-name>` to verify the selected
model. Planning and existing sessions retain their separately selected models.
Herdr is not required for work the user has not assigned to it.

### Implementation impact map

Main owns investigation, requirements, planning and acceptance. Before
implementation, keep one concise impact map with the approved plan and worker
brief. Map each changed behavior to its production seam and direct consumers,
exact affected test nodeids, old expectations to migrate and assertions to
preserve. Use source/caller evidence to identify consumers, including across
services for shared production changes. Mark unknown impacts explicitly and
update this same map when evidence changes. This is planning evidence, not an
additional gate or a reason to run a full suite.

For serial-to-concurrent migrations, include these checks in the map:

- Match orders/results by request, order, session and audit identity, rather than
  arrival position or a fixed order number.
- Wait for the actual completion event or thread/Future completion before
  observing terminal state. For example, `Event.is_set()` is an observation,
  not a wait for an asynchronously produced event.
- State whether an assertion concerns a historical return snapshot or fresh
  public state. After the original sender completes, fetch current state when
  testing current resource ownership; do not treat an old snapshot as fresh.
- Keep an independent real-time watchdog for synchronization, separate from
  controlled business time. Retain cleanup and negative regression evidence.

Preserve financial, identity, UNKNOWN resource ownership and no-replay
protections, along with success, boundary and negative assertions. Give the
worker the approved public seams, exact cases, independent expectations and
verified runner commands. Keep that same contract and per-case evidence available
for external review under the existing TDD contract. Do not re-approve unchanged
cases or duplicate the TDD cycle across roles.

### Scope expansion and reassessment

Pause expanded repairs when modules/services, behavior, architecture, risk or
expected effort/time materially exceed the approved plan. Main may do bounded
read-only classification and representative checks before discussion; it need
not establish every cause first. Group failures by confirmed common cause:
old expectations, fixture/observation problems, production regressions and
unknowns. A failure count is not a count of independent defects; no numeric
failure threshold replaces this assessment.

Main presents the following before requesting approval for expanded work:

- Cause groups and unknowns, target paths/modules, changed behavior and impact
  relative to the approved contract.
- Repair batches by common cause, dependencies/order, verification nodeids,
  preserved assertions, completion and renewed-pause conditions.
- Risks, exclusions, effort/time uncertainty and viable continue, narrow or
  split options with their costs.

Obtain user approval before expanded implementation. Routine in-scope fixture,
field, serialization, assertion or observation-timing corrections that preserve
the approved contract do not need repeated approval. Do not silently absorb a
material expansion into such corrections.

### Approved worker batches

Main owns overall assessment, planning, progress, acceptance, Git and publication.
An approved batch authorizes its worker to complete same-cause scoped repairs
autonomously, including routine fixture, field, serialization and observation
corrections. Preserve original failures and protected assertions; batch findings
and results instead of stopping for a release instruction after every test or
small correction. Escalate material scope, contract, architecture, risk or effort
changes through the reassessment procedure above.

Use the same worker for the same target/architecture, with an updated brief when
needed; use a fresh worker for a new target. External dots/Opus feedback supplies
independent PR review; it does not require a local reviewer. Do not add agents
merely to satisfy a skill recipe. These rules do not expand delegation authority
or change worker/reviewer Git, model, pane or file-write boundaries.

### Task state and recovery

Keep isolated task worktrees and essential evidence in durable directories,
never only in `/tmp`. Unique briefs can use `mktemp -d` under a persistent parent.
Maintain one compact task-state record: task and owner, completed work, pending
issues, branch/SHA, unsaved or uncommitted edits, active commands/handles, live
session/pane identity, next action and owner. Update it after a batch or commit;
do not add a manual handoff after each tool call.

Keep append-only execution evidence. Record each execution once with command,
test scope, code identity (including uncommitted changes where applicable),
environment, exit status and original log reference. Preserve failures and
exact-SHA limits; a later pass does not erase them. Handoffs link existing records
and update changed facts. Do not copy full histories, rebuild all impact maps or
hash every repository file for each small repair. Final task diff and version
checks remain required. Test-only repairs cover affected shared-helper consumers
but do not re-audit unrelated production code.

On capacity or connection interruption, first check active execution handles so
an in-flight command is not duplicated. Verify the state record against the
actual worktree, dirty/unsaved edits and live session/pane ownership. Preserve
saved work and distinguish missing evidence from completed work; do not guess
why temporary data disappeared or repeat completed commands to reconstruct a
handoff. Do not move or delete other tasks' worktrees.

Check workers on notification or ten minutes after the previous check. Read the
current batch handoff, verify live session/handles, and inspect only new execution
evidence. Record batch ID, handoff path, last actual progress and next check time.
Actual reads, edits or tool execution establish progress; a `working` badge alone
does not. Log silence alone is not a stall. Report unverifiable live-command
progress once as unverified, then only on state change; preserve active commands.
Evaluate the ten-minute no-progress threshold at these checks, without adding
deadline polling. The capacity exception below does not change ordinary cadence.

### Worker capacity recovery

Main owns recovery from a transient model-capacity error. Do not stop at the first
error to ask the user to type `go`. This is a bounded recovery policy, not a daemon
or permission to relaunch a worker blindly.

1. Verify the live task, assigned batch, session/model, worktree, latest actual
   progress and active commands/queued retries. Preserve saved and dirty work.
   Retry only after the failed turn has settled and no command or retry remains
   running or queued. Never interrupt or duplicate a tool, test, edit or submission.
   A stale capacity banner on a recovered worker does not justify another retry.
2. Retry the same batch in the same session and current model once after that
   verification. If that turn again fails with capacity and settles, allow at most
   two further retries, after 60 and 120 seconds respectively. Recheck identity
   and active commands before each attempt. Record attempt, delay, result and last
   real progress in durable task state. Stop retries as soon as actual work resumes.
3. When three retries are exhausted or ten continuous minutes pass without actual
   progress, report the verified blocker and waiting/model-change options. The
   user decides model changes. Do not silently change model, reasoning effort,
   service tier, credentials, permissions or infrastructure, or start a new session.

This narrow exception supersedes blanket no-auto-retry/first-capacity-error-stop
rules. Approval denials and scope, contract or architecture questions are not
capacity errors. Recovery does not expand scope, authorize rerunning active or
completed commands, or change test/CI timeouts. Ordinary connection failures
still require verified state before resuming the original session/model; report
confirmed ten-minute inactivity rather than continuing blind retriggers.

## Development verification

Run only directly affected or newly added test nodeids locally, in an existing
Python environment with the current worktree's source explicitly selected:

```sh
PYTHONPATH=src python -m pytest tests/path.py::test_name
```

Use that environment's Python executable (for example `.venv/bin/python`) when
needed; do not accidentally test an installed copy from another checkout.
Test-only changes run the changed tests. Shared test helpers also require their
directly affected consumer tests. Shared production modules require the union
of relevant consumer tests across services, not entire services. Standalone
trend-curve changes follow the same focused-nodeid rule. Record the selected
nodeids and why they cover the change, exact SHA, command, environment and results.
Pure documentation/configuration changes need no local backend run unless they
affect a testable contract; CI planner/workflow contracts need focused regression
checks. Missing dependencies or unavailable environments are blockers to report,
not passing evidence. Keep TDD, relevant stability checks and independent review.

Docker is not a prerequisite to task-branch publication. Optional focused Docker
diagnosis can use `make test TEST='tests/path.py::test_name'`; local development
does not require whole-service or full-backend suites. GitHub CI owns required backend coverage and the explicit optional
classification automatically on every final GitHub `main` push: `gateway`, `legacy`, `account`, and
`prediction`, with `TEST_N_LEG=0` and the permanent retirement manifest, excluding
`pressure` and `browser`. The 224 exact reviewed release-test identities are declared in
`scripts/ci_optional_tests.json` under `release-advisory-v1`; see
[Release advisory](ci.md#release-advisory). Their unchanged whole cases run serially in a separate workflow, automatically
only on main pushes, with a manual entry. Genuine failures can be deferred;
required CI and Deployment Preflight do not wait for it. Only the literal reviewed set is exempt from required execution. New names
and new parameter identities remain required. Financial/order, UNKNOWN, no-replay,
current-API handoff/restart, active drain, credential SSM runtime and stopped
data-recovery transaction tests remain required.
Documentation-only, LP-only, trend-only and unavailable/empty diff cases all run
that same coverage. Feature pushes, PR events and tag pushes do not trigger this
workflow. Preserve its existing main-only `workflow_dispatch` with no inputs,
plan and required main-ref guards and exact `github.sha` checkout. A manual
fallback needs separate authorization; dispatch alone is not successful evidence.
There is no workflow concurrency group, so later pushes or manual dispatches do
not cancel or replace earlier runs, including pending runs.
See [CI identity and execution](ci.md). CI also runs the non-LIVE portable prediction scenarios. It does not run
Deployment Preflight, Host Readiness or Production Smoke.

### Existing Docker test mechanics

CI reuses `make test SERVICE=<service>` and the worktree-specific `Dockerfile.dev`
image. Valid service names are `gateway`, `legacy`, `account`, and `prediction`;
unscoped `make test` fails before building. Gateway contains `frontend_gateway`
tests; Legacy contains Dashboard and remaining shared backend test files,
including standalone trend-curve tests. Keep selection prefixes current when
adding service-specific test families. The retained standalone trend-curve CI
job is not selected because legacy already includes its tests.

Makefile and CI use `TEST_N_LEG=0`: only the 29 reviewed files in
`scripts/ci_nleg_retired.json` are omitted from execution because N-leg is
permanently retired. CI separately excludes the 224 exact reviewed release-advisory identities;
ordinary local direct pytest remains unchanged. The three mixed/shared files
remain active, as do LP,
service/runtime and pause guards. Unknown/new files default to active coverage.
The shared validation fixture file stays importable. `TEST_N_LEG=1` and explicit
`TEST=...` remain manual diagnostic entries; no scheduled full N-leg regression
is required. Selection never changes production `N_LEG_PAUSED`.
See [the retirement classification and evidence](ci-nleg-retirement.md).
Service routing now lives in `scripts/ci_evidence.py`, shared by Make, the
collection proof and deployment evidence; update its prefixes for new service
families. Invalid manifests and empty partitions fail before the test build.
The Makefile's Prediction service default remains six pytest-xdist workers with
`--dist=loadgroup`, distributing individual tests across workers; solver benchmark
tests share one worker to reuse their full-handoff fixture cache. CI overrides
this to two workers for Prediction. `TEST_WORKERS=4` can reduce busy-host load and
`TEST_WORKERS=1` supports serial diagnosis. Non-Prediction services and explicit
`TEST=...` selections default to serial, and also accept `TEST_WORKERS`.
Portable scenarios run serially in CI. Known Prediction shared-port and cached
fixture groups retain their xdist grouping; global cross-service serial ordering
is not a separate deployment requirement. CI proves complete backend collection equals executed required nodeids plus
the explicit optional collection and declared retired nodeids, with no gaps or
overlap. Optional classification means `not-executed-advisory`, never passed;
it does not retire the test or weaken other active coverage. Each job retains selected
nodeids, phase durations, outcomes, JUnit and wall time, including failed runs.
The development image includes Node and `procps`, but excludes npm, Python/JS
Playwright and Chromium/browser assets. Test containers have no host mounts,
network, published ports, Docker socket, home directory or credentials.
Python bytecode is cached under `/tmp/open-trader-bytecache` inside the container.
The approved pytest-xdist dependency is pinned in development extras and consumed
from `uv.lock`; see [dependency reproducibility](dependency-reproducibility.md).
The 2026-09-29 approved cloud credential exception permits only the optional
`cloud-ssm` extra (pinned Tencent SSM SDK and common SDK); Docker development
installs it for offline SDK transport tests. Do not add other dependencies or
weaken existing skips/xfails. Playwright remains a host-only Production Smoke
prerequisite; ordinary backend CI and Deployment Preflight have zero browser cost. Deployment
Preflight reuses trusted main push/manual evidence for the selected final GitHub `main`
SHA when preparing an explicitly authorized deployment. Preflight selects the
newest exact-SHA run across push and main-only `workflow_dispatch`; a newer
unsuccessful run blocks older green evidence, and artifacts retain the real event. It does not rerun tests.

## Test design and stability

These principles apply to all tests, including new tests and stability repairs;
they do not change the scope-specific verification routes above.

### Preserve the contract

- State the behavior being protected before changing a test. Keep success,
  boundary, and negative assertions: expired facts must still fail, and a
  service must remain unavailable until all required recovery work completes.
- Preserve production deadlines, fail-closed checks, ordering, cancellation,
  and resource-cleanup guarantees. A stability repair must not silently change
  business behavior; obtain explicit user approval for a contract change.
- Do not skip/xfail failures, weaken assertions, inflate business deadlines or
  test timeouts, remove coverage, or add retry-until-green logic merely to make
  a run pass. Existing approved scope exemptions remain unchanged.

### Separate business time from scheduling

- Use an injected or narrowly scoped controllable clock for expiry, cooldown,
  and other business-time boundaries; advance it deliberately across the
  boundary and verify both sides. Do not make a valid fixture expire just
  because startup, CI load, or worker scheduling consumed a tiny real budget.
- Coordinate concurrent work with observable completion events, barriers, or
  conditions tied to the actual state transition. Arbitrary sleeps are not
  proof that work started or finished; bounded polling is acceptable when no
  completion signal is available and checks the real condition.
- Keep a bounded real-time watchdog independent of the controlled clock so
  hangs still fail. Separate startup/synchronization budgets from the business
  deadline being asserted; justify any watchdog adjustment with evidence while
  preserving the original contract, rather than simply increasing a timeout.
- Isolate clock overrides and restore them during teardown. Do not globally
  patch shared clock functions in ways that affect unrelated threads, event
  loops, subprocess supervision, or watchdogs. Release waiters and reclaim
  threads/processes/resources on failure as well as success.
- Retain real integration and separate timeout, cancellation, and cleanup
  tests. When elapsed time, scheduling, latency, or throughput is itself the
  contract, test it with real time and justified bounds/environment assumptions;
  controlled time must not replace that evidence. Fake clocks are not required
  for tests that do not benefit from them.

### Diagnose and verify repairs

1. Keep the original failure, exact SHA, command, environment, worker count,
   and logs. Reproduce and distinguish a product defect, invalid fixture,
   scheduling dependency, or environmental blocker before choosing a repair.
   A tight timeout or one green rerun alone does not prove a test is flaky;
   report an unconfirmed diagnosis as such.
2. After an authorized repair, show regression/negative evidence that the test
   still rejects the original wrong behavior. Where needed, use a temporary
   targeted mutation or fault injection and verify that it fails; do not publish
   the mutation. For example, accepting expired data or declaring readiness
   before every recovery worker finishes must still fail the test.
3. Repeat the affected tests with reproducible settings in serial and relevant
   supported concurrency (including two workers for Prediction CI). Exercise
   controlled scheduling delays/alternate interleavings, then run the required
   affected-scope checks. Record repetition counts, settings, and every outcome;
   a later pass does not erase a failed attempt, and repetition alone does not
   prove correctness. Diagnostic retries must not turn failures into success.
4. Report all failures and any remaining uncertainty or blocked verification.
   Main stages/commits only the exact changes and publishes the task update, then
   obtains refreshed independent review of the published head/base. For unpublished
   local-only work, restage task files and review `git diff --cached` without
   publishing. Existing exact-SHA, conflict-resolution, CI,
   and approval requirements still apply.

## Verification and deployment boundaries

Development verification, trusted CI, Deployment Preflight, Host Readiness, and
Production Smoke have distinct evidence. Each result applies only to its exact SHA.
See [the source-release preflight runbook](deployment-preflight.md) for the supported
forward-deployment wrapper, trust checks, environment limits and rollback path.

- `make deployment-preflight EXPECTED_SHA=<40hex> PYTHON_BIN=<release-python>`
  checks trusted exact-SHA CI and source/lock/runtime identity. Success exits zero;
  missing or mismatched evidence exits nonzero. It runs no backend pytest or Docker
  build. CI owns the four backend services and non-LIVE portable scenarios.
- `make host-readiness` is read-only and must end with `READY` or `BLOCKED`.
  On a fresh host with no Open Trader launchd agent or selected-service
  listener, use `FIRST_DEPLOY=1`. This checks that no managed agent or selected
  listener exists, skips only the old Account status probe, and retains the
  installer, wallet, browser, storage, and Futu checks. The default mode still
  requires running selected Gateway/Legacy/Account listeners and Account status.
  Selected Prediction instead requires the supported manager's read-only
  `--preflight` to exit zero with the exact preflight schema and `READY` or
  `RECOVERABLE`. It passes the selected checkout, runtime, Python and private
  config. A verified failed managed startup can pass without an old 8769
  listener; missing listeners alone or unknown/malformed/blocked manager proof
  cannot pass. First-deploy absence checks and post-install Smoke remain unchanged.
  It performs read-only macOS checks of system Chrome for the five marked
  Python browser regressions and checks the installed repository Playwright
  runner with cached Chromium. A missing host runner or browser is `BLOCKED`.
  A slow or unavailable business-state response from the old running
  Prediction instance does not block replacing that instance. Ownership and
  handoff remain required before deployment; Production Smoke verifies the new
  instance after deployment.
- The exact Smoke command is:

  ```sh
  make production-smoke EXPECTED_SHA=<40hex> EXPECTED_ROOT=<absolute immutable checkout> EXPECTED_RUNTIME_ROOT=/absolute/path/to/shared-runtime
  ```

  Supported Prediction entrypoints permanently pause non-LP business.
  Smoke accepts historical `N_LEG_PAUSED=0` and `1` inputs; unset/default, zero
  and one all require `N_LEG_PAUSED` health plus a valid LP dashboard. It never
  requests `/api/prediction-arbitrage/state`. Invalid input, running/missing
  health and missing LP evidence still block the gate. Stored zero cannot
  reactivate N-leg and does not relax credential or startup admission checks.

  It must end with `HEALTHY` or `ROLLBACK`. `make production-smoke` first runs
  the five marked Python browser regressions, then uses the direct cached JS
  runner against `tests/e2e/production-smoke.spec.ts` from the validated
  release root; both runs use that validated release root. The prediction
  error log comes from the shared runtime root. The browser blocks
  non-read-only requests before navigation. Smoke checks the selected
  Prediction permanent-pause contract before the browser run. It never downloads
  a browser or starts the fixture server.
- `make candidate-acceptance` and `make acceptance` are compatibility aliases
  for Deployment Preflight. They read GitHub and the selected local environment;
  they never install services, build images, rerun tests or submit orders.

Both readiness and Smoke accept a nonempty whitespace-separated
`RELEASE_SERVICES` list containing only `gateway`, `legacy`, `account`, and
`prediction`; the default remains `gateway legacy account prediction` for full
stack compatibility. For example:

```sh
make host-readiness RELEASE_SERVICES=gateway
make production-smoke RELEASE_SERVICES='gateway prediction' \
  EXPECTED_SHA=<40hex> EXPECTED_ROOT=<absolute immutable checkout> \
  EXPECTED_RUNTIME_ROOT=/absolute/path/to/shared-runtime \
  REPOSITORY_ROOT=/absolute/path/to/shared-runtime \
  PYTHON_BIN=/absolute/path/to/shared-runtime/.venv/bin/python \
  PLAYWRIGHT_NODE_PATH=/absolute/path/to/shared-runtime/node_modules
```

Smoke applies `EXPECTED_SHA` and `EXPECTED_ROOT` to selected services only;
unselected services may remain on older clean immutable releases. Every
selected service still needs its health, code root, process, listener, and log
identity checks. Account is one release unit, so its API and worker health
identities must match. Selected Prediction always validates paused health and the
LP dashboard, including under historical `N_LEG_PAUSED=0`. The browser integration
check remains common to every scope. Gateway-only
readiness uses `--mode gateway`; gateway plus legacy uses the existing stack
dry-run, and legacy selection retains the Trend and Futu checks.

Pure documentation, configuration, test-only, and unrelated changes remain
exempt from the acceptance and deployment gates; test-only changes still
receive any development validation relevant to their own scope.

## Authorized PR delivery loop

Main is the existing delivery owner, not a new permanent agent. The bounded loop
covers the approved implementation, verification and blocker repairs. Task-branch
publication has the standing authorization below. The 2026-10-23 user decision
authorizes Main's task-related feedback replies and verified addressed-thread
resolution as detailed below. Read-only review,
diagnosis and webhook observation do not grant write authority. Once authorized,
continue that loop without repeated reminders. Escalate changes to scope,
behavior, architecture, risk or test contracts. Preserve other workers' work.
This procedure coordinates [#293](https://github.com/raymizzou/open_trader/issues/293),
[#271](https://github.com/raymizzou/open_trader/issues/271) and
[#214](https://github.com/raymizzou/open_trader/issues/214).

### First reviewable implementation

The first reviewable implementation covers the approved scope with enough
evidence for independent review. Complete the new/changed and known directly
affected tests; for shared production changes, run the relevant consumer union.
Known confirmed affected failures must be handled before publication; passing
only new core tests does not establish this condition. Complete applicable
focused/security checks and add the dated operator-facing `CHANGELOG.md` entry.
Documentation-only work uses diff inspection and `git diff --check`, without
local backend tests. First publication does not require later improvements or
completed remote CI. Full backend CI supplements focused local checks by finding
previously unrecognized or omitted impacts, including cross-service and
environment/concurrency differences; it is not a reason to postpone known
affected failures or expand local verification to a full backend suite.

Standing user authorization dated 2026-10-09, scoped to `raymizzou/open_trader`
across machines and checkouts, requires Main to promptly stage/commit only the
task files, push the task branch and open a normal (non-draft) PR targeting
`main`, or update its existing PR. Do not ask again for push or PR permission.
Explicit local-only, no-push, do-not-publish or draft instructions override this
default. Workers never publish. This authorization also covers Main's task-local
repair commits/pushes to the same open PR without repeated permission. After
merge, use a new isolated repair branch and PR with the same publication authority
and separate merge authorization; a closed PR cannot receive the repair. This
publication authority grants no
force-push, direct `main` push, automatic merge, release/tag or deployment authority.
This standing authorization supersedes older requirements for completed review
before publication or separate push/PR permission.

Publish before external independent review completion, dots feedback, repair
closure, final acceptance or merge. Pending or unavailable external review does
not delay first publication; do not create a local fallback reviewer. Do not wait
for complete branch CI, add unneeded local full suites, or iterate on nonblocking
polish before the first PR. Continue required independent review and in-scope
repairs afterward. Publication is a review handoff, not a readiness or approval claim.

Verify the remote PR exists, its URL and exact head/base, and that its head
matches the pushed task commit. Record issue scope, local verification, known
limits, unverified work, pending review and CI. Report actual Git, authentication,
network or PR access/publication failures; do not label standing authorization
as pending permission. Claim dots has started only with observed evidence.

### Parallel post-publication review and delivery

Independent PR review for `raymizzou/open_trader` is supplied externally by dots
and Opus through GitHub PR reviews/comments. The 2026-10-23 user decision
supersedes older automatic local reviewer launch/reuse/model/pane and required
result-comment recipes. Do not launch, resume, replace or wait for a local
Codex/Herdr reviewer, including GPT-5.6, unless the user explicitly requests one
again. Do not create one to review this workflow change. Implementation workers
continue their assigned work. Keep already confirmed local findings as evidence;
retire a cancelled reviewer's unpublished result-comment task rather than making
it a new delivery blocker.

Pin issue requirements, PR URL, current state and immutable head/base SHAs, checks
and verification limits. Published review uses `git diff <base>...<head>`; an
explicit unpublished local-only review uses the staged diff. Record each review's
source, reviewed SHA/base, submission/comment URL, result and limits separately
from unresolved findings and CI. Old-head review and CI evidence does not transfer
to an updated target; assess findings and refresh applicable evidence when head or
base changes. This does not require pushing or rebasing solely because main advanced.

Main owns a durable task-specific wake/check mechanism while external review or
CI is pending. Record the mechanism/handle, monitoring owner, last retrieval and
next check in task state; verify that the wake/check is actually scheduled or
running. A single read is a snapshot, not an ongoing listener. Empty comments mean
no feedback observed, not completed review. Claim dots/Opus has started or completed
only from observed review evidence. Preserve pending/unknown status otherwise.
Continue the feedback loop below until its recorded stopping condition or accepted
handoff. Pending review does not delay an explicitly authorized conflict-free
merge; merging does not complete review or repair work.

### Feedback, CI and conflict repair

After publication and every push/update, before requesting or executing an
authorized merge, and at each pending-review/CI check, Main freshly retrieves:

- Current PR state, head/base SHAs and mergeability.
- All review submissions, inline threads with resolved/unresolved state and all
  nested comments, and conversation comments, from every source.
- Checks actually attached to the exact PR/head SHA, with their actual results.

Paginate every collection, including nested thread comments. Record retrieval time,
identities and completeness; incomplete reads are not a complete feedback check.
A missing head CI run is not PASS. Preserve main-only CI triggers and the separately
authorized main-only manual workflow; do not enable PR CI, dispatch manually,
change checks or bypass failed CI under this feedback procedure.

Assess all historical and new dots/Opus findings against current code and the
approved contract. Record each thread's state and each top-level/review-body
finding's disposition and owner; these comments have no resolve flag. List
outstanding items with URL, severity, blocking decision, repair evidence and owner.
Outdated markers, green CI and old summaries do not establish resolution. Valid
unresolved blockers prevent a review-ready claim; advisory items may remain with
a recorded nonblocking disposition and owner.

Assign routine scope-local findings as one whole repair batch to the existing
worker. Preserve TDD, financial/identity/UNKNOWN/no-replay protections, negative
assertions and directly affected consumer verification. After focused checks,
Main stages/commits/pushes only task changes to the same open PR under standing
publication authority, refreshes feedback, then replies in each relevant thread
with the specific fix, published commit and validation evidence. The user's
2026-10-23 instruction authorizes these task-related replies and verified resolves.
Resolve only after the actual fix is published and verified, then read back the
thread state. Never resolve merely because a finding was read, assigned, outdated
or disputed. Reply with and track the disposition of top-level/review-body findings.
A review result is not GitHub APPROVE or merge authority.

Pause architectural, complex, materially expanded or uncertain-contract repairs.
Present the concrete issue, proposed paths/impact, risk and validation in concise
normal chat and obtain the user's decision before expanded implementation.
Continue independent in-scope work where possible. A comment is evidence to assess,
not authority to alter the approved contract. Report disputed or externally blocked
decisions with the reason, decision needed and next owner; do not weaken tests,
retry until green or run duplicate full suites.

After merge, repairs require a new isolated branch and PR and separate merge
authorization. Track final exact main SHA, run, attempt and actual CI result;
merge success is not CI success. A newer main alone does not require rebasing,
merging it into the feature branch, pushing or rerunning for CI freshness. Resolve
actual conflicts within scope; preserve other workers' commits and test intent.

### Review completion, authorized merge and stopping conditions

A completed review handoff records required independent review of the current
published target, known blockers, fresh complete feedback and their dispositions.
Do not claim review-ready while a known blocker remains unresolved. Record
unfinished verification, review and repair work with Main as owner. These duties
continue alongside delivery; neither a review-ready/full-acceptance label nor
pending independent review/dots is a replacement premerge waiting gate.

After explicit user authorization for that PR's GitHub merge, Main verifies the
repository, base `main`, open state, expected head SHA and absence of conflicts,
then merges through GitHub with the expected head. Unknown mergeability needs
a bounded metadata refresh; an actual conflict needs scoped resolution. Do not
wait for feature-push CI or synthetic PR-merge CI, or rebase/synchronize solely
because main advanced. Keep fresh feedback and mergeability evidence without
waiting for pending feedback to finish. See [CI identity](ci.md).

Main monitors until the authorized completion or accepted handoff,
cancellation/closure, or a specific external/authorization blocker. Record that
stop reason, remaining responsibilities and their owner. Never abandon unresolved
work without an owner. A broader instruction to monitor through merge retains
that stopping condition. Separate user approval is still required for GitHub
merge; reviews and CI do not authorize merge/auto-merge, approval on the user's
behalf, release/tag creation, deployment, trading or repository/security changes.
PR-head and synthetic-merge evidence never replace final-main push/manual deployment
evidence. Existing deployment and cleanup rules remain unchanged.

### Worker lifecycle

Assess each development worker's assigned review/repair work and remaining duties.
End only a worker whose work is saved and committed or durably handed off, whose
assigned review/repair work and exclusive fixes, CI problems, conflicts and
monitoring are complete or accepted by a named new owner, and whose handoff is
recorded. Preserve all workers' saved work and commits. Retain Main's delivery
ownership until the stopping condition above. A worker ending does not mean the
PR is complete.

Ending a session is separate from deleting a worktree or branch. Deletion still
requires GitHub merge, user confirmation and a clean worktree under
[AGENTS.md](../../AGENTS.md#delivery-boundaries); never delete dirty work. These
rules do not authorize closing existing sessions as part of documentation work.

### Handoff and evidence record

Use the existing compact durable [task state and execution evidence](#task-state-and-recovery);
link original records and update only changed facts at handoff. Use `pending`,
`blocked`, `unrun` or `unknown` where appropriate, with reasons. The following
fields apply as relevant; they do not require a new full report per small repair.
This is a format, not an executed delivery:

```text
Task/approved scope; delivery owner; publication/reply authority:
Worktree/branch/SHA; dirty or unsaved edits; active commands/handles; session/pane:
PR URL; immutable head/base; final main SHA; Actions run/attempt/check IDs/URLs:
Local focused/security checks: command, environment, identity, result, limits:
Independent review: published head/base (or unpublished local-only staged tree), result/evidence:
Review source (dots/Opus); reviewed SHA/base; status and limits:
Review submissions/comments: URLs, verified authors/body/identities, dispositions:
Feedback: retrieval time, latest PR/head/base, complete paginated collections:
Threads/comments: URL, resolved/unresolved state or disposition, blocking decision/owner:
Repairs: fix commits, checks, independent review, update/reply evidence:
Merge: authorization, expected head, state/base/conflict check/time, final SHA:
CI: final main SHA, push/manual event, newest run/attempt, required/app 15368 actual result/artifacts:
Pending decisions/blockers; next action and owner; monitoring stop reason:
Monitoring: durable wake/check mechanism/handle, owner, last retrieval, next check:
Worker recovery: batch/session, attempts/delays/results, last actual progress:
Workers: saved work, exclusive duties/new owner, accepted handoff, lifecycle status:
```

## Merge, live processes, and deployment

The delivery path is isolated branch/worktree from freshly fetched
`origin/main` → focused local development/security checks and dated `CHANGELOG.md`
entry → Main stages/commits task-only files → prompt task-branch push/normal PR
→ independent review and feedback/repair work alongside delivery
→ explicit authorization for that PR + current no-conflict/expected-head checks
→ GitHub merge → final-main-push CI. Follow the
[delivery procedure](#authorized-pr-delivery-loop). Local `main` is
only a synchronized copy of GitHub `main`, never an integration or repair path.
Do not rebase, merge main into the task branch, push or rerun solely because the
base advanced. Resolve actual conflicts within the approved scope; material
changes need renewed approval. Rebase and conflict-resolution approval follows
[AGENTS.md](../../AGENTS.md#review-and-merge). Never force-push `main`.
A branch rewrite requires its own applicable authorization; the standing
publication authorization does not grant force-push. Do not discard
another worker's commits.

The PR head/base and final GitHub `main` commit after merge are distinct
identities. Historical feature-push and synthetic PR-merge checks do not transfer
evidence to the final main SHA. Main records that SHA, its Actions run/attempt and
actual result. No workflow concurrency group cancels/replaces an older main run
when a newer main SHA is pushed or a manual run is dispatched. Inspect the exact check-run name `required` from
GitHub Actions (app ID 15368), not only a green UI label; see [ci.md](ci.md).
Merge success is not CI success. Failed, cancelled or missing CI remains visible;
Main owns diagnosis and a scoped repair/revert proposal through PRs. No automatic
main write, revert, retry-until-green or deployment is authorized. After merge,
repairs require a new isolated branch and PR with separate merge authorization.
Independent review, feedback collection and reproducible blocker repair continue
until completed or durably handed off; merging does not complete them.

Deployment Preflight requires successful trusted main CI for the selected final
SHA, retained in main history, from push or the existing main-only manual workflow.
It selects the newest exact-SHA run across those events and verifies its latest
attempt and artifacts bound to the real event. A newer unsuccessful run blocks
older green evidence; manual fallback needs separate authorization.
A later tip does not erase that evidence; changing the selected release SHA needs
its own valid evidence. A failed main SHA cannot be deployed and does not change
running production.
It is not a PR merge gate, and merging alone never deploys or starts a preflight.

Branch/tag settings in [repository-protection.md](repository-protection.md)
have dated active read-back evidence. The 2026-10-22 approved change removed only
the main required-status object, retaining PR, deletion/non-fast-forward and tag
protections. Future settings changes need applicable authorization and exact
read-back. Old feature branches can still carry workflows that trigger extra
runs during migration; do not synchronize or push solely to remove them, or wait
for those runs. Older dated plans, reports and changelog entries describing local-main-first delivery
are historical evidence; this runbook supersedes their delivery instructions.
Host Readiness is separate and read-only; it does not mutate launchd, data, or
production.

When old code may remain in a background process, inspect the relevant process
and service-manager ownership/state. Stop or restart stale processes only
within the authorized deployment scope, then verify a fresh PID and
timestamped logs before claiming that live behavior changed.

Before the first deployment, manually move production once to a clean,
immutable detached release checkout. `make`, acceptance, readiness, and Smoke
never perform that migration. After explicit deployment authorization, deploy
only the exact CI-verified SHA through `scripts/deploy_release.py` and the existing
release runbook, then run Smoke against that detached checkout. Smoke reads selected-service health,
process/listener, logs, the selected Prediction N_LEG/LP contract, and browser evidence; it never deploys,
restarts, rolls back, or submits. `ROLLBACK` is evidence only.

Failed Deployment Preflight or Host `BLOCKED` blocks forward deployment. Missing,
expired, cancelled or mismatched CI evidence is not a reason to rerun local full
pytest. Report the missing check and obtain fresh trusted CI evidence through the
approved process. A real test failure returns through focused diagnosis, approved
in-scope repair, task-branch/PR update, independent review, CI and separately
authorized merge.
Do not weaken tests, invent attestations, provision credentials or expand scope to
turn an unknown result into success.

Deployment requires exact-SHA preflight success, fresh Host `READY`, and explicit
user authorization. Production Smoke must report `HEALTHY` for that same SHA.
Task-branch push and PR creation/updates have the standing authorization above;
GitHub merge, release/tag creation and deployment retain separate authorization
boundaries. Preserve the exact selected SHA, immutable
root, `code_root`, service owner and rollback evidence throughout the handoff.
Existing authorized rollback uses the recorded compatible release and retained
environment; it is not blocked by expired forward CI artifacts. Low-level helpers
retain their rollback semantics and are not a substitute for the supported
forward-deployment wrapper.

## Prediction-only Linux cloud topology

For CVM Prediction with a local Prediction-only Gateway, use the additional
`prediction-cloud-host-readiness` and `prediction-cloud-smoke` targets documented
in [prediction-cloud.md](prediction-cloud.md). They retain the exact-SHA,
read-only browser and independent ownership requirements across both machines.
The systemd helper's `PRECHECK_OK` and `BACKEND_SMOKE_OK` are component results,
not substitutes for READY and HEALTHY. Deployment and trading authorization remain separate. Trusted preflight replaces
the former duplicate Candidate tests; cloud host/runtime checks remain fresh. Operator handoff, metadata isolation
and shared-host resource evidence must be explicit; the gate does not invent it.

<a id="open-trader-pr-review-publication"></a>

## OpenTrader PR review publication

The 2026-10-23 external-review decision governs the default path: dots/Opus review
arrives through GitHub, and Main collects evidence, replies to task-related
findings and resolves only published, verified fixes under the
[feedback procedure](#feedback-ci-and-conflict-repair). Do not wait for a cancelled
local reviewer's unpublished result comment. Existing confirmed findings remain
evidence. No local reviewer is launched or resumed without a new explicit user
request; this section does not create one.

Only if the user explicitly requests a local reviewer again, the standing
2026-10-09 authorization permits that assigned reviewer to publish or update its
own result comment on the named authorized PR. An explicit local-only or
do-not-post instruction overrides that authority; unpublished staged reviews stay
local. Main gives that conditional assignment the PR URL, exact head/base,
publication owner and scoped result-comment permission. Before posting, verify
current identities and refresh applicable evidence if changed; check for an
existing result to avoid duplicates. Main verifies URL, author, body and reviewed
identities and accepts remaining delivery ownership. Report access/publication
failures accurately. These conditional duties do not revive a cancelled reviewer's
retired comment task or block the default external-review path.

A local reviewer, when explicitly assigned, remains read-only for files and Git.
Its result-comment authority grants no GitHub APPROVE, merge, additional push,
deployment, installation, live-data mutation, unrelated thread resolution or
other external communication. Main's authorized task-related replies and verified
resolves do not authorize unrelated messages or new scope. Merge, deployment,
manual CI dispatch and production/data/security changes retain separate authority.
