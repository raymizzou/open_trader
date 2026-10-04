# Reuse CI before a source release

The supported forward path is: successful exact-SHA main-push CI → fresh Host
Readiness → `scripts/deploy_release.py` → Production Smoke. The wrapper checks
CI and source/runtime identity immediately before calling one existing installer.
Deployment still requires explicit authorization. Neither CI nor this preflight
grants trading permission.

## What changed

Previously `make candidate-acceptance` built another test image, ran the whole
backend serially, then ran the portable prediction scenarios. CI now owns the
four-service backend partition and the non-LIVE portable scenarios. Deployment
reuses their evidence and does not run backend pytest or build a Docker image.
`make candidate-acceptance` and `make acceptance` are compatibility aliases for
`make deployment-preflight`; they no longer mean a separate test stage.

The backend still excludes `pressure` and `browser`. Gateway, Account and Legacy
remain serial. Prediction keeps two xdist workers and its explicit shared-port
and solver-fixture groups. Portable scenarios run serially. CI checks collected
nodeids against the full backend collection so new files, nested tests, marker
changes and omitted partitions cannot silently reduce coverage. The old single
cross-service pytest session is not reproduced: arbitrary global test ordering
is different. No second full backend execution is added to compensate for an
unspecified ordering dependency. Tests with a real shared-session contract must
state and preserve it explicitly.

## Trust and identity

The preflight reads GitHub through an existing authenticated `gh` session.
The selected Python must be an existing isolated Python 3.12 virtual environment
with system-site packages disabled. `PYTHONHOME` and `PYTHONUSERBASE` overrides
are rejected; a bare system interpreter cannot establish this environment match. It does
not create credentials, accept caller-supplied proof JSON, or trust a green UI
label. Missing API access is BLOCKED. It requires:

- The selected 40-character SHA in a clean detached checkout, still in GitHub
  main history, with a successful main-push CI run for that exact SHA
- The correct workflow path, completed current run attempt, all four services,
  portable scenarios and the exact `required` check from GitHub Actions app 15368
- Unexpired artifacts from that same run and attempt, with matching source,
  repository, scope, N-leg selection, lock, test and environment metadata
- The actual selected Python environment, checked read-only against the runtime
  dependency closure in that checkout's `uv.lock`, including selected extras,
  and code imports from that release's source

A later main commit does not invalidate a selected release that has its own
successful main-push evidence and remains in main history. PR-head and synthetic
PR-merge checks are different identities and cannot substitute.

Artifacts expire after three days. Missing, stale, cancelled, mismatched or
untrusted evidence blocks a forward deployment with an explanation. There is no
automatic full-test fallback or retry-until-green. Obtain fresh trusted CI evidence
through the normal approved process when required.

`Dockerfile.dev`, its image and its dependency manifest remain **test-only**.
The currently supported deployment artifact is the clean immutable source
checkout plus its separately provisioned, lock-consistent Python environment.
This is not a claim of reproducible production wheel/container bytes or a signed
release bundle. The separate release-artifact proposals are not implemented here.
No environment is installed, synchronized or modified by this preflight.
The source checkout must contain no ignored code files under `src` or `scripts`,
including bytecode caches. Use `PYTHONDONTWRITEBYTECODE=1` for source-reading
Host Readiness commands on the immutable checkout; the wrapper/preflight use
`-B` themselves, and the forward launchd templates disable bytecode writes
for the service processes (systemd already does this). Hidden index flags also block the check. The preflight does not
delete caches or repair a changed checkout.

## Commands and boundaries

Read-only check, from the selected immutable release:

```sh
make deployment-preflight EXPECTED_SHA=<40hex> \
  PYTHON_BIN=/absolute/release-venv/bin/python RELEASE_EXTRAS=cloud-ssm
```

Omit `RELEASE_EXTRAS` when none are used. Only `cloud-ssm` and `browser` are
supported explicit extras; declaring one requires its locked dependency closure.

After explicit deployment authorization and fresh Host Readiness, use the
wrapper for each forward installer action. It fixes the working directory,
script path, source root, runtime root and interpreter; it does not accept an
arbitrary shell command. Example Gateway-only action:

```sh
"$OPEN_TRADER_PYTHON" -B scripts/deploy_release.py --expected-sha <40hex> \
  --release-root /absolute/immutable/release/<40hex> \
  --runtime-root /absolute/shared-runtime \
  --python /absolute/release-venv/bin/python \
  dashboard --mode gateway
```

Set `OPEN_TRADER_PYTHON` to the same existing isolated release interpreter used
by `--python`; the wrapper also runs its preflight with that selected interpreter.

Supported kinds are `dashboard --mode gateway|legacy|stack`, `account [--evidence-out /absolute/evidence.json]`,
`prediction-launchd --mode production|shadow`, and
`prediction-systemd --config /absolute/cloud.json --action install|start`.
The cloud config must bind the exact same SHA, release root, runtime root and
interpreter; the existing systemd helper retains its trusted-path and ownership
checks. The launchd Prediction option accepts an explicit `--config`,
`--n-leg-paused 0|1`, and `--https-proxy http://127.0.0.1:1082`.
The proxy must be an existing loopback HTTP proxy without credentials or a path.
It is written only into the Prediction plist; loopback requests bypass it.
Omitting the option preserves the managed service's setting, and
`--https-proxy ''` explicitly disables it. The caller's shell proxy variables
are not copied into the service. A changed proxy restarts even the same release
through the existing identity, ownership and readiness checks.
Set the common `--extra cloud-ssm` or `--extra browser`
before the kind when needed.

Host Readiness and Smoke remain separate fresh checks with their existing host,
service ownership, process identity, browser, health and rollback requirements.
Use the same selected service set and SHA throughout. A wrapper exit of zero is
an installer result, not `READY` or `HEALTHY`. Run the existing Smoke gate after
installation; failure requires the existing operator-directed recovery process.
Cloud deployment must use the cloud readiness and Smoke gates on both machines.
The wrapper never auto-stops, auto-rolls back, starts a missing check, or submits
orders beyond the explicitly selected existing install/start action.

## Rollback

The low-level installers remain available for the existing, explicitly authorized
rollback procedures. Those use the recorded compatible immutable release,
retained environment, reader-generation/ownership checks and fresh Smoke. They
are not forced through latest forward-release evidence or expiring CI artifacts.
Dashboard `--mode single` remains a rollback action and is intentionally excluded
from the forward wrapper. The wrapper is the supported forward entrypoint; this
PR does not claim that every direct low-level invocation is globally guarded.
