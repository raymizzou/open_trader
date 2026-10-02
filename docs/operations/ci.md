# Full backend CI and focused local verification

`.github/workflows/ci.yml` runs on every branch push and every PR targeting
`main`, including documentation-only changes and the final merged-main push.
Every run selects all four existing backend services: `gateway`, `legacy`,
`account`, and `prediction`, with `TEST_N_LEG=1`. Full backend coverage excludes
`pressure` and `browser`; it does not include Candidate Acceptance, Host Readiness
or Production Smoke.

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
rebase or a changed base); its push receives the same full backend CI.
Record the PR head, base, tested SHA, event and Actions run together. Any head/base
change needs fresh applicable verification; stale or cancelled runs do not qualify.
After authorized GitHub merge, inspect the main-push run for that exact final SHA.
Only the selected final main SHA is eligible for separately authorized
predeployment Candidate Acceptance; CI never substitutes for it.

Changed paths and diff availability do not narrow CI. Documentation-only, isolated
LP, standalone trend-curve, shared, unknown, deleted/renamed, empty-diff and
missing-history changes all select the same four services with `TEST_N_LEG=1`.
The retained standalone trend-curve job stays skipped because the legacy suite
already includes its tests. The Makefile partitions the backend suite; update its
service prefixes when adding service-specific test families. N-leg test coverage
is enabled regardless of the operational `N_LEG_PAUSED` setting.

The plan and service artifacts identify the checked-out SHA; each service also
records the SHA-256 of `uv.lock`, dependency manifest, image identity and complete
build/test log. Artifacts expire after three days. Download them from the Actions
run; failure logs are also visible in each service job.

## Execution and verdict

Each service reuses `make test SERVICE=...` with `TEST_N_LEG=1` and the locked
Docker dev image. Non-Prediction services retain Makefile's serial default;
Prediction uses two xdist workers appropriate for a standard Linux runner.
Image builds can download locked dependencies; tests use the existing
network-disabled container, no host mounts, forwarded credentials, Docker socket
or published ports. There are no real account credentials, trading requests,
Candidate Acceptance, host readiness or production Smoke steps.

The workflow uses standard `ubuntu-24.04` runners, `contents: read`, commit-pinned
Actions and checkouts without persisted credentials. Each service times out after
60 minutes; planner/aggregate after five. New commits cancel older checks for the
same PR or branch; push and PR concurrency groups remain separate. Cancellation
is not success; inspect the latest exact-SHA run.

The aggregate always evaluates the planner and all five fixed job results. All
four backend services must succeed, and the retained trend-curve job must be
skipped. Failure, cancellation, an unexpectedly skipped service, missing result,
malformed or narrowed plan, disabled N-leg coverage, or a claimed documentation
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
creation, Candidate Acceptance and deployment; no CI job grants production or
trading authorization.
