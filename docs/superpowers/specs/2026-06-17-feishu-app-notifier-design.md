# Feishu App Notifier Design

## Goal

Add a Feishu app-bot notification channel alongside the existing WeCom and macOS
notifiers.

The first Feishu version uses a Feishu enterprise custom app, not a group custom
webhook robot. This matches the user's Feishu tenant, where the group custom
robot entry is not available. The trading workflow, daily summaries,
`watch-actions`, and same-day trigger dedupe remain unchanged.

## Decisions

- Keep the existing WeCom implementation and configuration.
- Add a new notifier key: `feishu_app`.
- Use Feishu app credentials:
  - `OPEN_TRADER_FEISHU_APP_ID`
  - `OPEN_TRADER_FEISHU_APP_SECRET`
- Use Feishu's tenant access token flow before sending messages.
- Support direct recipient configuration:
  - `OPEN_TRADER_FEISHU_RECEIVE_ID_TYPE=email`
  - `OPEN_TRADER_FEISHU_RECEIVE_ID=<user email>`
  - or `open_id`, `user_id`, `union_id`, `chat_id`
- Do not support `mobile` for direct sends; Feishu's message API rejects it.
- Send text messages in the first version. The existing daily and trigger
  message renderers already produce concise plain text with Markdown-like
  structure that is readable in Feishu.

## Architecture

Extend the existing notification module:

```text
src/open_trader/notifications.py
```

Add:

- `FeishuAppNotifier`
- `FeishuAppClient`
- Feishu tenant-token request helper
- Feishu message-send request helper
- Feishu response error handling

`DailyPremarketRunner` and `watch-actions` should not change their public
workflow. They already depend on the generic `Notifier` interface and use
message renderers shared across channels.

The notifier factory should recognize:

```bash
OPEN_TRADER_NOTIFIERS=feishu_app,macos
```

and should continue to support:

```bash
OPEN_TRADER_NOTIFIERS=wecom,macos
OPEN_TRADER_NOTIFIERS=feishu_app,wecom,macos
```

## Feishu API Flow

The notifier performs two API calls:

```text
APP_ID + APP_SECRET
-> POST /open-apis/auth/v3/tenant_access_token/internal
-> tenant_access_token
-> POST /open-apis/im/v1/messages?receive_id_type=<type>
-> message sent to receive_id
```

The send-message payload should use:

```json
{
  "receive_id": "<configured receive id>",
  "msg_type": "text",
  "content": "{\"text\":\"<rendered message>\"}"
}
```

The content field is a JSON-encoded string, matching Feishu's message API
contract.

## Configuration

Extend `config/daily_premarket.env.example`:

```bash
OPEN_TRADER_NOTIFIERS=feishu_app,macos
OPEN_TRADER_FEISHU_APP_ID=cli_replace_me
OPEN_TRADER_FEISHU_APP_SECRET=replace-me
OPEN_TRADER_FEISHU_RECEIVE_ID_TYPE=email
OPEN_TRADER_FEISHU_RECEIVE_ID=you@example.com
OPEN_TRADER_FEISHU_MESSAGE_FORMAT=text
```

The real app secret belongs only in the local env file.

For email delivery:

```bash
OPEN_TRADER_FEISHU_RECEIVE_ID_TYPE=email
OPEN_TRADER_FEISHU_RECEIVE_ID=you@example.com
```

For group delivery after a `chat_id` is known:

```bash
OPEN_TRADER_FEISHU_RECEIVE_ID_TYPE=chat_id
OPEN_TRADER_FEISHU_RECEIVE_ID=oc_xxxxxxxxxxxxx
```

## Daily And Trigger Behavior

No trading behavior changes:

- `run-daily-premarket` still generates daily advice, trading plan, trade
  actions, daily status, and Markdown reports before notifying.
- `watch-actions` still polls Futu quotes and sends a trigger notification only
  after an action condition is reached.
- `notification_state.json` still suppresses duplicate
  `(run_date, futu_symbol, trigger_status)` notifications on the same day.
- `--dry-run` still renders or writes artifacts without calling remote
  notification APIs.

## Error Handling

Feishu notification errors must not fail a valid trading run.

Rules:

- Missing Feishu app config when `feishu_app` is enabled raises a clear config
  error before attempting to send.
- Tenant-token request failures raise `NotificationSendError`.
- Send-message HTTP failures raise `NotificationSendError`.
- Feishu non-zero response codes raise `NotificationSendError` with the code and
  message, without logging the app secret.
- Daily runner records notification send failures in `notification_error`.
- `watch-actions` records same-day silence state only after notification send
  succeeds.

## Testing

Focused tests should cover:

- `FeishuAppNotifier` obtains a tenant token and sends a text message payload.
- `FeishuAppNotifier` sends the configured `receive_id_type` query parameter.
- `build_notifier_from_values()` builds `CompositeNotifier` for
  `feishu_app,macos`.
- Missing `OPEN_TRADER_FEISHU_APP_ID`, `OPEN_TRADER_FEISHU_APP_SECRET`,
  `OPEN_TRADER_FEISHU_RECEIVE_ID_TYPE`, or `OPEN_TRADER_FEISHU_RECEIVE_ID`
  produces clear `ValueError`.
- Feishu API non-zero `code` becomes `NotificationSendError`.
- Existing WeCom, macOS, daily runner, and `watch-actions` tests continue to
  pass.

## Out Of Scope

The first Feishu app-bot version does not include:

- Discovering `chat_id` automatically.
- Converting mobile/email to `open_id`.
- Interactive cards.
- File or image messages.
- Replacing or removing the existing WeCom channel.
