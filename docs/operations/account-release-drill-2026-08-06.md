# Account Release Upgrade/Rollback Drill — 2026-08-06

Issue #24 R5 bounded drill run against the live local production stack.
Raw evidence JSON is in `logs/account_release/baseline.json`,
`upgrade.json`, and `rollback.json` (gitignored); values below are copied from
those captures.

## Baseline (accepted release)

| Process | PID | Git SHA | Started at |
|---|---|---|---|
| Frontend Gateway (8766) | 27942 | `14526bc9` | 2026-08-06 12:08:11 |
| Legacy Dashboard (8767) | 27687 | `14526bc9` | 2026-08-06 12:07:48 |
| Account API (8768) | 30455 | `14526bc9` | 2026-08-06 12:09:56 |
| Account Sync Worker | 30292 | `14526bc9` | 2026-08-06 12:09:51 |
| Trend controllers CN/HK/US + allocation | 14749/14812/14869/14692 | `9c891da0` | pre-existing |

Snapshot at baseline: `healthy`, `snapshot_generation
sha256:532982af...`, `account_generation sha256:528eefd7...`.

## Upgrade drill (candidate `40b3b83a`)

Installed with `scripts/install_account_release.sh --repo-root
.worktrees/issue-24-account-release-drill --runtime-root
/Users/ray/projects/open_trader`:

| Process | PID | Git SHA | Started at |
|---|---|---|---|
| Account API (8768) | 69325 | `40b3b83a` | 2026-08-06 13:27:30 |
| Account Sync Worker | 69170 | `40b3b83a` | 2026-08-06 13:27:26 |

- Worker heartbeat `13:27:30`, phase `idle`, cwd
  `/Users/ray/projects/open_trader/.worktrees/issue-24-account-release-drill`.
- API `release_match: true`, listener `127.0.0.1:8768`, health `ok`.
- Snapshot via 8768: HTTP 200, `healthy`, `snapshot_generation
  sha256:64fd9e8a...`; via Gateway 8766: HTTP 200 with release
  `40b3b83a`.
- Unchanged processes: Gateway 27942, Legacy 27687, Trend
  14749/14812/14869, allocation 14692.
- Unchanged modules through 8766: `/api/dashboard` 200,
  `/api/trend-reports/tiger/history` 200, `/api/prediction-arbitrage/state`
  200.
- All 13 installer checks passed.

## Isolation checks on candidate

API-only restart (`launchctl kickstart -k` on `com.open-trader.account-api`):
API PID 69325 → 69704, Worker PID stayed 69170, Gateway PID stayed 27942,
health stayed `ok` at `40b3b83a`.

Worker outage (bootout `com.open-trader.account-sync-controller` for 20 s):
API PID stayed 69704 with health `ok`; Gateway snapshot route stayed HTTP
200; `/api/dashboard`, Trend history, and prediction state stayed 200. Worker
then restored at PID 70092 with a fresh heartbeat.

## Rollback drill (back to accepted `14526bc9`)

Installed with the same release installer against the accepted root:

| Process | PID | Git SHA | Started at |
|---|---|---|---|
| Account API (8768) | 70418 | `14526bc9` | 2026-08-06 13:29:09 |
| Account Sync Worker | 70307 | `14526bc9` | 2026-08-06 13:29:04 |

- Worker heartbeat `13:29:08`, phase `idle`, cwd
  `/Users/ray/projects/open_trader`.
- API `release_match: true`, listener `127.0.0.1:8768`, health `ok`.
- Snapshot via 8768 and via Gateway 8766: HTTP 200, `healthy`, release
  `14526bc9`; fresh `snapshot_generation sha256:94fac87a...`.
- Unchanged processes: Gateway 27942, Legacy 27687, Trend
  14749/14812/14869, allocation 14692.
- `/api/dashboard`, Trend history, prediction state through 8766: 200.
- All 13 installer checks passed; fresh worker/API logs show the new PIDs.

## Result

The Account module upgraded and rolled back as one release while Gateway,
Legacy Dashboard, Trend, Research, and Prediction processes stayed untouched.
The drill is the operational evidence for `make acceptance` on the final
candidate; only a `PASS` result from that gate is acceptance.
