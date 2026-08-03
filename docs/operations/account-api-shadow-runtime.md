# Account API Runtime Modes

The Account API has two loopback-only, read-only modes at
`127.0.0.1:8768`. It serves only `GET /healthz` and
`GET /api/v1/account/snapshot`.

- `shadow` is the R2 parity reader. Its snapshot is not a browser route.
- `production` is the R3 reader. Browsers reach its snapshot only through
  `http://127.0.0.1:8766/api/v1/account/snapshot`; the Gateway marks that route
  as production and does not fall back to Legacy.

The production sequence, rollback, controlled fault test, and acceptance gate
are in the [Account API production cutover runbook](account-api-production-cutover.md).
This document remains the shadow-mode reference.

The Account Sync Worker remains the sole writer of
`data/latest/account_sync_state.json`, `portfolio.csv`, and `quotes.json`.
The API only reads the Worker publication: it never calls a broker, triggers a
sync, writes a publication, or creates a second snapshot file.

## Install and inspect

Run from the target repository/worktree. The dry run renders and validates the
plist without changing launchd state; the install manages only
`com.open-trader.account-api` and does not restart the Worker.

```bash
scripts/install_account_api_launchd.sh --dry-run
scripts/install_account_api_launchd.sh
launchctl print gui/$(id -u)/com.open-trader.account-api
lsof -nP -iTCP:8768 -sTCP:LISTEN
curl -fsS http://127.0.0.1:8768/healthz
R2_PROBE_DIR="$(mktemp -d)"
curl -fsS -D "$R2_PROBE_DIR/headers" \
  -o "$R2_PROBE_DIR/snapshot.json" \
  http://127.0.0.1:8768/api/v1/account/snapshot
PYTHONPATH=src .venv/bin/python -m open_trader account-api-parity --data-dir data
tail -n 100 logs/account_api/launchd.out.log
tail -n 100 logs/account_api/launchd.err.log
scripts/uninstall_account_api_launchd.sh
```

The job writes dedicated logs at `logs/account_api/launchd.out.log` and
`logs/account_api/launchd.err.log`. The startup record is
`account_api_runtime`; use it with `launchctl print` to confirm the PID,
working directory, and Git SHA.

## Health, snapshot, and parity

`/healthz` is a liveness endpoint and always returns HTTP `200` for a running
API. Its JSON includes `mode: "shadow"`, the API and Worker Git SHAs, and
`release_match`. A snapshot returns HTTP `200` only when both SHAs are the
same 40-character lowercase Git SHA; a mismatch intentionally leaves health
at `200` and makes the snapshot unavailable.

The snapshot is the frozen v1 JSON contract. Its strong `ETag` is returned in
`$R2_PROBE_DIR/headers`; repeat the request with the exact `If-None-Match`
value to receive `304` with an empty body when unchanged. Stable reads require
the Account publication, quote publication, and Worker heartbeat to agree;
the API returns its unavailable state instead of mixing generations.

`account-api-parity` compares the live API with the raw
`account_sync_state.json.dashboard_projection` and `quotes.json`; it does not
call Legacy `/api/dashboard` or the Frontend Gateway. Exit codes are `0`
(`PASS`), `1` (`FAIL`), and `2` (`BLOCKED`). `BLOCKED` means the raw source
changed while proof was being pinned, so retry after the Worker reaches a
stable publication; it is not a successful parity assertion.

## Stop and rollback

To stop the shadow reader and remove its plist, run:

```bash
scripts/uninstall_account_api_launchd.sh
```

This is idempotent and affects only `com.open-trader.account-api`; it does not
stop the Account Sync Worker, alter published data, or change Gateway or
Dashboard behavior. For R2 shadow validation, use the default installer mode
(or explicitly `--mode shadow`); do not use this shadow procedure as the R3
production cutover procedure.
