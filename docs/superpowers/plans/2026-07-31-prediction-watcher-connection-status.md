# Prediction Watcher Connection Status Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Dashboard `Watcher` badge report Polymarket WebSocket/heartbeat connectivity independently from stale-book trading availability.

**Architecture:** Keep the existing backend payload and trading gates. Add one Dashboard projection helper, `predictionWatcherIsConnected(payload)`, using the WebSocket status plus a 30-second latest-message/heartbeat age. Use it for the page header and choose a separate stale-book alert copy; leave `predictionHealthIsNormal` in all trading/readiness paths.

**Tech Stack:** Existing Dashboard vanilla JavaScript projection, Python `pytest` test invoking the Dashboard JS harness, launchd deployment, `make acceptance`.

## Global Constraints

- WebSocket connected and latest message age must be no more than 30 seconds for `Watcher 正常`.
- Stale books and failed universe refreshes remain fail-closed for opportunities, notifications, and orders.
- Do not add a backend field, dependency, state store, or new connection model.
- Preserve the existing `Watcher 不可用` copy when the connection is actually disconnected or heartbeat-stale.

## File Map

- Modify: `src/open_trader/dashboard_static/dashboard.js` — add the connection-only projection helper, use it in the page header, and distinguish stale-book alert copy.
- Modify: `tests/test_dashboard_web.py` — lock connected-but-stale and disconnected rendering behavior in the existing JavaScript projection test.
- Modify: `CHANGELOG.md` — add the dated operator-facing entry before merge.

### Task 1: Separate connection status from trading health

**Files:**
- Modify: `tests/test_dashboard_web.py:2360-2410`
- Modify: `src/open_trader/dashboard_static/dashboard.js:2047-2245`

**Interfaces:**
- Consumes: `payload.relation_discovery.websocket.status`, `payload.relation_discovery.websocket.last_message_age_seconds`, and fallback `payload.health.heartbeat_age_seconds`.
- Produces: `predictionWatcherIsConnected(payload): boolean`, used only by `predictionStatusLabel` and `predictionExecutionAlert`.

- [ ] **Step 1: Extend the existing test with the failing behavior**

Add a connected-but-stale payload to `test_prediction_market_layout_a_uses_binary_health_and_four_truthful_metrics`:

```javascript
const connectedButBooksStale = {
  status:"degraded",
  stale:true,
  health:{status:"degraded",degraded_reasons:["books_stale"],heartbeat_age_seconds:"0.4"},
  relation_discovery:{websocket:{status:"connected",last_message_age_seconds:0.4}},
  failure_reason:"books_stale",
  readiness:{status:"ready"},
  breaker:{open:false},
};
console.log(JSON.stringify({
  connectedButStaleHeader:predictionPageHeader(connectedButBooksStale),
  connectedButStaleAlert:predictionExecutionAlert(connectedButBooksStale),
}));
```

Assert `connectedButStaleHeader` contains `Watcher 正常` and does not contain
`可参与盘口已过期`; assert `connectedButStaleAlert` contains
`当前盘口暂不可交易` and does not contain `Polymarket 数据连接异常`.

- [ ] **Step 2: Run the focused test and verify it fails for the intended reason**

Run:

```bash
pytest tests/test_dashboard_web.py::test_prediction_market_layout_a_uses_binary_health_and_four_truthful_metrics -q
```

Expected: FAIL because the current header uses `predictionHealthIsNormal` and
the current stale alert is labeled as a connection error.

- [ ] **Step 3: Add the minimal connection-only helper**

Insert after `predictionHealthIsNormal`:

```javascript
function predictionWatcherIsConnected(payload) {
  const websocket = payload?.relation_discovery?.websocket;
  const status = String(websocket?.status || "").trim().toLowerCase();
  const age = Number(websocket?.last_message_age_seconds ?? payload?.health?.heartbeat_age_seconds);
  if (!Number.isFinite(age) || age > 30) return false;
  if (status) return status === "connected";
  const reasons = Array.isArray(payload?.health?.degraded_reasons) ? payload.health.degraded_reasons : [];
  return !reasons.some((reason) => ["heartbeat_missing", "heartbeat_stale", "stream_disconnected"].includes(String(reason)));
}
```

Change `predictionStatusLabel` to call `predictionWatcherIsConnected(payload)`.
In `predictionPageHeader`, suppress the failure reason when the connection is
live. In `predictionExecutionAlert`, when `payload.stale` is true, return
`当前盘口暂不可交易` if the connection helper is true; otherwise retain the
existing `Polymarket 数据连接异常` alert.

- [ ] **Step 4: Run the focused test and verify it passes**

Run the same focused pytest command. Expected: PASS, including the existing
healthy and heartbeat-stale assertions.

- [ ] **Step 5: Run the Dashboard projection test module**

Run:

```bash
pytest tests/test_dashboard_web.py -q
```

Expected: all tests pass with no console or lint errors.

- [ ] **Step 6: Commit the implementation**

```bash
git add src/open_trader/dashboard_static/dashboard.js tests/test_dashboard_web.py
git commit -m "fix: separate watcher connection from book freshness"
```

### Task 2: Changelog and final verification

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Add the operator-facing changelog entry before merge**

Add a dated entry explaining that the Watcher badge now reflects WebSocket
connectivity while stale books still block trading.

- [ ] **Step 2: Run the full acceptance gate**

From the clean final-SHA acceptance worktree, run:

```bash
make acceptance
```

Expected: `PASS`.

- [ ] **Step 3: Redeploy and verify the exact accepted SHA**

Restart the Dashboard launchd service, then verify the new PID, worktree,
Git SHA, fresh `dashboard_runtime` line, launchd environment, and HTTP 200.

- [ ] **Step 4: Capture the live LLM page**

Open `http://127.0.0.1:8766/`, select `预测市场` → `LLM对冲套利`, and verify
the live page shows `Watcher 正常` while the trading alert remains unavailable
when books are stale. Check the browser console has no errors and save the
affected view screenshot for handoff.
