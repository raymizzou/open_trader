# Futu Controller Connection Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the five-second trend controllers from creating thousands of Futu contexts while preserving strategy frequency and order idempotency.

**Architecture:** Each controller process holds one quote client and lazily holds one read-only simulation-account client. Existing one-shot APIs keep their current ownership defaults; the controller explicitly borrows clients. Actual order clients remain short-lived. Futu quote setup uses the SDK's async connection mode and a finite synchronous-query connection timeout.

**Tech Stack:** Python 3.12, futu-api 10.08.6808, pytest, launchd, GNU Screen.

## Global Constraints

- Keep protection checks at exactly 5 seconds.
- Do not change trading decisions, reports, or order duplicate prevention.
- Do not add a connection pool, daemon, dependency, or two-hour soak.
- Use focused tests and live checks while developing; run `make acceptance` only as the final gate.
- After PASS, redeploy the exact accepted SHA and verify PID, cwd, SHA, fresh logs, and HTTP 200.

---

### Task 1: Bound quote connection setup

**Files:**
- Modify: `src/open_trader/futu_quote.py:78-91`
- Test: `tests/test_futu_quote.py`

**Interfaces:**
- Consumes: `OpenQuoteContext(..., is_async_connect=True)` and `set_sync_query_connect_timeout()`.
- Produces: `_default_context_factory(*, host: str, port: int) -> Any` with a 3-second connection wait bound.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_futu_quote.py`:

```python
import sys
from types import SimpleNamespace

import open_trader.futu_quote as futu_quote


def test_default_context_factory_uses_bounded_async_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    class Context:
        def __init__(self, **kwargs: object) -> None:
            calls.append(kwargs)
            self.timeout: float | None = None

        def set_sync_query_connect_timeout(self, timeout: float) -> None:
            self.timeout = timeout

    monkeypatch.setitem(
        sys.modules, "futu", SimpleNamespace(OpenQuoteContext=Context)
    )
    context = futu_quote._default_context_factory(
        host="127.0.0.1", port=11111
    )

    assert calls == [{
        "host": "127.0.0.1",
        "port": 11111,
        "is_async_connect": True,
    }]
    assert context.timeout == 3.0
```

- [ ] **Step 2: Verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_futu_quote.py::test_default_context_factory_uses_bounded_async_connection -q
```

Expected: FAIL because the current factory is synchronous and never sets the timeout.

- [ ] **Step 3: Implement the minimum factory change**

Replace the final return in `_default_context_factory` with:

```python
    context = OpenQuoteContext(
        host=host,
        port=port,
        is_async_connect=True,
    )
    context.set_sync_query_connect_timeout(3.0)
    return context
```

- [ ] **Step 4: Verify GREEN and commit**

```bash
.venv/bin/python -m pytest tests/test_futu_quote.py -q
git add src/open_trader/futu_quote.py tests/test_futu_quote.py
git commit -m "fix: bound Futu quote connection setup"
```

Expected: all quote tests PASS.

### Task 2: Borrow one account client for repeated projections

**Files:**
- Modify: `src/open_trader/a_share_trend.py:902-930`
- Test: `tests/test_a_share_trend.py`

**Interfaces:**
- Consumes: optional `account_client: object | None` with `account_snapshot()`.
- Produces: `load_futu_simulate_trend_account(...) -> AccountSnapshot`; supplied clients remain open, factory-created clients still close.

- [ ] **Step 1: Write the failing borrowed-client test**

Add to `tests/test_a_share_trend.py`:

```python
def test_futu_simulation_account_borrows_existing_client() -> None:
    class Client:
        closed = False

        def account_snapshot(self) -> dict[str, object]:
            return {
                "acc_id": 101,
                "net_value": "100",
                "cash": "100",
                "positions": [],
            }

        def close(self) -> None:
            self.closed = True

    client = Client()
    account = load_futu_simulate_trend_account(
        host="127.0.0.1",
        port=11111,
        simulate_acc_id=101,
        market="CN",
        expected_date="2026-07-22",
        account_client=client,
        account_factory=lambda **_kwargs: pytest.fail(
            "borrowed account opened another context"
        ),
    )

    assert account.net_value == Decimal("100")
    assert client.closed is False
```

- [ ] **Step 2: Verify RED**

```bash
.venv/bin/python -m pytest tests/test_a_share_trend.py::test_futu_simulation_account_borrows_existing_client -q
```

Expected: FAIL with `unexpected keyword argument 'account_client'`.

- [ ] **Step 3: Implement explicit ownership**

Add `account_client: object | None = None` to the function signature and replace client setup/cleanup with:

```python
    owns_client = account_client is None
    client = account_client
    if client is None:
        client = account_factory(
            host=host,
            port=port,
            simulate_acc_id=simulate_acc_id,
            trd_market=market,
        )
    try:
        snapshot = client.account_snapshot()
    finally:
        close = getattr(client, "close", None)
        if owns_client and callable(close):
            close()
```

Keep the existing market/account/snapshot boundary validation unchanged.

- [ ] **Step 4: Verify GREEN and commit**

```bash
.venv/bin/python -m pytest tests/test_a_share_trend.py -q
git add src/open_trader/a_share_trend.py tests/test_a_share_trend.py
git commit -m "fix: reuse Futu simulation account reads"
```

Expected: all account tests PASS, including existing factory-owned close tests.

### Task 3: Make watcher quote ownership explicit

**Files:**
- Modify: `src/open_trader/a_share_trend_watch.py:82-430`
- Modify: `src/open_trader/market_trend_watch.py:81-215`
- Test: `tests/test_a_share_trend_watch.py`
- Test: `tests/test_market_trend_watch.py`

**Interfaces:**
- Consumes: `close_quote_client: bool = True` on both watcher functions.
- Produces: unchanged legacy ownership by default; a controller passes `False`, so a successful once-pass leaves its quote open and a quote failure is re-raised for controller reset.

- [ ] **Step 1: Write failing borrowed-quote tests**

Add to `tests/test_a_share_trend_watch.py`, using the existing `SequenceQuote`, `portfolio`, and notifier helpers:

```python
def test_once_watcher_does_not_close_borrowed_quote(tmp_path: Path) -> None:
    quote = SequenceQuote([], trading_days=["2026-07-22"])
    result = watch_a_share_protection(
        portfolio_path=portfolio(tmp_path, symbol=None),
        state_path=tmp_path / "state.json",
        events_path=tmp_path / "events.jsonl",
        quote_client=quote,
        close_quote_client=False,
        notifier=NullNotifier(),
        poll_seconds=5,
        reconnect_seconds=5,
        once=True,
        now_fn=lambda: datetime.fromisoformat("2026-07-22T09:31:00+08:00"),
    )

    assert result.status == "completed"
    assert quote.closed is False
```

Add to `tests/test_market_trend_watch.py`:

```python
def test_once_market_watcher_reraises_failure_for_borrowed_quote(
    tmp_path: Path,
) -> None:
    class Quote:
        closed = False

        def get_trading_days(self, **_kwargs: object) -> list[str]:
            raise watcher_error("calendar offline")

        def close(self) -> None:
            self.closed = True

    quote = Quote()
    now = datetime(2026, 7, 22, 10, 0, tzinfo=ZoneInfo("Asia/Hong_Kong"))

    with pytest.raises(FutuQuoteError, match="calendar offline"):
        watch_market_protection(
            market="HK",
            data_dir=tmp_path / "data",
            portfolio_path=tmp_path / "unused.csv",
            state_path=tmp_path / "state.json",
            events_path=tmp_path / "events.jsonl",
            report_lock_path=tmp_path / "report.lock",
            quote_client=quote,
            close_quote_client=False,
            notifier=NullNotifier(),
            poll_seconds=5,
            reconnect_seconds=5,
            once=True,
            now_fn=lambda: now,
        )

    assert quote.closed is False
```

- [ ] **Step 2: Verify RED**

```bash
.venv/bin/python -m pytest \
  tests/test_a_share_trend_watch.py::test_once_watcher_does_not_close_borrowed_quote \
  tests/test_market_trend_watch.py::test_once_market_watcher_reraises_failure_for_borrowed_quote \
  -q
```

Expected: both FAIL with `unexpected keyword argument 'close_quote_client'`.

- [ ] **Step 3: Implement A-share watcher ownership**

Add `close_quote_client: bool = True` immediately after `quote_client`. In `outcome`, replace the initial client check with:

```python
        if client is None or not close_quote_client:
            return result
```

In all three `except FutuQuoteError` blocks, after recording the interruption and before clearing/closing `client`, add:

```python
                if once and not close_quote_client:
                    raise
```

This preserves the current reconnect loop for long-running standalone watchers.

- [ ] **Step 4: Implement and propagate market watcher ownership**

Add `close_quote_client: bool = True` immediately after `quote_client`. In the calendar failure block, after recording the interruption and before clearing/closing the client, add:

```python
            if once and not close_quote_client:
                raise
```

In the account-loader failure block, replace unconditional close with:

```python
        if close_quote_client:
            try:
                _close(client)
            except Exception:
                pass
```

Pass the flag to the delegated A-share watcher:

```python
        quote_client=client,
        close_quote_client=close_quote_client,
```

- [ ] **Step 5: Verify both watcher suites and commit**

```bash
.venv/bin/python -m pytest \
  tests/test_a_share_trend_watch.py \
  tests/test_market_trend_watch.py \
  -q
git add \
  src/open_trader/a_share_trend_watch.py \
  src/open_trader/market_trend_watch.py \
  tests/test_a_share_trend_watch.py \
  tests/test_market_trend_watch.py
git commit -m "fix: preserve controller-owned quote connections"
```

Expected: all watcher tests PASS and legacy close assertions remain green.

### Task 4: Reuse clients across controller loops

**Files:**
- Modify: `src/open_trader/trend_market_controller.py:34,320-347,679-735,1582-1647,1657-2092`
- Test: `tests/test_trend_market_controller.py`

**Interfaces:**
- Consumes: borrowed quote/account support from Tasks 2 and 3.
- Produces: optional borrowed quote parameters on `_derive_cycle` and `_cycle_to_reconcile`, optional borrowed quote/account loader parameters on `_run_protection_pass`, and process-lifetime clients in `run_trend_market_controller`.
- Preserves: `_new_order_client`, `_execute_locked_report`, and `_run_stop` remain short-lived action paths.

- [ ] **Step 1: Write the failing multi-loop regression**

Add to `tests/test_trend_market_controller.py`:

```python
def test_controller_reuses_quote_and_account_clients_across_loops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = replace(
        controller_config(tmp_path), trend_review_cn_simulate_acc_id=101
    )
    monkeypatch.setattr(socket, "gethostname", lambda: "executor")
    write_report(config)
    quote_clients: list[object] = []
    account_clients: list[object] = []

    class Quote:
        closed = False

        def get_trading_days(self, **_kwargs: object) -> list[str]:
            return ["2026-07-17", "2026-07-20", "2026-07-21"]

        def close(self) -> None:
            self.closed = True

    class Account:
        closed = False

        def close(self) -> None:
            self.closed = True

    def quote_factory(**_kwargs: object) -> object:
        quote = Quote()
        quote_clients.append(quote)
        return quote

    def account_factory(**_kwargs: object) -> object:
        account = Account()
        account_clients.append(account)
        return account

    def protect(
        _config: DailyPremarketConfig,
        _market: str,
        day: str,
        *,
        quote_client: object,
        account_loader: Callable[..., object],
    ) -> object:
        assert quote_client is quote_clients[0]
        account_loader(
            config.portfolio,
            expected_date=day,
            timezone=controller.TIMEZONES["CN"],
        )
        return protection_success()

    monkeypatch.setattr(controller, "FutuQuoteClient", quote_factory)
    monkeypatch.setattr(
        controller, "FutuSimulateOrderExecutionClient", account_factory
    )
    monkeypatch.setattr(
        controller,
        "load_futu_simulate_trend_account",
        lambda **kwargs: SimpleNamespace(positions=())
        if kwargs["account_client"] is account_clients[0]
        else pytest.fail("controller did not borrow its account client"),
    )
    monkeypatch.setattr(controller, "_run_protection_pass", protect)
    monkeypatch.setattr(
        controller,
        "_cycle_to_reconcile",
        lambda *_args, **_kwargs: active_cn_cycle(),
    )
    monkeypatch.setattr(controller, "_execution_due", lambda *_args: False)
    monkeypatch.setattr(controller, "_close_completed", lambda *_args: True)
    monkeypatch.setattr(
        controller, "_record_status", lambda *_args, **kwargs: kwargs
    )
    monkeypatch.setattr(
        controller,
        "_new_order_client",
        lambda *_args: pytest.fail("idle loop opened order client"),
    )

    class StopLoop(RuntimeError):
        pass

    sleeps = 0

    def stop_after_two_loops(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps == 2:
            raise StopLoop

    with pytest.raises(StopLoop):
        run_trend_market_controller(
            config,
            "CN",
            now_fn=lambda: NOW,
            sleep_fn=stop_after_two_loops,
        )

    assert len(quote_clients) == 1
    assert len(account_clients) == 1
    assert quote_clients[0].closed is True
    assert account_clients[0].closed is True
```

Add `Callable` to the test module's `collections.abc` import when needed.
Update existing monkeypatched `_derive_cycle`, `_cycle_to_reconcile`, and
`_run_protection_pass` fakes that are exercised through the controller loop to
accept `**_kwargs`; their return values and assertions stay unchanged.

- [ ] **Step 2: Verify RED**

```bash
.venv/bin/python -m pytest tests/test_trend_market_controller.py::test_controller_reuses_quote_and_account_clients_across_loops -q
```

Expected: FAIL because current helpers do not accept shared clients and the old loop constructs fresh contexts.

- [ ] **Step 3: Let calendar helpers borrow a quote**

Change `_derive_cycle` to accept a keyword-only `quote_client: object | None = None`, and replace construction/cleanup with:

```python
    owns_quote = quote_client is None
    quote = quote_client
    if quote is None:
        quote = FutuQuoteClient(
            host=config.futu_host, port=config.futu_port
        )
    try:
        trading_days = sorted(
            date.fromisoformat(item)
            for item in quote.get_trading_days(
                market=market,
                start=(today - timedelta(days=35)).isoformat(),
                end=(today + timedelta(days=35)).isoformat(),
            )
        )
    finally:
        if owns_quote:
            quote.close()
```

Add the same keyword-only parameter to `_cycle_to_reconcile` and pass `quote_client=quote_client` to its two nested `_derive_cycle` calls.

- [ ] **Step 4: Let protection borrow both readers**

Change the signature to:

```python
def _run_protection_pass(
    config: DailyPremarketConfig,
    market: str,
    trading_date: str,
    *,
    quote_client: object | None = None,
    account_loader: Callable[..., object] | None = None,
) -> object:
```

Only create the existing factory-backed account-loader closure when the argument is `None`. In both watcher calls use:

```python
        quote_client=quote_client,
        close_quote_client=quote_client is None,
```

Keep `quote_client_factory=quote_factory` for standalone/reconnect behavior.

- [ ] **Step 5: Hold and reset controller resources**

Import `FutuQuoteError`. After controller counters are initialized, add:

```python
    quote_client: object | None = None
    account_client: object | None = None

    def close_client(client: object | None) -> None:
        close = getattr(client, "close", None)
        if callable(close):
            close()

    def shared_quote() -> object:
        nonlocal quote_client
        if quote_client is None:
            quote_client = FutuQuoteClient(
                host=config.futu_host, port=config.futu_port
            )
        return quote_client

    def reset_quote() -> None:
        nonlocal quote_client
        close_client(quote_client)
        quote_client = None

    def load_account(
        _path: Path, *, expected_date: str, timezone: ZoneInfo
    ) -> object:
        nonlocal account_client
        del timezone
        account_id = require_trend_review_config(config, market)
        if account_client is None:
            account_client = FutuSimulateOrderExecutionClient(
                host=config.futu_host,
                port=config.futu_port,
                simulate_acc_id=account_id,
                trd_market=market,
            )
        try:
            return load_futu_simulate_trend_account(
                host=config.futu_host,
                port=config.futu_port,
                simulate_acc_id=account_id,
                market=market,
                expected_date=expected_date,
                account_client=account_client,
            )
        except Exception:
            close_client(account_client)
            account_client = None
            raise
```

Pass `quote_client=shared_quote()` and `account_loader=load_account` to the open-session protection pass. Pass the same quote to `_derive_cycle` and `_cycle_to_reconcile`.

When a protection/calendar/operation exception is a `FutuQuoteError`, call `reset_quote()` before recording the existing blocker. Do not reset a quote for domain abnormalities such as a missing protection line.

In the controller `finally`, close the two resources before pool/lock cleanup:

```python
        close_client(account_client)
        close_client(quote_client)
        pool.shutdown(wait=not once, cancel_futures=True)
        lock.__exit__(None, None, None)
```

Do not pass these readers into `_new_order_client`, `_execute_locked_report`, or `_run_stop`.

- [ ] **Step 6: Verify affected suites and commit**

```bash
.venv/bin/python -m pytest \
  tests/test_futu_quote.py \
  tests/test_a_share_trend.py \
  tests/test_a_share_trend_watch.py \
  tests/test_market_trend_watch.py \
  tests/test_trend_market_controller.py \
  -q
git add src/open_trader/trend_market_controller.py tests/test_trend_market_controller.py
git commit -m "fix: reuse trend controller Futu connections"
```

Expected: all affected tests PASS and the regression reports one quote/account construction across two loops.

### Task 5: Restore live services and pass acceptance

**Files:**
- No source changes unless a failing check first gets a focused red test in the relevant task.

**Interfaces:**
- Consumes: committed implementation and `/Users/ray/projects/open_trader/config/daily_premarket.env`.
- Produces: healthy OpenD protocol, three controllers on the accepted SHA, stable connection counts, and Dashboard acceptance PASS.

- [ ] **Step 1: Run the full suite before live mutation**

```bash
.venv/bin/python -m pytest -q
```

Expected: all tests PASS. Do not run `make acceptance` yet.

- [ ] **Step 2: Stop retrying controllers and restart OpenD**

```bash
launchctl bootout "gui/$(id -u)/com.open-trader.trend-market-controller.cn" 2>/dev/null || true
launchctl bootout "gui/$(id -u)/com.open-trader.trend-market-controller.hk" 2>/dev/null || true
launchctl bootout "gui/$(id -u)/com.open-trader.trend-market-controller.us" 2>/dev/null || true
osascript -e 'tell application "Futu_OpenD" to quit'
open -a Futu_OpenD
```

Run a real protocol query:

```bash
PYTHONPATH=src .venv/bin/python - <<'PY'
from datetime import date
from open_trader.futu_quote import FutuQuoteClient

client = FutuQuoteClient(host="127.0.0.1", port=11111)
try:
    today = date.today().isoformat()
    print(client.get_trading_days(market="HK", start=today, end=today))
finally:
    client.close()
PY
```

Expected: the query terminates and prints a list; context construction must not hang.

- [ ] **Step 3: Install committed controllers**

```bash
./scripts/install_daily_premarket_launchd.sh \
  --trend-only \
  --market all \
  --config /Users/ray/projects/open_trader/config/daily_premarket.env
launchctl list | rg 'com.open-trader.trend-market-controller.(cn|hk|us)'
```

Expected: three live PIDs. `launchctl print` must show this worktree as every working directory.

- [ ] **Step 4: Prove connections stay stable over six fast samples**

Run the following six times, 5 seconds apart:

```bash
date '+%Y-%m-%dT%H:%M:%S%z'
netstat -anv -p tcp | awk '$5 ~ /\.11111$/ && $6 == "ESTABLISHED" {count++} END {print count+0}'
sleep 5
```

Expected: connection count stays within a fixed small band rather than increasing each sample. Fresh `data/trend_controller/{CN,HK,US}/status.json` heartbeats advance, and new logs contain no repeated `Abnormal event timeout` loop.

- [ ] **Step 5: Restart Dashboard on the implementation SHA**

```bash
screen -S open_trader_dashboard_8766 -X quit 2>/dev/null || true
screen -dmS open_trader_dashboard_8766 zsh -lc 'cd /Users/ray/projects/open_trader/.worktrees/trend-market-controller-spec && exec env PYTHONPATH=src .venv/bin/python -u -m open_trader dashboard --portfolio /Users/ray/projects/open_trader/data/latest/portfolio.csv --data-dir /Users/ray/projects/open_trader/data --reports-dir /Users/ray/projects/open_trader/reports --config /Users/ray/projects/open_trader/config/daily_premarket.env --poll-seconds 5 --host 127.0.0.1 --port 8766 >> /tmp/open_trader_dashboard_8766.log 2>&1'
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8766/
```

Expected: HTTP `200`; fresh log lines show real Futu/Tiger refreshes rather than OpenD-unreachable errors.

- [ ] **Step 6: Run the final gate once**

```bash
make acceptance
```

Expected: final status `PASS`. On `FAIL`, add a focused red test and fix the failing behavior before rerunning. On `BLOCKED`, report the blocker and do not claim completion.

- [ ] **Step 7: Redeploy the exact accepted SHA and verify review state**

Record `git rev-parse HEAD`, rerun the launchd installer and Dashboard screen command without source or data edits, then run:

```bash
git status --short
git rev-parse HEAD
launchctl print "gui/$(id -u)/com.open-trader.trend-market-controller.cn" | rg 'state =|pid =|program =|working directory =|stdout path =|stderr path ='
launchctl print "gui/$(id -u)/com.open-trader.trend-market-controller.hk" | rg 'state =|pid =|program =|working directory =|stdout path =|stderr path ='
launchctl print "gui/$(id -u)/com.open-trader.trend-market-controller.us" | rg 'state =|pid =|program =|working directory =|stdout path =|stderr path ='
ps -axo pid=,lstart=,command= | rg 'open_trader (trend-market-controller|dashboard)' | rg -v rg
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8766/
tail -n 80 /tmp/open_trader_dashboard_8766.log
```

Expected: clean worktree, exact accepted SHA, new PIDs, worktree cwd, fresh logs, and HTTP `200`. Only then provide `http://127.0.0.1:8766/`.

### Task 6: Persist watcher interruption state across once-passes

**Files:**
- Modify: `src/open_trader/a_share_trend_watch.py`
- Modify: `src/open_trader/market_trend_watch.py`
- Test: `tests/test_a_share_trend_watch.py`
- Test: `tests/test_market_trend_watch.py`

**Interfaces:**
- Consumes: existing immutable `monitor_interrupted` and `monitor_recovered` events.
- Produces: `_monitor_interrupted(events_path: Path) -> bool`; both watcher entry points restore their interruption state from the latest durable monitor event.

- [ ] **Step 1: Write failing consecutive once-pass tests**

For A-share, run two borrowed once-passes against failing calendar quotes using the same event path and notifier, then one successful pass and one new failure. Assert notification titles and event types are exactly:

```python
["A股价格监控中断", "A股价格监控恢复", "A股价格监控中断"]
```

```python
["monitor_interrupted", "monitor_recovered", "monitor_interrupted"]
```

For HK, run two borrowed once-passes against quotes whose `get_trading_days()` raises the same `FutuQuoteError`. Assert both raise to the controller, but only one `港股价格监控中断` notification and one `monitor_interrupted` event are produced.

- [ ] **Step 2: Verify RED**

```bash
.venv/bin/python -m pytest \
  tests/test_a_share_trend_watch.py::test_once_watcher_persists_interruption_until_recovery \
  tests/test_market_trend_watch.py::test_once_market_watcher_deduplicates_durable_interruption \
  -q
```

Expected: FAIL because every new once-pass resets `interrupted = False` and sends the same interruption again.

- [ ] **Step 3: Implement the shared durable-state lookup**

Add beside the existing interruption record helpers:

```python
def _monitor_interrupted(events_path: Path) -> bool:
    for event in reversed(load_watch_events(events_path)):
        event_type = event.get("event_type")
        if event_type in {"monitor_interrupted", "monitor_recovered"}:
            return event_type == "monitor_interrupted"
    return False
```

Initialize `interrupted = _monitor_interrupted(events_path)` in `watch_a_share_protection`. Import the helper in `market_trend_watch.py` and use the same initialization there. Do not add timers, counters, or a second notification ledger: the existing event stream already provides the required durable state.

- [ ] **Step 4: Verify GREEN and commit**

```bash
.venv/bin/python -m pytest \
  tests/test_a_share_trend_watch.py \
  tests/test_market_trend_watch.py \
  -q
.venv/bin/python -m pytest -q
git add \
  src/open_trader/a_share_trend_watch.py \
  src/open_trader/market_trend_watch.py \
  tests/test_a_share_trend_watch.py \
  tests/test_market_trend_watch.py \
  docs/superpowers/plans/2026-07-22-futu-controller-connection-lifecycle.md
git commit -m "fix: deduplicate persistent quote interruptions"
```

Expected: watcher suites and full suite PASS. Keep all controllers stopped until Task 4 connection reuse is implemented and reviewed.
