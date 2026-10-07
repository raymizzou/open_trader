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
does not serve as their own reviewer. Follow the staged-review and repair rules
in `AGENTS.md` before publication.

When the user selects Herdr for implementation, start new Codex implementation
workers with `gpt-6.1-sol` and `high` reasoning unless the user chooses otherwise:

```sh
herdr agent start <worker-name> --kind codex --pane <available-pane-id> -- -m gpt-6.1-sol -c 'model_reasoning_effort="high"'
```

Before assigning work, use `herdr agent read <worker-name>` to verify the selected
model. Planning, independent review, and existing sessions retain their separately
selected models. Herdr is not required for work the user has not assigned to it.

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

Docker is not a prerequisite to push a reviewed branch. Optional focused Docker
diagnosis can use `make test TEST='tests/path.py::test_name'`; local development
does not require whole-service or full-backend suites. GitHub CI owns the complete active
backend test suite on every branch push and every PR targeting `main`,
including the final merged-main push: `gateway`, `legacy`, `account`, and
`prediction`, with `TEST_N_LEG=0` and the permanent retirement manifest, excluding
`pressure` and `browser`.
Documentation-only, LP-only, trend-only and unavailable/empty diff cases all run
that same coverage. Push CI tests the branch head; PR CI tests the synthetic merge
candidate. These intentionally separate runs cover distinct SHA identities.
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
permanently retired. The three mixed/shared files remain active, as do LP,
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
is not a separate deployment requirement. CI proves complete backend collection equals executed active nodeids plus
declared retired nodeids, with no gaps or overlap. Each job retains selected
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
Preflight reuses trusted main-push evidence for the selected final GitHub `main`
SHA when preparing an explicitly authorized deployment. It does not rerun tests.

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
   Restage the exact changes and obtain independent review before publication;
   existing exact-SHA, rebase, CI, and approval requirements still apply.

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
  requires running selected-service listeners and Account status.
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

  For a selected Prediction release with the reversible N_LEG pause enabled,
  add `N_LEG_PAUSED=1`. Smoke then requires the selected prediction health
  payload to report `N_LEG_PAUSED`, verifies the LP dashboard contract, and does
  not request `/api/prediction-arbitrage/state`. The default `N_LEG_PAUSED=0`
  keeps the normal state contract check for selected Prediction; a missing or
  contradictory pause status blocks the gate.

  It must end with `HEALTHY` or `ROLLBACK`. `make production-smoke` first runs
  the five marked Python browser regressions, then uses the direct cached JS
  runner against `tests/e2e/production-smoke.spec.ts` from the validated
  release root; both runs use that validated release root. The prediction
  error log comes from the shared runtime root. The browser blocks
  non-read-only requests before navigation. Smoke checks the selected
  Prediction N_LEG contract before the browser run according to
  `N_LEG_PAUSED` and never downloads a browser or starts the fixture server.
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
identities must match. Selected Prediction uses the N_LEG state probe when
`N_LEG_PAUSED=0`; paused selected Prediction validates paused health and the LP
dashboard instead. The browser integration check remains common to every scope. Gateway-only
readiness uses `--mode gateway`; gateway plus legacy uses the existing stack
dry-run, and legacy selection retains the Trend and Futu checks.

Pure documentation, configuration, test-only, and unrelated changes remain
exempt from the acceptance and deployment gates; test-only changes still
receive any development validation relevant to their own scope.

## Merge, live processes, and deployment

The delivery path is isolated branch/worktree from freshly fetched
`origin/main` → focused local development checks → staged independent review
(including a dated `CHANGELOG.md` entry) → authorized branch push → Draft PR
→ latest CI success → explicit user approval → GitHub merge. Local `main` is
only a synchronized copy of GitHub `main`, never an integration or repair path.
When the base advances, fetch/rebase, rerun focused checks and independent
review, and inspect fresh CI. Rebase and conflict-resolution approval follows
[AGENTS.md](../../AGENTS.md#review-and-merge). Never force-push `main`.
A reviewed branch rewrite also requires authorized publication; do not discard
another worker's commits.

There are three distinct identities: the reviewed PR head; GitHub's synthetic
PR merge commit (`github.sha` for PR CI); and the final GitHub `main` commit
after merge. Record the PR head, base and tested merge SHA with the Actions run.
Inspect the exact check-run name `required` from GitHub Actions (app ID 15368),
not only a green UI label; see [ci.md](ci.md). PR CI success never establishes
deployment evidence for a different final SHA. Deployment Preflight requires
successful main-push CI for the selected final SHA, retained in main history.
A later tip does not erase that evidence; changing the selected release SHA does.
It is not a PR merge gate, and merging alone never deploys or starts a preflight.

Branch/tag settings in [repository-protection.md](repository-protection.md)
are a proposed, separately approved next stage, not active enforcement. Older
dated plans, reports and changelog entries describing local-main-first delivery
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
in-scope repair, independent review, Draft PR, CI and separately authorized merge.
Do not weaken tests, invent attestations, provision credentials or expand scope to
turn an unknown result into success.

Deployment requires exact-SHA preflight success, fresh Host `READY`, and explicit
user authorization. Production Smoke must report `HEALTHY` for that same SHA.
Branch push, Draft PR, GitHub merge, release/tag creation and deployment remain
separate authorization boundaries. Preserve the exact selected SHA, immutable
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
