# Trend Market Controller Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace six fragile trend report/watcher schedules with one self-reconciling controller per market that always generates the required report, executes safely on exactly one named host, and recovers without duplicate Futu simulated orders.

**Architecture:** A new `trend_market_controller` module is the only operational orchestrator for CN, HK, and US. It reuses the existing report generators, one-pass protection watchers, close capture, Futu client, immutable trend-review ledger, and notifier; durable reports, batch locks, action facts, broker facts, and manual resolutions—not process memory—determine every transition. `launchd` keeps one process per market alive, while an exact hostname allow-list and a second guard immediately before `place_order` make copied deployments read-only.

**Tech Stack:** Python 3.12, standard-library `dataclasses`/`datetime`/`decimal`/`hashlib`/`json`/`pathlib`/`socket`/`concurrent.futures`, existing Futu and Trend Animals adapters, immutable JSON facts, Bash + launchd plists, pytest, vanilla JavaScript/CSS, existing Dashboard browser acceptance.

## Global Constraints

- Start implementation from local `main` in the existing isolated worktree; preserve unrelated user changes.
- Ordinary HK and US premarket automation is unchanged.
- `OPEN_TRADER_TREND_EXECUTOR_HOST` is the only executor designation: exact local-host match means `execute`; missing or non-matching means `readonly`.
- Read-only hosts generate no trend report, run no trend controller, mutate no order, monitor no protection line, capture no close, and send no trend-task notification.
- Install one persistent controller per enabled market, never a global controller and never active-active execution.
- CN/HK buys remain limited to 09:30–10:00 local market time; US buys remain limited to the complete 09:30–16:00 regular session.
- A missing report is always regenerated for the same logical data/execution dates; a frozen report is never recomputed because notification delivery failed.
- The first eligible execution check immutably locks one report SHA. A later revision is visible as an anomaly but changes no automatic action.
- Stable action identity is `(market, execution_date, symbol, side)`; report SHA, strategy version, action index, and signal event ID are evidence only.
- Every broker attempt has a monotonic attempt number and a distinct deterministic Futu remark.
- An intent without a conclusive broker fact becomes `uncertain` and is never retried without an immutable explicit resolution.
- A partially filled buy is completed only inside its window and never above frozen shares, frozen amount, available cash, or frozen risk limits.
- Multiple same-day exit reasons for one symbol merge into one `SELL_ALL`; no overlapping sell is submitted while an earlier sell remains active or ambiguous.
- External dependency failure for the entire valid window becomes one durable `missed` outcome; no order is submitted outside the strategy window.
- An operator-authorized legacy cutover may skip only an expired, unreplayable cycle whose frozen report and pending revision request are bound by exact path and SHA-256; it creates no report, batch, action, notification, or broker mutation.
- Do not add a database, queue, distributed lock, leader election, automatic failover, real-money execution, or cross-machine artifact synchronization.
- Run focused tests and direct safe workflows during development. Do not run `make acceptance` during intermediate work; run it only as the final Dashboard gate after all source changes are committed, and rerun it only if a FAIL required a new fix/commit.
- Only an acceptance `PASS` permits completion language. After PASS, redeploy the exact accepted Git SHA and verify PID, working directory, Git SHA, fresh logs, heartbeat, and HTTP 200.

---

## File Map

- Create `src/open_trader/trend_market_controller.py`: market-date/session reconciliation, report supervision, batch selection, one-pass protection execution, close capture, status projection, and notification deduplication.
- Create `tests/test_trend_market_controller.py`: end-to-end durable state-transition tests through `run_trend_market_controller`.
- Create `tests/test_trend_market_cli.py`: the one operational command namespace and host guards.
- Modify `src/open_trader/daily_premarket.py`: executor-host configuration and effective-mode calculation.
- Modify `config/daily_premarket.env.example`: document the one local executor-host value.
- Modify `src/open_trader/trend_review.py`: stable action keys, numbered remarks, batch locks, broker-first reconciliation, conflict/uncertain facts, resolutions, capped partial retries, and merged sells.
- Modify `tests/test_trend_review.py`: focused action-ledger, reconciliation, partial-fill, resolution, and migration tests.
- Modify `src/open_trader/kelly_order_execution.py`: a small mutation-guarding wrapper around the existing Futu simulated client.
- Modify `tests/test_kelly_order_execution.py`: prove the guard is checked at the mutation boundary.
- Modify `src/open_trader/cli.py`: add `trend-market run/status/resolve`; remove the old report/watcher and trend-review open/close routes.
- Modify `scripts/install_daily_premarket_launchd.sh` and `scripts/uninstall_daily_premarket_launchd.sh`: fenced controller installation/removal based on effective host mode.
- Create `ops/launchd/com.open-trader.trend-market-controller.plist.template`: `RunAtLoad` + `KeepAlive` controller job.
- Delete the four split trend report/watch plist templates after migration tests pass.
- Modify `tests/test_daily_premarket.py`: configuration and launchd installer/uninstaller behavior.
- Modify `src/open_trader/dashboard.py`, `src/open_trader/dashboard_static/dashboard.js`, and `src/open_trader/dashboard_static/dashboard.css`: project and render controller health and terminal execution states.
- Modify `src/open_trader/dashboard_acceptance.py`, `tests/test_dashboard_web.py`, and `tests/test_dashboard_acceptance.py`: desktop/mobile controller-health acceptance.
- Modify `README.md`: configuration, operations, manual resolution, migration, rollback, and verification instructions.
- Modify `src/open_trader/trend_market_controller.py` and `tests/test_trend_market_controller.py`: one-time immutable legacy cutover facts for pre-controller expired reports that cannot be safely replayed.

---

### Task 1: Make executor authority explicit and testable

**Files:**
- Modify: `src/open_trader/daily_premarket.py:80-130,181-323`
- Modify: `config/daily_premarket.env.example:41-53`
- Test: `tests/test_daily_premarket.py:80-150,1356-1390`

**Interfaces:**
- Produces: `TrendExecutionMode(mode: Literal["execute", "readonly"], executor_host: str, local_host: str, reason: str)`.
- Produces: `trend_execution_mode(config: DailyPremarketConfig, *, hostname_fn: Callable[[], str] = socket.gethostname) -> TrendExecutionMode`.
- Produces: `require_trend_executor(config: DailyPremarketConfig, *, hostname_fn: Callable[[], str] = socket.gethostname) -> TrendExecutionMode`.
- Consumed by: controller, CLI resolution, Dashboard projection, guarded Futu adapter, and launchd installer semantics.

- [ ] **Step 1: Write failing configuration and mode tests**

Add tests that load an exact hostname, a mismatched hostname, and an absent hostname:

```python
def test_trend_execution_mode_requires_exact_named_host(tmp_path: Path) -> None:
    config = replace(_daily_config(tmp_path), trend_executor_host="ray-mac")

    assert trend_execution_mode(config, hostname_fn=lambda: "ray-mac").mode == "execute"
    mismatch = trend_execution_mode(config, hostname_fn=lambda: "laptop")
    assert mismatch.mode == "readonly"
    assert mismatch.reason == "local host does not match OPEN_TRADER_TREND_EXECUTOR_HOST"


def test_missing_executor_host_is_readonly(tmp_path: Path) -> None:
    mode = trend_execution_mode(_daily_config(tmp_path), hostname_fn=lambda: "ray-mac")
    assert mode.mode == "readonly"
    with pytest.raises(ValueError, match="trend automation is readonly"):
        require_trend_executor(_daily_config(tmp_path), hostname_fn=lambda: "ray-mac")
```

Extend the environment-loader fixture with `OPEN_TRADER_TREND_EXECUTOR_HOST=ray-mac` and assert `config.trend_executor_host == "ray-mac"`. Add a second loader test proving the key is optional and defaults to the empty string.

- [ ] **Step 2: Run the focused tests and confirm the red state**

Run:

```bash
.venv/bin/python -m pytest tests/test_daily_premarket.py -k 'executor_host or execution_mode' -q
```

Expected: FAIL because the field, dataclass, and functions do not exist.

- [ ] **Step 3: Implement the minimal mode boundary**

Add the field at the end of `DailyPremarketConfig` so existing keyword construction stays compatible, parse the optional environment value, and add:

```python
@dataclass(frozen=True)
class TrendExecutionMode:
    mode: Literal["execute", "readonly"]
    executor_host: str
    local_host: str
    reason: str


def trend_execution_mode(
    config: DailyPremarketConfig,
    *,
    hostname_fn: Callable[[], str] = socket.gethostname,
) -> TrendExecutionMode:
    executor = config.trend_executor_host.strip()
    local = hostname_fn().strip()
    if executor and local == executor:
        return TrendExecutionMode("execute", executor, local, "executor host matched")
    reason = (
        "OPEN_TRADER_TREND_EXECUTOR_HOST is not configured"
        if not executor
        else "local host does not match OPEN_TRADER_TREND_EXECUTOR_HOST"
    )
    return TrendExecutionMode("readonly", executor, local, reason)


def require_trend_executor(
    config: DailyPremarketConfig,
    *,
    hostname_fn: Callable[[], str] = socket.gethostname,
) -> TrendExecutionMode:
    mode = trend_execution_mode(config, hostname_fn=hostname_fn)
    if mode.mode != "execute":
        raise ValueError(f"trend automation is readonly: {mode.reason}")
    return mode
```

Add `OPEN_TRADER_TREND_EXECUTOR_HOST=` to the example config with a comment that only the one execution machine sets its exact `socket.gethostname()` value; copied deployments leave it absent or non-matching.

- [ ] **Step 4: Run the focused tests and commit**

Run:

```bash
.venv/bin/python -m pytest tests/test_daily_premarket.py -k 'executor_host or execution_mode or require_trend_review_config' -q
git add src/open_trader/daily_premarket.py config/daily_premarket.env.example tests/test_daily_premarket.py
git commit -m "feat: define trend executor host mode"
```

Expected: selected tests PASS; commit contains only configuration/mode behavior.

---

### Task 2: Make the immutable action ledger broker-reconcilable

**Files:**
- Modify: `src/open_trader/trend_review.py:272-455,513-1012`
- Modify: `src/open_trader/kelly_order_execution.py:35-145`
- Test: `tests/test_trend_review.py:550-900,1275-1485`
- Test: `tests/test_kelly_order_execution.py`

**Interfaces:**
- Produces: `trend_action_key(market: str, execution_date: str, futu_code: str, side: str) -> str`.
- Produces: `trend_attempt_remark(market: str, execution_date: str, action_key: str, attempt: int) -> str`.
- Produces: `lock_trend_execution_batch(data_dir: Path, *, market: str, execution_date: str, report_path: Path, report: Mapping[str, object], locked_at: str) -> dict[str, object]`.
- Produces: `ExecutorGuardedOrderClient(delegate: object, authorize: Callable[[], object])`, delegating reads and guarding `place_order` immediately before the broker call.
- Preserves: `execute_trend_review_open(*, data_dir: Path, report: Mapping[str, object], client: object, market: str, execution_date: str, now: str, quote_prices: Mapping[str, Decimal]) -> dict[str, object]`, now consuming only the already locked report chosen by the controller.

- [ ] **Step 1: Add failing tests for identity, batch locking, and numbered remarks**

Add these assertions to `tests/test_trend_review.py`:

```python
def test_action_identity_ignores_report_revision_and_strategy_version() -> None:
    first = trend_review.trend_action_key("US", "2026-07-20", "US.TRV", "buy")
    second = trend_review.trend_action_key("US", "2026-07-20", "us.trv", "BUY")
    assert first == second
    assert trend_review.trend_attempt_remark("US", "2026-07-20", first, 1) != \
        trend_review.trend_attempt_remark("US", "2026-07-20", first, 2)


def test_execution_batch_keeps_first_report_sha(tmp_path: Path) -> None:
    first = cn_buy_report()
    revised = {**cn_buy_report(), "generated_at": "2026-07-20T08:59:00+08:00"}
    locked = trend_review.lock_trend_execution_batch(
        tmp_path, market="CN", execution_date="2026-07-20",
        report_path=tmp_path / "2026-07-17.json", report=first,
        locked_at="2026-07-20T09:30:00+08:00",
    )
    repeated = trend_review.lock_trend_execution_batch(
        tmp_path, market="CN", execution_date="2026-07-20",
        report_path=tmp_path / "2026-07-17-r1.json", report=revised,
        locked_at="2026-07-20T09:31:00+08:00",
    )
    assert repeated == locked
    assert repeated["report_sha256"] == trend_review._report_hash(first)
```

Change the existing partial-fill test to require attempt 2 to have a different remark while preserving the same action key.

- [ ] **Step 2: Add failing broker-reconciliation tests**

Cover the three broker-first branches:

```python
def test_existing_exact_broker_order_repairs_result_without_submit(tmp_path: Path) -> None:
    # No local ledger simulates sequential migration to this machine.
    report = cn_buy_report()
    action_key = trend_review.trend_action_key(
        "CN", "2026-07-20", "SH.600001", "buy"
    )
    client = FakeTrendSimClient()
    client.orders = [{
        "order_id": "SIM-EXISTING",
        "remark": trend_review.trend_attempt_remark(
            "CN", "2026-07-20", action_key, 1
        ),
        "code": "SH.600001",
        "trd_side": "BUY",
        "qty": "400",
        "dealt_qty": "0",
        "order_status": "SUBMITTED",
    }]
    result = trend_review.execute_trend_review_open(
        data_dir=tmp_path, report=report, client=client,
        market="CN", execution_date="2026-07-20",
        now="2026-07-20T09:31:00+08:00",
        quote_prices={"SH.600001": Decimal("10")},
    )
    assert result["submitted_count"] == 0
    assert client.requests == []
    assert list(tmp_path.glob("trend_review/ledgers/US/open/2026-07-20/*-result.json"))


def test_same_remark_with_conflicting_quantity_fails_closed(tmp_path: Path) -> None:
    report = cn_buy_report()
    action_key = trend_review.trend_action_key(
        "CN", "2026-07-20", "SH.600001", "buy"
    )
    client = FakeTrendSimClient()
    client.orders = [{
        "order_id": "SIM-CONFLICT",
        "remark": trend_review.trend_attempt_remark(
            "CN", "2026-07-20", action_key, 1
        ),
        "code": "SH.600001", "trd_side": "BUY", "qty": "999",
        "dealt_qty": "0", "order_status": "SUBMITTED",
    }]
    result = trend_review.execute_trend_review_open(
        data_dir=tmp_path, report=report, client=client,
        market="CN", execution_date="2026-07-20",
        now="2026-07-20T09:31:00+08:00",
        quote_prices={"SH.600001": Decimal("10")},
    )
    assert result["status"] == "conflict"
    assert client.requests == []


def test_intent_without_broker_fact_becomes_uncertain_and_never_resubmits(tmp_path: Path) -> None:
    client = FakeTrendSimClient(fail_orders=1)
    arguments = {
        "data_dir": tmp_path,
        "report": cn_buy_report(),
        "client": client,
        "market": "CN",
        "execution_date": "2026-07-20",
        "now": "2026-07-20T09:31:00+08:00",
        "quote_prices": {"SH.600001": Decimal("10")},
    }
    with pytest.raises(RuntimeError, match="place order failed"):
        trend_review.execute_trend_review_open(**arguments)
    client.fail_orders = 0
    recovered = trend_review.execute_trend_review_open(**arguments)
    assert recovered["status"] == "uncertain"
    assert len(client.requests) == 1
```

Replace the old tests that expected an absent broker order to retry the same intent; under the approved safety rule they now expect `uncertain` and zero additional submissions.

In `tests/test_kelly_order_execution.py`, add a fake context whose `order_list_query` returns an active partial order and whose `history_order_list_query` returns the same order plus one terminal order. Assert `list_orders(start="2026-07-20", end="2026-07-20")` returns both unique order IDs exactly once. This proves reconciliation sees active orders as well as history.

- [ ] **Step 3: Run the action tests and confirm the red state**

Run:

```bash
.venv/bin/python -m pytest tests/test_trend_review.py -k 'action_identity or execution_batch or broker_order or conflicting_quantity or uncertain or partial_buy' -q
```

Expected: FAIL on stable identity, batch artifact, numbered remark, conflict, and uncertainty behavior.

- [ ] **Step 4: Implement canonical identity with legacy-ledger discovery**

Replace the strategy-version action hash and shared remark with:

```python
def trend_action_key(market: str, execution_date: str, futu_code: str, side: str) -> str:
    identity = ":".join((
        _market(market), date.fromisoformat(execution_date).isoformat(),
        futu_code.strip().upper(), side.strip().lower(),
    ))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def trend_attempt_remark(
    market: str, execution_date: str, action_key: str, attempt: int
) -> str:
    if attempt <= 0:
        raise ValueError("attempt must be positive")
    remark = f"trend:{_market(market)}:{execution_date}:{action_key[:20]}:{attempt}"
    if len(remark.encode("utf-8")) > 64:
        raise ValueError("trend order remark exceeds Futu's 64-byte limit")
    return remark
```

For migration compatibility, discover prior intents by parsing every `*-intent.json` below the same market/date and matching request `futu_code` + `side`, regardless of its legacy action-key filename. Include every discovered legacy remark in the broker query and count its confirmed fills before proposing a canonical numbered attempt. When the new machine has no local ledger, also recognize broker remarks beginning `trend-review:{market}:{execution_date}:` as legacy action candidates; require exact symbol, side, and quantity before reconstructing a result, and fail closed on multiple/conflicting candidates. Never rewrite or move an old file.

- [ ] **Step 5: Implement immutable batch locking and broker-first attempt reconciliation**

Write the batch to `data/trend_review/ledgers/{market}/batches/{execution_date}.json` with `_write_immutable`:

```python
payload = {
    "schema_version": "open_trader.trend_review.batch.v1",
    "market": market,
    "execution_date": execution_date,
    "report_path": str(report_path),
    "report_sha256": _report_hash(report),
    "locked_at": locked_at,
}
```

If the file exists, validate and return the existing payload without replacing it. Before every proposed attempt:

1. Load all local intent/result facts for the stable action, including legacy paths.
2. Query Futu once for the execution-date range and index every order by remark.
3. If a local result exists, do not submit.
4. If the proposed remark exactly matches broker symbol/side/quantity, immutably create the missing result and do not submit.
5. If that remark exists with different facts, append a `conflict` action event and do not submit.
6. If an intent lacks a result and no broker fact is conclusive, append one `uncertain` event and do not submit.
7. Only when neither local nor broker facts exist, write the intent once and call `place_order` once.

The result/event payload retains report SHA and action index as evidence, but neither affects identity.

If migration finds a legacy intent/result but no batch file, use its `report_sha256` to find the matching valid report artifact and immutably reconstruct the batch before considering the latest revision. If no report matches that SHA, mark the controller blocked; never guess a new batch.

- [ ] **Step 6: Put the second host check directly at the mutation boundary**

Add this wrapper to `kelly_order_execution.py` and use it for every controller-created trend client:

```python
class ExecutorGuardedOrderClient:
    def __init__(self, delegate: object, authorize: Callable[[], object]) -> None:
        self._delegate = delegate
        self._authorize = authorize

    def place_order(self, request: dict[str, Any]) -> dict[str, Any]:
        self._authorize()
        return self._delegate.place_order(request)

    def __getattr__(self, name: str) -> object:
        return getattr(self._delegate, name)
```

Test that read methods delegate in readonly mode, while `place_order` calls `authorize` on every attempt and does not reach the delegate when it raises.

Deepen `FutuSimulateOrderExecutionClient.list_orders` without changing its return shape: query both `order_list_query` for current/active orders and `history_order_list_query` for the requested range, then deduplicate by non-empty order ID (falling back to canonical order JSON when Futu omits the ID). A failure from either query is a reconciliation blocker; do not submit from a partial broker view.

- [ ] **Step 7: Run focused and complete ledger tests, then commit**

Run:

```bash
.venv/bin/python -m pytest tests/test_kelly_order_execution.py tests/test_trend_review.py -q
git add src/open_trader/trend_review.py src/open_trader/kelly_order_execution.py tests/test_trend_review.py tests/test_kelly_order_execution.py
git commit -m "feat: reconcile trend actions by stable broker identity"
```

Expected: both files PASS, including legacy ledger compatibility and the accepted-order/crash recovery case.

---

### Task 3: Add explicit uncertainty resolution and bounded completion

**Files:**
- Modify: `src/open_trader/trend_review.py:402-455,513-1115`
- Test: `tests/test_trend_review.py:900-1425,1416-1685`

**Interfaces:**
- Produces: `resolve_trend_action(data_dir: Path, *, market: str, execution_date: str, symbol: str, side: str, resolution: Literal["confirm-submitted", "authorize-retry", "abandon"], actor: str, reason: str, resolved_at: str, futu_order_id: str | None = None) -> Path`.
- Extends: `execute_trend_review_open(*, data_dir: Path, report: Mapping[str, object], client: object, market: str, execution_date: str, now: str, quote_prices: Mapping[str, Decimal]) -> dict[str, object]`.
- Preserves and deepens: `execute_trend_review_stop(*, data_dir: Path, market: str, symbol: str, trading_date: str, event_id: str, client: object, now: str) -> dict[str, object]`, now sharing the stable sell action and numbered-attempt protocol.

- [ ] **Step 1: Write failing immutable-resolution tests**

Add one test for each allowed resolution and reject all invalid transitions:

```python
@pytest.mark.parametrize(
    ("resolution", "order_id", "expected"),
    [
        ("confirm-submitted", "SIM-42", "resolved_submitted"),
        ("authorize-retry", None, "retry_authorized"),
        ("abandon", None, "abandoned"),
    ],
)
def test_uncertain_action_resolution_is_immutable(
    tmp_path: Path, resolution: str, order_id: str | None, expected: str
) -> None:
    client = FakeTrendSimClient(fail_orders=1)
    with pytest.raises(RuntimeError, match="place order failed"):
        trend_review.execute_trend_review_open(
            data_dir=tmp_path, report=cn_buy_report(), client=client,
            market="CN", execution_date="2026-07-20",
            now="2026-07-20T09:31:00+08:00",
            quote_prices={"SH.600001": Decimal("10")},
        )
    client.fail_orders = 0
    assert trend_review.execute_trend_review_open(
        data_dir=tmp_path, report=cn_buy_report(), client=client,
        market="CN", execution_date="2026-07-20",
        now="2026-07-20T09:32:00+08:00",
        quote_prices={"SH.600001": Decimal("10")},
    )["status"] == "uncertain"
    path = trend_review.resolve_trend_action(
        tmp_path, market="CN", execution_date="2026-07-20",
        symbol="600001", side="buy", resolution=resolution,
        actor="ray", reason="checked Futu history",
        resolved_at="2026-07-20T09:40:00+08:00", futu_order_id=order_id,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["status"] == expected
    assert list(path.parent.glob("*.json")) == [path]
```

Also assert: `confirm-submitted` requires a non-empty Futu order ID; `authorize-retry` is the only resolution that permits attempt 2; a second contradictory resolution is rejected without changing the first file; actor and reason cannot be blank.

- [ ] **Step 2: Write failing partial-buy cap tests**

Use a frozen action of 400 shares, amount cap 4,000, lot 100, and a terminal 200-share fill. Prove:

- active `FILLED_PART` creates no overlap;
- at quote 15, attempt 2 is only 100 shares because the remaining amount is 1,000;
- cash below one lot creates no attempt;
- cumulative quantity never exceeds 400;
- v2/v3/v4 planned stop risk also caps the new lot count;
- after 10:00 CN/HK or 16:00 US, the partial position remains and the remainder is marked `missed` once.

The core assertion is:

```python
assert client.requests[-1] | {
    "qty": "100",
    "remark": trend_review.trend_attempt_remark("CN", "2026-07-20", action_key, 2),
} == client.requests[-1]
```

- [ ] **Step 3: Write failing merged-sell tests**

Create a formal `SELL_ALL` and a protection event for the same market/date/symbol. Assert one stable sell action, both reason/event IDs in its immutable events, and one broker request. Then simulate a terminal partial cancellation and prove the next attempt reads the live remaining position and submits only that remainder. An active or absent/ambiguous broker status must submit zero additional sells and record `uncertain` when inconclusive.

- [ ] **Step 4: Run the focused tests and confirm the red state**

Run:

```bash
.venv/bin/python -m pytest tests/test_trend_review.py -k 'resolution or risk_cap or amount_cap or partial_buy or merged_sell or sell_recovery' -q
```

Expected: FAIL because resolution facts, attempt authorization, amount/risk recapping, and merged sell identity are not implemented.

- [ ] **Step 5: Implement resolution facts without rewriting history**

Write resolutions under:

```text
data/trend_review/ledgers/{market}/actions/{execution_date}/{action_key}/resolutions/{timestamp}-{sha12}.json
```

The immutable payload is:

```python
{
    "schema_version": "open_trader.trend_review.resolution.v1",
    "market": market,
    "execution_date": execution_date,
    "action_key": action_key,
    "symbol": symbol,
    "futu_code": futu_code,
    "side": side,
    "resolution": resolution,
    "status": status,
    "actor": actor,
    "reason": reason,
    "futu_order_id": futu_order_id,
    "resolved_at": resolved_at,
}
```

Load resolutions alongside attempts. `confirm-submitted` makes the cumulative action submitted with the recorded order ID; `abandon` makes it terminal; `authorize-retry` consumes exactly once by permitting the next numbered remark. No branch deletes or edits an intent, result, broker order snapshot, action event, or earlier resolution.

- [ ] **Step 6: Implement affordable and risk-bounded buy remainder**

Add `_remaining_buy_quantity(action, report, snapshot, broker_orders, current_price) -> int`. Calculate confirmed quantity/notional from unique broker order IDs; then floor each cap to `lot_size` and choose the minimum:

```python
remaining_qty = frozen_qty - confirmed_qty
remaining_amount = target_amount - confirmed_notional
amount_qty = floor_to_lot(remaining_amount / (current_price * fx), lot_size)
cash_qty = floor_to_lot(available_cash / (current_price * fx), lot_size)
```

Implement the referenced helper exactly as:

```python
def _floor_to_lot(value: Decimal, lot_size: int) -> int:
    if lot_size <= 0 or not value.is_finite() or value <= 0:
        return 0
    return int(value // Decimal(lot_size)) * lot_size
```

For v2/v3/v4, also calculate the remaining frozen `planned_stop_risk` using `2 * atr * fx` plus the frozen `normal_cost_rate` from `risk_summary`, and floor its affordable quantity to the lot. Reject non-finite/non-positive price, FX, cash, amount, ATR, risk, or quantities before intent creation. For legacy v1, retain the quantity/amount/cash caps and do not invent unavailable risk fields.

- [ ] **Step 7: Route all sells through the same stable action executor**

Change `execute_trend_review_stop` to attach its `event_id` as a reason to the same `(market, trading_date, symbol, sell)` action root used by formal `SELL_ALL`. Aggregate reason IDs in action events, read the live position before every terminal retry, wait while any attempt is active/partially active, and require a conclusive broker terminal status before the next numbered sell attempt.

- [ ] **Step 8: Run all trend-review tests and commit**

Run:

```bash
.venv/bin/python -m pytest tests/test_trend_review.py -q
git add src/open_trader/trend_review.py tests/test_trend_review.py
git commit -m "feat: resolve uncertain trend orders and cap completion"
```

Expected: all trend-review tests PASS; no test expects automatic retry from an unresolved intent.

---

### Task 4: Build the self-reconciling per-market controller

**Files:**
- Create: `src/open_trader/trend_market_controller.py`
- Create: `tests/test_trend_market_controller.py`
- Modify: `src/open_trader/a_share_trend_watch.py:82-475` only if a one-pass hook is needed; prefer its existing `once=True` interface.
- Modify: `src/open_trader/market_trend_watch.py:70-160` only if a one-pass hook is needed; prefer its existing `once=True` interface.

**Interfaces:**
- Produces: `run_trend_market_controller(config: DailyPremarketConfig, market: str, *, revision: bool = False, once: bool = False, now_fn: Callable[[], datetime] = datetime.now, sleep_fn: Callable[[float], None] = sleep) -> dict[str, object]`.
- Produces: `load_trend_market_status(config: DailyPremarketConfig, market: str, *, now: datetime | None = None) -> dict[str, object]`.
- Produces internal: `ControllerCycle(market, as_of_date, execution_date, report_run_date, session, buy_window_open, market_open, next_check_at)`.
- Consumes: mode boundary from Task 1, batch/action functions from Tasks 2–3, existing `run_a_share_trend_report`, `run_market_trend_report`, one-pass protection watchers, and close capture.

- [ ] **Step 1: Write controller state-transition tests before creating the module**

Create deterministic filesystem tests through the controller interface. Start with this concrete fixture and test service builder:

```python
def controller_config(tmp_path: Path) -> DailyPremarketConfig:
    return DailyPremarketConfig(
        repo=tmp_path,
        python=Path(sys.executable),
        timezone="Asia/Shanghai",
        deadline="09:00",
        futu_host="127.0.0.1",
        futu_port=11111,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        logs_dir=tmp_path / "logs",
        portfolio=tmp_path / "data/latest/portfolio.csv",
        trend_executor_host="executor",
    )


def active_cn_cycle() -> ControllerCycle:
    return ControllerCycle(
        market="CN",
        as_of_date="2026-07-17",
        execution_date="2026-07-20",
        report_run_date="2026-07-17",
        session="morning",
        buy_window_open=True,
        market_open=True,
        next_check_at=datetime.fromisoformat("2026-07-20T09:31:05+08:00"),
    )


def valid_cn_report(*, as_of_date: str, execution_date: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "generated_at": f"{as_of_date}T18:00:00+08:00",
        "as_of_date": as_of_date,
        "execution_date": execution_date,
        "account": {
            "source_date": as_of_date,
            "fresh": True,
            "net_value": "100000",
            "available_cash": "100000",
            "positions": [],
            "exceptions": [],
            "position_count": 0,
        },
        "metadata": {"market": "CN", "broker": "eastmoney"},
        "strategy_snapshot": {
            "strategy_id": "trend_animals_warm_to_hot/CN/v1",
            "strategy_version": "v1",
            "process_version": "test-sha",
            "parameters": {"buy_window": "09:30-10:00"},
            "parameter_rows": [{
                "group": "execution", "name": "buy_window", "value": "09:30-10:00"
            }],
        },
        "strategy_judgments": {
            "formal_actions": [],
            "holding_decisions": [],
            "top10_candidates": [],
        },
    }
```

Monkeypatch the module's private `_derive_cycle`, `_load_latest_valid_report`, `_generate_report`, `_run_protection_pass`, `_execute_locked_report`, `_capture_close`, and `_notify_once` functions directly. The first end-to-end test must be complete and shaped like this:

```python
def test_start_after_original_trigger_generates_report_and_executes_inside_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    calls: list[tuple[str, str, object]] = []
    reports: list[tuple[Path, dict[str, object]]] = []

    def generate(
        _config: DailyPremarketConfig, market: str, run_date: str, revision: bool
    ) -> None:
        calls.append(("generate", market, (run_date, revision)))
        path = config.reports_dir / "trend_a_share/2026-07-17.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        report = valid_cn_report(
            as_of_date="2026-07-17", execution_date="2026-07-20"
        )
        path.write_text(json.dumps(report), encoding="utf-8")
        reports.append((path, report))

    monkeypatch.setattr(controller, "_derive_cycle", lambda _config, _market, _now: active_cn_cycle())
    monkeypatch.setattr(
        controller, "_load_latest_valid_report",
        lambda _config, _market, _date: reports[-1] if reports else None,
    )
    monkeypatch.setattr(controller, "_generate_report", generate)
    monkeypatch.setattr(
        controller, "_run_protection_pass",
        lambda _config, market, day: calls.append(("protect", market, day)),
    )
    monkeypatch.setattr(
        controller, "_execute_locked_report",
        lambda _config, market, day, path, report: calls.append(
            ("execute", market, (day, path.name))
        ) or {"status": "submitted", "submitted_count": 1},
    )
    monkeypatch.setattr(
        controller, "_capture_close",
        lambda _config, market, day: calls.append(("close", market, day)),
    )
    monkeypatch.setattr(
        controller, "_notify_once",
        lambda title, message, key: calls.append(
            ("notify", title, (message, key))
        ) or True,
    )

    result = run_trend_market_controller(
        config,
        "CN",
        once=True,
        now_fn=lambda: datetime.fromisoformat("2026-07-20T09:31:00+08:00"),
    )

    assert ("generate", "CN", ("2026-07-17", False)) in calls
    assert ("protect", "CN", "2026-07-20") in calls
    assert ("execute", "CN", ("2026-07-20", "2026-07-17.json")) in calls
    assert result["phase"] == "monitoring"
```

Use the same direct module monkeypatches for the other exact test names below. Each test must assert the report-generator call tuple, broker submission count, status phase/blocker, and immutable paths:

- `test_report_failure_before_freeze_retries_same_logical_dates`: first `generate_report` raises, second controller tick uses the same `("CN", "2026-07-17", False)` tuple and creates no revision filename.
- `test_frozen_delivery_failure_retries_delivery_without_rebuilding`: `load_report` returns a frozen receipt/report and the real report runner’s recovery path is invoked once; its expensive attempt function remains uncalled.
- `test_restart_after_report_freeze_does_not_regenerate`: pre-create the valid report and assert `generate_report` has zero calls across two `once=True` runs.
- `test_report_recovery_during_session_keeps_protection_ticks_running`: block `generate_report` on `threading.Event`, assert `protection_pass` runs, then release the event.
- `test_report_finished_after_window_is_preserved_and_actions_become_missed`: return `buy_window_open=False`, assert the report path remains and the action event is `missed` with no execute submission.
- `test_later_revision_does_not_change_locked_batch`: pre-create a batch for the base SHA plus an r1 report; assert `execute_report` receives the base artifact and one revision-anomaly notification is receipted.
- `test_readonly_controller_returns_without_report_broker_or_notification_calls`: hostname mismatch returns `phase=readonly` and every service call counter remains zero.
- `test_controller_restart_reconciles_existing_futu_order_without_submit`: use the real Task 2 action executor with a matching fake Futu order and assert zero `place_order` calls plus a repaired result.
- `test_close_capture_is_recovered_once_after_session_close`: return `session=closed`, run twice, and assert one immutable close fact and no second broker/calendar mutation.
- `test_explicit_revision_request_is_durable_while_controller_lock_is_held`: pre-create the controller lock as owned by a live process, call `run_trend_market_controller(config, "CN", revision=True, once=True)`, and assert one immutable request at `data/trend_controller/CN/revision_requests/2026-07-17.json` plus `phase=revision_requested`, without a second controller. Pre-create a batch lock and assert the same call is rejected because execution has begun.

- [ ] **Step 2: Run the new test file and confirm import failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_trend_market_controller.py -q
```

Expected: FAIL because `open_trader.trend_market_controller` does not exist.

- [ ] **Step 3: Implement market-cycle derivation using the existing calendars**

Use Futu trading days to find the most recent completed signal date and its next execution date. During/before a trading session, target the prior trading day’s report; after close, target today’s just-completed session and the next trading day. Feed legacy report generators exactly these run dates:

```python
report_run_date = (
    (date.fromisoformat(as_of_date) + timedelta(days=1)).isoformat()
    if market == "US"
    else as_of_date
)
```

Keep the authoritative window mapping in one constant:

```python
BUY_WINDOWS = {
    "CN": (time(9, 30), time(10, 0)),
    "HK": (time(9, 30), time(10, 0)),
    "US": (time(9, 30), time(16, 0)),
}
```

Validate loaded reports with the strict checks currently in CLI: schema, timezone-aware `generated_at`, market, broker, as-of filename/revision, execution date, complete action collections, strategy version, positive buy fields, account identity, and report freshness.

- [ ] **Step 4: Implement atomic disposable status and durable alert receipts**

Write status atomically with a temp file + `os.replace` at:

```text
data/trend_controller/{market}/status.json
```

Use schema `open_trader.trend_controller.status.v1` and include exactly:

```python
{
    "effective_mode": mode.mode,
    "executor_host": mode.executor_host,
    "local_host": mode.local_host,
    "pid": os.getpid(),
    "working_directory": str(Path.cwd().resolve()),
    "git_sha": _process_version(config.repo),
    "phase": phase,
    "heartbeat_at": now.isoformat(timespec="seconds"),
    "last_success": last_success,
    "blocker": blocker,
    "next_check_at": next_check_at.isoformat(timespec="seconds"),
}
```

Deduplicate controller failure, uncertainty, conflict, missed-window, and post-lock revision notifications by immutable receipt path `data/trend_controller/{market}/notifications/{execution_date}/{sha256(market|date|action|reason)}.json`. Write a receipt only after at least one configured notification channel succeeds.

- [ ] **Step 5: Implement the reconciliation loop with one supervised report future**

Inside a per-market `RunLock`, maintain at most one `ThreadPoolExecutor(max_workers=1)` report future. Keep the real business calls in the seven small private functions named in Step 1 so tests can monkeypatch them without a service interface. In `once=True` tests, perform the protection pass first, then call `future.result(timeout=1)` and harvest that report before returning from the one full tick; the protection-priority test releases its blocked future from the protection callback. In the persistent loop, keep a five-second heartbeat cadence. Schedule report/broker retries without blocking heartbeats at `min(300, 5 * 2 ** min(consecutive_failures, 6))` seconds. Process each tick in this order:

1. Refresh heartbeat/status from durable facts.
2. Derive the market cycle and load the latest valid report for its execution date.
3. If absent and no report future exists, submit the existing report generator for the same logical dates.
4. If the market is active, run one protection pass even while that future is pending.
5. When a valid report exists, lock/read the batch, fetch current buy quotes, and execute/reconcile the locked actions only inside their windows.
6. When the window expires, append one `missed` fact for each unfinished buy while continuing report recovery.
7. After session close, idempotently capture the close and build its review projection.
8. Record blocker/success/next-check, sleep with bounded backoff, and repeat.

An invalid frozen report is a blocker requiring `run --revision`; do not delete it or silently regenerate it. A later valid revision after batch lock emits an anomaly only. `once=True` performs one reconciliation tick for tests and returns its status without being exposed as an operational CLI option.

Make `run --revision` usable while launchd already owns the long-running controller lock. After host authorization but before taking that lock, derive the current cycle, reject the request if its execution batch already exists, and immutably write:

```python
{
    "schema_version": "open_trader.trend_controller.revision_request.v1",
    "market": market,
    "as_of_date": cycle.as_of_date,
    "execution_date": cycle.execution_date,
    "requested_at": now.isoformat(timespec="seconds"),
}
```

The persistent controller discovers that request, calls the existing report generator with `revision=True`, and writes a separate immutable completion fact containing the generated report path/SHA. It never edits the request. If no controller is live, the same invocation continues into the controller loop and services its own request. This remains the single `trend-market run` operation, not a second scheduler or command namespace.

- [ ] **Step 6: Wire existing one-pass protection and close functions**

For CN call `watch_a_share_protection` with its existing keyword arguments plus `once=True` only during morning/afternoon sessions. For HK/US call `watch_market_protection` with its existing keyword arguments plus `once=True` only during their active sessions. Supply callbacks to the shared sell executor from Task 3. Reuse the existing state/event/report-lock paths returned by `market_paths` and the CN equivalents; do not duplicate protection-line calculations.

At close, reuse `benchmark_fact`, `capture_trend_review_close`, and `build_trend_review_projection`. If the immutable daily close fact already exists, treat it as success without requesting new mutation.

- [ ] **Step 7: Run controller, watcher, report, and ledger tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_trend_market_controller.py \
  tests/test_trend_review.py \
  tests/test_a_share_trend_watch.py \
  tests/test_market_trend_watch.py \
  tests/test_a_share_trend.py \
  tests/test_market_trend.py -q
```

Expected: PASS; report recovery, protection monitoring, and execution remain the existing business implementations.

- [ ] **Step 8: Commit the controller**

Run:

```bash
git add src/open_trader/trend_market_controller.py src/open_trader/a_share_trend_watch.py src/open_trader/market_trend_watch.py tests/test_trend_market_controller.py
git commit -m "feat: add self-reconciling trend market controller"
```

Expected: commit contains the orchestrator and only the watcher hook changes actually required by its one-pass integration.

---

### Task 5: Collapse operational commands into one namespace

**Files:**
- Modify: `src/open_trader/cli.py:130-330,608-875,1543-2040`
- Create: `tests/test_trend_market_cli.py`
- Modify: `tests/test_trend_api_stats_cli.py` only to preserve the non-operational replay/stats routes if parser setup changes.

**Interfaces:**
- Produces: `open_trader trend-market run --market CN|HK|US [--revision] [--config PATH]`.
- Produces: `open_trader trend-market status --market CN|HK|US [--config PATH]`.
- Produces: `open_trader trend-market resolve --market CN|HK|US --execution-date YYYY-MM-DD --symbol SYMBOL --side buy|sell --resolution confirm-submitted|authorize-retry|abandon --actor ACTOR --reason REASON [--futu-order-id ORDER_ID]`.
- Removes operationally: `trend-a-share-report`, `watch-trend-a-share`, `trend-market-report`, `watch-trend-market`, `trend-review open`, and `trend-review close`.
- Preserves as non-operational analysis: `trend-review replay` and `trend-review sync-stats`.

- [ ] **Step 1: Write failing parser and routing tests**

Test all three new subcommands, conditional Futu order ID validation, and that every removed command causes argparse exit 2. Patch `run_trend_market_controller`, `load_trend_market_status`, and `resolve_trend_action` to assert exact routed values. Add:

```python
def test_readonly_run_and_resolve_have_no_side_effects(monkeypatch, capsys) -> None:
    config = DailyPremarketConfig(
        repo=Path.cwd(), python=Path(sys.executable), timezone="Asia/Shanghai",
        deadline="09:00", futu_host="127.0.0.1", futu_port=11111,
        data_dir=Path("data"), reports_dir=Path("reports"),
        logs_dir=Path("logs"), portfolio=Path("data/latest/portfolio.csv"),
        trend_executor_host="executor",
    )
    monkeypatch.setattr(socket, "gethostname", lambda: "readonly-copy")
    monkeypatch.setattr(cli, "load_env_config", lambda *_args, **_kwargs: config)
    assert cli.main(["trend-market", "run", "--market", "US"]) == 2
    assert "readonly" in capsys.readouterr().err
```

`status` must still return 0 and describe why the local machine is read-only.

- [ ] **Step 2: Run the CLI tests and confirm the red state**

Run:

```bash
.venv/bin/python -m pytest tests/test_trend_market_cli.py tests/test_trend_api_stats_cli.py -q
```

Expected: FAIL because the namespace is absent and old commands still parse.

- [ ] **Step 3: Implement the one operational namespace**

Build one parser with required nested subcommands. `run` loads config, then replaces only `config.repo` with `Path.cwd().resolve()` and `config.python` with `Path(sys.executable).resolve()` before calling the controller. The data/report/log/portfolio paths were already resolved from the config file and remain unchanged. This makes report/status process versions describe the exact checkout launchd is actually running, including an accepted worktree, instead of the checkout named by a shared data config. `status` never constructs a broker/notifier client; `resolve` calls `require_trend_executor` before writing the immutable fact. Print one JSON object for every successful command. Map readonly run/resolve to exit 2 and runtime/controller errors to exit 1.

Move the strict report loader/validator out of CLI into the controller module, then delete the old report/watch handlers and trend-review open/close parser branches. Leave replay and sync-stats available for historical analysis, but they must not generate reports, watch markets, or place orders.

- [ ] **Step 4: Run CLI and integration tests, then commit**

Run:

```bash
.venv/bin/python -m pytest tests/test_trend_market_cli.py tests/test_trend_api_stats_cli.py tests/test_premarket_cli.py -q
git add src/open_trader/cli.py tests/test_trend_market_cli.py tests/test_trend_api_stats_cli.py
git commit -m "feat: expose one trend market operation namespace"
```

Expected: new commands PASS; each removed operational command is rejected by argparse.

---

### Task 6: Replace calendar-split launchd jobs with fenced controllers

**Files:**
- Create: `ops/launchd/com.open-trader.trend-market-controller.plist.template`
- Delete: `ops/launchd/com.open-trader.trend-a-share-report.plist.template`
- Delete: `ops/launchd/com.open-trader.trend-a-share-watch.plist.template`
- Delete: `ops/launchd/com.open-trader.trend-market-report.plist.template`
- Delete: `ops/launchd/com.open-trader.trend-market-watch.plist.template`
- Modify: `scripts/install_daily_premarket_launchd.sh:1-320`
- Modify: `scripts/uninstall_daily_premarket_launchd.sh:1-90`
- Modify: `tests/test_daily_premarket.py:4009-4863`

**Interfaces:**
- Produces labels: `com.open-trader.trend-market-controller.cn`, `.hk`, and `.us`.
- Produces program arguments: `python -m open_trader trend-market run --market CN|HK|US --config {resolved_config_path}`.
- Adds installer option: `--config PATH`, defaulting to the current checkout’s `config/daily_premarket.env`.
- Preserves ordinary premarket jobs and the existing `--dry-run`, `--trend-only`, and `--market` switches.

- [ ] **Step 1: Replace split-job tests with controller-mode tests**

Keep the existing copied-repo shell harness, but replace assertions for report/watch pairs with:

- exact-host + `--trend-only --market all` renders exactly CN/HK/US controller plists;
- each plist contains `RunAtLoad=true`, `KeepAlive=true`, `ThrottleInterval=30`, and no `StartCalendarInterval`;
- `--config /absolute/shared/config.env` keeps that exact config argument while `WorkingDirectory` and `PYTHONPATH` point to the checkout containing the installer script;
- missing/mismatched executor host renders no trend plist and reports `effective mode: readonly`;
- readonly real install unloads/removes all old split and new controller labels;
- executor migration unloads every selected old report/watch label before loading its controller label;
- fake `launchctl print gui/$UID/{old_label}` confirms no old process remains before load;
- `--market US` touches only US when executing, while readonly cleanup removes all trend automation;
- ordinary premarket dry-run output is unchanged when `--trend-only` is absent.

- [ ] **Step 2: Run the launchd group and confirm the red state**

Run:

```bash
.venv/bin/python -m pytest tests/test_daily_premarket.py -k 'launchd_installer or launchd_uninstaller or launchd_template' -q
```

Expected: FAIL because split plists and calendar triggers still exist.

- [ ] **Step 3: Create the persistent controller plist**

The template must contain:

```xml
<key>RunAtLoad</key><true/>
<key>KeepAlive</key><true/>
<key>ThrottleInterval</key><integer>30</integer>
```

Use one stdout/stderr pair per market under `logs/daily_premarket/launchd-trend-controller-{market}.out.log` and `.err.log`. Do not include a calendar trigger.

For trend controller plists, separate source code from shared configuration: render `WorkingDirectory` and `PYTHONPATH` from the installer’s own `REPO_ROOT`, but render the CLI `--config` argument from the resolved `--config PATH`. Continue using the configured Python interpreter. This is required for exact-accepted-SHA deployment from an isolated worktree while keeping the existing absolute data/report/log/portfolio paths.

- [ ] **Step 4: Implement fenced installation and readonly cleanup**

Read `OPEN_TRADER_TREND_EXECUTOR_HOST` with the installer’s existing last-value-wins parser and compare it to `hostname`. For `--trend-only`:

1. Print local host, configured executor host, and effective mode.
2. Unload and remove all relevant old split plists.
3. Verify old labels are absent with `launchctl print gui/$UID/{label}` before proceeding.
4. In readonly mode, also remove controller plists and exit successfully with no installation.
5. In execute mode, render/lint one requested controller plist, load it, and report the label.

The controller’s first internal phase is broker/ledger reconciliation, so it remains fail-closed before any eligible submission. The installer never directly restarts an old watcher during rollback.

- [ ] **Step 5: Update uninstall behavior and remove split templates**

The uninstaller removes controller plus all legacy split labels for selected markets. `--market all` includes CN/HK/US trend labels. Delete the four obsolete templates only after the new dry-run tests pass.

- [ ] **Step 6: Run shell integration tests and commit**

Run:

```bash
.venv/bin/python -m pytest tests/test_daily_premarket.py -k 'launchd_installer or launchd_uninstaller or launchd_template' -q
git add scripts/install_daily_premarket_launchd.sh scripts/uninstall_daily_premarket_launchd.sh ops/launchd tests/test_daily_premarket.py
git commit -m "feat: install one persistent trend controller per market"
```

Expected: launchd tests PASS and no generated plist invokes a removed command.

---

### Task 7: Expose controller authority and failure states in the Dashboard

**Files:**
- Modify: `src/open_trader/dashboard.py:80-230,486-618,1148-1305,1717-1815`
- Modify: `src/open_trader/cli.py:2885-2940`
- Modify: `src/open_trader/dashboard_static/dashboard.js:2115-2145,2646-2760`
- Modify: `src/open_trader/dashboard_static/dashboard.css`
- Modify: `src/open_trader/dashboard_acceptance.py`
- Test: `tests/test_dashboard_web.py:2990-4150,8720-8800`
- Test: `tests/test_dashboard_acceptance.py`

**Interfaces:**
- Extends `DashboardConfig` with `trend_executor_host: str = ""`.
- Extends `DashboardState.to_dict()` with `trend_controllers: dict[str, dict[str, object]]`, keyed by `eastmoney`, `phillips`, and `tiger`.
- Consumes `data/trend_controller/{market}/status.json`, immutable batch facts, and action events.

- [ ] **Step 1: Write failing backend projection tests**

Cover execute/healthy, execute/missing heartbeat, execute/stale heartbeat, malformed status, and readonly/mismatch. Use a two-minute heartbeat threshold. Assert a readonly host never becomes unavailable merely because it has no local status file.

```python
assert payload["trend_controllers"]["tiger"] | {
    "effective_mode": "execute",
    "executor_host": "ray-mac",
    "local_host": "ray-mac",
    "health": "unavailable",
    "blocking": True,
} == payload["trend_controllers"]["tiger"]
```

Extend action projection tests so `uncertain`, `conflict`, and `missed` survive unchanged. If latest report SHA differs from the immutable batch SHA, assert the report payload exposes `execution_batch.report_sha256`, `latest_report_sha256`, and `revision_anomaly=true` while action executions remain linked to the locked SHA.

- [ ] **Step 2: Run backend tests and confirm the red state**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard.py tests/test_dashboard_web.py -k 'controller or uncertain or conflict or revision_anomaly' -q
```

Expected: FAIL because controller status is not in the payload and executions are still filtered only by the currently displayed report SHA.

- [ ] **Step 3: Implement strict status and batch projection**

Parse only schema-valid status documents. For execute mode, mark health unavailable when the file is absent, malformed, hostname mismatched, or heartbeat is more than two minutes old. For readonly mode, return health `readonly`, the mismatch/missing-config reason, and `blocking=false` without requiring a heartbeat.

Load the immutable batch for the report’s execution date. Use its SHA for `_trend_action_executions`, project the locked report artifact, and flag a later-report difference without applying its actions.

- [ ] **Step 4: Write failing frontend and acceptance tests**

Extend the existing Node render harness and acceptance fakes. Assert the trend workspace contains a `.trend-controller-status` card with mode, executor/local host, PID, Git SHA, phase, heartbeat, last success, blocker, and next check. Assert:

- `uncertain` renders `状态不确定，禁止自动重试`;
- `conflict` renders `订单事实冲突，禁止提交`;
- `missed` renders `已错过策略窗口`;
- execute + unavailable heartbeat is visually blocking;
- readonly says `只读部署，不运行本机控制器`;
- the card fits desktop and 375px mobile without horizontal overflow.

- [ ] **Step 5: Implement the compact controller card and status labels**

Add these status labels to `renderTrendExecutionRow`:

```javascript
uncertain: "状态不确定，禁止自动重试",
conflict: "订单事实冲突，禁止提交",
missed: "已错过策略窗口",
```

Render one compact facts card above the formal action tables. Use existing warm-ledger tokens, a two-column definition list on desktop and one column below 760px. Do not add controls; Dashboard remains read-only.

- [ ] **Step 6: Run all Dashboard tests and commit**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard.py tests/test_dashboard_web.py tests/test_dashboard_acceptance.py -q
git add src/open_trader/dashboard.py src/open_trader/cli.py src/open_trader/dashboard_static/dashboard.js src/open_trader/dashboard_static/dashboard.css src/open_trader/dashboard_acceptance.py tests/test_dashboard_web.py tests/test_dashboard_acceptance.py
git commit -m "feat: show trend controller health in dashboard"
```

Expected: backend, Node rendering, responsive static contracts, and acceptance helpers PASS.

---

### Task 8: Document, verify, migrate live jobs, and pass the final gate

**Files:**
- Modify: `README.md:351-575`
- Verify: all files changed in Tasks 1–7
- Verify live: launchd controllers, Dashboard screen process, controller status/logs, Futu read-only reconciliation, and `http://127.0.0.1:8766`

**Interfaces:**
- Produces operator workflow: configure one hostname, install controllers, inspect status, resolve uncertainty, migrate, roll back, and verify.
- Produces final accepted and deployed Git SHA.

- [ ] **Step 1: Replace old README commands with the final operator contract**

Document these exact examples:

```bash
hostname
# Set the exact output only on the execution machine:
OPEN_TRADER_TREND_EXECUTOR_HOST=ray-mac

.venv/bin/python -m open_trader trend-market status --market US
.venv/bin/python -m open_trader trend-market run --market US
.venv/bin/python -m open_trader trend-market run --market US --revision
.venv/bin/python -m open_trader trend-market resolve \
  --market US --execution-date 2026-07-20 --symbol TRV --side buy \
  --resolution confirm-submitted --futu-order-id SIM-42 \
  --actor ray --reason "verified in Futu order history"
```

Explain all three resolutions, the no-auto-failover rule, readonly behavior, report retry/freeze semantics, action/batch identity, partial completion caps, missed-window behavior, fenced migration, and rollback requiring stopped automation plus reconciliation before explicitly restoring an old watcher.

- [ ] **Step 2: Run focused automated suites**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_daily_premarket.py \
  tests/test_kelly_order_execution.py \
  tests/test_trend_review.py \
  tests/test_trend_market_controller.py \
  tests/test_trend_market_cli.py \
  tests/test_a_share_trend.py \
  tests/test_market_trend.py \
  tests/test_a_share_trend_watch.py \
  tests/test_market_trend_watch.py \
  tests/test_dashboard.py \
  tests/test_dashboard_web.py \
  tests/test_dashboard_acceptance.py -q
```

Expected: PASS with an exact passed-test count and no skipped failure hidden by `-k`.

- [ ] **Step 3: Run the complete suite and inspect the diff**

Run:

```bash
make test
git diff --check
git status --short
```

Expected: all tests PASS, `git diff --check` is silent, and status lists only intended task files.

- [ ] **Step 4: Run safe direct workflows before touching live jobs**

Run the read-only controller interface directly with an in-memory config whose executor hostname intentionally does not match; no temporary config file is required:

```bash
PYTHONPATH=src .venv/bin/python - <<'PY'
from dataclasses import replace
from pathlib import Path
import socket
from open_trader.daily_premarket import load_env_config
from open_trader.trend_market_controller import run_trend_market_controller

config = load_env_config(
    Path("/Users/ray/projects/open_trader/config/daily_premarket.env"),
    dry_run=False,
)
readonly = replace(
    config,
    trend_executor_host=f"definitely-not-{socket.gethostname()}",
)
print(run_trend_market_controller(readonly, "US", once=True))
PY
PYTHONPATH=src .venv/bin/python -m open_trader trend-market status \
  --market US \
  --config /Users/ray/projects/open_trader/config/daily_premarket.env
scripts/install_daily_premarket_launchd.sh \
  --config /Users/ray/projects/open_trader/config/daily_premarket.env \
  --dry-run --trend-only --market all
```

Expected: status reports readonly; run exits 2 without report, notification, ledger, or broker mutations; dry-run reports mode and renders jobs only when the actual local hostname matches configured executor host.

- [ ] **Step 5: Commit documentation and the complete implementation before acceptance**

Run:

```bash
git add README.md
git commit -m "docs: explain resilient trend controller operations"
git status --short
git rev-parse HEAD
```

Expected: clean worktree. Record this SHA as the candidate accepted SHA; make no source/data changes between the final gate and exact-SHA redeployment.

- [ ] **Step 6: Fence old live automation and install the controllers**

First inspect the shared local-only config at `/Users/ray/projects/open_trader/config/daily_premarket.env`. On this designated execution machine, use `apply_patch` to set its uncommitted line to the exact current hostname `OPEN_TRADER_TREND_EXECUTOR_HOST=Mac-mini.local`; never add this machine-specific config file to Git. Then migrate from the accepted worktree while passing that shared config explicitly:

```bash
hostname
rg '^OPEN_TRADER_TREND_EXECUTOR_HOST=' /Users/ray/projects/open_trader/config/daily_premarket.env
launchctl list | rg 'com\.open-trader\.(trend|premarket)'
ps aux | rg 'open_trader (trend|watch-trend)|trend-market run'
scripts/install_daily_premarket_launchd.sh \
  --config /Users/ray/projects/open_trader/config/daily_premarket.env \
  --trend-only --market all
launchctl list | rg 'com\.open-trader\.trend-market-controller\.(cn|hk|us)'
ps aux | rg 'open_trader trend-market run'
```

Expected: no old report/watch PID remains; exactly one controller process exists for each CN/HK/US market on the configured executor. On a readonly machine, no trend controller exists. Inspect each fresh status JSON and log; verify PID, working directory, Git SHA, phase, heartbeat timestamp, and blocker. Do not submit a manual test order.

- [ ] **Step 7: Restart the candidate Dashboard and run the final acceptance gate**

Start the Dashboard from this worktree so the gate checks the candidate code:

```bash
screen -S open_trader_dashboard_8766 -X quit 2>/dev/null || true
screen -dmS open_trader_dashboard_8766 zsh -lc \
  'cd /Users/ray/projects/open_trader/.worktrees/trend-market-controller-spec && exec env PYTHONPATH=src .venv/bin/python -u -m open_trader dashboard --portfolio /Users/ray/projects/open_trader/data/latest/portfolio.csv --data-dir /Users/ray/projects/open_trader/data --reports-dir /Users/ray/projects/open_trader/reports --config /Users/ray/projects/open_trader/config/daily_premarket.env --poll-seconds 5 --host 127.0.0.1 --port 8766 >> /tmp/open_trader_dashboard_8766.log 2>&1'
make acceptance
```

Expected: the final line is exactly `PASS`. `FAIL` requires diagnosis, a source commit, and a complete rerun of this gate. `BLOCKED` must be reported as blocked and cannot be replaced by curl, mocks, fixtures, unit tests, or screenshots.

- [ ] **Step 8: Redeploy the exact accepted SHA and verify fresh live evidence**

Confirm no source/data change occurred, then restart the controllers and Dashboard from the accepted checkout. Record:

```bash
git rev-parse HEAD
launchctl list | rg 'com\.open-trader\.trend-market-controller\.(cn|hk|us)'
pgrep -f 'open_trader trend-market run' | xargs ps -o pid,lstart,command -p
screen -ls
lsof -nP -iTCP:8766 -sTCP:LISTEN
tail -n 80 logs/daily_premarket/launchd-trend-controller-*.out.log
tail -n 80 /tmp/open_trader_dashboard_8766.log
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8766/
curl -sS http://127.0.0.1:8766/api/dashboard | .venv/bin/python -m json.tool >/dev/null
```

Expected: deployed Git SHA equals the accepted SHA; PIDs and timestamps are fresh; process working directories point to the accepted checkout; every executor heartbeat advances; logs are from the new PIDs; review URL returns HTTP 200 and valid JSON. Only then provide `http://127.0.0.1:8766` for user review.

---

### Task 9: Cut over expired legacy cycles that cannot be replayed

This live-discovered migration task runs before Task 8 Steps 7–8. It does not
add a public CLI or change report validation.

**Files:**
- Modify: `src/open_trader/trend_market_controller.py:950-1400`
- Test: `tests/test_trend_market_controller.py:1450-1800`

**Interfaces:**
- Produces: `_legacy_cutover_path(config: DailyPremarketConfig, market: str, as_of_date: str) -> Path`.
- Produces: `_record_legacy_cycle_cutover(config: DailyPremarketConfig, cycle: ControllerCycle, *, actor: str, reason: str, authorized_at: datetime) -> Path`.
- Produces: `_legacy_cycle_cutover(config: DailyPremarketConfig, cycle: ControllerCycle) -> bool`.
- Modifies: `_execution_completed(...)` returns `True` before batch loading only when `_legacy_cycle_cutover(...)` validates the exact immutable fact.

- [ ] **Step 1: Write failing exact-cutover tests**

Add one end-to-end state-transition test that writes an invalid historical r2,
creates its immutable revision request, records a cutover after the buy window,
and proves `_cycle_to_reconcile` advances to the current cycle without creating
a batch or action ledger:

```python
def test_legacy_cutover_skips_only_exact_expired_unreplayable_cycle(
    tmp_path: Path,
) -> None:
    config = controller_config(tmp_path)
    historical = active_cn_cycle()
    current = ControllerCycle(
        market="CN",
        as_of_date="2026-07-20",
        execution_date="2026-07-21",
        report_run_date="2026-07-20",
        session="morning",
        market_open=True,
        next_check_at=NOW + timedelta(seconds=5),
    )
    path, report = write_report(config, revision=2)
    report["schema_version"] = 999
    path.write_text(json.dumps(report), encoding="utf-8")
    authorized_at = datetime.fromisoformat("2026-07-21T18:00:00+08:00")
    controller._request_revision(config, historical, authorized_at)

    cutover = controller._record_legacy_cycle_cutover(
        config,
        historical,
        actor="ray",
        reason="historical replay evidence and dated account snapshot unavailable",
        authorized_at=authorized_at,
    )

    assert cutover.exists()
    assert controller._execution_completed(config, historical) is True
    assert controller._cycle_to_reconcile(config, current, authorized_at) == current
    assert not controller._batch_path(
        config, historical.market, historical.execution_date
    ).exists()
    assert not (config.data_dir / "trend_review/ledgers/CN/actions").exists()
```

Add this test helper and the focused boundary tests:

```python
def prepare_legacy_cutover(
    config: DailyPremarketConfig,
) -> tuple[ControllerCycle, Path, Path, datetime]:
    cycle = active_cn_cycle()
    report_path, report = write_report(config, revision=2)
    report["schema_version"] = 999
    report_path.write_text(json.dumps(report), encoding="utf-8")
    authorized_at = datetime.fromisoformat("2026-07-21T18:00:00+08:00")
    request_path = controller._request_revision(config, cycle, authorized_at)
    return cycle, report_path, request_path, authorized_at


@pytest.mark.parametrize("blocker", ["open_window", "batch"])
def test_legacy_cutover_rejects_open_window_or_existing_batch(
    tmp_path: Path,
    blocker: str,
) -> None:
    config = controller_config(tmp_path)
    cycle, _, _, authorized_at = prepare_legacy_cutover(config)
    if blocker == "open_window":
        authorized_at = NOW
    else:
        batch = controller._batch_path(config, cycle.market, cycle.execution_date)
        batch.parent.mkdir(parents=True, exist_ok=True)
        batch.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError):
        controller._record_legacy_cycle_cutover(
            config,
            cycle,
            actor="ray",
            reason="historical evidence unavailable",
            authorized_at=authorized_at,
        )


@pytest.mark.parametrize("bound_artifact", ["report", "request"])
def test_legacy_cutover_fails_closed_after_report_or_request_tampering(
    tmp_path: Path,
    bound_artifact: str,
) -> None:
    config = controller_config(tmp_path)
    cycle, report_path, request_path, authorized_at = prepare_legacy_cutover(config)
    controller._record_legacy_cycle_cutover(
        config,
        cycle,
        actor="ray",
        reason="historical evidence unavailable",
        authorized_at=authorized_at,
    )
    target = report_path if bound_artifact == "report" else request_path
    target.write_bytes(target.read_bytes() + b" ")

    with pytest.raises(ValueError, match="invalid legacy trend cutover"):
        controller._execution_completed(config, cycle)


def test_legacy_cutover_is_immutable_and_validates_operator_fields(
    tmp_path: Path,
) -> None:
    config = controller_config(tmp_path)
    cycle, _, _, authorized_at = prepare_legacy_cutover(config)
    values = {
        "config": config,
        "cycle": cycle,
        "actor": "ray",
        "reason": "historical evidence unavailable",
        "authorized_at": authorized_at,
    }
    first = controller._record_legacy_cycle_cutover(**values)
    assert controller._record_legacy_cycle_cutover(**values) == first
    with pytest.raises(FileExistsError, match="immutable artifact collision"):
        controller._record_legacy_cycle_cutover(
            **{**values, "reason": "different reason"}
        )
    for index, changed in enumerate((
        {"actor": ""},
        {"reason": ""},
        {"authorized_at": datetime(2026, 7, 21, 18)},
    )):
        other = controller_config(tmp_path / str(index))
        other_cycle, _, _, other_at = prepare_legacy_cutover(other)
        with pytest.raises(ValueError):
            controller._record_legacy_cycle_cutover(
                other,
                other_cycle,
                actor=str(changed.get("actor", "ray")),
                reason=str(changed.get("reason", "historical evidence unavailable")),
                authorized_at=changed.get("authorized_at", other_at),
            )
```

The tampering test changes one bound file byte after recording and expects the
cutover reader to wrap lower-level request/report failures as
`ValueError("invalid legacy trend cutover: <path>")`; it must not silently
return `False` once a cutover file exists.

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_trend_market_controller.py::test_legacy_cutover_skips_only_exact_expired_unreplayable_cycle \
  tests/test_trend_market_controller.py::test_legacy_cutover_rejects_open_window_or_existing_batch \
  tests/test_trend_market_controller.py::test_legacy_cutover_fails_closed_after_report_or_request_tampering \
  tests/test_trend_market_controller.py::test_legacy_cutover_is_immutable_and_validates_operator_fields -q
```

Expected: FAIL because the three cutover helpers do not exist.

- [ ] **Step 3: Implement the minimum immutable fact**

Reuse `_controller_root`, `_revision_paths`, `_revision_state`,
`_revision_baseline`, `_batch_path`, `_read_json`, `_write_immutable`, and
`_canonical_json_bytes`. Store one fact at
`data/trend_controller/{market}/legacy_cutovers/{as_of_date}.json` with this
exact schema:

```python
{
    "schema_version": "open_trader.trend_controller.legacy_cutover.v1",
    "market": cycle.market,
    "as_of_date": cycle.as_of_date,
    "execution_date": cycle.execution_date,
    "report_path": str(report_path),
    "report_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
    "revision_request_path": str(request_path),
    "revision_request_sha256": hashlib.sha256(request_path.read_bytes()).hexdigest(),
    "actor": actor.strip(),
    "reason": reason.strip(),
    "authorized_at": authorized_at.isoformat(timespec="seconds"),
}
```

The writer and reader both enforce: exact market/date identity; nonempty actor
and reason; timezone-aware authorization; authorization strictly after the
market's buy-window end; no execution batch; an existing valid pending revision
request with no completion; the latest report path/SHA is unchanged and lives
in the market report directory. The writer calls `require_trend_executor`
before touching data. `_write_immutable` provides idempotence and
rejects conflicting rewrites. Do not add a new class, dependency, public CLI,
notification, report transformation, or relaxed `_valid_report` branch.

- [ ] **Step 4: Verify GREEN and regression coverage**

Run:

```bash
.venv/bin/python -m pytest tests/test_trend_market_controller.py -q
.venv/bin/python -m pytest tests/test_trend_market_cli.py tests/test_trend_review.py -q
git diff --check
```

Expected: controller, CLI, and immutable-ledger tests PASS; no public command
changed and no whitespace errors exist.

- [ ] **Step 5: Commit and task-review before touching live data**

Run:

```bash
git add src/open_trader/trend_market_controller.py tests/test_trend_market_controller.py
git commit -m "fix: cut over unreplayable legacy trend cycles"
```

Expected: only the minimal controller/test files, plus an operator note if
needed, are committed. Run the task-review gate before invoking the helper on
shared data.

- [ ] **Step 6: Record only the authorized live legacy cycles and resume Task 8**

With all controller launchd labels still unloaded, run this one-time invocation.
It cuts over only consecutive expired invalid cycles and stops at the first
valid unfinished or current cycle:

```bash
PYTHONPATH=src .venv/bin/python - <<'PY'
import hashlib
from datetime import datetime
from pathlib import Path

from open_trader.daily_premarket import load_env_config
from open_trader import trend_market_controller as controller

config = load_env_config(
    Path("/Users/ray/projects/open_trader/config/daily_premarket.env")
)
reason = "legacy report cannot be safely rebuilt without date-bound replay evidence"
for market in ("CN", "HK", "US"):
    now = datetime.now(controller.TIMEZONES[market])
    current = controller._derive_cycle(config, market, now)
    for _ in range(60):
        cycle = controller._cycle_to_reconcile(config, current, now)
        if (
            cycle.as_of_date == current.as_of_date
            and cycle.execution_date == current.execution_date
        ):
            break
        try:
            report = controller._load_cycle_report(config, cycle)
        except ValueError as exc:
            if "invalid frozen trend report" not in str(exc):
                raise
            controller._request_revision(config, cycle, now)
            path = controller._record_legacy_cycle_cutover(
                config,
                cycle,
                actor="ray",
                reason=reason,
                authorized_at=now,
            )
            print(
                market,
                cycle.as_of_date,
                cycle.execution_date,
                path,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
            continue
        if report is not None:
            print(
                market,
                "stopped_at_valid_unfinished_cycle",
                cycle.as_of_date,
                cycle.execution_date,
            )
            break
        raise RuntimeError(
            f"{market} legacy cutover stopped at missing historical report "
            f"{cycle.as_of_date}/{cycle.execution_date}"
        )
    else:
        raise RuntimeError(f"{market} legacy cutover exceeded 60 cycles")
PY
```

The output contains only market/date/path/SHA-safe metadata, never account
contents or secrets. Do not cut over a valid report, any cycle with a batch, or
the current cycle.

After recording, reinstall all three controllers from the candidate worktree,
verify fresh PIDs/heartbeats/logs, and confirm each market advances beyond the
bound legacy cycles. Then rerun focused/full tests and continue Task 8 Steps
7–8. Do not submit a manual test order.

---

### Task 10: Cut over an explicitly missing historical report

This user-authorized extension reuses Task 9's immutable fact and private
writer. It adds no CLI, service, database, notification, or report generator.

**Files:**
- Modify: `src/open_trader/trend_market_controller.py:1187-1320`
- Test: `tests/test_trend_market_controller.py:1780-1940`
- Modify: `docs/superpowers/specs/2026-07-21-trend-market-controller-design.md`

**Interfaces:**
- Modifies: `_record_legacy_cycle_cutover(..., report_missing: bool = False) -> Path`.
- Preserves: `_legacy_cycle_cutover(config, cycle) -> bool` and the default
  report-bound behavior for facts without `report_missing`.

- [ ] **Step 1: Prove the missing-cycle transition is RED**

Add an HK-like historical cycle with no report, create its revision request,
call `_record_legacy_cycle_cutover(..., report_missing=True)`, and assert the
fact stores null report identity, `_execution_completed` is true,
`_cycle_to_reconcile` advances, and no batch/action artifacts exist.

Run:

```bash
.venv/bin/python -m pytest tests/test_trend_market_controller.py::test_missing_report_cutover_skips_exact_expired_cycle -q
```

Expected: FAIL because `report_missing` is not accepted yet.

- [ ] **Step 2: Implement the minimum writer/reader branch**

For `report_missing=True`, require `_revision_baseline(config, cycle) ==
(None, None, -1)`, no matching artifact in `_report_dir(config, market)`, a
pending revision request with null path/SHA and revision `-1`, no completion,
an expired execution window, no batch, executor host, and valid actor/reason/
timezone-aware authorization. Write `report_missing: true`, null report path
and SHA, and the existing request path/SHA binding. On every read, recompute
the same absence. Keep the old report-bound branch unchanged when the field is
absent or false.

- [ ] **Step 3: Add fail-closed boundary tests**

Prove: an existing report cannot be marked missing; a report appearing after
the fact invalidates completion; missing cutovers retain executor, operator,
expired-window, no-batch, and immutable-collision guards; existing report-bound
cutover tests still pass.

- [ ] **Step 4: Verify and commit without touching live state**

Run focused missing/default cutover tests, then:

```bash
.venv/bin/python -m pytest tests/test_trend_market_controller.py -q
.venv/bin/python -m pytest tests/test_trend_market_cli.py -q
.venv/bin/python -m pytest tests/test_trend_review.py -q
git diff --check
```

Expected: all suites pass without warnings. Commit source, tests, design, and
plan together. Do not deploy, run acceptance, or write a shared cutover fact.

---

## Spec-Coverage Checklist

- One controller per CN/HK/US market, one operational namespace, and unchanged ordinary premarket: Tasks 4–6.
- Executor-only reports/orders/protection/closes/notifications and automatic readonly copies: Tasks 1, 2, 4–6.
- Missing-report catchup, same-identity retry, frozen-delivery retry, protection priority, inside/outside-window outcomes, batch lock, later-revision anomaly: Task 4.
- Stable action identity, numbered remarks, broker-before-submit, cross-machine migration, conflict/uncertain fail-closed behavior: Task 2.
- Explicit immutable resolution, capped partial buys, merged/remaining-only sells: Task 3.
- Atomic status, deduplicated alerts, Dashboard health/action states: Tasks 4 and 7.
- Fenced migration, readonly cleanup, no direct old-watcher rollback, live process/log verification: Tasks 6 and 8.
- SHA-bound, operator-authorized, no-backfill cutover for unreplayable expired legacy cycles: Task 9.
- Explicitly authorized missing-report historical cutover with continuous
  absence validation and mandatory current/future reports: Task 10.
- Focused tests, full suite, safe direct workflow, live process restart, final acceptance PASS, exact-SHA redeploy: Task 8.
