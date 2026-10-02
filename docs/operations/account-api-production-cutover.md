# Account API Production Cutover

This is the #23 operator procedure. It moves active Account consumers to the
read-only Account HTTP contract, leaves Legacy responsible only for non-Account
module data, and disables the unused Premarket/T-signal entrypoints. It is not
an acceptance claim. Forward installation uses the
[source-release wrapper](deployment-preflight.md) after fresh Host Readiness;
Production Smoke verifies the installed release. `make acceptance` is now the
lightweight trusted-CI/identity preflight, not a backend rerun.

## Candidate and preflight

Deploy one clean detached checkout. Account API and Account Sync Worker are a
matched pair at that SHA; Gateway and Legacy Dashboard then use the same SHA.
Keep active Trend controller identities unchanged in this forward path. Moving
them to this SHA applies only to the separately authorized historical migration. Do not combine #23 consumers with the older #22 Account
release, and do not add a raw-read or dual-read fallback.

```bash
CANDIDATE_WORKTREE=/absolute/path/to/issue-23-account-consumers
CUTOVER_SHA="$(git -C "$CANDIDATE_WORKTREE" rev-parse HEAD)"
git -C "$CANDIDATE_WORKTREE" status --short
git worktree add --detach /absolute/path/to/open-trader-r4 "$CUTOVER_SHA"
export CUTOVER_ROOT=/absolute/path/to/open-trader-r4
# Existing isolated Python 3.12 release environment; command -v python3 alone
# does not establish the required lock-consistent virtual environment.
export OPEN_TRADER_PYTHON=/absolute/release-python/bin/python
export RUNTIME_ROOT=/absolute/shared-runtime
export PYTHONDONTWRITEBYTECODE=1
test -n "$OPEN_TRADER_PYTHON"
test -x "$OPEN_TRADER_PYTHON"
export PYTHONPATH="$CUTOVER_ROOT:$CUTOVER_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
"$OPEN_TRADER_PYTHON" -c 'import open_trader, pytest; import playwright.sync_api'
```

Stop if the candidate is dirty or the interpreter cannot import the candidate.
Keep the previous accepted whole-release checkout for rollback.

## Deployment order

Run dry runs first. Then apply the release in this exact order; a short,
observable Account-unavailable interval during a restart is acceptable.

```bash
cd "$CUTOVER_ROOT"
scripts/install_account_sync_launchd.sh --dry-run --repo-root "$CUTOVER_ROOT"
scripts/install_account_api_launchd.sh --dry-run --mode production --repo-root "$CUTOVER_ROOT"
scripts/install_dashboard_launchd.sh --dry-run --repo-root "$CUTOVER_ROOT"

# 1. Account writer and API — one matched release pair.
"$OPEN_TRADER_PYTHON" -B scripts/deploy_release.py --expected-sha "$CUTOVER_SHA" \
  --release-root "$CUTOVER_ROOT" --runtime-root "$RUNTIME_ROOT" \
  --python "$OPEN_TRADER_PYTHON" --extra browser account

# 2. Verify the two Account production reads before starting consumers.
curl -fsS -H 'X-Open-Trader-Account-Route: production' \
  http://127.0.0.1:8768/api/v1/account/snapshot > /tmp/open-trader-r4-snapshot.json

# Select one non-empty broker/generation from accepted_statement_generation in
# the saved snapshot, then substitute it below.
curl -fsS -H 'X-Open-Trader-Account-Route: production' \
  'http://127.0.0.1:8768/api/v1/account/statements/BROKER/GENERATION/trade-facts'

# 3. Gateway and Legacy Dashboard at CUTOVER_SHA.
"$OPEN_TRADER_PYTHON" -B scripts/deploy_release.py --expected-sha "$CUTOVER_SHA" \
  --release-root "$CUTOVER_ROOT" --runtime-root "$RUNTIME_ROOT" \
  --python "$OPEN_TRADER_PYTHON" --extra browser dashboard --mode stack

# 4. The historical #23 Trend-controller migration is a separate operation.
# Do not restart those controllers as part of this Account/Gateway forward path.
# Any still-needed migration requires its own authorized, identity-bound plan.

# 5. Disabled paths must stay absent. Do not run a Premarket/T-signal dry run.
launchctl list | rg 'com\.open-trader\.premarket(\.|$)' && exit 1 || true
ps ax -o command= | rg '(^|[[:space:]])(run-premarket|run-daily-premarket|watch-t|-m[[:space:]]+open_trader\.(daily_premarket|t_signal_runner))([[:space:]]|$)' && exit 1 || true
for command in run-premarket run-daily-premarket watch-t; do
  PYTHONSAFEPATH=1 "$OPEN_TRADER_PYTHON" -m open_trader "$command" --help
  test $? -eq 2
done
```

If an old Premarket label exists, unload only that exact label before proving
absence; never unload an unrelated job. No Premarket/T-signal notification or
dry-run is part of this cutover.

## Runtime proof before the final gate

Verify the selected Account, Gateway and Legacy process identities and fresh
logs against `$CUTOVER_ROOT` and `$CUTOVER_SHA`. Trend controllers are outside
this forward path: compare their PIDs, roots and SHAs with the recorded
pre-cutover values and require them unchanged. Only a separately authorized
historical Trend migration uses the new cutover identity for those controllers.

```bash
launchctl print gui/$(id -u)/com.open-trader.account-sync-controller
launchctl print gui/$(id -u)/com.open-trader.account-api
launchctl print gui/$(id -u)/com.open-trader.frontend-gateway
launchctl print gui/$(id -u)/com.open-trader.legacy-dashboard
for market in cn hk us; do
  launchctl print gui/$(id -u)/com.open-trader.trend-market-controller."$market"
  tail -n 100 "logs/daily_premarket/launchd-trend-controller-$market.out.log"
  tail -n 100 "logs/daily_premarket/launchd-trend-controller-$market.err.log"
done
lsof -nP -iTCP:8766 -sTCP:LISTEN
lsof -nP -iTCP:8767 -sTCP:LISTEN
lsof -nP -iTCP:8768 -sTCP:LISTEN
curl -fsS http://127.0.0.1:8768/healthz
curl -fsS http://127.0.0.1:8766/healthz
tail -n 100 logs/account_sync/launchd.out.log
tail -n 100 logs/account_api/launchd.out.log
tail -n 100 logs/frontend_gateway/launchd.out.log
tail -n 100 logs/legacy_dashboard/launchd.out.log
PYTHONSAFEPATH=1 "$OPEN_TRADER_PYTHON" -m open_trader account-sync-status --json
```

The browser reaches both owners only through Gateway: `/api/dashboard` is
non-Account module data, `/api/v1/account/snapshot` is Account data, and the
browser must not request `/api/quotes`. Legacy `/api/quotes` must return 404.
One fresh Account publication must update the browser's in-memory Account
snapshot without refetching Legacy options.

## Controlled Account-only fault

After normal reads succeed, stop only the confirmed Account API label. Account
consumers must fail closed; previously published Trend, Research and Prediction
display data must remain readable. Do not stop the Worker or Legacy process.

```bash
launchctl bootout gui/$(id -u)/com.open-trader.account-api
curl -sS -o /tmp/open-trader-r4-account.json -w '%{http_code}\n' \
  http://127.0.0.1:8766/api/v1/account/snapshot
rg '"code"[[:space:]]*:[[:space:]]*"account_module_unavailable"' \
  /tmp/open-trader-r4-account.json
curl -fsS http://127.0.0.1:8766/api/dashboard > /dev/null
curl -fsS http://127.0.0.1:8766/api/trend-reports/tiger/history > /dev/null
curl -fsS http://127.0.0.1:8766/api/prediction-arbitrage/state > /dev/null

scripts/install_account_api_launchd.sh --mode production --repo-root "$CUTOVER_ROOT"
curl -fsS -H 'X-Open-Trader-Account-Route: production' \
  http://127.0.0.1:8768/api/v1/account/snapshot > /dev/null
```

Confirm the Research workspace remains available from the existing Dashboard
payload after the fault. Restore Account before the final gate, then repeat the
health, listener and fresh-log checks.

## Final gate and rollback

After no source or data changes remain, run the gate once with the verified
shared interpreter:

```bash
PYTHON_BIN="$OPEN_TRADER_PYTHON" make acceptance EXPECTED_SHA=<40hex>
```

Preflight success is not a live-health verdict. For a forward deployment, use
`scripts/deploy_release.py` with the `account` kind, then the current Production
Smoke gate for the selected SHA and service set. Do not repeat a deployment just
to obtain a second acceptance label. Recheck fresh PID/cwd/SHA/log evidence;
HTTP 200 alone is insufficient. Retain the rollback procedure below.

Rollback #23 as one whole release to the retained prior accepted checkout:
stop the candidate Account API, replace the Worker only after its writer lock
is released, start the rollback Account API, restart Gateway and Legacy, then
unload the three candidate Trend controller labels and install CN, HK and US
controllers from that same rollback checkout. Prove each rollback controller's
launchd label, `status.json` PID/cwd/Git SHA/heartbeat and fresh stdout/stderr
logs before allowing it to generate a report. Never pair #23 consumers with a
#22 Account API, and never restore Legacy Account fields or raw-file reads as
an incident workaround. Repeat the production snapshot/facts, runtime identity,
disabled-path and isolation proofs on the rollback release.
