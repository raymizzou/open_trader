# Auto Blocker Feishu Notification Design

## Goal

Send a Feishu notification from the automated daily premarket workflow when the run is blocked or when live quote connectivity is abnormal.

## Scope

This change covers only `DailyPremarketRunner`. Manual CLI commands continue to print errors locally and will not send Feishu messages in this phase.

## Trigger Rules

The runner sends a blocker notification when all conditions are true:

- The run is not `dry_run`.
- `OPEN_TRADER_NOTIFY_DAILY_REPORT` is enabled.
- The run status is `failed`.
- Or the Futu plan check has an `error`, such as OpenD unreachable or quote server interruption.
- Or the Futu plan check has missing quotes.
- Or generated trade actions contain review rows.

Normal watch rows do not trigger a blocker notification. A normal success run with actionable orders still sends the existing `Open Trader 行动通知`.

## Message Shape

The title is `Open Trader 阻塞通知`. The message is Chinese text and includes:

- Run date and status.
- Futu connection or quote problem when present.
- Review count when generated actions require manual handling.
- Artifact paths for the status JSON and daily report when available.
- A concise next step.

## Error Handling

Notification failures must not change the run status. The existing `_notify()` best-effort behavior remains in place.

## Tests

Add tests for:

- Futu unavailable sends a blocker notification.
- Missing quote sends a blocker notification.
- Failed run sends a blocker notification.
- Dry-run and disabled notification settings do not send blocker notifications.

