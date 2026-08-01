# Prediction Universe Retry and Operator Alert

## Goal

Recover quickly from transient Polymarket universe refresh failures without
waiting for the normal five-minute refresh interval. After five consecutive
failed attempts, stop retrying, keep YES/NO execution fail-closed, show a
permanent operator error, and send one Feishu notification requesting manual
intervention.

## Retry state

`PolymarketMonitor` owns three process-local values:

- consecutive universe failure count;
- whether the five-attempt limit is exhausted;
- whether the exhausted-state notification has been scheduled.

The first failed refresh is attempt 1. Attempts 1 through 4 schedule the next
universe refresh five seconds after the failed attempt completes. The normal
event loop continues between attempts so WebSocket processing, readiness
refreshes, relation work, and runtime publication are not replaced by one
blocking retry loop.

Any successful universe refresh clears the failure count and exhausted state,
then schedules the next normal refresh five minutes later. A fifth consecutive
failure sets the exhausted state and schedules no further universe refresh in
that process. Restarting the Dashboard process is the explicit operator action
that clears the process-local latch and begins a new attempt sequence. In the
dual-process deployment this means the Legacy Dashboard process that owns
`PolymarketMonitor`, not the frontend Gateway by itself.

## Fail-closed behavior and state projection

All failed attempts retain the existing fail-closed semantics: cached events
remain visible, but standard YES/NO opportunities are not actionable and no
order or opportunity notification may be produced from the failed universe
state.

The existing prediction state API continues to carry monitor health without a
second state model. Its `health` object adds:

- `universe_refresh_attempts`: integer from 0 through 5;
- `universe_retry_exhausted`: boolean.

Before exhaustion, `universe_refresh_failed` remains the degraded reason. At
exhaustion, the reason becomes `universe_retry_exhausted`. The Dashboard renders:

- attempts 1 through 4: `监控市场刷新失败，正在自动重试（x/5）`;
- attempt 5: `监控市场连续 5 次刷新失败，已停止自动重试；请重启承载预测监控的 Dashboard 服务并检查 Polymarket 连接。`

The top-level Watcher connection indicator remains owned by WebSocket
connection/heartbeat state. This change does not label a live connection as
disconnected merely because the REST universe refresh is retrying or exhausted.

## Feishu notification

The monitor exposes one failure observer beside its existing ready-opportunity
observer. `PredictionExecutionService` implements the observer by reusing its
existing Feishu-only delivery helper and the notifier already supplied by the
Dashboard runtime. No notification dependency or second notifier configuration
is added.

On the transition into exhausted state, the monitor schedules the observer once
without blocking its event loop. The notification is:

- title: `预测市场监控需要人工干预`;
- body: five consecutive universe refreshes failed, automatic retry has stopped,
  the sanitized final error type, the last successful universe refresh time (or
  `从未成功`), the Dashboard URL, and a request to restart the Dashboard service
  that owns the prediction monitor and check the Polymarket connection.

The process-local latch prevents duplicate notifications while the process keeps
running. A manual restart begins a new failure episode and may notify once again
only after another five consecutive failures. Notification delivery failure is
recorded in sanitized diagnostics but does not unlock trading or resume universe
retries.

Acceptance must not send a real test notification. Tests use deterministic
recording notifiers and assert the Feishu-only path; live acceptance verifies
configuration and state without external delivery.

## Tests

Focused monitor tests prove:

- attempts 1 through 4 retry after five seconds and do not enter exhausted state;
- a success before attempt 5 clears the counter and returns to the normal
  five-minute schedule;
- five consecutive failures produce exactly five attempts, latch the permanent
  error, schedule exactly one failure observer call, and never make attempt 6;
- stopping the monitor cancels or reaps the failure-notification task cleanly;
- retrying does not relax standard YES/NO actionability gates.

Execution and Dashboard tests prove:

- the failure observer uses only configured Feishu channels and includes the
  sanitized error type, last-success time, and Dashboard URL;
- the API preserves both retry fields;
- transient and exhausted Chinese messages render from real projected state;
- existing Watcher connection semantics remain unchanged.

Final verification runs focused tests during development, then `make acceptance`
as the last gate. Only `PASS` proceeds to redeploying the exact accepted SHA and
verifying the new PID, working directory, SHA, fresh logs, and HTTP 200 review
URL.

## Non-goals

- Relaxing stale-book, readiness, breaker, order, or signal-notification gates.
- Retrying failed orders or notification-worthy opportunities.
- Persisting the retry latch across a Dashboard process restart.
- Adding a Dashboard retry button, acknowledgement API, new configuration key,
  generic retry framework, macOS notification, or XiaoAI notification.
- Changing the normal five-minute universe refresh interval, 30-second refresh
  timeout, relation discovery cadence, or WebSocket subscription policy.
