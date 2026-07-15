# Protection-Line Voice Alerts Design

## Goal

Speak one short alert when an A-share, Hong Kong, or US holding's real-time
price touches its active protection line. Keep Feishu as the full-text source of
truth and use voice only for this time-sensitive event.

## Confirmed Scope

- Support the existing A-share, Hong Kong, and US protection-line watchers.
- Speak when `last_price <= active_line`.
- Keep the existing Feishu and macOS protection-line notifications.
- Keep the manual voice test notification for installation and diagnostics.
- Use one configured Xiaozhi/Xiaoai device.
- Submit each alert to the existing Xiaozhi TTS queue without interrupting audio
  already playing.

## Out of Scope

- Daily premarket start, blocker, action, or completion voice notifications.
- Trend report, candidate, ranking, strength, danger, right-side, boiling, or
  champagne signal voice notifications.
- 做 T voice notifications. The existing template is paused until that workflow
  is redesigned separately.
- Ordinary price movement that has not touched an active protection line.
- Multiple speakers, acknowledgements, delivery retries, morning replay, or
  proof that the physical speaker finished playback.

## Existing Behavior

All three market watchers route through
`watch_a_share_protection()`. They already persist a `protection_triggered`
event when a holding's real-time price is less than or equal to its active
protection line. Delivery is currently split into Feishu and macOS channels;
Xiaozhi is deliberately excluded.

The generic Xiaozhi formatter currently contains templates for daily premarket
and 做 T notifications, while protection-line alerts fall through to a generic
message. The live `watch-t` CLI also uses `NullNotifier`, so its voice template
is not an active production path.

## Event and Delivery Flow

```text
CN/HK/US protection-line watcher
-> observe last_price <= active_line
-> persist protection_triggered
-> send the existing Feishu notification
-> send the existing macOS notification
-> apply the Shanghai voice-hours gate
   -> outside 08:00-23:00: persist suppressed_quiet_hours and stop
   -> inside 08:00-23:00: render and submit one Xiaozhi voice alert
      -> queued: persist queued
      -> failed: persist failed, do not retry, send a Feishu-only failure alert
```

Voice delivery is a separate attempt after the existing protection event has
been persisted. A voice failure must not change the protection event, stop the
watcher, or prevent Feishu/macOS delivery.

## Voice Allowlist

Automatic voice output uses an allowlist rather than a generic fallback:

- Protection-line trigger: speak.
- Manual test notification: speak when inside voice hours.
- Every other title or event: skip voice output.

Unknown notification types must not produce the current generic voice message.
This also ensures 做 T remains paused even if another caller later wires a
notifier into that workflow.

## Spoken Template

```text
Open Trader 紧急提醒：{市场}{名称}，代码{代码}，最新价{最新价}，已触及活动保护线{保护线}。建议全部卖出，请查看飞书确认并人工执行。
```

Rules:

- `{市场}` is `A股`, `港股`, or `美股`.
- Use the account position name when available; if it is missing, omit the name
  and speak only the code.
- Speak prices as ordinary decimal values; do not speak the `<=` operator.
- Do not claim that an order was placed or executed.
- Do not read paths, timestamps, endpoint URLs, tokens, or internal status
  codes.

## Deduplication

Each symbol receives at most one voice attempt per market trading date.
Continuous polling below the same protection line must not repeat the alert.

The watcher persists the outcome of the first voice decision, including quiet
suppression and submission failure. A process restart reads that outcome and
must not replay the voice alert. If the position remains held and is still
below its active protection line on the next trading date, it may alert once
again.

This is event deduplication, not delivery retry. A failed voice submission is
final for that symbol and trading date.

## Voice Hours

Voice is allowed from `08:00:00` inclusive until `23:00:00` exclusive in
`Asia/Shanghai`.

- Events first observed from 23:00 through 07:59 are intentionally suppressed.
- Suppression is not a delivery failure and does not generate a second Feishu
  warning.
- Suppressed events are not replayed at 08:00.
- The manual test notification follows the same gate. During quiet hours it
  returns an explicit `skipped: quiet hours` result without queueing audio.

## Xiaozhi Queue Contract

Open Trader continues to call the authenticated local Xiaozhi speak endpoint.
The request adds a `not_after` timestamp equal to the next 23:00 boundary in
Shanghai time.

```json
{
  "device_id": "configured-device-id",
  "title": "A股保护线触发 · 600000",
  "message": "Open Trader 紧急提醒：...",
  "not_after": "2026-07-15T23:00:00+08:00"
}
```

The endpoint validates authentication, the configured device, TTS readiness,
and `not_after`, then appends the item to the existing TTS queue. HTTP 200 with
`{"code": 0, "message": "queued"}` is the only delivery success Open Trader
requires.

There is no ordinary queue wait limit. The alert does not interrupt current
audio and may wait as long as necessary, except that Xiaozhi must discard it if
playback has not begun by `not_after`. That discard is an intentional quiet
hours outcome, not a failed Open Trader delivery, and is not replayed.

Open Trader does not require acknowledgement that the physical speaker began
or completed playback.

## Failure Handling

Open Trader makes one Xiaozhi request and never retries it. Submission failure
includes transport failure, invalid authentication, an offline device, a TTS
queue that is not ready, or an invalid API response.

After a failed submission, send this notification only to the configured
Feishu channel:

```text
Open Trader 语音播报失败

市场：{市场}
标的：{名称}（{代码}）
原事件：活动保护线触发
失败原因：{面向用户的简短原因}
处理：语音不重试，请按原保护线通知人工确认。
```

The warning must never route back to Xiaozhi. If the Feishu warning also fails,
write the failure to logs and stop; do not recurse or retry. User-facing output
must not expose tokens, full endpoint URLs, or stack traces.

## State and Observability

The Open Trader watch event log records one of these voice outcomes for each
triggered symbol:

- `queued`: Xiaozhi accepted the item into its TTS queue.
- `failed`: the one submission attempt failed.
- `suppressed_quiet_hours`: Open Trader intentionally did not submit it.

Xiaozhi logs `expired_quiet_hours` locally when a queued item reaches
`not_after` before playback starts. Open Trader does not wait for or reconcile
that later outcome.

Each record includes market, symbol, trading date, occurrence time, last price,
active protection line, and a redacted failure category when relevant. The
existing process version and PID logging remain the source for deployment
diagnostics.

## Error Isolation

- Feishu, macOS, and Xiaozhi remain separate channel attempts.
- A voice formatter or transport error cannot stop a market watcher.
- A Feishu failure cannot cause voice delivery to be reported as successful or
  failed; each channel keeps its own result.
- The Feishu voice-failure warning is best effort and terminal.

## Testing

Focused automated coverage must verify:

- The exact protection-line template for A-share, Hong Kong, and US positions.
- Name fallback, decimal price rendering, and the no-execution wording.
- Daily premarket, 做 T, trend report, and unknown notifications are skipped.
- Manual test notification remains available.
- Voice-hours boundaries at 07:59:59, 08:00:00, 22:59:59, and 23:00:00.
- Quiet suppression and queue expiration do not generate failure warnings.
- One attempt per symbol per trading date, no poll duplication, no restart
  replay, and eligibility again on the next trading date.
- Successful queueing, device offline, TTS not ready, authentication failure,
  transport failure, and malformed Xiaozhi responses.
- A voice failure sends one Feishu-only warning and never recurses if Feishu
  also fails.
- Xiaozhi drops an expired queue item before TTS starts and does not interrupt
  audio already playing.

## Live Verification and Deployment

This behavior affects notifications, background watchers, launchd jobs, and a
long-running Xiaozhi process. Unit tests alone are insufficient.

Before handoff:

1. Run the focused Open Trader and Xiaozhi automated tests and record the exact
   results.
2. Run a real manual test notification between 08:00 and 23:00 Shanghai time
   and confirm that the configured speaker produces audio.
3. Enable matching Xiaozhi/Open Trader authentication configuration without
   exposing the shared token.
4. Restart the Xiaozhi service and the CN/HK/US protection-line watchers so no
   process retains old code.
5. Repair any installed launchd job that points to a removed worktree or
   executable.
6. Verify fresh PIDs, working directories, Git SHAs, timestamps, and logs from
   the restarted processes.
7. Run `make acceptance` after all Open Trader modifications. Only `PASS` is an
   accepted result.
8. Redeploy the exact accepted Open Trader Git SHA, then verify its PID,
   working directory, Git SHA, fresh logs, HTTP 200 review URL, and provide that
   URL to the user as required by the project handoff gate.

If the browser or external environment required by acceptance is unavailable,
report `BLOCKED`; do not substitute unit tests, curl, fixtures, or screenshots.
