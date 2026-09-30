# Agent Verification and Delivery

This runbook is binding with `AGENTS.md` and the global agent instructions.
Read it before selecting or running development gates, Candidate Acceptance,
merge, or deployment. Documentation and configuration-only work does not run
`make test`; other exemptions below remain scope-specific.

## Development verification

`make test SERVICE=prediction` builds only the `dev` target of the
worktree-specific `Dockerfile.dev` image and runs that service's backend test
files with `-m "not pressure and not browser"`. Valid services are `gateway`,
`legacy`, `account`, and `prediction`; space-separated names cover shared
changes, for example `SERVICE='gateway prediction'`. `TEST='path::test_name'`
selects a narrower seam instead. Unscoped `make test` fails before building.
While N-leg arbitrage is paused, service-scoped development tests default to
`TEST_N_LEG=0`: the dedicated N-leg execution, validation, solver, resolver,
selection and scheduler files listed in Makefile `N_LEG_TESTS` are omitted.
LP, shared models/storage/runtime/service, release and pause-protection tests
remain active. This is a temporary test-selection policy, independent of the
production `N_LEG_PAUSED` setting. Set `TEST_N_LEG=1` to restore the complete
service suite; explicit `TEST=...` always runs the requested tests. Changes to
an omitted N-leg component must use its explicit `TEST` selection or
`TEST_N_LEG=1`. Run the complete Prediction suite before re-enabling N-leg.
Candidate Acceptance keeps its complete backend coverage regardless of this
development-only switch.
Scopes containing `prediction` default to six pytest-xdist workers with
`--dist=loadgroup`, distributing individual tests across workers. Solver
benchmark tests share one worker to reuse their full-handoff fixture cache.
Set `TEST_WORKERS=4` on a busy host or `TEST_WORKERS=1` for serial diagnosis;
tune `TEST_WORKERS` to available capacity.
Other services and explicit `TEST=...` selections default to serial execution;
they also accept `TEST_WORKERS`. Candidate Acceptance remains serial.
Python bytecode is cached under `/tmp/open-trader-bytecache` inside each test
container, reducing repeated interpreter startup without changing source trees.
Gateway contains `frontend_gateway` tests; Legacy contains Dashboard and the
remaining shared backend test files. Update the Makefile prefixes when adding
a service-specific test family. During
development and PR review, run only the affected service tests. Candidate
Acceptance owns complete backend coverage only when preparing an explicitly
authorized deployment, after GitHub PR merge, for the selected final GitHub
`main` SHA. Do not run it during development, review, before merge, or
automatically after merge. The image includes Node and `procps`, and excludes npm,
Python/JS Playwright, Chromium/browser assets, host mounts, network, published
ports, the Docker socket, the home directory, and credentials. The approved
pytest-xdist dependency is pinned in the development extras and consumed from
`uv.lock` by the dev image. See [dependency reproducibility](dependency-reproducibility.md)
for the locked Python/build-tool baseline and clean-build evidence.
The 2026-09-29 approved cloud credential exception permits only the optional
`cloud-ssm` extra (pinned Tencent SSM SDK and common SDK); Docker development
installs it for offline SDK transport tests. Do not add other dependencies or
weaken existing skips/xfails. Playwright is a host-only
Production Smoke prerequisite; ordinary development and Candidate Acceptance
have zero browser cost.

Changes confined to the standalone trend-curve collector, backtester, CLI,
storage, and their dedicated tests use `make test-trend-curve`; they do not
require the full suite, Candidate Acceptance, Host Readiness, or Production
Smoke. Changes to shared modules or dependencies, Dashboard/backend runtime,
reports, trading behavior, or wider production paths use the normal gates.

## Four separate gates

The results are independent: Docker development, Candidate Acceptance,
Host Readiness, and Production Smoke. Each result applies only to the exact
SHA named by its gate.

- `make candidate-acceptance` must end with `Candidate Acceptance: PASS` or
  `FAIL`. It is backend-only, excludes `pressure` and `browser`, and excludes
  only `LIVE-*` scenarios from the portable prediction suite.
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
- `make acceptance` is the non-mutating Docker Candidate Acceptance alias. It
  never installs launchd, performs an outage check, reads production, or
  submits orders.

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
`origin/main` → affected-scope development checks → staged independent review
(including a dated `CHANGELOG.md` entry) → authorized branch push → Draft PR
→ latest CI success → explicit user approval → GitHub merge. Local `main` is
only a synchronized copy of GitHub `main`, never an integration or repair path.
When the base advances, fetch/rebase, rerun affected checks and independent
review, and inspect fresh CI; conflicts or behavior changes require a new
approved plan. Never force-push `main`. A reviewed branch rewrite also requires
authorized publication; do not discard another worker's commits.

There are three distinct identities: the reviewed PR head; GitHub's synthetic
PR merge commit (`github.sha` for PR CI); and the final GitHub `main` commit
after merge. Record the PR head, base and tested merge SHA with the Actions run.
Inspect the exact check-run name `required` from GitHub Actions (app ID 15368),
not only a green UI label; see [ci.md](ci.md). PR CI success never establishes
Candidate Acceptance for a different final SHA. Recheck CI for the final main
SHA, and run Candidate Acceptance only for that selected final GitHub `main`
SHA when preparing an explicitly authorized deployment. It is not a PR merge
gate, and merging alone never starts it. A later SHA invalidates earlier
exact-SHA acceptance evidence.

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
only the exact Candidate-accepted SHA using the existing release runbook, then
run Smoke against that detached checkout. Smoke reads selected-service health,
process/listener, logs, the selected Prediction N_LEG/LP contract, and browser evidence; it never deploys,
restarts, rolls back, or submits. `ROLLBACK` is evidence only.

Candidate `FAIL` or Host `BLOCKED` blocks deployment. For a Candidate failure,
first complete one read-only audit of every reported error and its downstream
dependencies. Then make one batched fix-forward, rerun focused checks and
independent review, submit a repair PR, pass CI, obtain user merge approval,
and merge on GitHub. Rerun Candidate Acceptance for the resulting final main
deployment SHA; do not mutate production. Stop without edits for a
business-rule or architecture decision, an external credentials/services/
market/browser/data blocker, a dirty or invalid candidate checkout, a candidate
SHA not verified as the selected final GitHub main commit, a changed SHA, a
non-reproducible failure, or any repair requiring test weakening or scope
expansion. An isolated repair branch and a clean detached immutable release
checkout are expected; neither is required to be named `main`.

Deployment requires the exact-SHA Candidate `PASS`, Host `READY`, and explicit
user authorization. Production Smoke must report `HEALTHY` for that same SHA.
Branch push, Draft PR, GitHub merge, release/tag creation and deployment remain
separate authorization boundaries. Do not infer release or deployment permission
from push or merge approval. Preserve the exact accepted SHA, immutable root,
`code_root`, service owner and rollback evidence throughout the handoff.

## Prediction-only Linux cloud topology

For CVM Prediction with a local Prediction-only Gateway, use the additional
`prediction-cloud-host-readiness` and `prediction-cloud-smoke` targets documented
in [prediction-cloud.md](prediction-cloud.md). They retain the exact-SHA,
read-only browser and independent ownership requirements across both machines.
The systemd helper's `PRECHECK_OK` and `BACKEND_SMOKE_OK` are component results,
not substitutes for READY and HEALTHY. Candidate Acceptance timing, deployment
and trading authorization remain unchanged. Operator handoff, metadata isolation
and shared-host resource evidence must be explicit; the gate does not invent it.
