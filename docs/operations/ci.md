# Active backend CI and focused local verification

`.github/workflows/ci.yml` runs on every branch push and every PR targeting
`main`, including documentation-only changes and the final merged-main push.
Every run selects all four existing backend services: `gateway`, `legacy`,
`account`, and `prediction`, with `TEST_N_LEG=0`. Active backend coverage excludes
`pressure` and `browser`; it does not include Deployment Preflight, Host Readiness
or Production Smoke. Non-LIVE portable prediction scenarios run in a separate
serial job and are required by the aggregate.

The stable aggregate appears as **CI / required** in the UI (workflow `CI`, job
`required`), but its exact API check-run/context name is **`required`**, emitted
by **GitHub Actions**, app ID **15368**. A future required-check rule must bind
that exact name and source, not the display string or an arbitrary same-named
status. No repository settings are changed by this workflow. The
[repository protection design](repository-protection.md) is proposed, not active.

## Identity and coverage

Push jobs check out `github.sha`, the exact pushed branch head. PR jobs check out
`github.sha`, GitHub's synthetic candidate merge commit, to test integration with
the PR base. A feature branch with an open PR intentionally has separate push-head
and PR-merge runs: these verify distinct SHA identities and are not deduplicated.
The final GitHub `main` commit after merge may differ from both (including squash,
rebase or a changed base); its push receives the same active backend CI.
Record the PR head, base, tested SHA, event and Actions run together. Any head/base
change needs fresh applicable verification; stale or cancelled runs do not qualify.
After authorized GitHub merge, inspect the main-push run for that exact final SHA.
Only the selected final main SHA is eligible for separately authorized
predeployment identity and evidence checks; see [Deployment Preflight](deployment-preflight.md).
Deployment no longer repeats the backend suite.

Changed paths and diff availability do not narrow CI. Documentation-only, isolated
LP, standalone trend-curve, shared, unknown, deleted/renamed, empty-diff and
missing-history changes all select the same four services with `TEST_N_LEG=0`.
The retained standalone trend-curve job stays skipped because the legacy suite
already includes its tests. Make and `scripts/ci_evidence.py` share one service selection entry point.
Update that selector's service prefixes for new service-specific test families.
The [explicit retirement manifest](ci-nleg-retirement.md) omits exactly 29 reviewed
N-leg files from default execution. All other tests remain active, including the
three mixed/shared files, LP and pause guards. Production pause state is unchanged.
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
60 minutes; planner/aggregate after five. New commits cancel older checks for the
same PR or branch; push and PR concurrency groups remain separate. Cancellation
is not success; inspect the latest exact-SHA run.

The aggregate always evaluates the planner and all fixed job results. All
four backend services and the portable scenario job must succeed; the retained
trend-curve job must be skipped. Failure, cancellation, an unexpectedly skipped service, missing result,
malformed or narrowed plan, changed retirement policy, or a claimed documentation
exemption fails closed. Documentation-only changes receive no CI exemption.
Planner/aggregation contract tests exercise these cases and verify nonzero CLI
exit on failure. There is no workflow-level path filter that could leave the
required check permanently pending.

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
focused checks honestly; Docker is not a prerequisite to push a reviewed branch.

Follow the [PR-first verification runbook](agent-verification.md): focused local
checks, independent staged review, authorized branch push, Draft PR, latest
exact-SHA `required` success, and explicit user approval before GitHub merge.
Local main only synchronizes the remote. Merge remains separate from release/tag
creation, Deployment Preflight and deployment; no CI job grants production or
trading authorization.

## Collection and execution semantics

CI collects the complete backend universe under `not pressure and not browser`.
It separately collects all active service partitions and the exact retired files.
The executed and retired collections must be disjoint, and their union must equal
the universe. Missing, extra or duplicate nodeids and collection errors fail the
proof. Retired nodeids carry `not-executed-permanent-retirement`, never passed.
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
Artifacts bind the source SHA, run ID/attempt, manifest/policy, selection, lock,
image and dependency identities; hashes bind the records. Failure and skip
records remain visible. Missing records cannot describe a successful job.

The planning job retains stdlib unittest smoke tests. Real pytest collection and
metrics fixtures run in the required legacy partition, in the locked dev image.
The host smoke does not pretend to execute module-level pytest functions.

Prediction timing comparison must use the same source, locked dependencies,
runner/CPU and two workers, changing only inclusion of the retired collection.
File counts are classification evidence, not elapsed-time savings. Record both
selections and full wall time; no periodic full N-leg regression is required.
