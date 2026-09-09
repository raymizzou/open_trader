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

The runtime record is evidence, not authority by itself. Recovery requires the
current clean checkout and independently observed launchd plist path,
arguments, process cwd/PID, loopback listener, health response, and the unique
owner of `data/prediction_arbitrage/runtime.lock`. A valid but outdated saved
record is recoverable when the current live release is independently verified
and the old bytes are archived first. Malformed, unproven, or concurrently
changed record/live evidence is fail-closed. A changed PID/start time alone is
recoverable when the release identity remains verified.

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

Before handing off a verified running service, the scripts re-read the
authoritative runtime record and live identity. If those observations change,
the operation stops without booting out the changed owner or overwriting the
new record. Failed or interrupted transitions remain visible in the runtime
record; do not fabricate a ready/deployed claim.

Shadow mode shares the launchd label and therefore cannot be used to manage a
live production service. A shadow command must be refused before touching the
production plist, record, data, or process. Keep the production and shadow
runtime roots explicit when testing or diagnosing.

## After an operation

Confirm the command's exit status and inspect the resulting runtime record.
For an upgrade, `candidate` must be the independently observed target and
`previous_release` must be the observed old release. For an uninstall, the
record should be `stopped` with the last truthful ready evidence. Keep the
audit backup when a stale record is recovered; it preserves the exact bytes
that were replaced.

Do not call a successful preflight a deployment, and do not infer account,
trading, fill, or market-readiness state from this release record.
