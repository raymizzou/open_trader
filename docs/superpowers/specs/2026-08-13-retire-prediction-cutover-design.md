# Retire Prediction Cutover Machinery

## Status

- Date: 2026-08-13
- Issue: #45 follow-up
- Decision: approved design; implementation has not started
- Runtime impact: none; the production Prediction route remains `service`

## Goal

Remove the completed, one-time Legacy-to-Service cutover machinery now that
Prediction Service is the steady-state production owner. The repository should
stop presenting Legacy ownership as a supported rollback path while preserving
the existing Service, Gateway, browser, and live no-submit verification.

## Scope

Delete these files without replacement:

- `scripts/cutover_prediction_service.sh`;
- `scripts/prediction_cutover_account_proof.py`;
- `tests/test_prediction_cutover_launchd.py`.

Update `docs/operations/frontend-gateway-deployment-reference.md` to remove the
#45 one-time integrated cutover command, its helper-specific operational
guidance, and the explicit `--target legacy` rollback instructions. Retain the
document's steady-state Gateway/Legacy deployment and local verification
material where it does not instruct operators to change Prediction ownership.

Add a dated operator-facing `CHANGELOG.md` entry describing the retirement,
the unsupported Legacy rollback, and the verification performed. This entry is
part of the implementation commit and must precede any merge to `main`.

The generic Dashboard stack recovery provided by
`scripts/install_dashboard_launchd.sh --mode single` is outside this removal.
It remains a Dashboard deployment capability, but it is not and must not be
documented as a Prediction owner rollback.

## Steady-state contract

This retirement performs no transition. It does not read, write, or regenerate
the production route record. The configured route remains `service`, Gateway
continues forwarding Prediction traffic to Prediction Service, and Legacy is
not a supported Prediction rollback target after the deletion.

Existing steady-state surfaces remain unchanged:

- Prediction Service install, release, health, ownership, and listener checks;
- Gateway `service` routing and failure behavior;
- Prediction browser workflows;
- authenticated live-readiness checks that prove no-submit behavior and zero
  mutation calls or live notifications.

No replacement cutover command, account-proof helper, compatibility wrapper,
tombstone test, or migrated subset of the deleted cutover test suite is added.
Those tests exist only to prove machinery that is being removed; the retained
steady-state tests own the supported production topology.

Legacy-mode handling that remains inside Gateway or route parsing is not
removed by this ticket. Its presence is compatibility code, not an operator
rollback contract. Removing that compatibility path requires a separate design.

## Deletion implementation non-goals

- No production cutover, rollback, route-state edit, or ownership handoff.
- No launchd install, uninstall, bootstrap, bootout, kickstart, or restart.
- No production runtime, configuration, data, SQLite, log, port, process, or
  launchd mutation.
- No Prediction Service, Gateway, Legacy Dashboard, Account, browser, API,
  notification, execution, or order behavior change.
- No replacement code or tests and no new abstraction for retired behavior.
- No rewrite of prior historical design records or changelog entries.
- No deletion of generic Dashboard deployment/recovery machinery.

## Implementation shape

The implementation is three deletions plus two documentation edits:

1. delete the two cutover scripts and their dedicated test file;
2. remove the #45 cutover and Legacy rollback instructions from the active
   Gateway deployment reference;
3. add the required dated operator-facing `CHANGELOG.md` entry;
4. make no other source, test, configuration, data, or operational changes.

If any remaining active code or operational document depends on a deleted
file, stop and report that dependency instead of inventing a replacement.

## Verification

Focused deletion verification is read-only and must establish both absence of
the retired path and preservation of the supported steady state:

1. Confirm the diff contains only the three deletions, the intended
   `docs/operations/frontend-gateway-deployment-reference.md` edit, and the
   dated `CHANGELOG.md` entry.
2. Search active source, tests, and operational documentation for the deleted
   filenames and #45 Legacy rollback command. The retirement design and
   historical records may continue to name them as history.
3. Run the existing Prediction Service release/launchd tests and Frontend
   Gateway tests unchanged.
4. Run the existing focused Dashboard acceptance-registry check for
   authenticated live no-submit evidence unchanged.
5. Confirm the existing Prediction browser and live no-submit acceptance
   definitions are untouched.
6. Run shell syntax checks on the relevant retained Prediction Service and
   Dashboard/Gateway launchd installers, plus `git diff --check`.

### Final closure gate

After the deletion is committed and the already-authorized final #45 closure
workflow begins, run the project-required `make acceptance` gate. Its test
count will exclude the 115 retired cutover cases. Only `PASS` authorizes the
separately approved redeployment of the exact accepted Git SHA; then verify the
new PID, working directory, Git SHA, fresh logs, and HTTP 200 response.

The deletion implementation itself performs no live cutover, Legacy rollback,
route write, runtime/configuration/data mutation, or launchd operation. The
final acceptance, exact-SHA redeployment, and runtime verification belong to
the separately authorized #45 closure workflow, not to the deletion change.

## Completion criteria

The deletion change is complete when the disposable #45 files and active
rollback instructions are absent, the dated changelog entry is committed, the
route is still operationally defined as `service`, Legacy rollback is explicitly
unsupported, the focused checks are green, browser/live no-submit verification
definitions are unchanged, and the deletion work mutated no runtime surface.

Final #45 closure is complete only after the separate closure workflow obtains
`make acceptance` `PASS`, redeploys the exact accepted SHA, and verifies its
PID, working directory, Git SHA, fresh logs, and HTTP 200 response.
