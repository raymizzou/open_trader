# Active backend CI and focused local verification

`.github/workflows/ci.yml` runs automatically only on pushes to GitHub `main`,
including documentation-only changes and each final merged-main SHA. Feature
pushes, PR events and tag pushes do not trigger it. Preserve the existing
`workflow_dispatch` with no inputs: plan and required jobs guard
`refs/heads/main`, and all checkouts use the exact `github.sha`.
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

Jobs check out `github.sha`, the exact main SHA for the push or manual run. Record
the PR head/base, final merge SHA, event, Actions run ID, attempt and actual result. The final main
commit can differ from a PR head or historical synthetic merge SHA; evidence
does not transfer between them. After authorized GitHub merge, inspect the
main-push run for that exact final SHA. Failed, cancelled or missing results
remain visible; merge success is not CI success.

A separately authorized manual run (`gh workflow run ci.yml --ref main`) can
provide the same evidence for the then-current main SHA when automatic CI is
unavailable. It accepts no arbitrary SHA input. Verify the resulting run and
exact SHA; dispatch alone proves nothing. Deployment Preflight selects the
newest exact-SHA run across push and main-only `workflow_dispatch`, verifies its
latest attempt and binds artifacts to the real event. A newer failed, cancelled
or pending run blocks reuse of older green evidence.

Main owns focused diagnosis and a scoped repair/revert proposal through PRs.
There is no automatic main write, revert, retry-until-green or deployment.
A failed main SHA cannot be deployed and does not change running production.
Successful trusted exact-SHA main CI (push or the existing main-only manual
workflow) and complete artifacts are required for separately authorized
[Deployment Preflight](deployment-preflight.md),
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
N-leg files from default execution. The 279 exact release-test identities described below
are advisory; all other active tests remain required, including the three
mixed/shared files, LP and pause guards. Production pause state is unchanged.
Unknown/new files do not inherit retirement from their names.

The plan and service artifacts identify the checked-out SHA; each service also
records the SHA-256 of `uv.lock`, dependency manifest, image identity and complete
build/test log. Artifacts expire after three days. Download them from the Actions
run; failure logs are also visible in each service job.

The cross-job plan carries only four optional-policy fields: `policy`,
`manifest_sha256`, `optional_count` and `optional_sha256`. The planner validates
all literal identities locally before computing this summary. The aggregate
recomputes and compares the same source-bound summary; missing, changed or extra
summary fields fail closed. Full parameterized nodeids stay in the source
manifest, execution artifacts and partition proof, where the deployment verifier
checks them. They are not encoded or exported in the cross-job plan, so public
credential-shaped negative-test parameters do not enter that job output.
Local CLI tests verify this content contract; they do not reproduce GitHub's
hosted output scanner. Normal subsequent push evidence verifies hosted transport.

## Execution and verdict

Each service reuses `make test SERVICE=...` with `TEST_N_LEG=0` and the locked
Docker dev image. Non-Prediction services retain Makefile's serial default;
Prediction runs on three machines, each with two xdist workers. Gateway,
Legacy, Account and Portable retain one machine each. Prediction parameters
stay together, including the existing cross-file shared-port group. Advisory
runs on four machines with one process each; lp410 uses exactly two of them.
Image builds can download locked dependencies; tests use the existing
network-disabled container, no host mounts, forwarded credentials, Docker socket
or published ports. There are no real account credentials, trading requests,
Deployment Preflight, host readiness or production Smoke steps.

The workflow uses standard `ubuntu-24.04` runners, `contents: read`, commit-pinned
Actions and checkouts without persisted credentials. Each service times out after
60 minutes; planner/aggregate after five. There is no workflow concurrency group:
a later push or manual dispatch does not cancel or replace an earlier run,
including a pending run. Cancellation is not success; inspect the selected SHA's
actual run and latest attempt.

The aggregate always evaluates the planner and all fixed job results. All
four backend services and the portable scenario job must succeed; the retained
trend-curve job must be skipped. Failure, cancellation, an unexpectedly skipped service, missing result,
malformed or narrowed plan, changed retirement policy, or a claimed documentation
exemption fails closed. Documentation-only changes receive no CI exemption.
Planner/aggregation contract tests exercise these cases and verify nonzero CLI
exit on failure. There is no workflow-level path filter that could leave the
required check permanently pending.

## Release advisory

`scripts/ci_optional_tests.json` declares 279 reviewed full nodeids under
`release-advisory-v1`: the existing rollback regression plus 278 release,
installer and manager cases across ten files. The selector pins the sorted
literal set to SHA-256
`607edf6f00872abe4e5c8de99d5d73828fd63a7915fbb313dde0a1c448071621`.
Tests and business assertions stay unchanged. Required Prediction CI excludes
only these canonical identities, before xdist adds execution group names.
New names and new parameter identities stay required; no file, function prefix
or wildcard grants an exemption. Financial/order, UNKNOWN, no-replay,
current-API owner handoff/restart, active-service drain, credential SSM runtime
and stopped data-recovery transaction tests remain required.
The local direct pytest command and ordinary Make diagnostics retain their full
selection; CI applies its exclusion through the explicit `--ci-required` option.

The independent `.github/workflows/ci-rollback-advisory.yml` keeps its workflow
path and history. Its visible name is `Release advisory`; its job, runner scope
and Make target use `release-advisory`. It runs automatically only on pushes to
`main`, normally after a merge, and provides `workflow_dispatch` for manual use.
It does not run on PRs, ordinary branch pushes or a schedule. A PR therefore
has no premerge advisory execution. The exact `github.sha` is checked out and
the declared cases are partitioned over four machines, each with one serial session, with the same locked,
offline Docker environment and 60-minute job timeout. Parameter IDs are quoted
as whole command arguments.

The advisory keeps genuine failed, partial, cancelled and timed-out results,
original exit status, test logs, phase metrics, JUnit, source SHA and run/attempt
identity. Evidence uploads use `always()` and the same three-day retention.
There is no `continue-on-error`, automatic retry, issue creation or notification.
A failed advisory can be repaired later; the fixed `required` job neither waits
for it nor consumes its result. Deployment Preflight also does not wait for
optional success. Other slow tests remain required. No fixed wall-time saving
is claimed.

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
It separately collects the required service partitions, the exact optional nodes
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

## Three-machine evidence and source-bound compatibility

The required artifact scopes, in order, are `gateway`, `legacy`, `account`,
`prediction-1`, `prediction-2`, `prediction-3`, `portable`. Each artifact keeps
schema 2, its literal full selected/executed nodeids, phase metrics, JUnit,
source/lock/environment and exact run/attempt identity. The portable proof keeps
the complete four-service partition. Both required aggregation and preflight
validate every machine's payload and compare its nodeids to the deterministic
partition; a matrix job result or a matching test count alone is insufficient.
Missing, duplicate, overlapping, extra, failed or misbound required evidence is
rejected. Advisory results are independent and cannot substitute for required
coverage. No trigger, retry, timeout or per-machine worker increase is added.

Retained LP bindings keep their schema and fields. Their expected artifact scope
list comes from the already-verified selected clean release's preflight contract,
not from the binding's length or the newer manager's own version. An old selected
release retains its five-scope contract; a new selected release requires all
seven exact scopes, ordered and with distinct positive IDs and valid digests.
Unknown selected contracts fail closed. Runtime and trading decisions are unchanged.

OQ3 moves 55 exact startup/install identities to the existing advisory allowlist,
including all 39 lp410 cases, instance health/parallel identity and pure Runtime
resource/owner startup checks. Real process death, installation and competing
process tests still execute as actual processes. Current-account financial
recovery, UNKNOWN, no-replay, in-flight native/HTTP drain and SSM runtime checks
remain required. Future names and parameter identities remain required. The
tradeoff is that a genuine startup/install advisory failure no longer blocks
preflight; its original result and evidence remain visible.

See [current-service test coverage](ci-current-service-tests.md) for the migrated
scenario families, retained real-process boundaries, source-template/cache rules
and focused local runner setup.
