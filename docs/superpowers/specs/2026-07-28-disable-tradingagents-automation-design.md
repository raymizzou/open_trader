# Disable TradingAgents Automation

## Goal

Stop all future automated TradingAgents daily premarket report runs and all
notifications emitted by that workflow.

## Scope

- Preserve the manual `run-daily-premarket` command.
- Preserve existing reports and run artifacts.
- Preserve the CN, HK, and US `trend-market` controllers and their notifications.
- Remove any legacy `com.open-trader.premarket`,
  `com.open-trader.premarket.hk`, and `com.open-trader.premarket.us` launchd
  jobs.
- Set `OPEN_TRADER_NOTIFY_DAILY_REPORT=0` in the shared local
  `config/daily_premarket.env`.

The notification flag is specific to the TradingAgents daily premarket
workflow. The shared notifier list remains unchanged so trend-controller
notifications continue to work.

## Implementation

1. Run `scripts/uninstall_daily_premarket_launchd.sh` with its default scope.
   The default removes only the daily premarket jobs and leaves trend
   controllers installed.
2. Change the shared local configuration from
   `OPEN_TRADER_NOTIFY_DAILY_REPORT=1` to
   `OPEN_TRADER_NOTIFY_DAILY_REPORT=0`.
3. Do not modify application source code or delete historical artifacts.

## Verification

- No process command contains `run-daily-premarket`, `run-premarket`, or a
  TradingAgents worker.
- `launchctl` cannot find any of the three daily premarket labels.
- No matching plist exists in the user or system launch-agent directories.
- No matching cron, `at`, or `screen` task exists.
- Loading `config/daily_premarket.env` returns
  `notify_daily_report == False`.
- CN, HK, and US `trend-market` controller labels and processes remain present.

Because this is an operational configuration change rather than a Dashboard
source change, the Dashboard acceptance gate does not apply.
