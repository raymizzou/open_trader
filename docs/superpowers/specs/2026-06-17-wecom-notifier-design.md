# WeCom Notifier Design

## Goal

Add notification support to Open Trader so the daily premarket workflow sends a
WeChat-readable report through an Enterprise WeChat group robot, and an intraday
watcher sends a trigger notification when an action condition is reached.

The first version must keep the trading workflow file-based and auditable. It
must not place orders. It must also leave a clean path for a later voice speaker
integration by separating notification channels from message rendering and
trading logic.

## Decisions

- Channel: Enterprise WeChat group robot webhook.
- Daily content: one Markdown summary covering each symbol's short report and
  suggested action, plus local artifact paths.
- Intraday trigger rule: notify when the action condition is reached, not when
  price is merely close.
- Intraday silence rule: after a `(run_date, futu_symbol, trigger_status)` has
  been notified successfully, do not notify it again on the same day.
- Daily workflow: `run-daily-premarket` should include trade-action generation,
  so the daily notification and intraday watcher share the same dated action
  semantics.
- Watcher runtime: first version is an independent long-polling command.

## Architecture

Add a notification module:

```text
src/open_trader/notifications.py
```

The module owns notification channel interfaces and message rendering:

- `Notifier`: protocol with a `notify(title, message)` style interface.
- `NullNotifier`: no-op implementation for tests and disabled notifications.
- `MacOSNotifier`: move or keep the existing local macOS notification behavior
  behind the same protocol.
- `WeComWebhookNotifier`: sends Markdown or text payloads to an Enterprise
  WeChat group robot webhook.
- `CompositeNotifier`: sends the same rendered message through multiple
  configured notifiers.
- Rendering helpers for daily summaries and intraday trigger messages.

`DailyPremarketRunner` should depend only on the notifier interface. It should
not know webhook JSON details. The CLI loads config from
`config/daily_premarket.env`, builds the configured notifier, and passes it to
the runner.

Add an intraday watcher command:

```text
open-trader watch-actions
```

The watcher reads the dated trading plan and trade actions, polls Futu OpenD,
uses the existing quote-evaluation/action semantics, and sends a notification
when a new action condition is reached.

## Daily Data Flow

Extend `run-daily-premarket` to perform this sequence:

```text
run daily advice
-> build trading_plan.csv
-> fetch live Futu snapshots
-> generate trade_actions.csv and reports/trade_actions/<date>.md
-> write daily_run_status.json and reports/daily_runs/<date>.md
-> send WeCom daily summary
-> promote latest artifacts when the run is not a dry run
```

The daily notification uses the same dated artifacts that the user can inspect:

```text
data/runs/<YYYY-MM-DD>/trading_advice.csv
data/runs/<YYYY-MM-DD>/trading_plan.csv
data/runs/<YYYY-MM-DD>/trade_actions.csv
data/runs/<YYYY-MM-DD>/daily_run_status.json
reports/daily_runs/<YYYY-MM-DD>.md
reports/trade_actions/<YYYY-MM-DD>.md
```

The daily report should not parse `data/latest` for its content. It should use
the current run's dated files so a failed or dry run cannot accidentally send a
summary for older action data.

## Intraday Watch Data Flow

`watch-actions` performs this loop:

```text
load dated active trading plans and trade actions
-> load notification_state.json
-> poll Futu snapshots
-> evaluate each active plan against its latest quote
-> map reached trigger status to action semantics
-> if the notification key has not been sent today:
     send trigger notification
     record the notification key
-> sleep and repeat
```

The watcher state lives at:

```text
data/runs/<YYYY-MM-DD>/notification_state.json
```

The dedupe key is:

```text
run_date + futu_symbol + trigger_status
```

This means `US.MSFT entry_zone` is sent once per day. If the same symbol later
hits a different condition such as `stop_loss_hit`, that is a different key and
can be sent once.

The state file should be written atomically. If multiple watcher processes run
by mistake, updates must use a lock or equivalent protection so duplicate
messages are avoided as much as practical.

## Message Content

Daily WeCom notification is one concise Markdown message:

```text
# Open Trader 2026-06-17: success

Summary:
- Advice: 12 ok, 0 fallback, 0 error
- Actions: 2 ready, 1 review, 10 watch
- Futu: 13 checked, 0 missing, 2 triggered

Ready:
- US.MSFT BUY high @ 399, qty 3, reason...
- US.QQQ TRIM medium @ 520, qty 1, reason...

Review:
- US.TSLA REVIEW medium, missing_quote...

Watch:
- US.AAPL HOLD low, wait for entry zone...

Reports:
- reports/daily_runs/2026-06-17.md
- reports/trade_actions/2026-06-17.md
```

Each symbol line should include:

- `futu_symbol`
- `action`
- `priority`
- `last_price`
- `suggested_quantity` when present
- `status`
- a short `reason`

The message must stay readable on a phone. If a section is long, keep the
highest-priority rows first and include the report path for the complete detail.

Intraday trigger notification is a short Markdown message:

```text
# Open Trader Trigger

US.MSFT BUY triggered
- Price: 399
- Quantity: 3
- Notional: USD 1197
- Reason: current price entered entry zone
- Report: reports/trade_actions/2026-06-17.md
```

## Configuration

Extend `config/daily_premarket.env.example` with local notification settings:

```bash
OPEN_TRADER_NOTIFIERS=wecom,macos
OPEN_TRADER_WECOM_WEBHOOK_URL=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=replace-me
OPEN_TRADER_WECOM_MESSAGE_FORMAT=markdown
OPEN_TRADER_NOTIFY_DAILY_REPORT=1
OPEN_TRADER_NOTIFY_ACTION_TRIGGERS=1
```

The real webhook URL is a secret and belongs only in the local env file.

Recommended CLI defaults:

```text
run-daily-premarket:
  uses notification config from --config
  does not send WeCom messages when --dry-run is set

watch-actions:
  --date today
  --plan data/runs/<date>/trading_plan.csv or data/latest/trading_plan.csv
  --actions data/runs/<date>/trade_actions.csv or data/latest/trade_actions.csv
  --data-dir data
  --reports-dir reports
  --host 127.0.0.1
  --port 11111
  --poll-seconds 30
  --once for tests and manual checks
  --dry-run to render would-send messages without calling the webhook
```

## Reliability

Notification failures must not turn an otherwise valid trading analysis into a
failed trading run. They should be captured in status/log output as notification
errors.

Rules:

- Send the daily notification only after dated daily status and trade-action
  report artifacts are written.
- Do not read stale `latest` files when composing a dated daily message.
- In dry-run mode, render or log would-send messages but do not call the WeCom
  webhook.
- For intraday trigger notifications, write the silence state only after the
  webhook send succeeds. This avoids suppressing an alert that was never sent.
- If Futu OpenD is unavailable, the watcher should return a clear error.
- If an individual quote is missing, the watcher should skip that symbol for
  that polling cycle and keep running.
- Webhook timeouts and non-2xx responses should be treated as send failures and
  logged with enough detail to diagnose the problem without exposing the full
  webhook URL.

## Testing

Focused tests should cover:

- Env parsing builds the expected notifier configuration.
- `WeComWebhookNotifier` emits the expected JSON payload for Markdown messages.
- `CompositeNotifier` invokes each child notifier and isolates failures where
  appropriate.
- Daily runner generates trade actions and passes the dated daily summary to the
  notifier.
- Daily notification failure records an error but does not discard valid run
  artifacts.
- `watch-actions --once` sends a trigger notification when an action condition is
  reached.
- A second `watch-actions --once` with the same
  `(run_date, futu_symbol, trigger_status)` does not send again.
- `notification_state.json` is written atomically.
- Dry-run modes do not call the webhook.
- Futu unavailable, missing quote, empty action file, and malformed state file
  paths produce clear behavior.

## Out Of Scope

The first version does not include:

- Automatic order placement.
- Personal WeChat client automation.
- Official account or service-account template messages.
- Real speaker or voice-device integration.
- Hosted web reports, images, or rich cards.
- Price-near-threshold warnings.

The voice-device integration should be a later notifier channel that consumes
the same daily and trigger message model.
