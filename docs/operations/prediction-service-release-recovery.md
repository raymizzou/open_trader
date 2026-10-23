# Prediction Service release recovery

This guide covers the managed production launchd release record and the two
release scripts. It does not authorize a deployment, order submission, runtime
data repair, or a restart of an unrelated process.

## Inspect before changing anything

Run the production installer with `--preflight` from the intended clean,
tracked checkout:

```sh
scripts/install_prediction_service_launchd.sh \
  --mode production --preflight \
  --python /path/to/managed/python \
  --repo-root /path/to/open_trader \
  --runtime-root /path/to/runtime \
  --launch-agents-dir "$HOME/Library/LaunchAgents"
```

Use the Python interpreter configured for the managed service. The manager
checkout containing this script and the candidate checkout named by
`--repo-root` may be different; do not assume they share a virtualenv.

The command is read-only. Its stdout is JSON with schema
`open_trader.prediction_service.preflight.v1` and a status of `READY`,
`RECOVERABLE`, or `BLOCKED`. A blocked result is an explicit stop: preserve the
record, plist, logs, and data and investigate the stated reason. `--dry-run`
is separate and only renders the launchd plist.

The boolean `managed_identity_verified` is true only when preflight independently
verifies an existing healthy managed owner or the failed managed startup described
below. An empty host can return `READY` for first-install availability with this
field false. Normal `make host-readiness` requires this field to be exactly true
as well as `READY` or `RECOVERABLE`; it does not require the old 8769 listener.
For a completely empty host, select `FIRST_DEPLOY=1` to run the fresh-host ownership
and listener-absence checks. Missing, false or non-boolean proof cannot satisfy
normal managed readiness. Preflight's schema and status meanings are unchanged.

The runtime record is evidence, not authority by itself. Recovery requires the
current clean checkout and independently observed launchd plist path,
arguments, process cwd/PID, loopback listener, health response, and the unique
owner of `data/prediction_arbitrage/runtime.lock`. A valid but outdated saved
record is recoverable when the current live release is independently verified
and the old bytes are archived first. Malformed, unproven, or concurrently
changed record/live evidence is fail-closed. A changed PID/start time alone is
recoverable when the release identity remains verified. The runtime record keeps
the canonical tracked manifest path, including a selected custom manifest, across
READY and refresh publication. This path also binds early launchd self-context
verification before the service has health evidence.

A loaded label with no working health endpoint can report `RECOVERABLE` as a
verified failed startup. This requires a private retained release/config binding,
an exact consumed permit whose claimed PID is absent, the matching installed and
loaded plist/arguments, a clean tracked release, no listener, and an available
native runtime lock. A transient startup PID must have the exact service UID,
arguments and cwd. Missing health or a missing PID alone is insufficient. Unknown
inspection, a reused claimed PID, an unexpected process, or held resources stays
`BLOCKED`. Preflight does not change the record, permit, plist or data.

For a verified managed service, `RECOVERABLE` preflight output includes
sanitized evidence values: `recorded_release` contains only the saved
candidate checkout, SHA, source state, and generation fields; `recorded_ready`
and `observed_ready` contain only PID and process start time. These diagnostic
fields never include the full runtime record, health payload, logs, or config.

Interrupted transitions are handled from the observed phase: an old verified
owner is handed off safely; an independently ready intended candidate only
finalizes its record without a restart; and a proven absent owner follows the
normal retry path. Unknown or conflicting phases stop.

## Mutating operations

Use the production installer for a first install, same-release record refresh,
or upgrade. Use the production uninstaller to stop the managed service. Both
operations share an advisory exclusion bound to the canonical managed label;
another installer, uninstaller, or shadow operation must stop with
`release operation already in progress` while one is active. Retry after the
first operation exits. A leftover lock file is not proof of an active
operation; only a kernel-held advisory lock blocks a retry.

Teardown polling is bounded but must cover launchd's normal SIGTERM grace:
installer label-absence checks use the existing --wait-seconds bound (the same
bound used for readiness). The uninstaller retains its existing cleanup
reserve, which also covers the grace window. A label that remains loaded, or
an inspection that is unknown, remains a fail-closed error; do not remove the
plist or force-kill a process to make the check pass.

Candidate readiness samples the process cwd and listener again after a
nonempty health response. A pre-health startup sample is not sufficient
evidence for the final identity check.

Before handing off a verified running service, the scripts re-read the
authoritative runtime record and live identity. If those observations change,
the operation stops without booting out the changed owner or overwriting the
new record. Failed or interrupted transitions remain visible in the runtime
record; do not fabricate a ready/deployed claim.

When a candidate fails, stderr first records the primary rejection and a
sanitized readiness diagnostic (PID/cwd/listener, SHA/schema/mode/generations,
status, and observed log paths). Cleanup checks are reported separately. If
cleanup cannot be proven, the compatible runtime-record failure_reason remains
candidate_cleanup_not_proven; it does not erase the primary stderr diagnostic.

Shadow mode shares the launchd label and therefore cannot be used to manage a
live production service. A shadow command must be refused before touching the
production plist, record, data, or process. Keep the production and shadow
runtime roots explicit when testing or diagnosing.

## D2 startup permission and failed exits

Production starts one current-API D2 owner and the existing sole Scheduler.
The process must claim its exact single-use permit before it reads reliable
control or settings. A bare `prediction-service --mode production` entry only
claims an existing permission; it does not create or repair one. Shadow keeps
its separate read-only behavior.

A failed claimed startup, a fatal control save, or a crash leaves the permit
`CLAIMED`. The internal failed exit is 78, after actual native and HTTP work
ends. A late successful control write records the saved version but does not
clear the fatal state. Automatic relaunch cannot consume the same claim.
Production launchd uses `KeepAlive.SuccessfulExit=false` and `ExitTimeOut=0`;
only a verified exact launchd self-context maps internal 78 to OS exit 0.
Production systemd uses `RestartPreventExitStatus=78`, infinite stop time, and
`SendSIGKILL=no`. A stop caller can time out while the owner still drains.
Do not force-kill, delete the permit, edit its state, or infer native completion
from a request or Future result.

After investigating the failure, use an explicitly authorized management
operation. The launchd installer accepts `--restart`. A verified failed loaded service
requires this explicit option; an ordinary install or record refresh does not
restart it. The manager rechecks retained identity and config before stopping
only that label, then proves label/process/listener/native-owner absence before
arming once. A startup PID may exit during observation; a different PID or changed
static identity blocks recovery. Incomplete teardown leaves failure evidence and
never bootstraps a candidate. The existing running-service health and handoff
guards remain in force. Systemd accepts the
`restart` action. The foreground entry accepts `--operator-start` or
`--operator-restart` with the actual selected data directory, private config,
loopback endpoint and tracked release manifest. It validates under the existing
operation lock, proves the old process/listener/native owner absent, arms once,
and replaces itself with the service process. Ordinary healthy start is a
read-only no-op. Preflight, dry-run, record refresh and automatic relaunch never
arm a new permission. An unknown process, owner, target or config blocks the
operation before it stops the existing service.

Permission recovery does not resume trading. A saved pause remains paused.
Check the current API projection for requested and effective running state,
requested and saved control versions, pending/native writes and fatal status.
An immediate effective pause with a pending save is not durable confirmation.
No historical LP session, old request or disposable business row grants replay
or supplies current money. Incomplete current sources remain UNKNOWN.

The systemd management record is root-owned and readable only by the dedicated
service group (0640); its start-permit directory and file belong to the exact
service UID/GID (0700/0600). Ordinary foreground/launchd records remain private
0600. Missing systemd metadata cannot fall back to an ordinary record when the
trusted installed unit selects this same runtime. Do not repair permissions or
substitute another record to bypass a rejection.

## After an operation

Confirm the command's exit status and inspect the resulting runtime record.
For an upgrade, `candidate` must be the independently observed target and
`previous_release` must be the observed old release. For an uninstall, the
record should be `stopped` with the last truthful ready evidence. Keep the
audit backup when a stale record is recovered; it preserves the exact bytes
that were replaced.

Do not call a successful preflight a deployment, and do not infer account,
trading, fill, or market-readiness state from this release record.
