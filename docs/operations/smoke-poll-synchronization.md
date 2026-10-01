# Smoke polling synchronization

## Protected contract

Paused N-leg must emit no state, history, relation or N-leg reads after tab
switches and a fresh polling cycle. In split mode, cloud `/venues` and Air
`/execution/identity` run independently; Air applies the execution identity,
N-leg status and render. Waiting only for cloud venues can assert too early.

The test-only `prediction-poll-barrier.cjs` drains both relevant in-flight
flags, arms both request observers before the next poll, checks successful
response/body completion, and waits for both Dashboard callbacks to finish
applying their state/render. Standalone mode continues to wait only for venues.
The caller immediately checks that the completed cycle still applied paused
status, then checks the paused banner. The original accumulated and pending
N-leg negative assertions stay unchanged.
No sleeps, network-idle waits, production changes, browser gate changes or
additional dependencies are introduced.

## Offline evidence (2026-10-01)

Base: merged GitHub main `4439f91d408f6a4db8727a79e445159d10915758`.
The Node fixture runs the real Dashboard venues/identity functions and render
inside a VM with a small DOM stub. Transport and JSON-body completion are
individually released, so request completion cannot stand in for application.
It covers delayed initial Air identity, both split response orders, standalone
mode, headers-only completion, and completed transport with application held.

- The original venues-only algorithm fails with `poll observers armed before
  initial Air identity applied`
- Injected forbidden N-leg reads fail the original empty-read contract in all
  three supported schedules, even when the forbidden read has already finished
- Removing identity synchronization or final application synchronization is
  detected by permanent negative regression tests
- Three serial and three two-worker runs of `test_dashboard_smoke_poll.py`,
  `test_dashboard_e2e_fixture.py`, and `test_frontend_gateway_prediction_ui.py`
  each pass all 22 tests after review (the earlier three serial and three
  two-worker runs passed the then-current 19 tests). The existing HTTP fixture tests retain real isolated
  loopback transport and independent sessions
- Node syntax checks pass. Playwright `--list` collects the one Production
  Smoke test with its actual helper import, without launching a browser or
  contacting the configured URL

Independent staged review identified a caught-body-error gap: HTTP success and
cleared flags can follow rejected JSON parsing. The paused caller now makes an
immediate non-retrying status assertion before checking the banner, so a later
poll cannot recover and hide that failure. Three malformed-body schedules
(split in both completion orders and standalone) prove the error is rejected.

Earlier diagnostics are not hidden: the initial test-first run failed nine
cases because the new helper/import did not yet exist; the first standalone
fixture probe failed because the DOM root had not been bound in the harness.
Binding the actual render root fixed that test fixture; no assertion was
removed. The original-algorithm and intentional mutation failures are expected
negative evidence, not flaky reruns.

## Reproduction and limits

Run the three named Python files with the repository's scoped development
workflow (explicit `TEST='tests/test_dashboard_smoke_poll.py
 tests/test_dashboard_e2e_fixture.py tests/test_frontend_gateway_prediction_ui.py'`).
Use serial and `TEST_WORKERS=2`; do not run Candidate Acceptance for this PR.

The current cloud host has no Docker; host diagnostics do not establish a
Docker gate pass. Exact-SHA GitHub Actions remains the service-gate evidence.
The VM fixture tests synchronization and rendering logic, not browser event
transport or DOM behavior. Browser behavior remains **UNKNOWN**: no browser,
Production Smoke, live service, trading endpoint, Candidate Acceptance or
Host Readiness was run. Actual browser validation stays under the existing
supported Production Smoke route or separately approved fixture-only execution.
