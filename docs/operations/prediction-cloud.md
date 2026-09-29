# Prediction on CVM, local browser over SSH

This workflow deploys only Prediction. It does not install OpenD, Legacy,
Account, Nginx, Docker or a new OS. Existing CRS, Nginx, V2Ray and x-ui remain
outside the operation. Local merge, push, cloud provisioning, deployment and
trading authorization are separate. The client never starts the cloud backend.

## Credentials: Tencent SSM and an instance role

Install the optional `cloud-ssm` extra in the release's Python 3.12 virtualenv.
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

## Ownership and backups before cutover

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
Candidate Acceptance runs only after local merge for an explicitly authorized
deployment SHA. The old macOS launchd gate is not evidence for systemd.
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
with its `cloud-ssm` extra, and retain that environment with the release for
rollback. This is an operator-authorized provisioning step, not done by the gates.

Create `/etc/open-trader/prediction-cloud.json` (root-owned `0600`, non-secret).
The unit scopes Git `safe.directory` to this exact root-owned release; it never
changes global Git trust. Preflight verifies Git identity as the service user.
Example:

```json
{
  "release_root": "/opt/open-trader/releases/<SHA>",
  "runtime_root": "/var/lib/open-trader/prediction",
  "python": "/opt/open-trader/venvs/<SHA>/bin/python",
  "user": "prediction",
  "expected_sha": "<SHA>",
  "region": "<verified-SSM-region>",
  "secret": "open-trader-prediction",
  "version": "v1",
  "role": "prediction-ssm-reader",
  "n_leg_paused": 1
}
```

The cloud config accepts simple alphanumeric/dash/underscore SSM references and
absolute paths without spaces or systemd specifiers. Use those names when
creating the dedicated credential. Retain the prior N-leg pause policy explicitly;
this example does not authorize enabling any trading strategy.

Copy the authoritative non-secret `config/prediction_arbitrage.json` and data into
the runtime only after the ownership handoff above. Notification and LLM provider
configuration are not automatically imported from the Mac; verify the required
features and their separate credentials before claiming functional parity.

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

`render` prints the complete unit. `preflight` is read-only, runs wallet status as
the service user, checks immutable release, stopped owner, SDK, reader generation
and storage, and returns only `PRECHECK_OK`; that is not full Host Readiness.
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
  "expected_sha": "<SHA>"
}
```

Set `OPEN_TRADER_PYTHON` to the client interpreter when `python3` is not 3.12+.
The client runtime must be private (`0700`) and separate from the release.

```sh
./scripts/prediction-client.sh start
./scripts/prediction-client.sh status
./scripts/prediction-client.sh stop
```

Open `http://127.0.0.1:8766/`. These commands manage only their recorded Gateway
and dedicated SSH process; they never stop an existing local Prediction, reuse
an unrelated SSH master, or call remote start/stop. An occupied 8766/8769 blocks
startup. Repeating start returns the current verified connection. An ambiguous or
changed PID blocks signaling. A broken connection reports BLOCKED; inspect
`gateway.log` and `ssh.log` (replaced on each new client session), then stop the owned client and start it again.
Closing the browser or stopping this client leaves the cloud service running.

## Two-host gates

Use these additional targets for this topology, retaining the ordinary macOS
commands for local deployments. Both are read-only and need local config copies,
remote config path and the exact same SHA. They must be run from the accepted
local immutable release, with the local browser dependencies already installed.

The operator evidence JSON contains `git_sha` and three explicit attestations:
`old_owner_stopped`, `metadata_isolation_verified`, `resources_reviewed` (all true),
with corresponding `*_evidence` strings describing actual observations and their
locations/timestamps. These are manual handoff/security/resource evidence, not
claims manufactured by the gate. Review shared-host memory headroom under real
load; 2 GiB total memory alone does not prove capacity. Never mark these true
without the underlying observations.

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

Readiness combines the stopped remote preflight with local Chrome and cached
Playwright checks. Smoke combines remote systemd/process/lock/log/business
identity, the owned local Gateway/tunnel, the existing five marked Python browser
regressions and the read-only production browser scenario. Missing evidence ends
BLOCKED or ROLLBACK. Neither target installs a browser, starts a fixture server,
mutates an account or restarts services. Candidate Acceptance remains separate
and required before an authorized deployment. Report real cloud validation as
UNKNOWN until these gates run on the actual accepted release.
