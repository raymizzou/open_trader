# Trend Review Execution Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every valid trend report action execute exactly once in its own market session, reconcile against Futu historical orders, persist immutable review evidence, and expose the confirmed compact execution status in Dashboard.

**Architecture:** Keep `trend_review.py` as the workflow boundary and extend the existing Futu execution adapter instead of adding a second execution system. Stable per-market/date/symbol/action keys make report revisions safe; append-only event files record state transitions while broker history remains the execution authority. Watchers call the idempotent workflow on every poll, and Dashboard projects the same ledger into the existing action rows.

**Tech Stack:** Python 3.13, pytest, Futu OpenAPI, existing JSON immutable ledger, vanilla JavaScript/CSS Dashboard, launchd/screen deployment scripts.

## Global Constraints

- Start from local `main` in the isolated `fix/trend-review-execution-closure` worktree.
- Use the report payload `execution_date`, never the report filename, as the execution authority.
- A report revision may change only symbols without an intent; any symbol with an intent is frozen.
- BUY uses a market order and live price for quantity; SELL_ALL sells the whole remaining position.
- A completed action never auto-corrects after a report revision and cannot re-enter on the same date.
- Partial BUY fills top up only the remaining target before the buy window ends; incomplete sells continue across dates.
- Persist immutable facts; never overwrite prior status evidence or invent missing close NAV.
- Strategy upgrades occur only while the market is closed; an open position retains entry attribution and never lowers its protection line.
- Dashboard adds only the approved B execution-detail row and must not horizontally overflow at 375px.
- Do not run `make acceptance` until the final gate.
- Do not create test orders; the only real order allowed is an action required by a valid report.

---

### Task 1: Historical Futu Order Facts

**Files:**
- Modify: `src/open_trader/kelly_order_execution.py`
- Modify: `tests/test_kelly_order_execution.py`

**Interfaces:**
- Consumes: the existing selected simulated account in `FutuSimulateOrderExecutionClient.account`.
- Produces: `FutuSimulateOrderExecutionClient.list_orders(*, start: str | None = None, end: str | None = None) -> dict[str, Any]`, returning normalized `orders` from `history_order_list_query`; `MarketRoutingOrderExecutionClient.list_orders(...)` routes by market.

- [ ] **Step 1: Write failing adapter tests**

Add a fake context whose `order_list_query` is empty and whose `history_order_list_query` returns a `FILLED_ALL` order. Assert `list_orders(start="2026-07-17", end="2026-07-17")` calls history with `trd_env="SIMULATE"`, the selected `acc_id`/`acc_index`, `start`, and `end`, and returns the completed order. Add an error test asserting a non-zero broker code raises `FutuOrderExecutionError(error_type="history_order_list_query_failed")`.

- [ ] **Step 2: Confirm the tests fail**

Run: `pytest tests/test_kelly_order_execution.py -k 'history_order' -v`

Expected: FAIL because `list_orders` still calls `order_list_query` and does not accept a date range.

- [ ] **Step 3: Implement the historical query**

Change the adapter to call:

```python
ret_code, data = self.context.history_order_list_query(
    start=start,
    end=end,
    trd_env=TRD_ENV_SIMULATE,
    acc_id=self.account["acc_id"],
    acc_index=self.account["acc_index"],
)
```

Omit `start` and `end` from kwargs only when both are absent. Preserve raw broker fields including `order_id`, `order_status`, `qty`, `dealt_qty`, `dealt_avg_price`, `code`, `trd_side`, `remark`, `create_time`, and `updated_time`; do not manufacture fees.

- [ ] **Step 4: Run focused and neighboring tests**

Run: `pytest tests/test_kelly_order_execution.py tests/test_trend_review.py -v`

Expected: PASS.

- [ ] **Step 5: Commit the broker fact adapter**

```bash
git add src/open_trader/kelly_order_execution.py tests/test_kelly_order_execution.py
git commit -m "fix: reconcile trend orders from Futu history"
```

### Task 2: Revision-Safe BUY and SELL_ALL Closure

**Files:**
- Modify: `src/open_trader/trend_review.py`
- Modify: `src/open_trader/cli.py`
- Modify: `tests/test_trend_review.py`
- Modify: `tests/test_premarket_cli.py`

**Interfaces:**
- Consumes: `client.account_snapshot()`, `client.list_orders(start=..., end=...)`, `client.place_order(request)`, report `execution_date`, `strategy_judgments.formal_actions`, and market-local `now`.
- Produces: stable action key `sha256(f"{market}:{execution_date}:{futu_code}:{side}")`; append-only files under `data/trend_review/ledgers/<market>/actions/<execution_date>/<action-key>/`; `execute_trend_review_open(...)` handling BUY and SELL_ALL with immutable status events.

- [ ] **Step 1: Add failing action identity and revision tests**

In `tests/test_trend_review.py`, add cases proving a `2026-07-16-r3.json` payload with `execution_date="2026-07-17"` executes on the US-local 2026-07-17 session even when Shanghai is already 2026-07-18; a later numeric revision cannot duplicate an existing NDAQ intent; a removed completed action remains projected as `early_revision_executed`; and a new symbol in the revision may execute.

- [ ] **Step 2: Add failing BUY/SELL conflict and fill tests**

Add tests where BUY creates one `MARKET` request, SELL_ALL creates a full-position `MARKET` sell, same-symbol BUY+SELL suppresses BUY, partial BUY history submits only `target_qty - cumulative_dealt_qty`, the completed target does not resubmit, buy-window expiry records `missed`, and a partial SELL_ALL continues on the following market date.

- [ ] **Step 3: Confirm workflow tests fail**

Run: `pytest tests/test_trend_review.py -k 'revision or execution_date or sell_all or partial or conflict or missed' -v`

Expected: FAIL on report-hash identity, US date comparison, absent SELL_ALL execution, and absent partial-fill reconciliation.

- [ ] **Step 4: Implement stable intent and append-only events**

Add private helpers with these signatures:

```python
def _action_key(market: str, execution_date: str, futu_code: str, side: str) -> str:
    identity = f"{market}:{execution_date}:{futu_code.upper()}:{side.upper()}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _broker_order_state(order: Mapping[str, object]) -> str:
    raw = str(order.get("order_status") or order.get("status") or "").upper()
    return {
        "SUBMITTING": "submitted",
        "SUBMITTED": "submitted",
        "FILLED_PART": "partially_filled",
        "FILLED_ALL": "filled",
        "FAILED": "failed",
        "DISABLED": "failed",
        "DELETED": "failed",
    }.get(raw, "pending")


def _append_action_event(
    root: Path, action_key: str, payload: Mapping[str, object]
) -> Path:
    body = _canonical_json_bytes(payload)
    timestamp = str(payload["recorded_at"]).replace(":", "-")
    path = root / action_key / f"{timestamp}-{hashlib.sha256(body).hexdigest()[:12]}.json"
    return _write_immutable(path, body)


def _market_execution_date(market: str, now: datetime) -> str:
    zone = {
        "CN": ZoneInfo("Asia/Shanghai"),
        "HK": ZoneInfo("Asia/Hong_Kong"),
        "US": ZoneInfo("America/New_York"),
    }[market]
    aware = now if now.tzinfo is not None else now.replace(tzinfo=zone)
    return aware.astimezone(zone).date().isoformat()
```

Use `ZoneInfo("America/New_York")` for US, `ZoneInfo("Asia/Hong_Kong")` for HK, and `ZoneInfo("Asia/Shanghai")` for CN. Event filenames are `<UTC timestamp>-<sha256(payload)[:12]>.json`; `_write_immutable` enforces append-only storage. Store report revision/hash on each event, but never include it in the action key or broker remark.

- [ ] **Step 5: Implement exact remaining-quantity execution**

For each stable action, reconcile all broker history rows matching remark/code/side. Derive `cumulative_dealt_qty` from distinct broker order IDs, compute BUY target once from the first intent’s NAV/live price/weight/lot, and submit only a positive remainder while the market window is valid. For SELL_ALL, read current position quantity each pass and submit that remainder; do not stop future passes because a prior-day sell was partial. Convert broker states into `pending`, `submitted`, `partially_filled`, `filled`, `failed`, `blocked`, `missed`, or `incomplete` events.

- [ ] **Step 6: Select reports by numeric revision and payload date**

In `src/open_trader/cli.py`, load every valid market report payload, filter by `execution_date`, order the remaining candidates by numeric `-rN` revision (plain filename is revision zero), and pass market-local `now` to `execute_trend_review_open`. Keep the latest valid revision per un-frozen symbol.

- [ ] **Step 7: Run the workflow and CLI suites**

Run: `pytest tests/test_trend_review.py tests/test_premarket_cli.py -v`

Expected: PASS.

- [ ] **Step 8: Commit the execution closure**

```bash
git add src/open_trader/trend_review.py src/open_trader/cli.py tests/test_trend_review.py tests/test_premarket_cli.py
git commit -m "fix: close trend report execution loop"
```

### Task 3: Watcher Compensation, Close Capture, and Notifications

**Files:**
- Modify: `src/open_trader/a_share_trend_watch.py`
- Modify: `src/open_trader/market_trend_watch.py`
- Modify: `src/open_trader/cli.py`
- Modify: `src/open_trader/trend_review.py`
- Modify: `tests/test_a_share_trend_watch.py`
- Modify: `tests/test_market_trend_watch.py`
- Modify: `tests/test_premarket_cli.py`
- Modify: `tests/test_trend_review.py`

**Interfaces:**
- Consumes: idempotent `run_trend_review_open(config, market, trading_date)` and history-backed `run_trend_review_close(...)`.
- Produces: every-poll compensation, immutable missing-close facts, deadline/failure notifications, and watcher events containing callback result, PID, start time, and Git SHA.

- [ ] **Step 1: Add failing watcher recovery tests**

Assert each watcher invokes the open callback again on a later poll after an initial callback failure and after restart; callback errors do not stop protection polling; a successful repeated callback creates no duplicate order; and watcher events include callback outcome. Assert explicit broker rejection notifies immediately and an unfinished action notifies at the earlier of buy-window end or market close minus 30 minutes.

- [ ] **Step 2: Add failing close-fact tests**

Assert `run_trend_review_close` reads historical orders for the trading date, freezes filled quantity/average/order ID/status, retries a missing NAV before the next open, writes an explicit `missing` close fact when recovery is impossible, and still permits later dates to enter projection metrics.

- [ ] **Step 3: Confirm watcher and close tests fail**

Run: `pytest tests/test_a_share_trend_watch.py tests/test_market_trend_watch.py tests/test_premarket_cli.py tests/test_trend_review.py -k 'callback or recovery or notification or close or missing' -v`

Expected: FAIL because the session callback is one-shot and close capture uses current orders.

- [ ] **Step 4: Make callbacks compensating and idempotent**

Call `_run_review_callback` on every eligible polling cycle, relying on the stable action ledger to avoid duplication. Record `compensation_scan_started`, `compensation_scan_succeeded`, or `compensation_scan_failed` in existing watcher event logs. On initialization record the actual PID, ISO start timestamp, current working directory, and `git rev-parse HEAD` value.

- [ ] **Step 5: Implement notification deadlines and close recovery**

Use the existing notifier path and a stable notification key `<market>:<date>:<action-key>:<condition>` so restarts cannot duplicate notices. Notify definitive rejects and system failures immediately; notify pending/partial actions at the configured deadline. Query broker history for close facts and preserve later dates when a prior daily fact says `status="missing"`.

- [ ] **Step 6: Run focused suites**

Run: `pytest tests/test_a_share_trend_watch.py tests/test_market_trend_watch.py tests/test_premarket_cli.py tests/test_trend_review.py -v`

Expected: PASS.

- [ ] **Step 7: Commit watcher recovery**

```bash
git add src/open_trader/a_share_trend_watch.py src/open_trader/market_trend_watch.py src/open_trader/cli.py src/open_trader/trend_review.py tests/test_a_share_trend_watch.py tests/test_market_trend_watch.py tests/test_premarket_cli.py tests/test_trend_review.py
git commit -m "fix: compensate trend execution on every watcher poll"
```

### Task 4: Compact Dashboard Execution Detail

**Files:**
- Modify: `src/open_trader/dashboard.py`
- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Modify: `src/open_trader/dashboard_static/dashboard.css`
- Modify: `tests/test_dashboard.py`
- Modify: `tests/test_dashboard_web.py`

**Interfaces:**
- Consumes: immutable action events and broker-derived fields from Tasks 1–3.
- Produces: each existing report action includes `execution.status`, `filled_qty`, `target_qty`, `avg_fill_price`, `order_ids`, `updated_at`, and `reason`; the existing action row renders one compact detail row beneath it.

- [ ] **Step 1: Add failing API projection tests**

Create ledger fixtures for pending, submitted, partial, filled, failed, blocked, missed, incomplete, and early-revision-executed states. Assert the latest immutable event is projected without dropping prior revision actions and without comparing unrelated actual/simulated/market series against each other.

- [ ] **Step 2: Add failing desktop/mobile rendering tests**

Assert the action row contains Chinese labels `待执行`, `已提交`, `部分成交`, `全部成交`, `失败`, `受阻`, `错过`, `未完成`, or `早期版本已执行` as applicable; detail text shows fill progress, average fill price, order ID, time, and failure reason only when present. Assert the approved section introduces no new card or button and the 375px viewport has `scrollWidth <= clientWidth`.

- [ ] **Step 3: Confirm Dashboard tests fail**

Run: `pytest tests/test_dashboard.py tests/test_dashboard_web.py -k 'trend_review or execution or mobile' -v`

Expected: FAIL because execution detail is not in the projection or DOM.

- [ ] **Step 4: Implement only the approved B row**

Project the immutable event fields in `dashboard.py`. In `dashboard.js`, append a semantic detail row directly below each existing action row; omit absent values instead of showing placeholder cards. In `dashboard.css`, allow detail fields to wrap and use `min-width: 0`, `overflow-wrap: anywhere`, and a single-column mobile layout under the existing mobile breakpoint.

- [ ] **Step 5: Run Dashboard focused tests**

Run: `pytest tests/test_dashboard.py tests/test_dashboard_web.py -v`

Expected: PASS.

- [ ] **Step 6: Commit Dashboard status UI**

```bash
git add src/open_trader/dashboard.py src/open_trader/dashboard_static/dashboard.js src/open_trader/dashboard_static/dashboard.css tests/test_dashboard.py tests/test_dashboard_web.py
git commit -m "feat: show trend action execution status"
```

### Task 5: Acceptance Contract, Real NDAQ Reconciliation, and Exact-SHA Deployment

**Files:**
- Modify: `src/open_trader/dashboard_acceptance.py`
- Modify: `tests/test_dashboard_acceptance.py`
- Modify: `Makefile` only if the existing acceptance target cannot invoke the new checks.

**Interfaces:**
- Consumes: report loader, action ledger, Futu history, Dashboard API/DOM, watcher events, and service manager state.
- Produces: one final `PASS`, `FAIL`, or `BLOCKED` acceptance result with per-market evidence.

- [ ] **Step 1: Add failing acceptance tests**

Add fixtures proving: a normal day without a report is `FAIL`; a holiday with calendar evidence is allowed; before open `pending` passes; during/after the window a tradable action without a unique intent/order is `FAIL`; unavailable required browser/external environment is `BLOCKED`; every submitted action must match one history order and one Dashboard row; and three watcher PIDs/SHAs/compensation events are required.

- [ ] **Step 2: Confirm acceptance tests fail**

Run: `pytest tests/test_dashboard_acceptance.py -v`

Expected: FAIL on the newly required execution and process evidence.

- [ ] **Step 3: Implement strict acceptance checks**

Return `FAIL` for missing/duplicate/mismatched action evidence, stale process SHA, absent compensation event, absent post-close facts, browser overflow, or HTTP failure. Return `BLOCKED` only when the required browser or external service cannot be reached and the condition cannot be replaced with fixtures, curl, or screenshots.

- [ ] **Step 4: Run all automated tests**

Run: `pytest -q`

Expected: all tests PASS.

- [ ] **Step 5: Commit the acceptance contract**

```bash
git add src/open_trader/dashboard_acceptance.py tests/test_dashboard_acceptance.py Makefile
git commit -m "test: enforce trend execution acceptance"
```

- [ ] **Step 6: Reconcile the real 2026-07-17 NDAQ action**

First run `date` and a US-market-session check. If the 2026-07-17 US regular session is still open, run the real `trend-review open --market US --date 2026-07-17` workflow against the configured simulated account, then query `history_order_list_query` until NDAQ has a terminal or partial broker state. If that session has closed, run compensation only to append the immutable `missed` fact; never place the order on 2026-07-18 or a later US session.

Expected: one stable NDAQ action key with either a real broker order uniquely matched by ID or a `missed` event carrying the closed-window reason.

- [ ] **Step 7: Directly verify affected workflows**

Run the report loader for CN/HK/US, one compensation pass for each watcher, Futu historical-order reconciliation for each configured simulated account, the close projection for available closed sessions, and the Dashboard API. Inspect returned order IDs/statuses and fresh watcher log timestamps rather than relying only on test output.

Expected: every report action is pending before its window or has broker/blocked/missed evidence after it; no duplicates exist.

- [ ] **Step 8: Run the final Dashboard gate once the tree is final**

Run: `make acceptance`

Expected: `PASS`. A `FAIL` must be fixed and rerun; a `BLOCKED` must be reported as blocked and cannot be substituted by another check.

- [ ] **Step 9: Deploy the exact accepted Git SHA**

Commit any final source/test changes before the gate, capture `git rev-parse HEAD`, and redeploy that exact SHA. Restart the A-share, US, and HK watchers plus Dashboard; verify old PIDs exited; inspect new PID, working directory, SHA, start timestamp, compensation log event, and fresh logs for all four processes.

- [ ] **Step 10: Verify the review endpoint**

Request the deployed Dashboard review URL and require HTTP 200. Confirm the deployed process SHA equals the accepted SHA and no source or data changes occurred after acceptance.

Expected: exact accepted SHA live, three healthy watchers, fresh compensation events, Dashboard HTTP 200, and a directly openable review URL.
