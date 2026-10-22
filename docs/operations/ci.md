# Active backend CI and focused local verification

`.github/workflows/ci.yml` runs only on pushes to GitHub `main`, including
documentation-only changes and each final merged-main SHA. Feature pushes,
PR events and tag pushes do not trigger it.
Every run selects all four existing backend services: `gateway`, `legacy`,
`account`, and `prediction`, with `TEST_N_LEG=0`. Active backend coverage excludes
`pressure` and `browser`; it does not include Deployment Preflight, Host Readiness
or Production Smoke. Non-LIVE portable prediction scenarios run in a separate
serial job and are required by the aggregate.

The stable aggregate appears as **CI / required** in the UI (workflow `CI`, job
`required`), but its exact API check-run/context name is **`required`**, emitted
by **GitHub Actions**, app ID **15368**. Deployment evidence must bind that exact
name and source, not the display string or an arbitrary same-named status.
It is not a premerge gate. No repository settings are changed by this workflow;
the [dated settings read-back](repository-protection.md) records active PR/tag
protections and the separately approved removal of the main required-status rule.

## Identity and coverage

Jobs check out `github.sha`, the exact pushed main SHA. Record the PR head/base,
final merge SHA, event, Actions run ID, attempt and actual result. The final main
commit can differ from a PR head or historical synthetic merge SHA; evidence
does not transfer between them. After authorized GitHub merge, inspect the
main-push run for that exact final SHA. Failed, cancelled or missing results
remain visible; merge success is not CI success.

Main owns focused diagnosis and a scoped repair/revert proposal through PRs.
There is no automatic main write, revert, retry-until-green or deployment.
A failed main SHA cannot be deployed and does not change running production.
Successful trusted exact-SHA main-push CI and complete artifacts are required
for separately authorized [Deployment Preflight](deployment-preflight.md),
source/lock/runtime checks, fresh Host Readiness and Production Smoke. An older
main ancestor can be selected only with its own valid evidence. Deployment does
not repeat the backend suite.

Changed paths and diff availability do not narrow CI. Documentation-only, isolated
LP, standalone trend-curve, shared, unknown, deleted/renamed, empty-diff and
missing-history changes all select the same four services with `TEST_N_LEG=0`.
The retained standalone trend-curve job stays skipped because the legacy suite
already includes its tests. Make and `scripts/ci_evidence.py` share one service selection entry point.
Update that selector's service prefixes for new service-specific test families.
The [explicit retirement manifest](ci-nleg-retirement.md) omits exactly 29 reviewed
N-leg files from default execution. The one exact rollback node described below
is advisory; all other active tests remain required, including the three
mixed/shared files, LP and pause guards. Production pause state is unchanged.
Unknown/new files do not inherit retirement from their names.

The plan and service artifacts identify the checked-out SHA; each service also
records the SHA-256 of `uv.lock`, dependency manifest, image identity and complete
build/test log. Artifacts expire after three days. Download them from the Actions
run; failure logs are also visible in each service job.

## Execution and verdict

Each service reuses `make test SERVICE=...` with `TEST_N_LEG=0` and the locked
Docker dev image. Non-Prediction services retain Makefile's serial default;
Prediction uses two xdist workers appropriate for a standard Linux runner.
Image builds can download locked dependencies; tests use the existing
network-disabled container, no host mounts, forwarded credentials, Docker socket
or published ports. There are no real account credentials, trading requests,
Deployment Preflight, host readiness or production Smoke steps.

The workflow uses standard `ubuntu-24.04` runners, `contents: read`, commit-pinned
Actions and checkouts without persisted credentials. Each service times out after
60 minutes; planner/aggregate after five. There is no workflow concurrency group:
a later main push does not cancel or replace an earlier main SHA's run, including
a pending run. Cancellation is not success; inspect the selected SHA's actual
run and latest attempt.

The aggregate always evaluates the planner and all fixed job results. All
four backend services and the portable scenario job must succeed; the retained
trend-curve job must be skipped. Failure, cancellation, an unexpectedly skipped service, missing result,
malformed or narrowed plan, changed retirement policy, or a claimed documentation
exemption fails closed. Documentation-only changes receive no CI exemption.
Planner/aggregation contract tests exercise these cases and verify nonzero CLI
exit on failure. There is no workflow-level path filter that could leave the
required check permanently pending.

## Rollback advisory

`scripts/ci_optional_tests.json` declares exactly one optional node:
`tests/test_lp_d2_service_lifecycle.py::test_supported_rollback_uses_current_api_without_disposable_record_audit`.
Its test and business assertions are unchanged. Required Prediction CI excludes
only this canonical identity, before xdist adds execution group names. New tests,
including similarly named neighbors, stay required. The local direct pytest
command and ordinary Make diagnostics retain their full selection; CI applies
its exclusion through the explicit `--ci-required` plugin option.

The independent `.github/workflows/ci-rollback-advisory.yml` workflow is named
`Rollback advisory`. It runs automatically only on pushes to `main`, normally
after a merge, and provides `workflow_dispatch` for manual use. It does not run
on PRs, ordinary branch pushes or a schedule. A PR therefore has no premerge
advisory execution. The exact `github.sha` is checked out and the whole node runs
once, serially, in its own standard runner with the same locked offline Docker
environment and 60-minute job timeout.

The advisory keeps genuine failed, cancelled and timed-out results, original
exit status, test logs, phase metrics, JUnit, source SHA and run/attempt identity.
Evidence uploads use `always()` and the same three-day retention. There is no
`continue-on-error`, automatic retry, issue creation or notification. A failed
advisory can be repaired later; the fixed `required` job neither waits for it nor
consumes its result. Deployment Preflight also does not wait for optional success.
This explicitly removes automatic blocking for this one rollback regression;
it does not make other slow tests optional. No fixed wall-time saving is claimed.

## Local verification and delivery

Local work runs only directly affected/new test nodeids in an existing Python
environment against the current worktree, for example:

```sh
PYTHONPATH=src python -m pytest tests/path.py::test_name
```

Test-only changes run changed tests; shared test helpers add their directly
affected consumers. Shared production modules run the union of relevant consumer
tests across services, not whole services. Pure documentation/configuration work
needs no local backend run unless it changes a testable contract. Report blocked
focused checks honestly; Docker is not a prerequisite to task-branch publication.

Follow the [PR-first verification runbook](agent-verification.md#first-reviewable-implementation):
applicable development/security checks, Main's task-only stage/commit/push and
normal PR under standing authorization, then independent review alongside delivery.
No additional push/PR permission question is needed; the runbook's explicit
local-only/no-push/do-not-publish/draft overrides apply. Publish in-scope repairs
to the same open PR before refreshed review. After merge, repairs use a new
isolated branch and PR with separate merge authorization. Focused verification,
independent review, complete feedback collection and reproducible blocker repairs
remain Main-owned duties; report them accurately and continue unfinished work.

After explicit authorization for that PR's merge, Main checks repository, base
`main`, open state, expected head and absence of conflicts, then merges through
GitHub with the expected head SHA. Unknown mergeability needs a bounded refresh;
actual conflicts need scoped resolution. Do not wait for feature/PR CI, pending
review/dots or an old review-ready/full-acceptance label. A newer main alone does
not require a rebase, branch synchronization, push or rerun for CI freshness.
During migration, old feature-branch workflows can still trigger extra runs;
do not synchronize/push solely to remove them, or wait for them.
Local main only synchronizes the remote. Merge remains separate from release/tag
creation, Deployment Preflight and deployment; no CI job grants production or
trading authorization.

## Collection and execution semantics

CI collects the complete backend universe under `not pressure and not browser`.
It separately collects the required service partitions, the exact optional node
and the retired files. The three collections must be pairwise disjoint, and
their union must equal the universe. Missing, extra or duplicate nodeids and collection errors fail the
proof. Optional declarations carry `not-executed-advisory`; they are classification
evidence, never an execution or passed record. The exact optional manifest,
selection and proof must agree; missing, duplicate, wildcard, unknown or expanded
entries fail closed. Retired nodeids carry `not-executed-permanent-retirement`, never passed.
Portable scenarios are separately selected with `not LIVE`. Gateway, Legacy and
Account stay serial. Prediction retains two workers and its shared-port groups;
manual diagnostics retain solver-cache groups. Portable scenarios stay serial.

Each test container records canonical selected nodeids, setup/call/teardown
outcomes and durations, worker identity, Python/platform, CPU model/provenance, architecture, CPU count
and wall seconds. Unavailable CPU models are explicit unknown; they do not fail
business tests but cannot establish comparable CPU environments.
The controller writes the records after serial or xdist execution. Named test
containers retain no mounts or network; the runner copies their JSON and JUnit
files, then removes only its own container. The runner retains the original
failure code when collection, copy, environment recording or upload also fails.
Artifacts bind the source SHA, run ID/attempt, retirement and optional manifests/policies, required/optional tier, selection, lock,
image and dependency identities; hashes bind the records. Failure and skip
records remain visible. Missing records cannot describe a successful job.

The planning job retains stdlib unittest smoke tests. Real pytest collection and
metrics fixtures run in the required legacy partition, in the locked dev image.
The host smoke does not pretend to execute module-level pytest functions.

Prediction timing comparison must use the same source, locked dependencies,
runner/CPU and two workers, changing only inclusion of the retired collection.
File counts are classification evidence, not elapsed-time savings. Record both
selections and full wall time; no periodic full N-leg regression is required.
