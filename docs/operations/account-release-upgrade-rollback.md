# Account Release Upgrade and Rollback

This is the #24 operator procedure. It treats Account Sync Worker and Account
API as one Account release and proves the module can be upgraded and rolled
back without restarting Gateway, Legacy Dashboard, Trend controllers,
Research, or Prediction processes. Only the final `make acceptance` result
from the committed candidate can be `PASS`; this procedure is the operational
proof that feeds it.

## Fixed topology

```text
Browser / operator review
           |
    Frontend Gateway 127.0.0.1:8766   (unchanged by Account releases)
           |
    +----------+----------------+
    |                            |
Legacy Dashboard             Account API 127.0.0.1:8768
127.0.0.1:8767               (read-only, loopback, production mode)
    |                            |
    |                     Account Sync Worker (single writer, no listener)
    |                            |
    +------ shared data dir -----+
```

One Account release is one Git SHA applied to both `account-sync-worker` and
`account-api`. Gateway, Legacy Dashboard, and Trend controllers keep their own
processes, PIDs, and SHAs; an Account release must not restart them.

## Diagnostics

```bash
curl -fsS http://127.0.0.1:8768/healthz
curl -fsS http://127.0.0.1:8766/healthz
curl -fsS http://127.0.0.1:8767/healthz
curl -fsS -H 'X-Open-Trader-Account-Route: production' \
  http://127.0.0.1:8768/api/v1/account/snapshot
launchctl print gui/$(id -u)/com.open-trader.account-sync-controller
launchctl print gui/$(id -u)/com.open-trader.account-api
lsof -nP -iTCP:8766 -sTCP:LISTEN
lsof -nP -iTCP:8767 -sTCP:LISTEN
lsof -nP -iTCP:8768 -sTCP:LISTEN
tail -n 50 logs/account_sync/launchd.out.log
tail -n 50 logs/account_api/launchd.out.log
```

## Preflight

Deploy a clean release checkout at the target SHA and keep the current
accepted release checkout untouched for rollback. The runtime root (shared
`data/`, `config/`, and portfolio) stays the production root even when the
release code comes from another checkout.

```bash
RELEASE_ROOT=/absolute/path/to/account-release-<sha>
RUNTIME_ROOT=/Users/ray/projects/open_trader
OPEN_TRADER_PYTHON="${OPEN_TRADER_PYTHON:-$RUNTIME_ROOT/.venv/bin/python}"
git -C "$RELEASE_ROOT" status --short
git -C "$RELEASE_ROOT" rev-parse HEAD
test -x "$OPEN_TRADER_PYTHON"
PYTHONPATH="$RELEASE_ROOT:$RELEASE_ROOT/src" \
  "$OPEN_TRADER_PYTHON" -c 'import open_trader'
```

Stop if the release checkout is dirty, the SHA cannot be resolved, or the
interpreter cannot import the release.

## Upgrade

Run the dry run first, then install the release with the shared runtime root.
The installer runs writer first, waits for the writer lock to be released,
waits for a fresh account publication, starts the API, and then cross-checks
that API and Worker publish the same Git SHA.

```bash
"$RELEASE_ROOT/scripts/install_account_release.sh" --dry-run \
  --repo-root "$RELEASE_ROOT" --runtime-root "$RUNTIME_ROOT"
"$RELEASE_ROOT/scripts/install_account_release.sh" \
  --repo-root "$RELEASE_ROOT" --runtime-root "$RUNTIME_ROOT" \
  --python "$OPEN_TRADER_PYTHON" \
  --evidence-out "$RUNTIME_ROOT/logs/account_release/upgrade.json"
```

After the install, prove the other modules were not part of the release:

```bash
launchctl print gui/$(id -u)/com.open-trader.frontend-gateway
launchctl print gui/$(id -u)/com.open-trader.legacy-dashboard
for market in cn hk us; do
  launchctl print gui/$(id -u)/com.open-trader.trend-market-controller."$market"
done
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8766/api/dashboard
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8766/api/trend-reports/tiger/history
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8766/api/prediction-arbitrage/state
```

The Gateway, Legacy, and Trend PIDs and Git SHAs must equal their pre-upgrade
values. The Account snapshot must now carry the new release SHA through both
`8768` and the Gateway route on `8766`.

## Rollback

Rollback is the same installer pointed at the retained accepted release root,
which executes the reverse order: stop the candidate Writer first and wait for
its lock to be released, start the accepted Writer and wait for a successful
publication, then start the accepted API and wait for same-SHA health. The
Gateway route on `8766` needs no change because it always points at `8768`.

```bash
ACCEPTED_ROOT=/absolute/path/to/accepted-account-release
"$RELEASE_ROOT/scripts/install_account_release.sh" \
  --repo-root "$ACCEPTED_ROOT" --runtime-root "$RUNTIME_ROOT" \
  --python "$OPEN_TRADER_PYTHON" \
  --evidence-out "$RUNTIME_ROOT/logs/account_release/rollback.json"
```

Repeat the diagnostics and the other-module endpoint checks. The snapshot must
again carry the accepted SHA on `8768` and through `8766`. At no point are two
Writers running: the installer only starts the next Writer after the previous
job is gone and its lock is free.

## Isolation rules

- Restarting `com.open-trader.account-api` alone must not restart
  `account-sync-controller`, Gateway, Legacy, or any Trend controller.
- A stale or missing Worker heartbeat must not restart the API or remove the
  Gateway route; it surfaces as Account stale/unavailable per the v1 contract.
- With Account unavailable, `/api/dashboard`, Trend history, and prediction
  state must stay readable on `8766`.

## Acceptance gate

`make acceptance` validates process identity, frozen-artifact contracts, and
truthful display of whatever state exists. It never requires today's report,
an allocation terminal state, or a controller first success; those are
deterministic pytest concerns and daily operator monitoring, not gate inputs.

## Evidence checklist

Each upgrade and rollback evidence JSON must record: API and Worker PID, cwd,
Git SHA, start time, Worker heartbeat, snapshot generation, API listener on
`8768`, and fresh log paths. Keep the accepted release checkout until the
rollback drill succeeds and the accepted release is restored and stable; only
then remove migration-only rollback assets.
