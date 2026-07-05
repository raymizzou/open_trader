# Xiaozhi Voice Templates Design

## Goal

Make Xiaoai/Xiaozhi voice notifications useful as short audible prompts while
leaving the full details in Feishu and the UI.

The voice channel should answer three questions quickly:

- What happened?
- How urgent is it?
- What should the user do next?

## Scope

This design covers voice rendering for existing Open Trader notifications after
`xiaozhi` is configured in `OPEN_TRADER_NOTIFIERS`.

The existing Feishu and UI notification content stays unchanged. Voice output is
allowed to be shorter than the original notification message.

The test notification command remains unchanged and does not need a special
business template.

## Principles

- Keep each voice message around 8 to 18 seconds.
- Do not speak file paths, raw timestamps, internal status codes, or long lists.
- Do not say that an order has been placed or executed.
- Prefer direct next steps: check Feishu, check UI, review blockers, or confirm
  manually.
- If the notification type cannot be recognized, use a short fallback voice
  message.

## Notification Types

### Daily Start

Source title examples:

- `Open Trader 港股开始通知`
- `Open Trader 美股开始通知`

Voice template:

```text
Open Trader 提醒：{市场}盘前流程已开始。正在生成今日交易复核清单，完成后会继续通知。
```

Example:

```text
Open Trader 提醒：美股盘前流程已开始。正在生成今日交易复核清单，完成后会继续通知。
```

### Daily Blocker

Source title examples:

- `Open Trader 港股阻塞通知`
- `Open Trader 美股阻塞通知`

Voice template:

```text
Open Trader 重要提醒：{市场}盘前流程遇到阻塞，原因是{主要原因}。请先查看飞书或 UI，处理后再决定是否交易。
```

Reason mapping:

```text
futu_error -> Futu 行情异常
missing_quotes -> 有行情缺失
trade_action_review -> 有交易动作需要人工复核
advice_error -> 建议生成异常
plan_error -> 交易计划异常
run_failed -> 流程运行失败
already_running -> 已有任务在运行
```

If multiple reasons exist, speak only the highest-priority reason. Priority:

```text
run_failed
futu_error
missing_quotes
plan_error
advice_error
trade_action_review
already_running
```

Example:

```text
Open Trader 重要提醒：港股盘前流程遇到阻塞，原因是 Futu 行情异常。请先查看飞书或 UI，处理后再决定是否交易。
```

### Daily Action

Source title examples:

- `Open Trader 港股行动通知`
- `Open Trader 美股行动通知`

Voice behavior:

```text
Skip voice output.
```

Rationale: the action notification duplicates details that are better consumed
in Feishu or the UI. The later completion notification already includes the
useful action counts.

### Daily Completion

Source title examples:

- `Open Trader 港股完成通知`
- `Open Trader 美股完成通知`

Voice template:

```text
Open Trader 完成提醒：{市场}盘前流程已完成，本次用时{耗时}，状态是{状态}。{动作摘要}{下一步}
```

If duration cannot be calculated, omit the duration phrase:

```text
Open Trader 完成提醒：{市场}盘前流程已完成，状态是{状态}。{动作摘要}{下一步}
```

Duration source:

```text
preferred -> parse explicit start/finish fields if present in the message
normal daily run -> read started_at/finished_at from the status file path in the completion message
unavailable -> omit duration
```

Duration format:

```text
less than 60 seconds -> 45 秒
at least 1 minute -> 3 分 20 秒
at least 1 hour -> 1 小时 12 分
```

Status mapping:

```text
success -> 正常
partial -> 部分完成
failed -> 失败
already_running -> 已有任务在运行
```

Action summary:

```text
ready > 0 or review > 0 -> 今日有{ready_count}项可复核，{review_count}项需人工判断。
otherwise -> 今日没有需要立即处理的交易动作。
```

Next-step mapping:

```text
ready -> 可以查看飞书复核清单。
review_required -> 请先人工复核标记项。
blocked -> 请先处理阻塞原因。
```

Example:

```text
Open Trader 完成提醒：美股盘前流程已完成，本次用时 4 分 18 秒，状态是部分完成。今日有4项可复核，1项需人工判断。请先人工复核标记项。
```

### T Signal

Source title example:

```text
Open Trader｜做T提醒｜US ARM｜买入做T
```

Voice template:

```text
Open Trader 做 T 提醒：{标的}触发{动作}信号，建议比例{比例}。当前状态：{状态}。请确认后再操作。
```

If the ratio is missing:

```text
Open Trader 做 T 提醒：{标的}触发{动作}信号。当前状态：{状态}。请确认后再操作。
```

Action mapping:

```text
BUY_T -> 买入做 T
SELL_T -> 卖出做 T
```

Example:

```text
Open Trader 做 T 提醒：US ARM 触发买入做 T 信号，建议比例15%。当前状态：盘中有效。请确认后再操作。
```

## Fallback

If the voice formatter cannot classify a notification, speak:

```text
Open Trader 有新通知，请查看飞书或 UI。
```

## Data Flow

The existing notifier flow remains:

```text
Open Trader event -> notifier title/message -> Feishu receives full text
Open Trader event -> notifier title/message -> Xiaozhi receives short voice text
```

The Xiaozhi notifier should apply a voice formatter before sending the request
to `/xiaozhi/notify/speak`. The formatter may return no text for notifications
that should be skipped, such as daily action notifications.

## Error Handling

- Formatter errors should not block Feishu notifications.
- If formatting fails for Xiaozhi, fall back to the short generic voice message.
- If Xiaozhi delivery fails, preserve the existing notification attempt logging.
- Skipped voice output for daily action notifications should count as an
  intentional skip, not a delivery error.

## Testing

Add focused tests for:

- Daily start template.
- Daily blocker priority and reason labels.
- Daily action notification skipped for Xiaozhi.
- Daily completion template with and without duration.
- T signal template with and without ratio.
- Unknown notification fallback.

Use fixture messages that match current Open Trader titles and message shapes.
