# Feishu Notifier Design

## Goal

Add notification support to Open Trader so the daily premarket workflow sends a
Feishu-readable report through a Feishu group bot, and an intraday watcher sends
a trigger notification when an action condition is reached.

The first version must keep the trading workflow file-based and auditable. It
must not place orders. It must also leave a clean path for a later voice speaker
integration by separating notification channels from message rendering and
trading logic.

## Decisions

- Channel: Feishu group bot webhook.
- Daily content: one structured text order-review sheet covering each actionable
  symbol's exact price, quantity, estimated notional, post-trade position,
  post-trade weight, post-trade average cost when available, stop, risk, and
  review notes. The notification must not be a generic summary.
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
- `FeishuWebhookNotifier`: sends structured text payloads to a Feishu group bot
  webhook. The first version should use plain text with stable line breaks; rich
  Feishu cards can be added later without changing the trading calculations.
- `CompositeNotifier`: sends the same rendered message through multiple
  configured notifiers.
- Rendering helpers for daily order-review sheets and intraday trigger messages.

`DailyPremarketRunner` should depend only on the notifier interface. It should
not know webhook JSON details. The CLI loads config from
`config/daily_premarket.env`, builds the configured notifier, and passes it to
the runner.

The daily renderer should be deterministic. It may translate fixed labels into
Chinese, but it must not ask a model to re-summarize the report. Re-summarizing
the report can discard concrete trade details and produce circular language such
as "important because it was marked high priority."

The upstream change-classifier prompt should also be tightened so that
`summary`, `rationale`, and `watch_trigger` contain evidence that can support
the order-review sheet. At least one concrete detail should be present when the
source data provides it: price, stop, target, target weight, quantity, percent
trim/add, prior-vs-latest action change, catalyst, or risk condition.

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
-> send Feishu daily order-review sheet
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

The daily order-review sheet should join these dated inputs:

- `portfolio.csv` or the portfolio fields carried into the run: current
  quantity, current market value, current weight, and current average cost when
  available.
- `trading_plan.csv`: rating, entry zone, stop loss, targets, max weight,
  catalyst, time horizon, and structured plan text.
- `trade_actions.csv`: normalized action, suggested quantity, estimated
  notional, priority/status, and machine-readable reason/error fields.
- Futu quote snapshots used by the run: latest price and quote availability.
- `premarket_actions.csv` and `change_classifications.csv`: concrete rationale,
  prior-vs-latest advice change, and watch trigger.

Post-trade quantity, weight, and average cost should be calculated from the same
inputs used for the report. If the calculation cannot be made because a required
field is missing or malformed, the symbol should become `REVIEW` with a clear
missing-field reason.

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

Daily Feishu notification is one concise order-review sheet:

```text
# Open Trader 2026-06-17: success

Summary:
- Advice: 12 ok, 0 fallback, 0 error
- Actions: 2 ready, 1 review, 10 watch
- Futu: 13 checked, 0 missing, 2 triggered

Ready:

## US.RKLB | high | ADD

Current:
- Last price: 109.00
- Current quantity: 120
- Current weight: 1.36%
- Current average cost: 101.20

Suggested action:
- Trigger price: buy first tranche on 99-102 pullback; buy next tranche only on
  confirmed close above 113 with volume
- This order: buy 80 shares
- Estimated notional: USD 8,720
- Post-trade quantity: 200
- Post-trade weight: about 2.20%
- Post-trade average cost: about 104.32

Risk:
- Hard stop: 94
- New-position risk to stop: about USD 800-900
- Total position risk to stop: about 0.25%-0.35% of portfolio

Why it matters:
- RKLB changed from reduce/watch to Overweight. Evidence: 63% YoY revenue
  growth, gross margin improvement to 38%, USD 1.24B net cash, KeyBanc 135
  target, and a bounce near the 50-day SMA.

Review before action:
- Confirm target weight can rise from 1.36% to about 2.20%.
- Avoid chasing above the trigger plan.
- Confirm the 94 stop is acceptable before placing any order.

Review:
- US.TSLA REVIEW medium, missing_quote...

Watch:
- US.AAPL HOLD low, wait for entry zone...

Reports:
- reports/daily_runs/2026-06-17.md
- reports/trade_actions/2026-06-17.md
```

Each actionable symbol section should include:

- `futu_symbol`
- `action`
- `priority`
- `last_price`
- current quantity
- current position weight
- current average cost when available
- trigger price or execution condition
- suggested quantity
- estimated notional
- post-trade quantity
- post-trade position weight
- post-trade average cost when available
- stop price when available
- estimated risk when available
- `status`
- a concrete `reason`
- review checklist

The message must stay readable on a phone. If a section is long, keep the
highest-priority rows first and include the report path for the complete detail.
The top-level Feishu message may show only the highest-priority actionable rows
when the body would become too long, but those rows must still preserve exact
price, quantity, post-trade, and risk details. Lower-priority rows can be
summarized by count with a report path.

If any key input is missing, do not invent values. Mark the row as `REVIEW` and
state the exact missing input. Examples:

```text
## US.MSFT | high | REVIEW

Reason: cannot calculate post-trade average cost because current average cost is
missing from the portfolio input.
Needed before action:
- current quantity
- current average cost
- target quantity or target weight
```

Examples of unacceptable message content:

- "Why it matters: RKLB is high priority because it was marked high priority."
- "Summary: review RKLB position, price condition, and order risk."
- "Suggested action: add" with no trigger price, quantity, or post-trade effect.

Examples of acceptable message content:

- "Trigger price: buy first tranche at 99-102; second tranche only after close
  above 113 with volume."
- "This order: buy 80 shares; estimated notional USD 8,720; post-trade quantity
  200; post-trade weight about 2.20%; post-trade average cost about 104.32."
- "Hard stop: 94; total position risk to stop about 0.25%-0.35% of portfolio."

Intraday trigger notification is a short structured text message:

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
OPEN_TRADER_NOTIFIERS=feishu,macos
OPEN_TRADER_FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/replace-me
OPEN_TRADER_FEISHU_MESSAGE_FORMAT=text
OPEN_TRADER_NOTIFY_DAILY_REPORT=1
OPEN_TRADER_NOTIFY_ACTION_TRIGGERS=1
```

The real webhook URL is a secret and belongs only in the local env file.

Recommended CLI defaults:

```text
run-daily-premarket:
  uses notification config from --config
  does not send Feishu messages when --dry-run is set

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
- In dry-run mode, render or log would-send messages but do not call the Feishu
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
- `FeishuWebhookNotifier` emits the expected JSON payload for text messages.
- `CompositeNotifier` invokes each child notifier and isolates failures where
  appropriate.
- Daily runner generates trade actions and passes the dated daily order-review
  sheet to the notifier.
- Daily renderer includes trigger price, suggested quantity, estimated notional,
  post-trade quantity, post-trade weight, and post-trade average cost when the
  inputs are present.
- Daily renderer marks a row `REVIEW` instead of inventing values when current
  quantity, cost, latest price, target quantity, or target weight is missing.
- Prompt tests reject circular or generic classifier guidance such as
  "important because priority is high" and require concrete evidence in
  classifier output fixtures.
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
- Personal chat-client automation.
- Feishu interactive cards or approval workflows.
- Real speaker or voice-device integration.
- Hosted web reports, images, or rich cards.
- Price-near-threshold warnings.

The voice-device integration should be a later notifier channel that consumes
the same daily and trigger message model.
