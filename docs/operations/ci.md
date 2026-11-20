# Trend V3 CI and focused local verification

`.github/workflows/ci.yml` runs Trend V3 on PRs targeting `main`, on each
push to GitHub `main` and through the separately authorized main-only
`workflow_dispatch`. There are no path filters or dispatch inputs. PRs on
`refs/pull/N/merge` run normally. Feature work receives CI through its PR.
The workflow has no concurrency group that cancels an earlier run.

Formal CI must cover PRs. Disabling that coverage requires Ray's explicit
approval. Verify that changes to workflow triggers still run `required` on a
PR. Before an authorized merge, verify successful **CI / required** on the
current PR candidate. The exact check-run name is **`required`**, emitted by
**GitHub Actions** (app ID **15368**). See the
[repository settings read-back](repository-protection.md) for the separately
managed branch and tag protections.

## Identity and coverage

`trend_v3` checks out and verifies `github.sha`, the PR's synthetic merge
candidate or the selected main commit. It runs on `ubuntu-24.04` with a
three-minute job limit. It installs the independent package and its development
dependencies in a virtual environment, runs Ruff on source and tests, then
runs pytest for the service. The checkout action is commit-pinned, the token has
`contents: read`, and checkout does not persist credentials.

The fixed `required` job depends only on `trend_v3`. It evaluates a failed,
cancelled, skipped or missing dependency result as failure whenever the gate
runs. A run cancelled by GitHub also provides no successful evidence. Off-main
manual dispatch skips both jobs. Main records PR head and base, the checked-out
candidate, and the actual run and attempt for the final main SHA after merge.
CI evidence applies only to the candidate that ran.

## Restore the full-backend jobs

Ray's 2026-11-20 decision keeps the legacy backend, portable and partition
checks off by default. The routing, runner, tests and artifact verifier are
available for retained full-backend evidence and local diagnostics.

When Ray says to open those checks, locate the isolated removal commit in
main's history by its exact subject, `ci: disable legacy jobs by default`.
On a task branch from current `origin/main`, use
`git revert <removal-commit>` and deliver it through a PR. Keep the Trend V3
service and its CI job. Review the revert diff and verify that the restored
required gate includes Trend V3 and the original full-backend evidence
checks. The restored workflow runs the gateway, legacy, account and
prediction suites with `TEST_N_LEG=0` and the non-LIVE portable scenarios.
The PR still requires Ray's explicit merge approval.

## Existing backend evidence

The [existing stack's Deployment Preflight](deployment-preflight.md) still
requires a successful trusted main push or main-only manual run with all seven
full-backend artifacts for the selected exact SHA. A Trend V3-only run cannot
provide that proof. An older main ancestor remains eligible with its own
unexpired evidence. A newer unsuccessful run for the same SHA blocks reuse
of an older green run. Dispatch requires separate authorization and runs the
workflow defined at the selected main ref. The
[Trend V3 deployment workflow](../../services/trend_v3/AGENTS.md#deployment-on-mini)
uses the service-specific steps and applies only to Trend V3.

Under the retained full-backend contract, changed paths and diff availability
do not narrow coverage. Documentation-only, isolated LP, standalone trend-curve,
shared, unknown, deleted/renamed, empty-diff and
missing-history changes all select the same four services with `TEST_N_LEG=0`.
The retained standalone trend-curve job stays skipped because the legacy suite
already includes its tests. Make and `scripts/ci_evidence.py` share one service selection entry point.
Update that selector's service prefixes for new service-specific test families.
The [explicit retirement manifest](ci-nleg-retirement.md) omits exactly 29 reviewed
N-leg files from default execution. The 273 exact release-test identities described below
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

### Full-backend execution and verdict

Each service reuses `make test SERVICE=...` with `TEST_N_LEG=0` and the locked
Docker dev image. Non-Prediction services retain Makefile's serial default;
Prediction runs on three machines, each with two xdist workers. Gateway,
Legacy, Account and Portable retain one machine each. Prediction parameters
stay together, including the existing cross-file shared-port group. The separately
dispatched advisory runs on four machines with one process each. The lp410
cases use exactly two of them.
Image builds can download locked dependencies; tests use the existing
network-disabled container, no host mounts, forwarded credentials, Docker socket
or published ports. There are no real account credentials, trading requests,
Deployment Preflight, host readiness or production Smoke steps.

The previous workflow used standard `ubuntu-24.04` runners, `contents: read`,
commit-pinned Actions and checkouts without persisted credentials. Each service times out after
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

`scripts/ci_optional_tests.json` declares 273 reviewed full nodeids under
`release-advisory-v1`: the existing rollback regression plus 272 release,
installer and manager cases across ten files. The selector pins the sorted
literal set to SHA-256
`0343677be27fc53783631374b15fb20de6985d5f821f45aa5efa002918af3e78`.
In the retained full-backend contract, required Prediction excludes
only these canonical identities, before xdist adds execution group names.
New names and new parameter identities stay required; no file, function prefix
or wildcard grants an exemption. Financial/order, UNKNOWN, no-replay,
current-API owner handoff/restart, active-service drain, credential SSM runtime
and stopped data-recovery transaction tests remain required.
The local direct pytest command and ordinary Make diagnostics retain their full
selection; CI applies its exclusion through the explicit `--ci-required` option.

The independent `.github/workflows/ci-rollback-advisory.yml` keeps its workflow
path and history. Its visible name is `Release advisory`; its job, runner scope
and Make target use `release-advisory`. It runs by separately authorized
`workflow_dispatch` only. It checks out the exact `github.sha` and partitions
the declared cases over four machines. Each machine runs one serial session in
the locked offline Docker environment, with a 60-minute timeout. Parameter IDs are quoted
as whole command arguments.

The advisory keeps genuine failed, partial, cancelled and timed-out results,
original exit status, test logs, phase metrics, JUnit, source SHA and run/attempt
identity. Evidence uploads use `always()` and the same three-day retention.
There is no `continue-on-error`, automatic retry, issue creation or notification.
A failed advisory can be repaired later; the fixed `required` job neither waits
for it nor consumes its result. Deployment Preflight also does not wait for
optional success. The other tests remain part of the full-backend contract.

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
actual conflicts need scoped resolution. Verify successful PR **CI / required**
before merge; pending review/dots or an old review-ready/full-acceptance label does
not replace this check. A newer main alone does
not require a rebase, branch synchronization, push or rerun for CI freshness.
Do not synchronize/push solely to remove duplicate runs from old feature-branch workflows.
Local main only synchronizes the remote. Merge remains separate from release/tag
creation, Deployment Preflight and deployment; no CI job grants production or
trading authorization.

## Retained full-backend collection and execution

The full-backend runner collects the complete backend universe under
`not pressure and not browser`. It separately collects the required service partitions, the exact optional nodes
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

The previous planning job ran stdlib unittest smoke tests. Real pytest collection and
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
coverage. No retry, timeout or per-machine worker increase is added.

Retained LP bindings keep their schema and fields. Their expected artifact scope
list comes from the already-verified selected clean release's preflight contract,
not from the binding's length or the newer manager's own version. An old selected
release retains its five-scope contract; a new selected release requires all
seven exact scopes, ordered and with distinct positive IDs and valid digests.
Unknown selected contracts fail closed. Runtime and trading decisions are unchanged.

OQ3 moves 49 exact startup/install identities to the existing advisory allowlist,
including all 39 lp410 cases, instance health/parallel identity and five pure Runtime
resource startup checks. Real process death, installation and competing
process tests still execute as actual processes. Current-account financial
recovery, UNKNOWN, no-replay, in-flight native/HTTP drain and SSM runtime checks
remain required. Future names and parameter identities remain required. The
tradeoff is that a genuine startup/install advisory failure no longer blocks
preflight; its original result and evidence remain visible.

See [current-service test coverage](ci-current-service-tests.md) for the migrated
scenario families, retained real-process boundaries, source-template/cache rules
and focused local runner setup.


## Shard allocation limits

The six fatal-stop/owner-lock cases named in
[`test_terminal_failure_and_owner_lock_cases_remain_required`](../../tests/test_ci_optional.py)
remain required, including actual process exclusion/death and terminal failure.
They are outside the 273 literal advisory identities. Their restoration changes
routing, not their assertions or process boundaries.

Function-name hashing gives deterministic identity and parameter grouping; it
is not a measured timing balance. Hosted per-machine durations and three
consecutive main-run timing results remain UNKNOWN. The fixed three/four shard
counts are repeated in workflow matrices, partition/evidence selection, preflight
scopes and the production validator's accepted binding tuples. This is a known
compatibility and maintenance cost. Future balancing needs measured per-machine
data and an approved scoped PR; it is not authorized automatically.

Required CLI output separates malformed job-result JSON from bounded artifact
validation reasons. A useful error message never changes the fail-closed verdict.
