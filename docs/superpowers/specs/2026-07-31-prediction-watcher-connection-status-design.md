# Prediction Watcher Connection Status

## Goal

Make the top-level `Watcher` indicator answer only whether Open Trader is
currently connected to Polymarket. A stale or unavailable order book may block
trading, but must not label a live connection as unavailable.

## Design

- Treat the Watcher as connected only when the WebSocket reports `connected`
  and its latest message is no more than 30 seconds old.
- Keep the existing health/readiness checks unchanged for opportunity display,
  notifications, and trading. Stale books and failed universe refreshes remain
  fail-closed.
- When the connection is live but trading data is stale, show `Watcher 正常`
  and describe the alert as `当前盘口暂不可交易`.
- When the WebSocket is disconnected or its heartbeat is older than 30 seconds,
  show `Watcher 不可用` and keep the connection-error alert.

The change stays in the existing Dashboard projection helpers; it does not add
a backend field or a second state model.

## Tests

- A connected WebSocket with a fresh heartbeat and `books_stale` health renders
  `Watcher 正常`, while the trading readiness remains unavailable.
- A disconnected or stale WebSocket heartbeat renders `Watcher 不可用` and the
  connection-error alert.
- Existing execution and trading-availability tests remain unchanged.

## Non-goals

- Relaxing any order, notification, readiness, or stale-book gate.
- Changing the 30-second heartbeat threshold.
- Fixing Polymarket universe or book refresh reliability.
