# US Session Price Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make each US holding show one compact price from its actual Futu market session, and use that same price for market value and unrealized P&L.

**Architecture:** Keep the watcher-facing `QuoteSnapshot` path unchanged. Add Dashboard-only snapshot and market-state methods to `FutuQuoteClient`, let `DashboardQuoteService` select one price deterministically, and keep the existing frontend valuation path consuming `last_price`. Render the selected session and quote time in the existing price cell while keeping the global fetch time in the Header.

**Tech Stack:** Python 3.12, stdlib dataclasses/Decimal/zoneinfo, futu-api 10.08, static HTML/CSS/JavaScript, pytest, Playwright, screen, make.

## Global Constraints

- Follow `docs/superpowers/specs/2026-07-15-us-session-price-dashboard-design.md` exactly.
- Preserve `FutuQuoteClient.get_snapshots()` and `QuoteSnapshot`; watchers remain盘中-only and must not call the new Dashboard methods.
- Futu `get_market_state()` is the sole session source of truth; do not infer sessions from browser or server clocks.
- Display exactly one price per US holding: `夜盘/盘前/盘中/盘后 + price + ET time`, or the actual source session plus `上一有效价`.
- Display `fetched_at` only in the Header as `Asia/Shanghai (CST)`; never repeat it per holding.
- Do not add dependencies, configuration, persistence, background tasks, or a frontend framework.
- Keep the Dashboard read-only and do not change notification, order, or watcher behavior.
- After each committed task: stop any stale listener on port 8766, remove the old runtime log, start the exact current SHA in `screen`, run `make acceptance`, and require `PASS` before continuing.
- After the final `PASS`, redeploy that exact accepted SHA once more and verify PID, cwd, SHA, fresh PID-bearing logs, and HTTP 200 before offering the review URL.

---

### Task 1: Add Dashboard-only Futu snapshot and state readers

**Files:**
- Modify: `src/open_trader/futu_quote.py:1-141`
- Modify: `tests/test_futu_quote.py:21-55, 190-230`

**Interfaces:**
- Consumes: existing `FutuQuoteClient.context.get_market_snapshot()` and `.get_market_state()`.
- Produces: `DashboardQuoteSnapshot` and `FutuQuoteClient.get_dashboard_snapshots(futu_symbols) -> dict[str, DashboardQuoteSnapshot]`.
- Produces: `FutuQuoteClient.get_market_states(futu_symbols) -> dict[str, str]`.
- Preserves: `FutuQuoteClient.get_snapshots(futu_symbols) -> dict[str, QuoteSnapshot]` unchanged for watchers.

- [ ] **Step 1: Add failing tests for four prices, update time, state parsing, and invalid values**

Extend `FakeOpenQuoteContext` with a state response and make its snapshot rows contain Dashboard fields:

```python
def get_market_state(self, symbols: list[str]) -> tuple[int, object]:
    self.requested_market_state_symbols = symbols
    return 0, FakeDataFrame([
        {"code": "US.VIXY", "market_state": "OVERNIGHT"},
        {"code": "US.QQQ", "market_state": "PRE_MARKET_BEGIN"},
    ])
```

Add these tests and import `DashboardQuoteSnapshot` from `open_trader.futu_quote`:

```python
def test_futu_quote_client_returns_dashboard_session_snapshots() -> None:
    class SessionContext(FakeOpenQuoteContext):
        def get_market_snapshot(self, symbols: list[str]) -> tuple[int, object]:
            self.requested_symbols = symbols
            return 0, FakeDataFrame([
                {
                    "code": "US.DRAM",
                    "last_price": "61.23",
                    "pre_price": "60.73",
                    "after_price": "62.22",
                    "overnight_price": "61.50",
                    "update_time": "2026-07-15 03:03:01.150",
                },
                {
                    "code": "US.BAD",
                    "last_price": "NaN",
                    "pre_price": "0",
                    "after_price": "-1",
                    "overnight_price": "",
                    "update_time": "2026-07-15 03:04:00",
                },
            ])

    client = FutuQuoteClient(
        host="127.0.0.1", port=11111,
        context_factory=SessionContext,
        connectivity_checker=lambda host, port: True,
    )

    assert client.get_dashboard_snapshots(["US.DRAM", "US.BAD"]) == {
        "US.DRAM": DashboardQuoteSnapshot(
            futu_symbol="US.DRAM",
            last_price=Decimal("61.23"),
            pre_price=Decimal("60.73"),
            after_price=Decimal("62.22"),
            overnight_price=Decimal("61.50"),
            update_time="2026-07-15 03:03:01.150",
        ),
        "US.BAD": DashboardQuoteSnapshot(
            futu_symbol="US.BAD",
            last_price=None,
            pre_price=None,
            after_price=None,
            overnight_price=None,
            update_time="2026-07-15 03:04:00",
        ),
    }


def test_futu_quote_client_returns_per_symbol_market_states() -> None:
    client = FutuQuoteClient(
        host="127.0.0.1", port=11111,
        context_factory=FakeOpenQuoteContext,
        connectivity_checker=lambda host, port: True,
    )

    assert client.get_market_states(["US.VIXY", "US.QQQ"]) == {
        "US.VIXY": "OVERNIGHT",
        "US.QQQ": "PRE_MARKET_BEGIN",
    }
    assert client.context.requested_market_state_symbols == ["US.VIXY", "US.QQQ"]


def test_futu_quote_client_keeps_watcher_snapshot_contract() -> None:
    client = FutuQuoteClient(
        host="127.0.0.1", port=11111,
        context_factory=FakeOpenQuoteContext,
        connectivity_checker=lambda host, port: True,
    )

    assert client.get_snapshots(["US.VIXY"]) == {
        "US.VIXY": QuoteSnapshot("US.VIXY", Decimal("94.5"))
    }
```

- [ ] **Step 2: Run the focused tests and confirm RED**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_futu_quote.py::test_futu_quote_client_returns_dashboard_session_snapshots \
  tests/test_futu_quote.py::test_futu_quote_client_returns_per_symbol_market_states \
  tests/test_futu_quote.py::test_futu_quote_client_keeps_watcher_snapshot_contract -q
```

Expected: collection or test failure because `DashboardQuoteSnapshot`, `get_dashboard_snapshots`, and `get_market_states` do not exist.

- [ ] **Step 3: Implement the minimal Dashboard-only Futu interface**

Add `dataclass` to the imports and define:

```python
@dataclass(frozen=True)
class DashboardQuoteSnapshot:
    futu_symbol: str
    last_price: Decimal | None
    pre_price: Decimal | None
    after_price: Decimal | None
    overnight_price: Decimal | None
    update_time: str


def _positive_decimal(value: object) -> Decimal | None:
    if value in {None, ""}:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() and parsed > 0 else None
```

Keep `get_snapshots()` watcher-facing, but reuse `_positive_decimal()` inside it. Add these methods to `FutuQuoteClient`:

```python
def get_dashboard_snapshots(
    self, futu_symbols: Sequence[str]
) -> dict[str, DashboardQuoteSnapshot]:
    requested = set(futu_symbols)
    ret_code, data = self.context.get_market_snapshot(list(futu_symbols))
    if ret_code != 0:
        self._raise_quote_error(str(data), error_type="snapshot_failed")
    snapshots: dict[str, DashboardQuoteSnapshot] = {}
    for record in data.to_dict("records"):
        code = str(record.get("code", "")).strip()
        if code not in requested:
            continue
        snapshots[code] = DashboardQuoteSnapshot(
            futu_symbol=code,
            last_price=_positive_decimal(record.get("last_price")),
            pre_price=_positive_decimal(record.get("pre_price")),
            after_price=_positive_decimal(record.get("after_price")),
            overnight_price=_positive_decimal(record.get("overnight_price")),
            update_time=str(record.get("update_time", "")).strip(),
        )
    return snapshots

def get_market_states(self, futu_symbols: Sequence[str]) -> dict[str, str]:
    requested = set(futu_symbols)
    ret_code, data = self.context.get_market_state(list(futu_symbols))
    if ret_code != 0:
        self._raise_quote_error(str(data), error_type="market_state_failed")
    return {
        code: str(record.get("market_state", "")).strip()
        for record in data.to_dict("records")
        if (code := str(record.get("code", "")).strip()) in requested
    }

def _raise_quote_error(self, message: str, *, error_type: str) -> None:
    interrupted = "网络中断" in message
    raise FutuQuoteError(
        message,
        error_type="quote_server_interrupted" if interrupted else error_type,
        next_step=QUOTE_INTERRUPTED_NEXT_STEP if interrupted else SNAPSHOT_FAILED_NEXT_STEP,
        opend_reachable=True,
        context_ok=True,
        snapshot_ok=error_type == "market_state_failed",
    )
```

Replace the duplicated `get_snapshots()` error branch with `_raise_quote_error(...)` and parse `last_price` through `_positive_decimal()`. Do not add session fields to `QuoteSnapshot`.

- [ ] **Step 4: Run the focused and watcher tests**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_futu_quote.py tests/test_futu_watch.py \
  tests/test_a_share_trend_watch.py tests/test_decision_plan_watch.py -q
```

Expected: PASS; watcher tests still construct the two-field `QuoteSnapshot`.

- [ ] **Step 5: Commit Task 1**

```bash
git add src/open_trader/futu_quote.py tests/test_futu_quote.py
git commit -m "feat: expose dashboard session quotes"
```

- [ ] **Step 6: Deploy the Task 1 SHA and run the required gate**

```bash
screen -S open_trader_dashboard_8766 -X quit 2>/dev/null || true
listener=$(lsof -tiTCP:8766 -sTCP:LISTEN 2>/dev/null || true)
test -z "$listener" || kill -TERM $listener
for attempt in {1..20}; do
  listener=$(lsof -tiTCP:8766 -sTCP:LISTEN 2>/dev/null || true)
  test -z "$listener" && break
  sleep 0.5
done
test -z "$(lsof -tiTCP:8766 -sTCP:LISTEN 2>/dev/null || true)"
rm -f /tmp/open_trader_dashboard_8766.log
screen -dmS open_trader_dashboard_8766 zsh -lc 'cd /Users/ray/projects/open_trader && exec env PYTHONPATH=src .venv/bin/python -u -m open_trader dashboard --portfolio data/latest/portfolio.csv --data-dir data --reports-dir reports --poll-seconds 5 --host 127.0.0.1 --port 8766 >> /tmp/open_trader_dashboard_8766.log 2>&1'
make acceptance
```

Expected: `PASS`. Fix any `FAIL`; report `BLOCKED` without substituting another check.

---

### Task 2: Select one valuation price in DashboardQuoteService

**Files:**
- Modify: `src/open_trader/dashboard_quotes.py:1-215`
- Modify: `tests/test_dashboard_quotes.py:1-350`
- Modify: `tests/test_dashboard_web.py:738-768` for the extended result constructor

**Interfaces:**
- Consumes: `DashboardQuoteSnapshot`, `get_dashboard_snapshots()`, and `get_market_states()` from Task 1.
- Produces per quote: `last_price`, `price_session`, `price_time`, `current_session_quote`, and `market_state`.
- Produces top-level: `fallback_count` and `us_session_status` while preserving existing missing/stale diagnostics.

- [ ] **Step 1: Replace the Dashboard fake client and write failing session-selection tests**

In `tests/test_dashboard_quotes.py`, make `FakeQuoteClient` accept Dashboard snapshots and market states:

```python
class FakeQuoteClient:
    def __init__(
        self,
        snapshots: dict[str, DashboardQuoteSnapshot],
        states: dict[str, str] | None = None,
        state_error: FutuQuoteError | None = None,
    ) -> None:
        self.snapshots = snapshots
        self.states = states or {}
        self.state_error = state_error
        self.requested_symbols: list[str] = []
        self.requested_state_symbols: list[str] = []
        self.closed = False

    def get_dashboard_snapshots(
        self, futu_symbols: Sequence[str]
    ) -> dict[str, DashboardQuoteSnapshot]:
        self.requested_symbols = list(futu_symbols)
        return self.snapshots

    def get_market_states(self, futu_symbols: Sequence[str]) -> dict[str, str]:
        self.requested_state_symbols = list(futu_symbols)
        if self.state_error is not None:
            raise self.state_error
        return self.states

    def close(self) -> None:
        self.closed = True
```

Add a helper and parameterized test:

```python
def session_snapshot(**prices: str | None) -> DashboardQuoteSnapshot:
    return DashboardQuoteSnapshot(
        futu_symbol="US.MSFT",
        last_price=Decimal(prices["last"]) if prices.get("last") else None,
        pre_price=Decimal(prices["pre"]) if prices.get("pre") else None,
        after_price=Decimal(prices["after"]) if prices.get("after") else None,
        overnight_price=Decimal(prices["overnight"]) if prices.get("overnight") else None,
        update_time="2026-07-15 03:03:01.150",
    )


@pytest.mark.parametrize(
    ("state", "expected_price", "expected_session"),
    [
        ("OVERNIGHT", "61.5", "overnight"),
        ("PRE_MARKET_BEGIN", "60.73", "pre_market"),
        ("MORNING", "61.23", "regular"),
        ("AFTER_HOURS_BEGIN", "62.22", "after_hours"),
    ],
)
def test_quote_service_selects_active_us_session_price(
    tmp_path: Path, state: str, expected_price: str, expected_session: str
) -> None:
    config = dashboard_config(tmp_path)
    write_portfolio(config.portfolio_path)
    snapshot = session_snapshot(
        last="61.23", pre="60.73", after="62.22", overnight="61.50"
    )
    client = FakeQuoteClient({"US.MSFT": snapshot, "US.AAPL": snapshot}, {
        "US.MSFT": state, "US.AAPL": state,
    })

    result = DashboardQuoteService(config, client_factory=lambda: client).refresh()
    quote = result.quotes["US.MSFT"]

    assert quote["last_price"] == expected_price
    assert quote["price_session"] == expected_session
    assert quote["price_time"] == "2026-07-15 03:03:01.150"
    assert quote["current_session_quote"] is True
    assert result.fallback_count == 0
```

Add tests for active fallback, normal closed state, and state-query degradation:

```python
def test_quote_service_labels_active_session_fallback_without_fake_time(tmp_path: Path) -> None:
    config = dashboard_config(tmp_path)
    write_portfolio(config.portfolio_path)
    snapshot = session_snapshot(last="61.23", pre=None, after="62.22", overnight=None)
    client = FakeQuoteClient({"US.MSFT": snapshot, "US.AAPL": snapshot}, {
        "US.MSFT": "OVERNIGHT", "US.AAPL": "OVERNIGHT",
    })

    result = DashboardQuoteService(config, client_factory=lambda: client).refresh()
    quote = result.quotes["US.MSFT"]

    assert result.status == "partial"
    assert result.fallback_count == 2
    assert quote["last_price"] == "62.22"
    assert quote["price_session"] == "after_hours"
    assert quote["price_time"] == ""
    assert quote["current_session_quote"] is False
    assert "当前时段无报价" in result.diagnostic["message"]


def test_quote_service_treats_closed_fallback_as_normal(tmp_path: Path) -> None:
    config = dashboard_config(tmp_path)
    write_portfolio(config.portfolio_path)
    snapshot = session_snapshot(last="61.23", pre="60.73", after="62.22", overnight="61.50")
    client = FakeQuoteClient({"US.MSFT": snapshot, "US.AAPL": snapshot}, {
        "US.MSFT": "CLOSED", "US.AAPL": "CLOSED",
    })

    result = DashboardQuoteService(config, client_factory=lambda: client).refresh()

    assert result.status == "ok"
    assert result.fallback_count == 0
    assert result.us_session_status == "closed"
    assert result.quotes["US.MSFT"]["price_session"] == "after_hours"
    assert result.quotes["US.MSFT"]["current_session_quote"] is False


def test_quote_service_degrades_to_regular_price_when_market_state_fails(tmp_path: Path) -> None:
    config = dashboard_config(tmp_path)
    write_portfolio(config.portfolio_path)
    snapshot = session_snapshot(last="61.23", pre="60.73", after="62.22", overnight="61.50")
    error = FutuQuoteError("state failed", error_type="market_state_failed", snapshot_ok=True)
    client = FakeQuoteClient(
        {"US.MSFT": snapshot, "US.AAPL": snapshot}, state_error=error
    )

    result = DashboardQuoteService(config, client_factory=lambda: client).refresh()

    assert result.status == "partial"
    assert result.us_session_status == "unknown"
    assert result.quotes["US.MSFT"]["last_price"] == "61.23"
    assert result.quotes["US.MSFT"]["price_session"] == ""
    assert result.quotes["US.MSFT"]["current_session_quote"] is False
    assert result.diagnostic["error_type"] == "market_state_failed"


def test_quote_service_degrades_when_any_us_market_state_is_missing(tmp_path: Path) -> None:
    config = dashboard_config(tmp_path)
    write_portfolio(config.portfolio_path)
    snapshot = session_snapshot(last="61.23", pre="60.73", after="62.22", overnight="61.50")
    client = FakeQuoteClient(
        {"US.MSFT": snapshot, "US.AAPL": snapshot},
        {"US.MSFT": "OVERNIGHT"},
    )

    result = DashboardQuoteService(config, client_factory=lambda: client).refresh()

    assert result.status == "partial"
    assert result.us_session_status == "unknown"
    assert result.quotes["US.MSFT"]["last_price"] == "61.23"
    assert result.quotes["US.MSFT"]["price_session"] == ""
    assert "市场状态不可用" in result.diagnostic["message"]
```

- [ ] **Step 2: Run the new service tests and confirm RED**

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_dashboard_quotes.py -q
```

Expected: FAIL because the protocol, result model, and selection fields are absent.

- [ ] **Step 3: Implement deterministic selection and diagnostics**

Import `DashboardQuoteSnapshot`, replace the Dashboard protocol methods, and append `fallback_count: int = 0` and `us_session_status: str = ""` to `QuoteRefreshResult`. Include both in `to_dict()`.

Define the deterministic orders:

```python
ACTIVE_US_SESSION_ORDERS = {
    "OVERNIGHT": ("overnight", "after_hours", "regular", "pre_market"),
    "PRE_MARKET_BEGIN": ("pre_market", "overnight", "after_hours", "regular"),
    "MORNING": ("regular", "pre_market", "overnight", "after_hours"),
    "AFTERNOON": ("regular", "pre_market", "overnight", "after_hours"),
    "AFTER_HOURS_BEGIN": ("after_hours", "regular", "pre_market", "overnight"),
}
INACTIVE_US_SESSION_ORDERS = {
    "PRE_MARKET_END": ("pre_market", "overnight", "after_hours", "regular"),
    "WAITING_OPEN": ("pre_market", "overnight", "after_hours", "regular"),
    "AFTER_HOURS_END": ("after_hours", "regular", "pre_market", "overnight"),
}
CLOSED_US_SESSION_ORDER = ("after_hours", "regular", "pre_market", "overnight")


def _session_prices(snapshot: DashboardQuoteSnapshot) -> dict[str, Decimal | None]:
    return {
        "regular": snapshot.last_price,
        "pre_market": snapshot.pre_price,
        "after_hours": snapshot.after_price,
        "overnight": snapshot.overnight_price,
    }


def _select_us_price(
    snapshot: DashboardQuoteSnapshot, market_state: str
) -> tuple[Decimal | None, str, bool, str]:
    active_order = ACTIVE_US_SESSION_ORDERS.get(market_state)
    order = active_order or INACTIVE_US_SESSION_ORDERS.get(
        market_state, CLOSED_US_SESSION_ORDER
    )
    prices = _session_prices(snapshot)
    for index, session in enumerate(order):
        if price := prices[session]:
            current = active_order is not None and index == 0
            return price, session, current, snapshot.update_time if current else ""
    return None, "", False, ""
```

In `refresh()`, call `get_dashboard_snapshots()` for the full universe, then call `get_market_states()` only for `US.*` symbols. Catch a market-state `FutuQuoteError` separately so the snapshots remain usable. Build rows through `_quote_row(...)`, count only active-state fallbacks, and set:

```python
status = "partial" if missing_count or fallback_count or state_error else "ok"
cacheable = missing_count == 0 and state_error is None
if cacheable:
    self.last_success_at = fetched_at
    self.last_quotes = {symbol: dict(quote) for symbol, quote in quotes.items()}
```

Derive `us_session_status` from successfully returned US states: `active` when every state is in `ACTIVE_US_SESSION_ORDERS`, `closed` when none is active, `mixed` when both kinds occur, and `unknown` when state lookup failed or returned no US states. If any requested US symbol is absent from the returned state map, treat the state lookup as incomplete: use regular prices for all US rows, set `us_session_status="unknown"`, and return `partial` with the market-state diagnostic. Normal `closed` does not increment `fallback_count` and does not make the quote refresh partial.

For US rows with no state, choose `snapshot.last_price` with blank session and time. For non-US rows, keep the current plain `last_price` behavior. Add all four new API fields to successful and missing rows so the schema is stable.

Create one `_partial_diagnostic(missing_count, fallback_count, state_error)` helper that joins only present facts and retains the existing OpenD metadata. Use exact user-facing fragments:

```python
if fallback_count:
    messages.append(f"{fallback_count} 个标的当前时段无报价，已使用上一有效价。")
if state_error is not None:
    messages.append("美股市场状态不可用，已使用盘中价。")
```

- [ ] **Step 4: Update existing constructors and run backend tests**

Add `fallback_count=0` to `quote_result()` in `tests/test_dashboard_web.py`. Update existing Dashboard quote fixtures to use `DashboardQuoteSnapshot` and states; keep assertions for partial missing quotes and stale-cache preservation.

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_futu_quote.py tests/test_dashboard_quotes.py tests/test_dashboard_web.py \
  tests/test_futu_watch.py tests/test_a_share_trend_watch.py \
  tests/test_decision_plan_watch.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit Task 2**

```bash
git add src/open_trader/dashboard_quotes.py tests/test_dashboard_quotes.py tests/test_dashboard_web.py
git commit -m "feat: select US session valuation prices"
```

- [ ] **Step 6: Deploy the Task 2 SHA and run the required gate**

```bash
screen -S open_trader_dashboard_8766 -X quit 2>/dev/null || true
listener=$(lsof -tiTCP:8766 -sTCP:LISTEN 2>/dev/null || true)
test -z "$listener" || kill -TERM $listener
for attempt in {1..20}; do
  listener=$(lsof -tiTCP:8766 -sTCP:LISTEN 2>/dev/null || true)
  test -z "$listener" && break
  sleep 0.5
done
test -z "$(lsof -tiTCP:8766 -sTCP:LISTEN 2>/dev/null || true)"
rm -f /tmp/open_trader_dashboard_8766.log
screen -dmS open_trader_dashboard_8766 zsh -lc 'cd /Users/ray/projects/open_trader && exec env PYTHONPATH=src .venv/bin/python -u -m open_trader dashboard --portfolio data/latest/portfolio.csv --data-dir data --reports-dir reports --poll-seconds 5 --host 127.0.0.1 --port 8766 >> /tmp/open_trader_dashboard_8766.log 2>&1'
make acceptance
```

Expected: `PASS`.

---

### Task 3: Render one compact session price and one Header fetch time

**Files:**
- Modify: `src/open_trader/dashboard_static/dashboard.js:5407-5419, 5904-5912`
- Modify: `src/open_trader/dashboard_static/dashboard.css:142-146, before media queries at 3407`
- Modify: `tests/test_dashboard_web.py` near existing `run_dashboard_js` quote tests

**Interfaces:**
- Consumes quote fields from Task 2.
- Produces `.session-quote`, `.session-quote-label`, `.session-quote-price`, and `.session-quote-time` inside the existing price cell.
- Preserves plain price rendering for HK/CN and existing valuation through `quote.last_price`.

- [ ] **Step 1: Add failing pure-JavaScript rendering tests**

Add a `run_dashboard_js` test:

```python
def test_dashboard_renders_one_compact_us_session_price_and_header_time() -> None:
    output = run_dashboard_js(r'''
const active = renderQuotePrice({market:"US", asset_class:"stock"}, {
  last_price:"61.50", price_session:"overnight",
  price_time:"2026-07-15 03:03:01.150", current_session_quote:true,
});
if(!active.includes("夜盘") || !active.includes("61.50") || !active.includes("03:03 ET"))throw new Error(active);
if((active.match(/61\.50/g)||[]).length!==1)throw new Error("price repeated: "+active);
const fallback = renderQuotePrice({market:"US", asset_class:"option"}, {
  last_price:"0.59", price_session:"regular", price_time:"",
  current_session_quote:false,
});
if(!fallback.includes("盘中") || !fallback.includes("上一有效价"))throw new Error(fallback);
const hk = renderQuotePrice({market:"HK", asset_class:"stock"}, {last_price:"510"});
if(hk!=="510")throw new Error("non-US changed: "+hk);
if(quoteRefreshText({fetched_at:"2026-07-15T15:03:13+08:00",stale:false})!=="刷新于 2026-07-15 15:03:13 CST")throw new Error("bad header time");
if(quoteRefreshText({last_success_at:"2026-07-15T14:59:00+08:00",stale:true})!=="上次成功 2026-07-15 14:59:00 CST")throw new Error("bad stale time");
if(quoteStatusText({status:"ok",us_session_status:"closed",fallback_count:0,missing_count:0})!=="美股休市")throw new Error("bad closed status");
if(quoteStatusText({status:"partial",us_session_status:"active",fallback_count:2,missing_count:0})!=="部分标的当前时段无报价")throw new Error("bad fallback status");
console.log("ok");
''')
    assert "ok" in output
```

- [ ] **Step 2: Run the test and confirm RED**

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_dashboard_web.py::test_dashboard_renders_one_compact_us_session_price_and_header_time -q
```

Expected: FAIL because the session markup and `quoteRefreshText()` do not exist.

- [ ] **Step 3: Implement compact rendering and Header formatting**

Add these helpers beside `renderQuotePrice()`:

```javascript
function sessionQuoteLabel(value) {
  return ({overnight: "夜盘", pre_market: "盘前", regular: "盘中", after_hours: "盘后"})[value] || "";
}

function quoteTimeEt(value) {
  const match = String(value || "").match(/\b\d{4}-\d{2}-\d{2}[ T](\d{2}:\d{2})/);
  return match ? `${match[1]} ET` : "";
}

function quoteRefreshText(payload) {
  const stale = Boolean(payload && payload.stale);
  const raw = stale ? payload.last_success_at : (payload.fetched_at || payload.last_success_at);
  if (!hasValue(raw)) return stale ? "尚无成功行情" : "尚未刷新";
  const text = String(raw).replace("T", " ").replace(/[+-]\d{2}:\d{2}$/, "");
  return `${stale ? "上次成功" : "刷新于"} ${text} CST`;
}

function quoteStatusText(payload) {
  if (payload && payload.fallback_count > 0 && payload.missing_count === 0) {
    return "部分标的当前时段无报价";
  }
  if (payload && payload.status === "ok" && payload.us_session_status === "closed") {
    return "美股休市";
  }
  return quoteStatusLabel(payload && payload.status);
}
```

Replace the successful US branch in `renderQuotePrice()` with:

```javascript
const session = String(holding && holding.market || "").toUpperCase() === "US"
  ? sessionQuoteLabel(quote.price_session) : "";
if (!session) return escapeHtml(String(quote.last_price));
const detail = quote.current_session_quote
  ? quoteTimeEt(quote.price_time)
  : "上一有效价";
return `<span class="session-quote"><span class="session-quote-label">${escapeHtml(session)}</span><strong class="session-quote-price">${escapeHtml(String(quote.last_price))}</strong>${detail ? `<span class="session-quote-time">· ${escapeHtml(detail)}</span>` : ""}</span>`;
```

In `renderQuoteStatus()`, replace the `last-refresh` assignment with:

```javascript
elements["last-refresh"].textContent = quoteRefreshText(payload);
```

Use `quoteStatusText(payload)` for the non-stale `quote-status` pill so active fallbacks say `部分标的当前时段无报价` and normal closure says `美股休市`.

Add compact CSS before the responsive media queries:

```css
.session-quote {
  align-items: baseline;
  display: inline-flex;
  gap: 4px;
  white-space: nowrap;
}

.session-quote-label {
  color: var(--accent);
  font-size: 11px;
  font-weight: 700;
}

.session-quote-price {
  color: inherit;
  font-size: inherit;
  font-variant-numeric: tabular-nums;
}

.session-quote-time {
  color: var(--muted);
  font-size: 10px;
  font-weight: 500;
}
```

Do not add a second row, card, tooltip, expand control, or four-price grid.

- [ ] **Step 4: Run static frontend and server tests**

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_dashboard_web.py tests/test_dashboard_quotes.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit Task 3**

```bash
git add src/open_trader/dashboard_static/dashboard.js \
  src/open_trader/dashboard_static/dashboard.css tests/test_dashboard_web.py
git commit -m "feat: render compact US session prices"
```

- [ ] **Step 6: Deploy the Task 3 SHA and run the required gate**

```bash
screen -S open_trader_dashboard_8766 -X quit 2>/dev/null || true
listener=$(lsof -tiTCP:8766 -sTCP:LISTEN 2>/dev/null || true)
test -z "$listener" || kill -TERM $listener
for attempt in {1..20}; do
  listener=$(lsof -tiTCP:8766 -sTCP:LISTEN 2>/dev/null || true)
  test -z "$listener" && break
  sleep 0.5
done
test -z "$(lsof -tiTCP:8766 -sTCP:LISTEN 2>/dev/null || true)"
rm -f /tmp/open_trader_dashboard_8766.log
screen -dmS open_trader_dashboard_8766 zsh -lc 'cd /Users/ray/projects/open_trader && exec env PYTHONPATH=src .venv/bin/python -u -m open_trader dashboard --portfolio data/latest/portfolio.csv --data-dir data --reports-dir reports --poll-seconds 5 --host 127.0.0.1 --port 8766 >> /tmp/open_trader_dashboard_8766.log 2>&1'
make acceptance
```

Expected: `PASS`.

---

### Task 4: Make the acceptance gate enforce the new API and browser contract

**Files:**
- Modify: `src/open_trader/dashboard_acceptance.py:140-181, 257-390, 450-505`
- Modify: `tests/test_dashboard_acceptance.py`

**Interfaces:**
- Consumes live `/api/quotes` and `.session-quote` markup.
- Produces acceptance failures for missing session metadata, duplicate/expanded price displays, missing Header CST time, and mobile overflow.

- [ ] **Step 1: Add failing quote-payload validation tests**

Import `validate_quotes_payload` and add:

```python
def valid_quotes_payload() -> dict[str, object]:
    return {
        "status": "ok",
        "fetched_at": "2026-07-15T15:03:13+08:00",
        "us_session_status": "active",
        "quotes": {
            "US.DRAM": {
                "market": "US", "symbol": "DRAM", "last_price": "61.5",
                "price_session": "overnight", "price_time": "2026-07-15 03:03:01",
                "current_session_quote": True, "market_state": "OVERNIGHT",
            }
        },
    }


def test_validate_quotes_payload_accepts_one_selected_us_session_price() -> None:
    assert validate_quotes_payload(valid_quotes_payload()) == []


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("last_price", "", "价格无效"),
        ("price_session", "", "时段缺失"),
        ("market_state", "", "市场状态缺失"),
        ("price_time", "", "当前时段行情时间缺失"),
    ],
)
def test_validate_quotes_payload_rejects_incomplete_current_quote(
    field: str, value: object, expected: str
) -> None:
    payload = valid_quotes_payload()
    payload["quotes"]["US.DRAM"][field] = value  # type: ignore[index]
    assert any(expected in error for error in validate_quotes_payload(payload))
```

- [ ] **Step 2: Add a failing browser-contract unit test**

Create `_check_session_prices(page)` tests with fake locators that return:

```text
Header: 刷新于 2026-07-15 15:03:13 CST
US price cells: 夜盘 61.50 · 03:03 ET
```

Assert it rejects a price cell containing two session labels, a Header without `CST`, a current price without `ET`, any per-row `CST`, and a mobile price whose right edge exceeds the viewport. Extend the fake locator with `bounding_box()` and the fake page with `viewport_size` for this assertion.

- [ ] **Step 3: Run the focused acceptance tests and confirm RED**

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_dashboard_acceptance.py -k 'quotes_payload or session_prices' -q
```

Expected: FAIL because both acceptance helpers are absent.

- [ ] **Step 4: Implement API and browser acceptance checks**

Add:

```python
SESSION_LABELS = ("夜盘", "盘前", "盘中", "盘后")
SESSION_KEYS = {"overnight", "pre_market", "regular", "after_hours"}


def validate_quotes_payload(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not payload.get("fetched_at"):
        errors.append("行情 API 缺少全局获取时间")
    if payload.get("us_session_status") not in {"active", "closed", "mixed"}:
        errors.append("行情 API 缺少有效的美股时段状态")
    us_quotes = [
        quote for quote in (payload.get("quotes") or {}).values()
        if quote.get("market") == "US"
    ]
    if not us_quotes:
        errors.append("行情 API 没有美股报价")
    for quote in us_quotes:
        symbol = str(quote.get("symbol", ""))
        try:
            price = Decimal(str(quote.get("last_price", "")))
        except (InvalidOperation, ValueError):
            price = Decimal("0")
        if not price.is_finite() or price <= 0:
            errors.append(f"US.{symbol} 价格无效")
        if quote.get("price_session") not in SESSION_KEYS:
            errors.append(f"US.{symbol} 时段缺失")
        if not quote.get("market_state"):
            errors.append(f"US.{symbol} 市场状态缺失")
        if quote.get("current_session_quote") is True and not quote.get("price_time"):
            errors.append(f"US.{symbol} 当前时段行情时间缺失")
    return errors


def _check_session_prices(page: Any) -> None:
    header = page.locator("#last-refresh").inner_text().strip()
    assert "CST" in header, "Header 获取时间缺少 CST"
    prices = page.locator(
        '.account-holding-row:visible .account-holding-price .session-quote'
    )
    assert prices.count() >= 1, "美股持仓没有分时段价格"
    for index in range(prices.count()):
        price = prices.nth(index)
        text = re.sub(r"\s+", " ", price.inner_text()).strip()
        assert sum(label in text for label in SESSION_LABELS) == 1, "单个标的展示了多个时段"
        assert "CST" not in text, "标的行重复展示全局获取时间"
        assert "ET" in text or "上一有效价" in text, "标的价格没有时间或回退说明"
        if page.viewport_size and page.viewport_size["width"] <= 500:
            box = price.bounding_box()
            assert box is not None, "无法读取标的价格位置"
            assert box["x"] + box["width"] <= page.viewport_size["width"] + 1, (
                "移动端标的价格超出视口"
            )
```

Add a separate fetcher so the existing Dashboard API contract stays untouched:

```python
def _fetch_quotes_payload(url: str) -> dict[str, Any]:
    with urlopen(f"{url.rstrip('/')}/api/quotes", timeout=15) as response:
        if response.status != 200:
            raise RuntimeError(f"Quotes API HTTP {response.status}")
        return json.load(response)
```

In `main()`, fetch `first_quotes` immediately after `first`, validate it, fetch `second_quotes` immediately after `second`, and validate it:

```python
first_quotes = _fetch_quotes_payload(args.url)
errors.extend(validate_quotes_payload(first_quotes))
# existing wait and second Dashboard fetch
second_quotes = _fetch_quotes_payload(args.url)
errors.extend(validate_quotes_payload(second_quotes))
```

Call `_check_session_prices(page)` after `_check_account_holdings(page)` in both desktop and mobile browser passes. Preserve the existing Dashboard payload, refresh-signature, and source checks.

- [ ] **Step 5: Run acceptance unit tests**

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_dashboard_acceptance.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit Task 4**

```bash
git add src/open_trader/dashboard_acceptance.py tests/test_dashboard_acceptance.py
git commit -m "test: enforce US session prices in acceptance"
```

- [ ] **Step 7: Deploy the Task 4 SHA and run the required gate**

```bash
screen -S open_trader_dashboard_8766 -X quit 2>/dev/null || true
listener=$(lsof -tiTCP:8766 -sTCP:LISTEN 2>/dev/null || true)
test -z "$listener" || kill -TERM $listener
for attempt in {1..20}; do
  listener=$(lsof -tiTCP:8766 -sTCP:LISTEN 2>/dev/null || true)
  test -z "$listener" && break
  sleep 0.5
done
test -z "$(lsof -tiTCP:8766 -sTCP:LISTEN 2>/dev/null || true)"
rm -f /tmp/open_trader_dashboard_8766.log
screen -dmS open_trader_dashboard_8766 zsh -lc 'cd /Users/ray/projects/open_trader && exec env PYTHONPATH=src .venv/bin/python -u -m open_trader dashboard --portfolio data/latest/portfolio.csv --data-dir data --reports-dir reports --poll-seconds 5 --host 127.0.0.1 --port 8766 >> /tmp/open_trader_dashboard_8766.log 2>&1'
make acceptance
```

Expected: `PASS`, including live `/api/quotes`, two refresh cycles, desktop and mobile session-price checks.

---

### Task 5: Run live comparison, final gate, and exact-SHA deployment

**Files:**
- Verify only; no source changes expected.

**Interfaces:**
- Consumes: current OpenD, live portfolio, `/api/quotes`, `screen`, acceptance gate.
- Produces: final evidence and review URL.

- [ ] **Step 1: Run the complete automated suite and watcher regression set**

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_futu_watch.py tests/test_a_share_trend_watch.py \
  tests/test_decision_plan_watch.py tests/test_market_trend_watch.py -q
```

Expected: PASS with no watcher contract failures.

- [ ] **Step 2: Compare a real US quote with raw Futu session data**

Run this read-only check:

```bash
PYTHONPATH=src .venv/bin/python - <<'PY'
import json
from decimal import Decimal
from urllib.request import urlopen
from futu import OpenQuoteContext

with urlopen("http://127.0.0.1:8766/api/quotes", timeout=15) as response:
    dashboard = json.load(response)
symbol = next(key for key in dashboard["quotes"] if key.startswith("US."))
ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
try:
    snapshot_code, snapshots = ctx.get_market_snapshot([symbol])
    state_code, states = ctx.get_market_state([symbol])
finally:
    ctx.close()
assert snapshot_code == 0 and state_code == 0
raw = snapshots.to_dict("records")[0]
state = states.to_dict("records")[0]["market_state"]
selected = dashboard["quotes"][symbol]
assert selected["market_state"] == state
assert selected["price_session"] in {"overnight", "pre_market", "regular", "after_hours"}
assert float(selected["last_price"]) > 0
source_field = {
    "overnight": "overnight_price",
    "pre_market": "pre_price",
    "regular": "last_price",
    "after_hours": "after_price",
}[selected["price_session"]]
assert Decimal(str(selected["last_price"])) == Decimal(str(raw[source_field]))
if selected["current_session_quote"]:
    assert selected["price_time"] == str(raw["update_time"])
print(json.dumps({
    "symbol": symbol,
    "market_state": state,
    "selected_session": selected["price_session"],
    "selected_price": selected["last_price"],
    "raw_update_time": raw.get("update_time"),
}, ensure_ascii=False))
PY
```

Expected: one JSON object whose Dashboard state matches Futu and whose selected price is positive.

- [ ] **Step 3: Inspect processes before the final gate**

```bash
screen -ls | rg 'open_trader_dashboard_8766'
lsof -nP -iTCP:8766 -sTCP:LISTEN
ps -axo pid,lstart,command | rg 'open_trader dashboard .*--port 8766'
```

Expected: exactly one 8766 listener from `/Users/ray/projects/open_trader`; stop any orphaned pre-change process before continuing.

- [ ] **Step 4: Run the final acceptance gate**

```bash
make acceptance
```

Expected: exact final line contains `"status": "PASS"`. On `FAIL`, fix and repeat Task 4 verification; on `BLOCKED`, report the blocker and do not offer the task for review.

- [ ] **Step 5: Redeploy the exact accepted SHA**

After `PASS`, capture the SHA and restart without source or data changes:

```bash
accepted_sha=$(git rev-parse HEAD)
screen -S open_trader_dashboard_8766 -X quit 2>/dev/null || true
listener=$(lsof -tiTCP:8766 -sTCP:LISTEN 2>/dev/null || true)
test -z "$listener" || kill -TERM $listener
for attempt in {1..20}; do
  listener=$(lsof -tiTCP:8766 -sTCP:LISTEN 2>/dev/null || true)
  test -z "$listener" && break
  sleep 0.5
done
test -z "$(lsof -tiTCP:8766 -sTCP:LISTEN 2>/dev/null || true)"
log_size=$(stat -f '%z' /tmp/open_trader_dashboard_8766.log 2>/dev/null || echo 0)
screen -dmS open_trader_dashboard_8766 zsh -lc 'cd /Users/ray/projects/open_trader && exec env PYTHONPATH=src .venv/bin/python -u -m open_trader dashboard --portfolio data/latest/portfolio.csv --data-dir data --reports-dir reports --poll-seconds 5 --host 127.0.0.1 --port 8766 >> /tmp/open_trader_dashboard_8766.log 2>&1'
```

- [ ] **Step 6: Verify the review deployment**

```bash
for attempt in {1..20}; do
  code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 3 http://127.0.0.1:8766/ 2>/dev/null || true)
  test "$code" = "200" && break
  sleep 1
done
pid=$(lsof -tiTCP:8766 -sTCP:LISTEN)
cwd=$(lsof -a -p "$pid" -d cwd -Fn | sed -n 's/^n//p')
running_sha=$(git -C "$cwd" rev-parse HEAD)
curl -sS --max-time 15 http://127.0.0.1:8766/api/quotes >/dev/null
test "$code" = "200"
test "$cwd" = "/Users/ray/projects/open_trader"
test "$running_sha" = "$accepted_sha"
tail -c +$((log_size + 1)) /tmp/open_trader_dashboard_8766.log | rg "\\| $pid \\|"
printf 'review_url=http://127.0.0.1:8766\npid=%s\nsha=%s\n' "$pid" "$running_sha"
```

Expected: HTTP 200, one new PID, exact accepted SHA, fresh log lines containing that PID, and review URL `http://127.0.0.1:8766`.
