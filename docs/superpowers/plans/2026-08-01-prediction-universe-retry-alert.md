# Prediction Universe Retry and Operator Alert Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Retry a failed Polymarket universe refresh every five seconds, recover immediately after any success, and after five consecutive failures stop retrying, keep standard YES/NO trading fail-closed, show a permanent operator error, and send one Feishu notification.

**Architecture:** Keep `PolymarketMonitor` as the sole owner of refresh cadence and process-local retry state. Extract one testable “refresh once if due” seam from its existing event loop so retries never form a blocking inner loop. Project the two retry fields through the existing monitor snapshot/API, reuse `PredictionExecutionService` and its Feishu-only delivery helper for the one exhausted-state alert, and render the state in the existing Dashboard alert component.

**Tech Stack:** Python 3.12, `asyncio`, existing prediction monitor/execution/store modules, vanilla JavaScript, pytest, Node VM renderer tests, launchd dual-process Dashboard stack, existing `make acceptance` gate.

**Approved design:** `docs/superpowers/specs/2026-08-01-prediction-universe-retry-alert-design.md`

## Global Constraints

- Attempt 1 is the first failed refresh. Attempts 1–4 retry five seconds after that attempt completes. Attempt 5 latches exhaustion and permits no sixth refresh in that process.
- A successful refresh clears the consecutive count and exhaustion state, then restores the existing five-minute cadence.
- Keep the regular event loop alive between attempts; do not add an inner retry loop or `sleep(5)` inside `_refresh_universe_bounded()`.
- Keep standard YES/NO fail-closed on every universe failure. Cached rows remain read-only. Do not relax readiness, stale-book, breaker, preview, confirmation, or order checks.
- Restarting the process that owns `PolymarketMonitor` is the only reset after exhaustion. In the dual stack that is the Legacy Dashboard process, not the Gateway alone.
- Reuse the configured Feishu notifier. Do not add a notifier, table, configuration key, acknowledgement endpoint, retry button, generic retry framework, macOS alert, XiaoAI alert, or live test delivery.
- Preserve WebSocket connection/heartbeat semantics and LLM-hedge eligibility. A universe refresh failure must not falsely label an otherwise connected watcher as disconnected.
- Use deterministic recording notifiers in tests. Final acceptance must not send a real Feishu notification.
- Run focused tests while developing. Finalize and commit source, tests, and `CHANGELOG.md` before running `make acceptance`; run that gate only at the final candidate.
- Preserve unrelated changes in `/Users/ray/projects/open_trader`. All work stays in this isolated worktree and branch. Do not merge to `main` without separate user authorization.

---

### Task 1: Make universe refresh cadence recover quickly and latch after attempt five

**Files:**

- Modify: `src/open_trader/polymarket_monitor.py`
- Test: `tests/test_polymarket_monitor.py`

- [ ] **Step 1: Write failing tests for exact retry cadence and the five-attempt latch**

Add tests around one new private seam:

```python
async def _refresh_universe_if_due(
    self,
    client: object,
    *,
    current: float,
    next_refresh: float,
) -> tuple[float, bool]:
    """Run at most one due refresh and return (next_due, succeeded)."""
```

Define `class TransportError(RuntimeError): pass` in the test module, stub `_refresh_universe_bounded()` to raise it, patch `time.monotonic()` to the completion times `0, 5, 10, 15, 20`, and call the seam with due times through a sixth due check. Assert:

```python
assert refresh_calls == 5
assert due_times[:4] == [5.0, 10.0, 15.0, 20.0]
assert snapshot["health"]["universe_refresh_attempts"] == 5
assert snapshot["health"]["universe_retry_exhausted"] is True
assert "universe_retry_exhausted" in snapshot["health"]["degraded_reasons"]
assert "universe_refresh_failed" not in snapshot["health"]["degraded_reasons"]
```

The sixth call must return without invoking `_refresh_universe_bounded()` even when its `current` value reaches the returned due time.

Add a second test where three failures are followed by one success. Patch the completion time of the success to `15.0` and assert:

```python
assert next_refresh == 15.0 + UNIVERSE_REFRESH_SECONDS
assert succeeded is True
assert health["universe_refresh_attempts"] == 0
assert health["universe_retry_exhausted"] is False
assert "universe_refresh_failed" not in health["degraded_reasons"]
assert "universe_retry_exhausted" not in health["degraded_reasons"]
```

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_polymarket_monitor.py \
  -k 'universe_retry or universe_refresh_recovers' -q
```

Expected: FAIL because the retry fields and single-attempt scheduling seam do not exist.

- [ ] **Step 2: Implement the smallest process-local retry state**

Add only these constants beside the current universe cadence:

```python
UNIVERSE_RETRY_SECONDS = 5
UNIVERSE_MAX_ATTEMPTS = 5
```

Initialize:

```python
self._universe_refresh_attempts = 0
self._universe_retry_exhausted = False
```

Implement `_refresh_universe_if_due()` with this exact policy:

1. Return `(next_refresh, False)` without calling the client when exhausted or not yet due.
2. Await `_refresh_universe_bounded(client)` once.
3. On failure, call the existing sanitized `_record_error(exc, "universe")`, increment the count up to five, and schedule the next due time from a fresh `time.monotonic()` reading plus five seconds.
4. On attempt five, set exhaustion and do not allow another client call. The returned due time is immaterial after the latch, but keep it finite for simple diagnostics.
5. On success, clear `_universe_failed`, the count, and exhaustion, and return a fresh completion time plus `UNIVERSE_REFRESH_SECONDS` with `succeeded=True`.

Replace only the existing universe-refresh branch in `run_forever()`:

```python
next_refresh, universe_refreshed = await self._refresh_universe_if_due(
    client,
    current=current,
    next_refresh=next_refresh,
)
if universe_refreshed:
    next_readiness_refresh = time.monotonic() + READINESS_REFRESH_SECONDS
```

Do not retain `self._universe_at is None` as a bypass condition; it would defeat the five-second due time during startup failures.

- [ ] **Step 3: Project health without weakening fail-closed behavior**

Add to `_health()`:

```python
"universe_refresh_attempts": self._universe_refresh_attempts,
"universe_retry_exhausted": self._universe_retry_exhausted,
```

Choose the degraded reason as:

```python
if self._universe_retry_exhausted:
    reasons.append("universe_retry_exhausted")
elif self._universe_failed:
    reasons.append("universe_refresh_failed")
```

Do not change `snapshot()` actionability logic: any degraded monitor continues to force standard YES/NO opportunities and event markets to `actionable=False`.

- [ ] **Step 4: Run the focused monitor suite and commit**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_polymarket_monitor.py -q
```

Expected: `89` baseline tests plus the new retry tests all PASS; record the actual count shown by pytest.

```bash
git add src/open_trader/polymarket_monitor.py tests/test_polymarket_monitor.py
git commit -m "fix: retry prediction universe refreshes"
```

---

### Task 2: Notify exactly once when the monitor enters exhausted state

**Files:**

- Modify: `src/open_trader/polymarket_monitor.py`
- Modify: `src/open_trader/prediction_arbitrage_execution.py`
- Test: `tests/test_polymarket_monitor.py`
- Test: `tests/test_prediction_arbitrage_execution.py`

- [ ] **Step 1: Write failing monitor-observer tests**

Add a failure observer beside the ready observer with this interface:

```python
def set_failure_observer(
    self,
    observer: Callable[[Mapping[str, object]], Mapping[str, object] | object],
) -> None:
    ...
```

In an async test, attach a recording observer, drive five failures through `_refresh_universe_if_due()`, yield to the event loop, and reap the task. Assert one call with exactly these facts:

```python
assert calls == [{
    "attempts": 5,
    "error_type": "TransportError",
    "last_success_at": "2026-08-01T12:00:00+00:00",
}]
```

Drive a sixth due check and reap again; the call count must remain one. Repeat with `_universe_at = None` and assert `last_success_at is None`.

Add a task-lifecycle test that starts a slow failure observer, stops `run_forever()`, and asserts the failure-notification task is cancelled/reaped without an unhandled-task warning.

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_polymarket_monitor.py -k 'failure_observer or failure_notification' -q
```

Expected: FAIL because the observer and task do not exist.

- [ ] **Step 2: Schedule the observer without blocking the watcher**

Add only these fields:

```python
self._failure_observer = None
self._universe_failure_notification_scheduled = False
self._universe_failure_notification_task: asyncio.Task[object] | None = None
self._diagnostics["universe_notification_error"] = None
```

On the transition from attempt four to exhausted attempt five:

```python
payload = {
    "attempts": UNIVERSE_MAX_ATTEMPTS,
    "error_type": type(exc).__name__,
    "last_success_at": (
        self._universe_at.isoformat() if self._universe_at is not None else None
    ),
}
self._universe_failure_notification_scheduled = True
self._universe_failure_notification_task = asyncio.create_task(
    asyncio.to_thread(observer, payload)
)
```

Guard scheduling with the process-local boolean so the observer is called once per exhausted episode. Reap the task at the top of the normal watcher loop. If the task raises, store only the exception class in `diagnostics.universe_notification_error`. If it returns a mapping whose `state` is not `sent`, store only its short `reason` code, never free-form exception text or credentials. Notification failure must not clear exhaustion or resume retries.

Add `_universe_failure_notification_task` to the existing `finally` cancellation tuple.

- [ ] **Step 3: Write failing execution-service tests for exact Feishu-only content**

Add:

```python
def test_notify_monitor_failure_uses_feishu_only_and_operator_copy(
    tmp_path: Path,
) -> None:
    service, _trading, _store, _monitor, macos, feishu = (
        standard_notification_fixture(tmp_path)
    )

    result = service.notify_monitor_failure({
        "attempts": 5,
        "error_type": "TransportError",
        "last_success_at": "2026-08-01T12:00:00+00:00",
    })

    assert result == {"state": "sent"}
    assert macos.calls == 0
    assert feishu.calls == 1
    title, message = feishu.messages[-1]
    assert title == "预测市场监控需要人工干预"
    assert "连续 5 次刷新失败" in message
    assert "自动重试已停止" in message
    assert "TransportError" in message
    assert "2026-08-01T12:00:00+00:00" in message
    assert "Dashboard：http://127.0.0.1:8766/" in message
    assert "重启承载预测监控的 Dashboard 服务" in message
    assert "Polymarket 连接" in message
```

Add cases for `last_success_at=None` rendering `从未成功`, Feishu delivery failure returning `{"state": "failed", "reason": "notification_failed"}`, and an invalid error type such as `"TransportError: secret"` rendering `unknown_error` without the supplied string.

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_prediction_arbitrage_execution.py -k notify_monitor_failure -q
```

Expected: FAIL because `notify_monitor_failure()` does not exist.

- [ ] **Step 4: Implement the notification method by reusing Feishu delivery**

Add public synchronous method:

```python
def notify_monitor_failure(
    self, failure: Mapping[str, object]
) -> dict[str, object]:
```

Accept an error type only when it fully matches `[A-Za-z_][A-Za-z0-9_]{0,79}`; otherwise use `unknown_error`. Format the exact title and these body lines:

```text
监控市场连续 5 次刷新失败，自动重试已停止。
最后错误：TransportError
上次成功刷新：2026-08-01T12:00:00+00:00
Dashboard：http://127.0.0.1:8766/
请重启承载预测监控的 Dashboard 服务，并检查 Polymarket 连接。
```

Use `从未成功` when the last-success value is absent. Call only `_deliver_feishu_notification(title, message)`. Return `{"state": "sent"}` on success or `{"state": "failed", "reason": "notification_failed"}` on failure. Do not route through signal leases, order preflight, macOS notification, or a new renderer/framework.

- [ ] **Step 5: Run focused monitor and notification regressions, then commit**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_polymarket_monitor.py \
  tests/test_prediction_arbitrage_execution.py \
  -k 'universe or failure_observer or failure_notification or notify_monitor_failure or notify_ready' -q
```

Expected: PASS, including unchanged signal-notification behavior.

```bash
git add src/open_trader/polymarket_monitor.py \
  src/open_trader/prediction_arbitrage_execution.py \
  tests/test_polymarket_monitor.py \
  tests/test_prediction_arbitrage_execution.py
git commit -m "feat: alert on exhausted universe retries"
```

---

### Task 3: Wire and render the retry state in the existing Dashboard

**Files:**

- Modify: `src/open_trader/dashboard_web.py`
- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Test: `tests/test_dashboard_web.py`

- [ ] **Step 1: Write failing runtime-wiring and API-projection tests**

Extend `test_prediction_arbitrage_configured_lifecycle_reconciles_before_start_and_stops` so `FakeMonitor` records both observers:

```python
def set_failure_observer(self, observer: object) -> None:
    self.__class__.failure_observer = observer
```

Give `FakeExecution` a `notify_monitor_failure()` method and assert:

```python
assert callable(FakeMonitor.observer)
assert callable(FakeMonitor.failure_observer)
```

In `test_prediction_arbitrage_projects_live_monitor_and_store_rows_for_ui`, add to the fake monitor health and its exact expected projection:

```python
"universe_refresh_attempts": 4,
"universe_retry_exhausted": False,
```

This proves `_prediction_state_payload()` preserves both fields without introducing a second state model.

- [ ] **Step 2: Write failing JavaScript renderer tests for transient and permanent copy**

Add one Node renderer test with connected watcher state and two payloads:

```javascript
const retrying = {
  status:"degraded", stale:true,
  health:{
    status:"degraded",
    degraded_reasons:["universe_refresh_failed"],
    universe_refresh_attempts:3,
    universe_retry_exhausted:false,
  },
  relation_discovery:{websocket:{status:"connected",last_message_age_seconds:0.4}},
  readiness:{status:"ready"}, breaker:{open:false},
};
const exhausted = {
  ...retrying,
  health:{
    ...retrying.health,
    degraded_reasons:["universe_retry_exhausted"],
    universe_refresh_attempts:5,
    universe_retry_exhausted:true,
  },
};
```

Assert the rendered alert contains exactly:

```python
assert "监控市场刷新失败，正在自动重试（3/5）" in rendered["retrying"]
assert "监控市场连续 5 次刷新失败，已停止自动重试；请重启承载预测监控的 Dashboard 服务并检查 Polymarket 连接。" in rendered["exhausted"]
assert "Watcher 正常" in rendered["retryingHeader"]
assert "Watcher 正常" in rendered["exhaustedHeader"]
```

Extend `test_prediction_llm_trading_health_is_independent_from_top_twenty_refresh` to assert `universe_retry_exhausted` remains a Top-20-only reason: YES/NO is unavailable, but otherwise healthy LLM hedge remains available.

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_dashboard_web.py \
  -k 'prediction and (retry or lifecycle or projects_live_monitor or llm_trading_health)' -q
```

Expected: FAIL because the observer is not wired and the new reason/copy is unsupported.

- [ ] **Step 3: Wire the existing service to the monitor**

Immediately after the existing ready-observer binding in `serve_dashboard()` add:

```python
prediction_monitor.set_failure_observer(
    prediction_execution.notify_monitor_failure
)
```

Do not construct another service or notifier. Gateway wiring remains unchanged because Legacy owns the monitor.

- [ ] **Step 4: Render retry-specific copy before the generic stale alert**

In `predictionFailureReasonLabel()`, add:

```javascript
universe_retry_exhausted: "监控市场连续刷新失败，已停止自动重试",
```

In `predictionTradingAvailable()`, add `universe_retry_exhausted` to `topTwentyOnlyReasons` so the existing LLM-hedge independence is preserved.

In the stale branch of `predictionExecutionAlert()`, inspect the complete `health.degraded_reasons` list rather than relying only on the first failure reason:

```javascript
const universeAttempts = Number(payload?.health?.universe_refresh_attempts || 0);
const universeReasons = Array.isArray(payload?.health?.degraded_reasons)
  ? payload.health.degraded_reasons.map((reason) => String(reason || ""))
  : [];
const universeExhausted = payload?.health?.universe_retry_exhausted === true
  || universeReasons.includes("universe_retry_exhausted");
```

Render the exact permanent copy first when exhausted. Otherwise, when attempts are 1–4 and `universe_refresh_failed` is present, render `监控市场刷新失败，正在自动重试（x/5）`. Keep the existing `当前盘口暂不可交易`/connection-error fallback for every other stale condition, the existing danger styling, and the `失败关闭` pill.

- [ ] **Step 5: Run Dashboard-focused tests and commit**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_dashboard_web.py \
  -k 'prediction' -q
```

Expected: PASS, including existing WebSocket-connected and LLM-hedge independence cases.

```bash
git add src/open_trader/dashboard_web.py \
  src/open_trader/dashboard_static/dashboard.js \
  tests/test_dashboard_web.py
git commit -m "feat: show prediction universe retry state"
```

---

### Task 4: Prove the complete behavior without external notification delivery

**Files:**

- Modify: `CHANGELOG.md`
- Test: `tests/test_polymarket_monitor.py`
- Test: `tests/test_prediction_arbitrage_execution.py`
- Test: `tests/test_dashboard_web.py`

- [ ] **Step 1: Run all three focused suites together**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_polymarket_monitor.py \
  tests/test_prediction_arbitrage_execution.py \
  tests/test_dashboard_web.py -q
```

Expected: all tests PASS. Record the exact count and duration. This run uses only recording notifiers and must show no real Feishu delivery.

- [ ] **Step 2: Run a deterministic five-failure workflow directly**

Use a short Python diagnostic from the worktree that constructs `PolymarketMonitor` with the test store/fakes, attaches a recording failure observer, drives `_refresh_universe_if_due()` through five raised `TransportError` instances and a sixth due check, and prints only:

```text
refresh_calls=5
attempts=5
exhausted=true
observer_calls=1
reason=universe_retry_exhausted
```

Assert those values in the script before printing. Do not use production credentials, submit orders, or instantiate the live Feishu notifier.

- [ ] **Step 3: Add the dated operator-facing changelog entry**

Under `## 2026-08-01`, add:

```markdown
- 预测市场 Top 20 监控列表刷新失败后改为每 5 秒自动重试；任一次成功即恢复正常 5 分钟节奏，连续 5 次失败后停止重试、保持 YES/NO 失败关闭，并通过 Feishu 提醒人工重启承载预测监控的 Dashboard 服务。
```

Do not claim final acceptance or live notification delivery in the changelog.

- [ ] **Step 4: Commit the final candidate and confirm it is clean**

```bash
git add CHANGELOG.md
git commit -m "docs: record prediction universe retry recovery"
git status --short
git rev-parse HEAD
```

Expected: no tracked or untracked implementation residue. Record the SHA as `CANDIDATE_SHA`.

---

### Task 5: Deploy the candidate, run the final gate once, and redeploy the accepted SHA

**Files:**

- No source edits after `CANDIDATE_SHA` is recorded.
- Runtime logs under `logs/frontend_gateway/` and `logs/legacy_dashboard/` are evidence, not commit inputs.

- [ ] **Step 1: Preflight and deploy the candidate dual-process stack**

Ensure the worktree can use the shared virtual environment and prediction configuration without copying secrets into Git. If the prediction config is intentionally ignored and absent in the worktree, link only that existing local file from the main runtime path.

Run:

```bash
scripts/install_dashboard_launchd.sh --dry-run \
  --repo-root "$PWD" \
  --runtime-root /Users/ray/projects/open_trader \
  --python /Users/ray/projects/open_trader/.venv/bin/python

scripts/install_dashboard_launchd.sh \
  --repo-root "$PWD" \
  --runtime-root /Users/ray/projects/open_trader \
  --python /Users/ray/projects/open_trader/.venv/bin/python \
  --wait-seconds 60
```

Expected: Legacy becomes healthy on `8767`, Gateway reports healthy upstream on `8766`, and both listeners belong to the known launchd jobs. This deployment itself must not trigger or simulate five failures and must not send a notification.

- [ ] **Step 2: Verify candidate runtime facts and live state**

```bash
launchctl print "gui/$UID/com.open-trader.legacy-dashboard"
launchctl print "gui/$UID/com.open-trader.frontend-gateway"
lsof -nP -iTCP:8766 -sTCP:LISTEN
lsof -nP -iTCP:8767 -sTCP:LISTEN
curl -fsS http://127.0.0.1:8766/healthz
curl -fsS http://127.0.0.1:8767/healthz
curl -fsS http://127.0.0.1:8766/api/prediction-arbitrage/state
tail -n 100 logs/frontend_gateway/launchd.out.log
tail -n 100 logs/legacy_dashboard/launchd.out.log
tail -n 100 logs/legacy_dashboard/launchd.err.log
```

Verify each process PID, cwd, `git_sha == CANDIDATE_SHA`, source state, fresh startup timestamp, no traceback, Gateway `upstream_status == "ok"`, and API health includes integer `universe_refresh_attempts` plus boolean `universe_retry_exhausted`.

- [ ] **Step 3: Run the one final Dashboard acceptance gate**

Only now run:

```bash
DASHBOARD_URL=http://127.0.0.1:8766 \
DASHBOARD_LOG="$PWD/logs/legacy_dashboard/launchd.out.log" \
make acceptance
```

Expected terminal result: `PASS`.

- On `FAIL`, diagnose and fix, update tests/changelog if needed, commit a new candidate, redeploy it, and rerun the gate.
- On `BLOCKED`, report the blocker; do not substitute curl, fixtures, mocks, or screenshots.
- Only `PASS` establishes `ACCEPTED_SHA=$(git rev-parse HEAD)`.

- [ ] **Step 4: Redeploy the exact accepted SHA with no source or data edits**

Run the same non-dry-run installer command from Step 1. Then repeat the PID/cwd/SHA/log/health checks from Step 2 and verify:

- both PIDs are fresh after redeployment;
- Legacy and Gateway both run from this worktree at `ACCEPTED_SHA`;
- Legacy fresh logs show no traceback and owns `PolymarketMonitor`;
- Gateway health reports upstream `ok`;
- `http://127.0.0.1:8766/` returns HTTP `200`.

Provide `http://127.0.0.1:8766/` for user review only after all checks pass. Do not merge the branch unless the user separately authorizes the merge.
