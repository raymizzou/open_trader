# Issue #17 Frontend Gateway Production Cutover Design

## Goal

Complete Phase 0 by documenting and proving the production Frontend Gateway
stack without changing Dashboard presentation, trading rules, reports,
execution, or worker behavior.

The stable operator and browser URL remains `http://127.0.0.1:8766/`.
Frontend Gateway owns that public loopback listener and forwards `/api/*` to
the internal Legacy Dashboard on `127.0.0.1:8767`.

## Scope

This issue is a release and operations handoff for the behavior already built
in Issues #14, #15, and #16. It will:

- add a dedicated Dashboard stack section to `README.md`;
- finish the existing Frontend Gateway operations reference;
- add a dated operator-facing `CHANGELOG.md` entry before any merge;
- run focused tests, the complete test suite, direct stack checks, and the
  final Dashboard acceptance gate;
- redeploy the exact accepted Git SHA and capture live runtime evidence; and
- publish the evidence to Issue #17.

No new deployment or diagnostic script is needed. The existing launchd
installer, health endpoints, runtime records, acceptance command, `launchctl`,
`lsof`, and `curl` already provide the required controls and evidence.

## Documentation Contract

### README

`README.md` will gain a standalone Dashboard stack section rather than leaving
the deployment commands embedded only in the prediction-market workflow. It
will state:

- `8766` is the only user and review URL;
- `8767` is an internal Legacy Dashboard endpoint and must not be exposed;
- `scripts/install_dashboard_launchd.sh` installs or refreshes the two-process
  stack;
- `scripts/install_dashboard_launchd.sh --mode single` restores the preserved
  single-process layout; and
- the exact commands for checking launchd jobs, listeners, health, and fresh
  Gateway and Legacy logs.

The README will link to the detailed operations reference instead of copying
its temporary-port smoke-test procedure.

### Operations Reference

`docs/operations/frontend-gateway-deployment-reference.md` will be the detailed
runbook for Issues #14 through #17. It will retain the current install,
rollback, uninstall, and temporary-port procedures, and add the production
handoff sequence:

1. freeze a committed candidate SHA;
2. run focused and complete automated tests;
3. deploy the candidate stack and directly verify both listeners, both health
   identities, and one API request through Gateway;
4. run `make acceptance` as the final Dashboard gate;
5. on `PASS`, redeploy the unchanged accepted SHA without changing source or
   runtime data; and
6. verify both new PIDs, cwd, SHA, source state, start time, fresh runtime logs,
   and HTTP 200 from `8766`.

### Changelog

The dated `CHANGELOG.md` entry will describe completion of the production
Gateway cutover and explicitly state that it changes no page, strategy, report,
execution, or worker behavior. The entry is committed before the candidate SHA
is accepted or considered for merge.

## Verification and Deployment

All source and documentation changes must be committed before verification so
one immutable candidate SHA identifies the tested and deployed version.

Verification runs in this order:

1. focused Frontend Gateway, launchd stack, and dual-runtime acceptance tests;
2. the complete pytest suite;
3. candidate stack deployment through
   `scripts/install_dashboard_launchd.sh`;
4. direct checks for independent `8766` and `8767` listeners, correct module
   identities from both health endpoints, Gateway `upstream_status=ok`, and one
   successful `/api/*` request through `8766`;
5. `make acceptance` as the final gate; and
6. exact-SHA redeployment and live runtime proof.

`make acceptance` keeps its existing result semantics:

- `PASS`: continue to exact-SHA redeployment and handoff;
- `FAIL`: diagnose and fix the smallest root cause, add a focused regression
  check when behavior changes, then rerun the complete final gate; or
- `BLOCKED`: report the external or browser blocker and do not present the task
  as complete.

After `PASS`, no source or runtime data change is allowed before redeployment.
The redeployed Gateway and Legacy processes must each report the accepted SHA,
a clean source state, the expected working directory, a distinct live PID, and
a runtime log newer than its process start. `http://127.0.0.1:8766/` must return
HTTP 200 and is the only review URL.

## GitHub and Integration Boundary

The final Issue #17 comment will record the accepted SHA, exact test counts,
direct stack checks, both process identities, log freshness, and the review
URL. Issues #14 through #16 remain open until their commits are published to
GitHub.

This work stops on its isolated feature branch after handoff. It does not
merge into `main`, push any branch, or close Issues #14 through #17 without
explicit user authorization.

## Non-Goals

- No Dashboard layout, copy, interaction, or API contract change.
- No strategy, report, execution, account-sync, controller, notification, or
  worker cadence change.
- No new process manager, deployment wrapper, diagnostics collector, or
  rollback implementation.
- No exposure of Legacy Dashboard outside loopback.
- No Account, Trend, Research, or Prediction module extraction; those remain
  later phases under Issue #13.
