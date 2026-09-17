# Agent Verification and Delivery

This runbook is binding with `AGENTS.md` and the global agent instructions.
Read it before selecting or running development gates, Candidate Acceptance,
merge, or deployment. Documentation and configuration-only work does not run
`make test`; other exemptions below remain scope-specific.

## Development verification

`make test` builds only the `dev` target of the worktree-specific
`Dockerfile.dev` image and runs the existing backend pytest suite with
`-m "not pressure and not browser"`. Use `TEST='path::test_name'` for a
focused selector. The image includes Node and `procps`, and excludes npm,
Python/JS Playwright, Chromium/browser assets, host mounts, network, published
ports, the Docker socket, the home directory, and credentials. Do not add
dependencies or weaken existing skips/xfails. Playwright is a host-only
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

  For a release with the reversible N_LEG pause enabled, add
  `N_LEG_PAUSED=1`. Smoke then requires the prediction health payload to report
  `N_LEG_PAUSED`, verifies the LP dashboard contract, and does not request
  `/api/prediction-arbitrage/state`. The default `N_LEG_PAUSED=0` keeps the
  normal state contract check; a missing or contradictory pause status blocks
  the gate.

  It must end with `HEALTHY` or `ROLLBACK`. `make production-smoke` first runs
  the five marked Python browser regressions, then uses the direct cached JS
  runner against `tests/e2e/production-smoke.spec.ts` from the validated
  release root; both runs use that validated release root. The prediction
  error log comes from the shared runtime root. The browser blocks
  non-read-only requests before navigation. Smoke checks the N_LEG state
  contract before the browser run and never downloads a browser or starts the
  fixture server.
- `make acceptance` is the non-mutating Docker Candidate Acceptance alias. It
  never installs launchd, performs an outage check, reads production, or
  submits orders.

Pure documentation, configuration, test-only, and unrelated changes remain
exempt from the acceptance and deployment gates; test-only changes still
receive any development validation relevant to their own scope.

## Merge, live processes, and deployment

The synchronous local-main `--ff-only` merge path does not deploy. Local merge
gates are Candidate Acceptance `PASS`, staged independent review, the dated
`CHANGELOG.md` entry, any required rebase and reverification, and `--ff-only`.
Host Readiness is separate and read-only; it does not mutate launchd, data, or
production.

When old code may remain in a background process, inspect the relevant process
and service-manager ownership/state. Stop or restart stale processes only
within the authorized deployment scope, then verify a fresh PID and
timestamped logs before claiming that live behavior changed.

Before the first deployment, manually move production once to a clean,
immutable detached release checkout. `make`, acceptance, readiness, and Smoke
never perform that migration. Before explicit deployment authorization, deploy
only the exact Candidate-accepted SHA using the existing release runbook, then
run Smoke against that detached checkout. Smoke reads health,
process/listener, logs, N_LEG state, and browser evidence; it never deploys,
restarts, rolls back, or submits. `ROLLBACK` is evidence only.

Candidate `FAIL` or Host `BLOCKED` blocks deployment. For a Candidate failure,
first complete one read-only audit of every reported error and its downstream
dependencies. Then make one batched fix-forward, rerun focused checks and
Candidate Acceptance, and do not mutate production. Stop without edits for a
business-rule or architecture decision, an external credentials/services/
market/browser/data blocker, dirty or non-main state, a changed SHA, a
non-reproducible failure, or any repair requiring test weakening or scope
expansion.

Deployment requires the exact-SHA Candidate `PASS`, Host `READY`, and explicit
user authorization. Production Smoke must report `HEALTHY` for that same SHA.
Local merge, remote push, and remote deployment remain separate actions; never
automate push or deployment.
