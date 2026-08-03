# Account API Production Cutover

This is the R3 (#21) operator procedure. It prepares a browser cutover from
Legacy-owned Account reads to the read-only Account API while keeping the
Account Sync Worker as the only writer. It is a runbook, not an acceptance
claim: do not call the cutover accepted until the final `make acceptance`
returns `PASS` from the candidate checkout.

## Release identities

The immutable Account protected-file baseline is
`c0129b6f0c74d00a92166e867402337514c80a34`. Record the candidate only after
the cutover documentation and tests are committed. Use a detached checkout so
the deployed Worker, Account API, and Dashboard all resolve the same SHA:

```bash
BASELINE_SHA=c0129b6f0c74d00a92166e867402337514c80a34
CANDIDATE_WORKTREE=/absolute/path/to/issue-21-account-api-cutover
CUTOVER_SHA="$(git -C "$CANDIDATE_WORKTREE" rev-parse HEAD)"
git -C "$CANDIDATE_WORKTREE" status --short
git -C "$CANDIDATE_WORKTREE" diff --exit-code "$BASELINE_SHA".."$CUTOVER_SHA" -- \
  src/open_trader/account_snapshot.py \
  src/open_trader/account_api.py \
  src/open_trader/account_sync_worker.py \
  ops/launchd/com.open-trader.account-api.plist.template \
  scripts/install_account_api_launchd.sh \
  scripts/install_account_sync_launchd.sh
git worktree add --detach /absolute/path/to/open-trader-r3 "$CUTOVER_SHA"
export CUTOVER_ROOT=/absolute/path/to/open-trader-r3
export OPEN_TRADER_PYTHON="$CUTOVER_ROOT/.venv/bin/python"
```

The protected-file diff must be empty. A non-empty status or protected-file
diff stops the cutover. Do not improvise a second writer or edit the deployed
checkout.

## Preflight and cutover

The Dashboard installer defaults to stack mode in #21. **Do not run
`scripts/install_dashboard_launchd.sh --mode single` during this procedure.**
Single mode is the old Dashboard topology, not an Account API cutover path.

```bash
cd "$CUTOVER_ROOT"
scripts/install_account_sync_launchd.sh --dry-run --repo-root "$CUTOVER_ROOT"
scripts/install_account_api_launchd.sh --dry-run --mode production --repo-root "$CUTOVER_ROOT"
scripts/install_dashboard_launchd.sh --dry-run --repo-root "$CUTOVER_ROOT"

# The Worker installer stops the old writer and proves controller.lock is released
# before starting the candidate Worker.
scripts/install_account_sync_launchd.sh --repo-root "$CUTOVER_ROOT"
scripts/install_account_api_launchd.sh --mode production --repo-root "$CUTOVER_ROOT"
scripts/install_dashboard_launchd.sh --repo-root "$CUTOVER_ROOT"
```

After the Worker reports a fresh candidate publication, verify the exact
processes, publications, and route before browser acceptance:

```bash
launchctl print gui/$(id -u)/com.open-trader.account-sync-controller
launchctl print gui/$(id -u)/com.open-trader.account-api
launchctl print gui/$(id -u)/com.open-trader.frontend-gateway
launchctl print gui/$(id -u)/com.open-trader.legacy-dashboard
lsof -nP -iTCP:8766 -sTCP:LISTEN
lsof -nP -iTCP:8767 -sTCP:LISTEN
lsof -nP -iTCP:8768 -sTCP:LISTEN
curl -fsS http://127.0.0.1:8768/healthz
curl -fsS http://127.0.0.1:8766/healthz
tail -n 100 logs/account_sync/launchd.out.log
tail -n 100 logs/account_api/launchd.out.log
tail -n 100 logs/frontend_gateway/launchd.out.log
tail -n 100 logs/legacy_dashboard/launchd.out.log
PYTHONPATH=src .venv/bin/python -m open_trader account-api-parity --data-dir data
```

Confirm one listener per port; every runtime record has the detached `cwd`,
clean source state, fresh PID/start time, and `$CUTOVER_SHA`. Account health
must report `status: ok`, `mode: production`, and matching
`api_git_sha`/`worker_git_sha`; Gateway health must report both
`legacy_upstream_status: ok` and `account_upstream_status: ok`.

Verify the public production route and its strong ETag without bypassing the
Gateway:

```bash
PROBE_DIR="$(mktemp -d)"
curl -fsS -D "$PROBE_DIR/headers" -o "$PROBE_DIR/snapshot.json" \
  http://127.0.0.1:8766/api/v1/account/snapshot
ETAG="$(awk 'BEGIN{IGNORECASE=1} /^ETag:/{gsub("\\r", ""); print $2}' "$PROBE_DIR/headers")"
test -n "$ETAG"
curl -sS -o /dev/null -D "$PROBE_DIR/conditional-headers" \
  -H "If-None-Match: $ETAG" \
  http://127.0.0.1:8766/api/v1/account/snapshot
rg '^HTTP/.* (304|200)' "$PROBE_DIR/conditional-headers"
```

If a publication advances between requests, a contract-valid `200` with a new
ETag is valid; otherwise the conditional request must be `304`. Keep
`$PROBE_DIR` until operator acceptance.

## Controlled Account-only fault and recovery

Only after the normal preflight passes, prove that Account failure is isolated
from Legacy-owned modules. Stop only the confirmed Account API label, verify
the Gateway returns explicit Account unavailability, and leave the Worker and
Legacy Dashboard running:

```bash
launchctl bootout gui/$(id -u)/com.open-trader.account-api
curl -sS -o /dev/null -w '%{http_code}\n' \
  http://127.0.0.1:8766/api/v1/account/snapshot
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8766/api/dashboard
scripts/install_account_api_launchd.sh --mode production --repo-root "$CUTOVER_ROOT"
curl -fsS http://127.0.0.1:8766/api/v1/account/snapshot
PYTHONPATH=src .venv/bin/python -m open_trader account-api-parity --data-dir data
```

The Account route must be `503 account_module_unavailable` while stopped;
Legacy `/api/dashboard` stays independently available. Restart only the
candidate Account API and repeat the health, listener, log, ETag, and parity
checks. Do not fault the Worker as part of this browser-isolation proof.

## Rollback and re-cutover

Rollback is the inverse release sequence: keep one Worker, stop the candidate
API, replace the Worker only after its writer lock is released, then start the
rollback API and stack from the prior accepted detached checkout. Do not run
two Worker installers concurrently.

```bash
export ROLLBACK_ROOT=/absolute/path/to/prior-accepted-checkout
launchctl bootout gui/$(id -u)/com.open-trader.account-api
scripts/install_account_sync_launchd.sh --repo-root "$ROLLBACK_ROOT"
scripts/install_account_api_launchd.sh --mode production --repo-root "$ROLLBACK_ROOT"
scripts/install_dashboard_launchd.sh --repo-root "$ROLLBACK_ROOT"
```

Check `data/account_sync/controller.lock` is not held before and after each
Worker replacement, then repeat the PID/cwd/SHA/listener/log, Gateway health,
parity, and ETag checks above using the rollback SHA. Re-cut over by repeating
the same ordered candidate commands after the rollback Worker lock is released.

Do not delete plists, logs, detached worktrees, or `$PROBE_DIR` until the
operator has accepted the final `make acceptance` result. Cleanup happens only
after that acceptance record is retained.
