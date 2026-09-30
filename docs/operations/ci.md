# PR and main development CI

`.github/workflows/ci.yml` runs on every PR targeting `main` and every push to
`main`, including documentation-only changes. The stable aggregate is **CI /
required** (workflow `CI`, job `required`). No branch protection or repository
settings are changed by this workflow.

## Identity and selection

PR jobs check out `github.sha`, GitHub's candidate merge commit, so they test the
proposed integration with the PR base. This is deliberately different from the
PR head SHA. On `main` pushes the same value is the exact pushed main commit.
The plan and service artifacts identify the checked-out SHA; each service also
records the SHA-256 of `uv.lock`, dependency manifest, image identity and complete
build/test log. Artifacts expire after three days. Download them from the Actions
run; failure logs are also visible in each service job.

The planner diffs the PR base SHA against the candidate merge SHA, or the push's
`before` SHA against the pushed SHA. Full checkout history avoids shallow-diff
omissions. Renames are represented as deletion plus addition, so both old and
new paths count. NUL-delimited Git output preserves unusual filenames. Missing
history, an initial push or an empty diff conservatively selects all four backend
services, never the documentation exemption.

Routing is a deterministic union, in gateway/legacy/account/prediction order:

- `frontend_gateway*` modules and matching tests select gateway
- Account, Futu/Tiger account, holding snapshot, statement import and FX families
  select account, aligned with Makefile's service-test prefixes. Shared source
  modules (FX, market scope, account HTTP/snapshot/sync state, broker clients and Dashboard publication/quote modules)
  override test-family ownership and conservatively select all four services
- Prediction, Predict, Polymarket, relation and LP families select prediction
- Dashboard modules/assets and remaining top-level backend test files select
  legacy, matching Makefile's remaining-test partition
- The existing standalone trend-curve research/backtest modules and three
  dedicated research/backtest/CLI test files select only `make test-trend-curve`;
  when legacy is also selected it already includes these dedicated test files
- Shared modules, dependencies, CI/build scripts and all unrecognized paths
  select all four services; unknown files are not silently ignored
- Only the named root Markdown documents and documentation files under `docs/`
  with the explicit prose/image extension allowlist qualify for exemption;
  executable or unknown documentation-tree files still broaden coverage

Ordinary service selections retain `TEST_N_LEG=0`. N-leg, solver, resolver,
executable cost, market solution, selection, partial-fill and snapshot-scheduler
changes set `TEST_N_LEG=1`. Prediction source families (except isolated Polymarket LP modules), shared
Prediction runtime/service/arbitrage test families, Prediction fixtures and broad
shared/unknown selections also enable those tests.
This restores Makefile's omitted tests without changing the operational pause.
The route tests include deletion, rename, unknown paths, docs and N-leg cases.
When adding service/test families, update both Makefile selection and the planner
as needed, along with routing regression tests. Test-name routing preserves
Makefile's exact filenames versus wildcard families; added unmatched test files
remain in legacy rather than being selected by a gate that would omit them. A
regression checks route/Makefile selection parity across all backend test files.

## Execution and verdict

Each selected service reuses `make test SERVICE=...` and the locked Docker dev
image, with two xdist workers appropriate for a standard Linux runner. Image
builds can download locked dependencies; test execution uses the existing
network-disabled container, no host mounts, forwarded credentials, Docker socket
or published ports. There are no real account credentials, trading requests,
Candidate Acceptance, host readiness or production Smoke steps.

The workflow uses standard `ubuntu-24.04` runners, `contents: read`, commit-pinned
Actions and checkouts without persisted credentials. Each service times out after
60 minutes; planner/aggregate after five. New commits cancel older checks for the
same PR or branch. Cancellation is not success; inspect the latest candidate run.

The aggregate always evaluates the planner and all five fixed job results. Every
selected scope must succeed, and every unselected scope must be skipped. Failure,
cancellation, an unexpectedly skipped selected job, a missing result, malformed
plan or empty non-exempt plan fails closed. A documentation-only run still runs
the routing/aggregation contract tests, then explicitly reports the exemption in
its successful required check. Unit tests inject failure/cancel/skip/missing
results and verify a nonzero CLI exit for failure. There is no workflow-level
path filter that would leave a required check permanently pending.

This is affected-service development CI, not a new every-PR full-backend or
browser gate. Broad coverage occurs only for shared/unknown paths; Candidate
Acceptance and deployment remain separate, explicitly authorized operations.
