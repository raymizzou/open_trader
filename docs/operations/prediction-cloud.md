# Prediction on CVM, local browser over SSH

This workflow deploys only Prediction. It does not install OpenD, Legacy,
Account, Nginx, Docker or a new OS. Existing CRS, Nginx, V2Ray and x-ui remain
outside the operation. Branch push, Draft PR, GitHub merge, release/tag creation,
cloud provisioning, deployment and
trading authorization are separate. The client never starts the cloud backend.

## Credentials: service-owned file or Tencent SSM

For the #202 paused Shadow API probe, use an operator-imported JSON bundle at
`/var/lib/open-trader/prediction-credentials/polymarket.json`. The parent directory
must be owned by `prediction` with mode `0700`; the regular file must be owned
by `prediction` with mode `0600`. The reader rejects symlinks, invalid owners,
permissions and fields. Keep the bundle outside Git, the release, logs and
systemd environment; the unit contains only its path. The source credentials
are in the Mac Keychain; `prediction_arbitrage.json` does not contain them.
The operator performs the separate one-time private import. Do not run wallet
setup or create, replace or revoke an API key for this probe.

The file backend is explicit: `OPEN_TRADER_CREDENTIAL_BACKEND=file` and
`OPEN_TRADER_CREDENTIAL_FILE=/var/lib/open-trader/prediction-credentials/polymarket.json`.
Missing or invalid files fail closed with no fallback. The process only opens
the file for reading. No instance role or metadata isolation attestation is
needed for this file profile. Existing SSM profiles remain available:

Install the optional `cloud-ssm` extra only for an SSM profile in the release's Python 3.12 virtualenv.
The approved pins are `tencentcloud-sdk-python-ssm==3.1.160` and
`tencentcloud-sdk-python-common==3.1.182`. Ordinary macOS use keeps Keychain.

Before provisioning, check the account's SSM console for available regions,
pricing, endpoint and permission to create a custom secret. Do not infer SSM
availability from the CVM region. Hong Kong SSM availability and this account's
bill are not yet verified. Provisioning and enabling billing need authorization.

The operator enters the secret in the SSM console, never in agent chat, a shell
argument, Git, an environment file, or command output. Store a JSON object:

```json
{
  "com.open-trader.polymarket": {
    "signing-private-key": "<operator enters privately>",
    "builder-key": "<operator enters privately>",
    "builder-secret": "<operator enters privately>",
    "builder-passphrase": "<operator enters privately>"
  },
  "com.open-trader.predict": {
    "api-key": "<only if Predict is enabled>",
    "privy-private-key": "<only if Predict trading is enabled>"
  }
}
```

Bind a dedicated CAM role to the CVM. Grant only `ssm:GetSecretValue` on the
exact secret resource (use the resource identifier shown by the account's CAM
console), not `ssm:*` or a wildcard resource. Do not attach administrator policies
or store permanent Tencent SecretId/SecretKey on the host. Pin an explicit SSM
version; changes become effective only after an authorized service restart.
The service reads the bundle once per process into memory. An SSM outage at
startup blocks startup; it never falls back to a local private key. Revoking the
role does not erase keys already loaded into a running process. Wallet key
rotation is a separate wallet migration, not an automatic password rotation.

Only the following non-secret references belong in runtime configuration:

```text
OPEN_TRADER_CREDENTIAL_BACKEND=tencent-ssm
OPEN_TRADER_SSM_REGION=<verified SSM region>
OPEN_TRADER_SSM_SECRET=<secret name>
OPEN_TRADER_SSM_VERSION=<explicit version>
OPEN_TRADER_SSM_ROLE=<bound instance role name>
```

SSM protects storage, not an already compromised process or root user. Disable
core dumps for the service. Instance roles are machine-wide: before binding a
wallet-reading role to this shared host, separately approve and verify isolation
of instance metadata access from other service users/containers. Do not silently
change shared firewall rules. If that isolation cannot be demonstrated, host
readiness is BLOCKED; use a dedicated instance or another approved isolation
arrangement. The operator's SSH private key stays on their own computer.

Official references:
- [CVM instance roles](https://cloud.tencent.com/document/product/213/47668)
- [GetSecretValue](https://intl.cloud.tencent.com/zh/document/product/1078/38649)
- [SSM resource permissions](https://intl.cloud.tencent.com/zh/document/product/598/57154)
- [Official Python SDK](https://github.com/TencentCloud/tencentcloud-sdk-python)

## Ownership and backups before a future trading cutover

The #202 paused Shadow probe uses a separate runtime, leaves the local
production owner running and does not migrate its history or runtime data.
The following handoff procedure applies only to a later authorized trading
cutover under #203.

A filesystem lock protects one host only. Before a new production owner starts,
identify the old service, immutable SHA, wallet addresses and authoritative
runtime root. Reconcile all orders, positions, pending intents and UNKNOWN
receipts through existing read APIs. Never delete incidents, clear reservations
or invent an empty account. Stop the old owner only with deployment/cutover
authorization and prove the process, listener and lock are gone. Disable its
automatic restart for the handoff; do not allow two hosts to trade the same wallet.

While the old owner is stopped, copy the complete Prediction runtime data and
non-secret configuration, retaining a timestamped backup and hashes. For an
online SQLite backup, use Python `sqlite3.Connection.backup()` with a read-only
source URI, then `PRAGMA integrity_check` on the backup. Never copy only the main
SQLite file while WAL writers are active. Final cutover uses a stopped snapshot
so databases, related files and runtime settings agree. Do not transfer Keychain
exports or a host-specific ready/PID record as proof of the new host's identity.

Restore only while the relevant owner is stopped and its lock is free. Preserve
the failed runtime for audit, restore a verified consistent snapshot, and inspect
reader/contract generation before choosing a compatible immutable release.
A database snapshot can be older than exchange facts: re-reconcile before any
trading resumes. Stopping the service does not cancel exchange orders.

## Acceptance boundary

Development uses affected-service Docker checks and independent staged review.
Use the [PR-first delivery flow](agent-verification.md): reviewed branch, Draft
PR, exact CI evidence, user-approved GitHub merge. Local `main` only synchronizes
the remote. Candidate Acceptance runs only after GitHub merge, on the selected
final GitHub `main` SHA for an explicitly authorized deployment. PR-head or
synthetic PR merge checks cannot be reused as final-SHA Candidate evidence;
fixes return through a new PR. The old macOS launchd gate is not evidence for systemd.
Production verification must include the remote systemd PID, `/proc` cwd and
command, listener and runtime lock, actual `code_root`, release SHA and manifest,
logs since service start, N-leg pause contract and the LP read model. Health HTTP
200 alone is insufficient. The local Gateway must load the same accepted SHA;
its browser test runs with non-read-only HTTP requests blocked before navigation.

No live credential, systemd installation, Linux runtime or end-to-end browser
acceptance is implied by the offline tests. Unknown evidence blocks deployment.

## Configuration and commands

The examples below are installation instructions, not evidence of an existing
cloud release. Use an accepted 40-character SHA in every release path/reference.
The initial CentOS 8 / systemd 239 host has Python 3.12 under `/usr/local/bin`;
reconfirm those facts before provisioning. Do not replace system Python or use
Ubuntu package commands. Check compatible wheels/imports on Linux before cutover.

Prepare a dedicated `prediction` service user and group, an immutable root-owned
checkout `/opt/open-trader/releases/<SHA>`, and a service-owned runtime
`/var/lib/open-trader/prediction`. Keep code outside the writable runtime. The runtime and its `config` directory
must be service-owned `0700`, and `config/prediction_arbitrage.json` must be
service-owned `0600`. The service user must have a same-named primary group.
Release/config/venv paths and their ancestors must be root-owned and not
group/world-writable; release symlinks are rejected. Venv symlinks, including
the standard Python interpreter link, must resolve to trusted root-owned paths.
Create a Python 3.12 venv outside the code checkout, install the accepted release
(add `cloud-ssm` only for SSM), and retain that environment with the release for
rollback. This is an operator-authorized provisioning step, not done by the gates.

Create `/etc/open-trader/prediction-cloud.json` (root-owned `0600`, non-secret).
The unit scopes Git `safe.directory` to this exact root-owned release; it never
changes global Git trust. Preflight verifies Git identity as the service user.
A credentialless paused-Shadow example is:

```json
{
  "release_root": "/opt/open-trader/releases/<SHA>",
  "runtime_root": "/var/lib/open-trader/prediction",
  "python": "/opt/open-trader/venvs/<SHA>/bin/python",
  "user": "prediction",
  "expected_sha": "<SHA>",
  "mode": "shadow",
  "n_leg_paused": 1
}
```

For the #202 same-wallet probe, add these non-secret fields to that paused
Shadow config after the operator has privately imported the credential file:

```json
{
  "credential_backend": "file",
  "credentials_file": "/var/lib/open-trader/prediction-credentials/polymarket.json"
}
```

These fields extend the complete example above; they are not a standalone
config. File credentials are accepted only with `mode: shadow` and
`n_leg_paused: 1`. The service remains read-only in an independent runtime.

The cloud config requires an explicit `"mode": "production"` or
`"mode": "shadow"`; a missing or unknown value fails closed. The selected mode
is pinned into ExecStart and echoed by remote evidence. Modes never change
automatically, and selecting another mode requires the matching stopped-owner,
credential, readiness and authorization gates. The helper also accepts simple
alphanumeric/dash/underscore SSM references and absolute paths without spaces or
systemd specifiers. Use those names when creating the dedicated credential.
Production and non-paused Shadow continue to require those four SSM references.
Credentialless paused Shadow requires them to be omitted, pins
`OPEN_TRADER_CREDENTIAL_BACKEND=disabled`, and never touches Keychain, SSM or
instance metadata.
Retain the prior N-leg pause policy explicitly; this example does not authorize
enabling any trading strategy.

For #202, supply only the same-wallet non-secret
`config/prediction_arbitrage.json` in the independent runtime; do not copy
local history or trading runtime data. A later #203 trading cutover requires
the ownership handoff and consistent data backup described above. Notification
and LLM provider configuration are not automatically imported from the Mac.

From the accepted release on CVM, with `OPEN_TRADER_PYTHON` set to that release's
venv Python, the operator can run:

```sh
scripts/prediction-systemd.sh render
scripts/prediction-systemd.sh preflight
scripts/prediction-systemd.sh install
scripts/prediction-systemd.sh start
scripts/prediction-systemd.sh status
scripts/prediction-systemd.sh stop
```

`render` prints the complete unit. `preflight` is read-only. It checks the
immutable release, stopped owner, SDK, reader generation and storage and returns
only `PRECHECK_OK`; that is not full Host Readiness. Explicit paused Shadow
without credentials skips the wallet read. A credentialed profile runs
`wallet read-auth` as the service user, separating GET-only credential
derivation from complete authenticated account reads. The installed SDK's
ordinary `create()` can create credentials or deploy a wallet, so it is not
used by this read-only probe. Production and non-paused SSM Shadow preflight
also passes `--require-trading-region`, retaining the existing geoblock gate;
paused Shadow API probes omit the trading-region requirement.

Using the accepted release's Python and non-secret runtime config as the
`prediction` user, run `prediction-arb wallet read-auth --config <runtime-config>`
and `prediction-arb data-check --config <runtime-config> --sample 5` with the
same explicit backend/path environment as the unit. `read-auth` requires a
complete same-wallet order, position, trade and balance/allowance read; it
reports guard attempts separately. `data-check` reads the complete paginated
reward catalog, then a bounded sample of its current markets, token books,
24-hour price history and same-wallet reward/account categories. It reports
scope, time, source, completeness and an unknown reason for each category.
Its best result is `PARTIAL`: an API probe is not full product-pool coverage
or the #204 continuous-feed acceptance. Investigate every `BLOCKED` category;
do not treat empty rows or a successful public request as account proof.
Record only the redacted JSON report, never SDK payloads or credentials.
`install` requires root, verifies the unit with systemd-analyze, preserves the old
unit, writes the complete unit and reloads systemd. It leaves the service stopped.
`start` requires a matching installed/stopped record; production startup may
resume existing automatic strategies, so deployment and trading/cutover authority
must be established first. It waits up to 120 seconds for independently observed
identity. No command forces cleanup or rolls back automatically on failure.
Unknown ownership or interrupted transitions require inspection; do not delete
records to force a retry. After a failed/interrupted start has fully exited,
`stop` can record a clean stopped state only after the saved unit/config match
and both the listener and runtime lock are absent. A live unknown owner remains
BLOCKED and requires inspection. Autostart at host boot is a separate operator decision:
only after Smoke passes, `systemctl enable open-trader-prediction.service` enables
it. The installer does not silently enable trading after a future reboot.

View logs with `journalctl -u open-trader-prediction.service --since today`.
The shared runtime holds `prediction-systemd-release.json` and prior unit backups.
Keep the previous immutable checkout and venv. Rollback requires stopping and
verifying the current owner, selecting a compatible prior SHA/config, passing its
gates and using `install` then `start`; restoring a unit alone is not recovery.

On each client computer, keep the accepted detached release and a Python 3.12
interpreter. Configure the SSH alias using strict host-key checking, no agent
forwarding and the user's own key. Unlock the SSH key yourself when necessary.
Create `~/.config/open-trader/prediction-client.json`:

```json
{
  "release_root": "/absolute/immutable/release/<SHA>",
  "runtime_root": "/absolute/user-owned/prediction-client",
  "python": "/absolute/python3.12",
  "ssh_alias": "open-trader-hk",
  "expected_sha": "<SHA>",
  "mode": "shadow"
}
```

Set `OPEN_TRADER_PYTHON` to the client interpreter when `python3` is not 3.12+.
The client runtime must be private (`0700`) and separate from the release.

The client requires explicit `mode: "production"` or `"mode": "shadow"` and
verifies the same mode in health. Production must report
`production_owner=true` and `mutations=enabled`; Shadow must report the reverse
and a null `first_violation`. A standalone Shadow UI is labeled read-only. Credentialless paused Shadow
keeps its existing no-reader/LP-503 contract; explicit file credentials enable
background LP display reads even with N-leg paused. All Shadow views
disable order, augment, cancel and automatic controls while retaining
read-model rows and details. If an already identified service or a
Prediction-only view cannot refresh its venues identity, the UI fails closed as
UNKNOWN and blocks writes until a successful venues response restores valid
identity.

```sh
./scripts/prediction-client.sh start
./scripts/prediction-client.sh status
./scripts/prediction-client.sh stop
```

For a parallel Shadow client, set `"gateway_port": 8876` and
`"tunnel_port": 8879` explicitly and open
`http://127.0.0.1:8876/`. The local tunnel forwards only
`127.0.0.1:8879 -> remote 127.0.0.1:8769`. Production retains its original
`8766/8769` defaults; Shadow defaults to the parallel ports so it cannot silently
claim the production listeners. These commands manage only their recorded Gateway
and dedicated SSH process; they never stop an existing local Prediction, reuse
an unrelated SSH master, or call remote start/stop. An occupied selected port
blocks startup; an occupied production port does not stop or replace that process.
Repeating start returns the current verified connection. An ambiguous or
changed PID blocks signaling. A broken connection reports BLOCKED; inspect
`gateway.log` and `ssh.log` (replaced on each new client session), then stop the owned client and start it again.
Closing the browser or stopping this client leaves the cloud service running.

## #204: cloud display with Air execution

The complete Air Dashboard can use a paused, authenticated cloud Shadow for
selected display reads while the existing Air Prediction remains the sole
production execution owner. Cloud uses #202's private file credentials; invalid
or missing credentials fail startup without a public-only fallback. `disabled`
retains no LP reader startup even when a prediction config exists.

| Browser request | Owner |
| --- | --- |
| GET `/venues`, `/lp/dashboard`, `/lp/account/trades` | Cloud snapshots |
| GET `/execution/identity` | Gateway alias to Air's existing `/venues` |
| GET `/state`, `/lp/auto/state`, `/lp/sessions/current` | Air execution |
| GET all N-leg history (signals/executions/incidents), relations, modes and reports | Air engine records/IDs |
| All Prediction POSTs, including preflight, refresh and cancellation | Air |
| Other Dashboard pages and APIs | Existing Legacy/Account connections |

The display routes never forward browser Cookie, Authorization or CSRF to the
cloud and never return cloud Set-Cookie to the browser. Cloud venues cannot
replace Air identity or CSRF. The identity alias permits using the existing Air
immutable release: no new Air API is required for split display. Air's current
preflight, reservations, protection and recovery remain authoritative.

For the split client add these non-secret fields to the configuration above:

```json
{
  "mode": "shadow",
  "gateway_port": 8766,
  "tunnel_port": 8879,
  "execution_port": 8769,
  "execution_expected_sha": "<existing Air 40-character SHA>",
  "cloud_expected_sha": "<cloud 40-character SHA>"
}
```

`expected_sha` identifies the local immutable Gateway checkout; the other SHAs
identify their respective services independently. Without `cloud_expected_sha`,
the cloud SHA defaults to `expected_sha`. The split client defaults to 8766/8879
and requires Shadow cloud health with N-leg paused. It starts the complete
Gateway and its own independent SSH tunnel, never a Prediction backend. Start,
status and stop use the same `prediction-client.sh` commands. Existing listeners
block startup; replacing the managed Air Gateway requires its separately
authorized deployment procedure. Do not stop Air Prediction to free a port.

Status verifies local Gateway and cloud identities and reports Air execution
identity separately. Air offline may report `CONNECTED` with
`execution_status=unavailable`: cloud browsing continues, but the browser clears
Air authorization and disables writers. A responding wrong Air SHA/mode is an
identity failure. Cloud outage remains a display outage, with no fallback to Air
snapshots and no automatic strategy stop/start. With healthy Air, display stale,
failure or a pending cloud request does not block existing Air submissions or
cancellations; Air still performs its own real-time checks. Auto controls and their existing funds/slots/round details
poll the same Air payload independently and do not use cloud Dashboard `auto` data.
Air identity also supplies the N-leg execution/pause state; cloud paused or
offline cannot suppress healthy Air state/history reads. Cloud N-leg remains
paused, so its history handler is not relaxed to serve Air-engine signal IDs.

Cloud reuses the existing LP history, candidate scan/maintenance/competition,
book sampling, reward/observation and Dashboard snapshot workers. History keeps
its persisted retry scheduling but skips preparation notifications and their
notification-only deadlines. No trading/protection tick, automatic scheduler,
N-leg worker, report worker or share-watch notification loop starts. Snapshots
retain genuine completeness, read errors and UNKNOWN; `usd_value_unknown`
remains an accepted display limitation. The 60-second cloud account freshness
boundary and bounded (maximum 100 rows) trade pages are checked without upstream
HTTP reads in request handlers. Failed background reads publish stale snapshots.
Cloud independently persists existing history/catalog/observation data; no Air
SQLite migration or cross-host synchronization occurs.

Cloud Smoke keeps disabled-profile LP 503 validation. Authenticated paused
Shadow instead requires a real, fresh background account snapshot and records
account, catalog, candidate, history and reward source evidence. It does not
require every reward's derived USD value to be known. Two-host Smoke checks the
configured independent cloud/local SHAs and a healthy exact Air execution SHA;
its result does not grant live mutation authorization. Record both hosts' SHA,
PID/root, connection ports, snapshot times/scope and measured resources in the
operator evidence when an actual deployment is authorized. Development tests
are offline evidence only, not proof of live supply or deployment.

To return to local display, stop the client using its original saved config
(first preserving its owned-process identity check), then restore the existing
local Gateway configuration without `--prediction-display-upstream-port` and
with `--prediction-upstream-port 8769` through the authorized Gateway deployment
workflow. Air Prediction and its runtime/owner remain running. No page switch,
writer failover, balance alignment, order overlay or missing-history marker is
introduced; selected cloud content is displayed as returned.

## Two-host gates

Use these additional targets for this topology, retaining the ordinary macOS
commands for local deployments. Both are read-only and need local config copies,
remote config path and the exact same SHA. They must be run from the accepted
local immutable release, with the local browser dependencies already installed.

The operator evidence JSON contains `git_sha` and explicit attestations with
corresponding `*_evidence` strings describing actual observations and their
locations/timestamps. Production requires `old_owner_stopped`,
`metadata_isolation_verified` and `resources_reviewed` (all true). Shadow
requires `resources_reviewed` and an exact `independent_runtime_root`;
SSM-backed Shadow additionally requires `metadata_isolation_verified`.
The gate separately rejects a remote component
whose mode, pause policy or credential backend differs. These are manual
handoff/security/resource evidence, not claims manufactured by the gate. Review
shared-host memory headroom under real load; 2 GiB total memory alone does not
prove capacity. Never mark these true without the underlying observations.
File-backed preflight additionally requires a complete service-user
`read-auth`.

```sh
make prediction-cloud-host-readiness \
  CLOUD_CLIENT_CONFIG=/path/client.json \
  CLOUD_SERVICE_CONFIG=/path/local-copy-of-service.json \
  CLOUD_REMOTE_CONFIG=/etc/open-trader/prediction-cloud.json \
  CLOUD_OPERATOR_EVIDENCE=/path/operator-evidence.json

make prediction-cloud-smoke \
  CLOUD_CLIENT_CONFIG=/path/client.json \
  CLOUD_SERVICE_CONFIG=/path/local-copy-of-service.json \
  CLOUD_REMOTE_CONFIG=/etc/open-trader/prediction-cloud.json \
  CLOUD_OPERATOR_EVIDENCE=/path/operator-evidence.json
```

Readiness combines the stopped remote preflight and its mode/pause/credential
component profile with local Chrome and cached Playwright checks. Smoke combines
remote systemd/process/lock/log/business
identity, the owned local Gateway/tunnel, the existing five marked Python browser
regressions and the read-only browser scenario, adapting its Prediction-only entry point
without changing ordinary production smoke. Missing evidence ends
BLOCKED or ROLLBACK. Client, service and remote evidence modes must match; a
Shadow guard violation or unavailable backend cannot pass.
For credentialless paused Shadow, smoke also verifies the actual unit/process
environment remains disabled and read-only; do not manufacture an owner-stop
attestation or stop an unrelated local production process.
Cloud Smoke checks release identity, process, pause and read-only state; it
does not turn a `PARTIAL` API report into a complete #204 feed or Dashboard
acceptance.
Neither target installs a browser, starts a fixture server,
mutates an account or restarts services. Candidate Acceptance remains separate
and required before an authorized deployment. Report real cloud validation as
UNKNOWN until these gates run on the actual accepted release.
