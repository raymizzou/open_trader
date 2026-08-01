# YES/NO Arbitrage Signal Workflow Design

**Date:** 2026-08-01

**Status:** Approved product design; implementation plan pending

**Target:** Existing Open Trader prediction-market Dashboard

## 1. Goal

Replace the five-second `当前机会` interaction with a truthful manual signal
workflow:

1. the watcher detects and durably records a YES/NO signal
2. Feishu notifies the user without linking back to the Dashboard
3. the user opens the Dashboard independently
4. the `套利信号` component refreshes once per second
5. an open, currently actionable signal has a `重新检查` button
6. `重新检查` runs the existing fresh preview and preflight
7. only the existing modal action `确认下单` may submit a real order

This change does not add unattended automatic ordering.

## 2. Approved UI

The approved mock direction is Variant A, `原位替换`.

- Preserve the existing warm Dashboard visual language, density, typography,
  spacing, borders, and component styles.
- Remove the complete `当前机会` panel.
- Keep `当前监控范围` in its existing left column.
- Promote the former history panel into the right column.
- Rename the selected panel and tab from `历史记录` / `信号历史` to
  `套利信号`.
- Keep the sibling tabs `交易与合并` and `事故`.
- The panel title follows the selected tab; there is no generic `历史记录`
  heading.
- A new signal never switches the selected tab automatically.
- Remove the neutral per-market `仅监控` label from `当前监控范围`; meaningful
  exceptional or degraded states remain visible.

The prototype is disposable design evidence. Production code must implement
the approved behavior in the existing Dashboard functions and remove the
prototype-only assets and query parameter.

## 3. Signal Table Contract

The `套利信号` table columns are:

1. `出现时间（HKT）`
2. `标的`
3. `持续`
4. `触发时利润`
5. `实时利润`
6. `通知`
7. `操作`

### 3.1 Profit semantics

- `触发时利润` is the stored `initial_profit`: the calculated profit when the
  signal episode first crossed all configured signal thresholds. It is not the
  configured threshold constant and may be higher than that constant.
- `实时利润` is the current backend-calculated `estimated_profit` for an open
  signal, using the latest watcher-owned order books, quantity, and fee rules.
- `实时利润` is shown only while the signal remains open and current. Closed
  signals display `—`.
- `峰值利润` and `最终利润` are not displayed in the signal UI. Existing audit
  fields may remain stored for compatibility.
- Realized profit exists only in `交易与合并`, under `已实现`, after execution
  and merge or settlement have produced a real result.

### 3.2 Action semantics

- Open actionable signals sort before closed signals.
- Closed signals then sort newest first.
- `操作` contains `重新检查` only when the backend projects the signal as
  currently actionable.
- An empty operation cell means the signal cannot currently be operated.
- There is no `窗口结束` column and no disabled action button for a closed
  signal.
- The UI does not show `未下单，当前可能已失效`.
- If signal freshness, watcher health, or the signal request fails, the UI
  fails closed by removing action buttons.

## 4. Refresh and Time Truth

The existing full prediction page continues its five-second state refresh.

While the YES/NO workspace is visible, a separate one-second timer requests
only:

```text
/api/prediction-arbitrage/history?kind=signals&limit=...
```

The successful response updates only the `套利信号` component DOM. It must not
rerender the readiness strip, metrics, monitoring scope, strategy tabs, or the
rest of the page. The timer stops when the user leaves the YES/NO workspace.

The browser does not request Polymarket books and does not calculate profit.
The existing watcher remains the only owner of public WebSocket book state and
profit calculation. The one-second request reads the latest local backend
projection.

Two explicit clocks are shown:

- Page header: `Watcher 数据时间`, derived from backend `heartbeat_at`.
- Signal panel: `信号刷新时间`, set after the latest successful signal request.

Both clocks display HKT plus relative age, for example:

```text
2026-08-01 11:03:39 HKT · 0 秒前
```

On failure, the corresponding time stops advancing and turns red. A failed
signal request also removes signal action buttons until a successful response
restores current truth.

## 5. Backend Signal Projection

Reuse the existing `signals` table and signal episode lifecycle. No schema
migration or new live-profit store is required.

The signal history response adds or guarantees these fields for each row:

- `signal_id`
- `opportunity_id`
- `market_id`
- `event_title`
- `event_title_zh`, when cached
- `started_at`
- `ended_at`
- `observed_duration_ms`
- `initial_profit`
- `live_profit`, populated from current `estimated_profit` only for an open
  current signal
- `notification_state`
- `actionable_now`

`opportunity_id` must be persisted when the signal is upserted so
`重新检查` can call the existing preview endpoint without reconstructing an
identity from display text.

`actionable_now` is server-owned. It is true only when the signal is open, its
corresponding current opportunity exists, the opportunity is complete and
actionable, watcher/readiness facts are fresh, no execution is active, and the
circuit breaker is closed.

The existing `initial_profit` immutability remains unchanged. Existing
`peak_profit` and `final_profit` fields remain audit-only and are not renamed or
deleted from durable history.

## 6. Manual Order Flow

The production flow reuses the existing preview and execution endpoints:

1. User clicks `重新检查` on an open signal.
2. UI posts its persisted `opportunity_id` to
   `/api/prediction-arbitrage/preview`.
3. Backend freshly rechecks rules, books, fees, quantity, balance, allowance,
   region, relayer, execution lock, and circuit breaker.
4. Any changed or expired fact rejects the preview without submitting.
5. A complete preview opens the existing `确认真实下单` modal.
6. Only `确认下单` posts the preview ID to the existing execution endpoint.

Signal notification and signal refresh never submit or pre-authorize an order.

## 7. Feishu Signal Notification

Persisting a new standard YES/NO signal episode schedules notification work
asynchronously. Persistence and UI actionability must not wait for Feishu or
translation.

Notification rules:

- One successful Feishu notification per stable `market_id` in any rolling
  30-minute period.
- The cooldown starts only after successful delivery.
- Every signal episode is still persisted during the cooldown.
- A failed delivery may retry at most three times while the same signal window
  remains open.
- Stop retrying after the signal closes.
- Notification work never calls preview, preflight, or execution and never
  submits an order.

The message title is:

```text
【YES/NO 套利信号】+$0.38
```

The message body contains only:

- bilingual title when the Chinese title is already cached, otherwise English
- YES price
- NO price
- quantity
- maximum cost
- current profit
- discovered time in HKT

It contains no Dashboard link, Polymarket link, interactive button, wallet,
internal ID, rule trace, or `未下单，当前可能已失效` disclaimer.

The existing LLM-hedge notification behavior is unchanged.

## 8. Title Translation

Translate market titles only. Other fields remain untranslated.

- English renders immediately.
- Cached Chinese renders above English in both `当前监控范围` and
  `套利信号`.
- A missing translation shows English only until a later refresh finds the
  cached Chinese result.
- Translation is one background worker and never blocks signal persistence,
  Feishu, signal response, action buttons, preview, or execution.
- Reuse the existing `llm_cache` table with a namespaced title key; do not add a
  translation table.
- Use Codex model `gpt-5.6-luna`, reasoning effort `high`, and priority service
  tier (`fast`). Do not use DeepSeek.
- Translation failure is fail-open to the English title and may be retried by a
  later request.

## 9. Scope

In scope:

- standard YES/NO signal history and current actionability
- one-second local signal-component refresh
- Feishu observation notification
- HKT display and freshness truth
- asynchronous bilingual title cache
- existing two-step manual execution
- removal of `当前机会` and neutral `仅监控` labels

Out of scope:

- automatic or unattended ordering
- an automatic-order toggle, queue, scheduler, or state machine
- changes to LLM hedge interaction or notification
- new database tables
- browser-side book fetching or profit calculation
- redesigning the existing Dashboard visual system
- mobile or remote trade control

## 10. Failure Behavior

- Store write failure: do not notify and do not expose an action button.
- Watcher/book/readiness stale: close or de-authorize the live signal and remove
  its button.
- Signal endpoint failure: retain the last visible rows for context, freeze and
  redden the signal refresh clock, and remove all signal buttons.
- Translation failure: show English; no trading behavior changes.
- Feishu failure: record the failed attempt, retry within the stated bound, and
  leave trading behavior unchanged.
- Preview rejection: show the existing backend reason and submit nothing.
- Execution or incident behavior after confirmation remains governed by the
  existing execution boundary and circuit breaker.

## 11. Acceptance Scenarios

The implementation is ready only when the final Dashboard acceptance gate also
passes these behaviors:

1. `当前机会` is absent and Variant A layout is present in the original style.
2. `套利信号`, `交易与合并`, and `事故` tabs retain their selected state.
3. A new signal does not switch away from another selected tab.
4. An open signal shows HKT time, bilingual title when cached, immutable
   `触发时利润`, changing `实时利润`, and one `重新检查` button.
5. The signal component updates at one-second cadence without replacing the
   monitoring-scope DOM or resetting an expanded monitoring row.
6. Closing the signal changes `实时利润` to `—` and removes its button.
7. A failed signal request freezes/redens its refresh time and removes buttons.
8. `重新检查` can open a complete fresh preview; an expired signal is rejected
   without submission.
9. `确认下单` remains the only action that can create an execution.
10. The first signal sends the specified link-free Feishu message; another
    episode for the same `market_id` within 30 minutes is persisted but not
    sent.
11. Failed Feishu delivery retries no more than three times and never after the
    signal closes.
12. English titles render before translation and later gain cached Chinese
    without delaying signal availability.
13. The existing LLM hedge surface and notifications remain unchanged.
14. Desktop and 375px layouts preserve readable titles and usable controls.
