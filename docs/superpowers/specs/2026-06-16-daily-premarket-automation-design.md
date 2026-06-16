# Daily Premarket Automation Design

## Goal

Build an unattended daily runner that starts before the US market open, finishes
with a usable premarket state before the deadline, and can move cleanly from the
current Mac Air development machine to a dedicated Mac mini.

The first version runs on macOS with a user-level `launchd` job. It does not
place trades. It produces trading advice, structured plans, live Futu plan
checks, logs, status files, and macOS notifications.

## Decisions

- Runtime host: local Mac first, with the same setup movable to Mac mini.
- Trigger: Monday through Friday at 18:30 Asia/Shanghai.
- Deadline: 21:10 Asia/Shanghai.
- Notification: local logs plus macOS notification.
- Workflow: `run-premarket` -> `build-trading-plan` -> `check-futu-plan`.
- Scheduler: `launchd` only starts the job; repo code owns all business logic.
- Fallback: if today's advice for a symbol is missing or fails, reuse the latest
  prior successful advice for that symbol when available.

## Architecture

Use two layers:

1. A user-level `launchd` plist starts the daily command on schedule.
2. A repo-owned Python runner performs environment checks, locking, orchestration,
   deadline enforcement, fallback, logging, status writing, and notifications.

The `launchd` plist remains intentionally small. It should set the working
directory and call one repo command, for example:

```bash
.venv/bin/python -m open_trader run-daily-premarket --date today
```

The plist template lives in the repo and is installed to:

```text
~/Library/LaunchAgents/com.open-trader.premarket.plist
```

## Automated Workflow

The new runner command performs this sequence:

```text
run-daily-premarket
-> load config
-> acquire run lock
-> preflight local inputs and services
-> run daily TradingAgents advice
-> apply per-symbol fallback where needed
-> build structured trading plan
-> evaluate live Futu quotes against active plans
-> write status summary
-> send macOS notification
-> release run lock
```

Default operational parameters:

```text
portfolio: data/latest/portfolio.csv
data-dir: data
reports-dir: reports
max-workers: 4
ta-timeout-seconds: 600
ta-max-retries: 2
no-symbol-timeout: true, constrained by the global deadline
futu host: 127.0.0.1
futu port: 11111
excluded symbols: existing default blacklist, currently AGRZ and ARGG
```

The runner should reuse existing Python functions where possible instead of
shelling out to the CLI subcommands. This keeps error handling and status
collection structured.

## Outputs

Existing artifacts remain the main data products:

```text
data/runs/<YYYY-MM-DD>/trading_advice.csv
data/runs/<YYYY-MM-DD>/trading_plan.csv
data/latest/trading_advice.csv
data/latest/trading_plan.csv
reports/premarket/<YYYY-MM-DD>.md
```

The automated runner adds run-level observability:

```text
logs/daily_premarket/<YYYY-MM-DD>.log
data/runs/<YYYY-MM-DD>/daily_run_status.json
reports/daily_runs/<YYYY-MM-DD>.md
```

`daily_run_status.json` is machine-readable and includes:

```text
run_date
started_at
finished_at
deadline_at
status: success | partial | failed
premarket: eligible/advice/actions/ok/fallback/error
trading_plan: active/fallback/error
futu_plan_check: checked/missing/error/triggered
artifacts: advice_csv/plan_csv/report/log
```

`reports/daily_runs/<YYYY-MM-DD>.md` is the human-readable run summary. It should
include timing, overall status, advice counts, fallback symbols, error symbols,
active plans, Futu quote check results, triggered plan states, and links or paths
to the detailed artifacts.

## Deadline and Fallback

The runner starts at 18:30 and uses 21:10 Asia/Shanghai as the hard deadline.
The goal is not to guarantee that every symbol finishes with fresh advice. The
goal is to guarantee that the system produces an explicit, usable premarket
state before the market opens.

Per-symbol rules:

- If today's analysis succeeds, use today's advice.
- If today's analysis fails or is unfinished at the deadline, look up the latest
  prior `status=ok` advice for that symbol.
- If prior successful advice exists, write a row for today with a fallback
  status and explicit fallback metadata.
- If no prior successful advice exists, write an error row for today.

Fallback metadata should include:

```text
source_status=fallback
fallback_reason=<error or deadline reason>
fallback_from_date=<prior run date>
```

The trading plan builder must accept both fresh `ok` advice and fallback advice,
while preserving source status in the plan output so a reused recommendation is
visible and auditable.

## Error Handling

The runner should be resilient to partial failures:

- A run lock prevents overlapping daily jobs from writing `latest` artifacts at
  the same time.
- Missing API keys, missing portfolio, missing TradingAgents path, or invalid
  configuration fails during preflight.
- Futu OpenD unavailable during preflight does not block advice and plan
  generation. It skips the Futu plan check and marks the run `partial`.
- Missing quotes for individual symbols do not fail the whole run. They are
  listed in status and the daily report.
- Single-symbol TradingAgents or model failures use fallback when possible.
- A full unhandled exception writes failure status and preserves any dated
  artifacts already written.

`latest` artifact promotion must stay conservative:

- Dated run artifacts are always preserved.
- `data/latest/trading_advice.csv` and `data/latest/trading_plan.csv` are updated
  only after the runner has a complete dated set where each expected symbol has
  an explicit `ok`, `fallback`, or `error` state.
- Failed or half-written files must not replace the previous latest snapshot.

## Notification

Use local macOS notifications for the first version.

Examples:

```text
Open Trader daily run finished: 14 plans, 1 triggered
Open Trader daily run partial: 12 ok, 2 fallback
Open Trader daily run failed: see logs/daily_premarket/2026-06-16.log
```

Remote notifications are deliberately out of scope for the first version. They
can be added after the workflow is stable on the Mac mini.

## Configuration

Use simple local configuration files and install scripts:

```text
config/daily_premarket.env
ops/launchd/com.open-trader.premarket.plist.template
scripts/install_daily_premarket_launchd.sh
scripts/uninstall_daily_premarket_launchd.sh
```

The env file stores machine-specific values:

```bash
OPEN_TRADER_REPO=/Users/ray/projects/open_trader
OPEN_TRADER_PYTHON=/Users/ray/projects/open_trader/.venv/bin/python
OPEN_TRADER_TIMEZONE=Asia/Shanghai
OPEN_TRADER_DEADLINE=21:10
OPEN_TRADER_FUTU_HOST=127.0.0.1
OPEN_TRADER_FUTU_PORT=11111
DEEPSEEK_API_KEY=<local secret>
OPENAI_API_KEY=<local secret>
```

Secrets should not be committed. The repo may include a sample env file, but the
real `config/daily_premarket.env` should remain local-only if it contains API
keys.

## Mac Mini Migration

The Mac mini migration path should be documented and scriptable:

1. Copy or clone the repo to the Mac mini.
2. Recreate `.venv` or install dependencies.
3. Install and log in to Futu OpenD.
4. Verify Futu OpenD is reachable at `127.0.0.1:11111`.
5. Fill in `config/daily_premarket.env`.
6. Install the launchd plist with the repo script.
7. Run the daily command manually once.
8. Confirm logs, status JSON, daily summary report, and macOS notification.

## Testing

Focused tests should cover:

- Config parsing and required-field validation.
- Run lock behavior when a second job starts.
- Deadline handling and per-symbol fallback.
- Fallback advice flowing into trading plan generation.
- Futu unavailable producing a partial run, not a failed advice run.
- Missing per-symbol quotes being recorded without failing the run.
- Status JSON and Markdown summary contents.
- launchd plist template rendering from config.

Manual verification should include:

```bash
.venv/bin/python -m open_trader run-daily-premarket --date today --dry-run
.venv/bin/python -m open_trader run-daily-premarket --date 2026-06-16 --dry-run
scripts/install_daily_premarket_launchd.sh --dry-run
```

Before enabling the scheduled job on Mac mini, run one non-dry-run manually with
Futu OpenD connected and confirm `check-futu-plan` results appear in the daily
summary.

## Out of Scope

- Trade placement.
- Remote notifications.
- Web dashboard.
- Database-backed job history.
- Market-holiday calendar integration.
- Cloud or server deployment.
