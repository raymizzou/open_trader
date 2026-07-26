# Prediction Market Arbitrage Monitor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Do not spawn subagents unless the user explicitly selects that execution mode.

**Goal:** Run a read-only Polymarket binary-bundle arbitrage watcher continuously on the user's Mac, preserve confirmed signal history, and add the user-approved A prediction-market workspace to the existing Dashboard.

**Architecture:** A concrete watcher refreshes the top 20 events every five minutes, consumes the public market WebSocket, confirms candidates with one paired `/books` request, and writes a throttled runtime snapshot plus formal signal lifecycles to stdlib SQLite. The existing Dashboard reads that database through one new read-only API and renders the locked A prototype. One launchd job runs the watcher through `caffeinate -s`.

**Tech Stack:** Python 3.12, stdlib `urllib`/`sqlite3`/`Decimal`/`fcntl`, `websockets>=15,<16` synchronous client, existing vanilla HTML/CSS/JavaScript Dashboard, pytest, Playwright-backed `make acceptance`, macOS launchd.

## Global Constraints

- Work only in `/Users/ray/projects/open_trader/.worktrees/prediction-arbitrage-scanner` on branch `feat/prediction-arbitrage-scanner`, based on local `main` at `f98812d`.
- The approved UI source of truth is prototype branch
  `prototype/prediction-market-ui` at commit `193fac7`.
- Implement Variant A exactly. If implementation needs any new user-visible
  state, label, section, navigation item, or layout not present in that
  prototype or the design, stop and obtain UI approval first.
- Add top navigation in this exact order: `持仓`, `预测市场`, `策略回测`,
  `凯利实验室`. Add no bottom navigation.
- Every monitored event visibly shows `24h 成交量` and its value.
- Event order is exact: signal-eligible first; then group-appropriate profit
  descending; then 24-hour volume descending; then stable event ID.
- Fee-unverified markets remain monitored and visible but never become formal
  signals. V1 supports only fee-free formal signals.
- Formal signal thresholds are fixed at `$0.01` net edge per pair and `$1.00`
  estimated total net profit. Do not add configuration for them in V1.
- Use public endpoints only. Add no credential, wallet, authentication, order,
  notification, execution, generic exchange interface, ORM, task queue, or
  event bus.
- Persist formal signals indefinitely. Do not persist raw WebSocket frames or
  raw order-book ticks.
- Keep the implementation concrete to Polymarket. Kalshi, Predict.fun,
  NegRisk bundles, and cross-platform matching remain deferred.
- Use the repository-root virtual environment
  `/Users/ray/projects/open_trader/.venv`; the isolated worktree has no local
  `.venv`.
- Follow red-green-refactor for every behavior. Observe the focused test fail
  for the intended reason before changing production code.
- Do not run `make acceptance` after intermediate changes. Run it once as the
  final gate after focused tests, full tests, live API checks, process
  deployment, and commits are ready.
- Only `make acceptance` `PASS` qualifies the task as complete or ready for
  user review. `FAIL` must be fixed; `BLOCKED` must be reported as blocked.
- Before merging to `main`, the dated operator-facing `CHANGELOG.md` entry must
  already be committed.

---

## File Map

### Add

- `src/open_trader/prediction_arbitrage.py`: immutable domain values, Decimal
  bundle math, thresholds, and deterministic event sorting.
- `src/open_trader/polymarket_public.py`: public Gamma/CLOB HTTPS client and
  response validation.
- `src/open_trader/polymarket_stream.py`: public market WebSocket session,
  heartbeat, subscription deltas, and message parsing.
- `src/open_trader/prediction_arbitrage_store.py`: SQLite schema, runtime
  snapshot, signal lifecycle, and history queries.
- `src/open_trader/prediction_arbitrage_watch.py`: one concrete continuous
  watcher.
- `ops/launchd/com.open-trader.prediction-arbitrage.plist.template`: persistent
  AC-awake launchd job.
- `scripts/install_prediction_arbitrage_launchd.sh`: render, load, restart, and
  verify the watcher.
- `scripts/uninstall_prediction_arbitrage_launchd.sh`: stop and remove only the
  prediction watcher.
- `tests/test_prediction_arbitrage.py`
- `tests/test_polymarket_public.py`
- `tests/test_polymarket_stream.py`
- `tests/test_prediction_arbitrage_store.py`
- `tests/test_prediction_arbitrage_watch.py`
- `tests/test_prediction_arbitrage_cli.py`
- `tests/test_prediction_arbitrage_launchd.py`

### Modify

- `pyproject.toml`: declare the direct WebSocket dependency.
- `src/open_trader/cli.py`: add `prediction-arb watch|status`.
- `src/open_trader/dashboard_web.py`: add the read-only prediction API.
- `src/open_trader/dashboard_static/index.html`: add the locked top navigation
  and prediction workspace mount.
- `src/open_trader/dashboard_static/dashboard.js`: fetch, poll, and render the
  approved four UI states.
- `src/open_trader/dashboard_static/dashboard.css`: recreate approved A
  responsive styling within the warm-ledger shell.
- `src/open_trader/dashboard_acceptance.py`: make watcher/data/order/UI checks
  mandatory in `make acceptance`.
- `tests/test_dashboard_web.py`: API and exact static UI contracts.
- `tests/test_dashboard_acceptance.py`: strict live acceptance contracts and
  screenshot matrix.
- `README.md` and `README.zh-CN.md`: operation, scope, cost, and status commands.
- `CHANGELOG.md`: dated operator-facing delivery entry.

---

### Task 1: Add Decimal Bundle Math and the Locked Sort Rule

**Files:**

- Create: `tests/test_prediction_arbitrage.py`
- Create: `src/open_trader/prediction_arbitrage.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class BookTop:
    token_id: str
    ask_price: Decimal
    ask_size: Decimal
    timestamp_ms: int

@dataclass(frozen=True)
class BundleCandidate:
    gross_unit_edge: Decimal
    executable_size: Decimal
    gross_profit_upper_bound: Decimal
    net_unit_edge: Decimal | None
    estimated_net_profit: Decimal | None
    formal: bool

def calculate_bundle(
    yes: BookTop,
    no: BookTop,
    *,
    minimum_order_size: Decimal,
    fee_verified_zero: bool,
) -> BundleCandidate: ...

def monitored_event_sort_key(event: Mapping[str, object]) -> tuple[object, ...]: ...
```

- [ ] **Step 1: Write the failing calculation and ordering tests**

Cover:

- best sizes `80` and `100` produce executable size `80`
- YES `0.47` plus NO `0.50` produces gross/net unit edge `0.03`
- formal threshold equality at net edge `0.01` and profit `1.00` is included
- insufficient minimum size, edge below `0.01`, or profit below `1.00` is not
  formal
- an unverified-fee market exposes only gross values and is never formal
- an eligible event sorts before an ineligible event with a larger gross profit
- same eligibility sorts profit descending
- equal profit sorts 24-hour volume descending
- complete equality sorts stable event ID ascending
- missing profit sorts after finite profit in the same eligibility group

- [ ] **Step 2: Run the focused test and verify RED**

```bash
cd /Users/ray/projects/open_trader/.worktrees/prediction-arbitrage-scanner
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_prediction_arbitrage.py -q
```

Expected: collection fails because `open_trader.prediction_arbitrage` does not
exist.

- [ ] **Step 3: Implement the smallest pure domain module**

Use fixed constants:

```python
MIN_NET_EDGE = Decimal("0.01")
MIN_ESTIMATED_PROFIT = Decimal("1.00")
```

Validate finite positive sizes, prices in `(0, 1)`, and timezone-independent
integer timestamps. Keep serialization out of this module.

The sort key should follow:

```python
(
    0 if event["signal_eligible"] else 1,
    -profit if profit is not None else Decimal("Infinity"),
    -volume_24h,
    event_id,
)
```

Eligible events use `estimated_net_profit`; ineligible events use
`gross_profit_upper_bound`.

- [ ] **Step 4: Run the focused test and verify GREEN**

Run the command from Step 2.

Expected: all tests in `tests/test_prediction_arbitrage.py` pass.

- [ ] **Step 5: Commit the domain rule**

```bash
git add src/open_trader/prediction_arbitrage.py tests/test_prediction_arbitrage.py
git diff --cached --check
git commit -m "feat: define prediction bundle signals"
```

---

### Task 2: Read and Validate Public Polymarket REST Data

**Files:**

- Create: `tests/test_polymarket_public.py`
- Create: `src/open_trader/polymarket_public.py`

**Interfaces:**

```python
class PolymarketPublicError(RuntimeError): ...

class PolymarketPublicClient:
    def fetch_top_events(self, limit: int = 20) -> tuple[dict[str, object], ...]: ...
    def fetch_market_details(self, condition_id: str) -> dict[str, object]: ...
    def fetch_books(self, token_ids: tuple[str, str]) -> tuple[BookTop, BookTop]: ...
```

- [ ] **Step 1: Write failing fake-HTTP tests**

Inject an opener callable; do not patch global networking in the implementation.
Assert:

- Gamma request uses exactly `active=true`, `closed=false`, `limit=20`,
  `order=volume24hr`, and `ascending=false`
- nested inactive, closed, non-order-book, non-binary, or malformed markets are
  skipped and counted
- JSON-encoded `outcomes` and `clobTokenIds` are mapped in YES/NO order
- event `volume24hr` is finite and non-negative
- CLOB details accept formal eligibility only when `tbf == 0` and fee curve
  rate is absent/disabled/zero
- Gamma eligibility requires explicit `feesEnabled=false` and
  `takerBaseFee=0`; missing or contradictory Gamma fields are monitoring-only
- missing, non-zero, malformed, or contradictory fee values return
  `fee_unverified`
- CLOB `mos` becomes the market minimum order size
- `/books` sends exactly two token IDs in one POST
- returned books are matched by `asset_id`, not response position
- duplicate, unknown, or missing IDs fail closed
- unsorted ask arrays choose the minimum valid ask
- timestamps older than 10 seconds or skewed by more than 2 seconds fail
- HTTP, JSON, and top-level type failures raise `PolymarketPublicError` with no
  credential or response-body leakage

- [ ] **Step 2: Run the REST tests and verify RED**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_polymarket_public.py -q
```

Expected: collection fails because the client module does not exist.

- [ ] **Step 3: Implement fixed public endpoints with stdlib**

Use:

```python
GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
CLOB_URL = "https://clob.polymarket.com"
```

Use `urllib.request.Request`, `urlopen`, `json`, and finite timeouts. Do not add
`requests`, an SDK, credential parameters, or a generic HTTP wrapper.

Return normalized dictionaries containing only fields consumed by the watcher
and Dashboard. Serialize `Decimal` later as strings.

- [ ] **Step 4: Run the REST and domain tests**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_polymarket_public.py tests/test_prediction_arbitrage.py -q
```

Expected: both files pass.

- [ ] **Step 5: Commit the public REST boundary**

```bash
git add src/open_trader/polymarket_public.py tests/test_polymarket_public.py
git diff --cached --check
git commit -m "feat: read public Polymarket market data"
```

---

### Task 3: Add the Public Market WebSocket Session

**Files:**

- Create: `tests/test_polymarket_stream.py`
- Create: `src/open_trader/polymarket_stream.py`
- Modify: `pyproject.toml`

**Interfaces:**

```python
@dataclass(frozen=True)
class MarketStreamUpdate:
    token_id: str
    best_ask: Decimal | None
    ask_size: Decimal | None
    timestamp_ms: int

class PolymarketMarketStream:
    def __enter__(self) -> "PolymarketMarketStream": ...
    def __exit__(self, *exc: object) -> None: ...
    def receive(self, timeout: float) -> tuple[MarketStreamUpdate, ...]: ...
    def update_subscriptions(
        self, *, add: set[str], remove: set[str]
    ) -> None: ...
    @property
    def last_pong_at(self) -> datetime | None: ...
```

- [ ] **Step 1: Write failing protocol tests with a fake socket**

Assert:

- URL is
  `wss://ws-subscriptions-clob.polymarket.com/ws/market`
- initial frame contains sorted token IDs, `type=market`, and
  `custom_feature_enabled=true`
- `PING` is sent every 10 seconds and `PONG` updates health
- add/remove frames exactly use documented `operation=subscribe|unsubscribe`
- JSON object and JSON array frames are accepted
- `book` selects the minimum valid ask
- the session seeds per-token ask levels from `book`, applies SELL
  `price_change` updates/removals, and emits the current best ask and size
- `best_bid_ask` without size can trigger REST recheck but cannot manufacture an
  executable size
- malformed, unknown, or out-of-range market frames are ignored without
  corrupting previous state
- connection close/error propagates as one concise stream exception

- [ ] **Step 2: Run the stream test and verify RED**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_polymarket_stream.py -q
```

Expected: collection fails because the stream module does not exist.

- [ ] **Step 3: Declare and make the dependency available**

Add:

```toml
"websockets>=15,<16",
```

to `[project].dependencies`, then run:

```bash
/Users/ray/projects/open_trader/.venv/bin/python -m pip install \
  "websockets>=15,<16"
```

- [ ] **Step 4: Implement one concrete synchronous session**

Use `websockets.sync.client.connect` with library protocol pings disabled;
Polymarket's text `PING`/`PONG` is the health contract. Inject `connect_fn` and
`monotonic_fn` only for tests.

Do not add asyncio, a background thread, a stream interface, or a reconnect
manager. Reconnect belongs to the watcher.

- [ ] **Step 5: Run stream and domain tests**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_polymarket_stream.py tests/test_prediction_arbitrage.py -q
```

Expected: both files pass.

- [ ] **Step 6: Commit the market stream**

```bash
git add pyproject.toml src/open_trader/polymarket_stream.py \
  tests/test_polymarket_stream.py
git diff --cached --check
git commit -m "feat: stream Polymarket order books"
```

---

### Task 4: Persist Runtime and Formal Signal Lifecycles in SQLite

**Files:**

- Create: `tests/test_prediction_arbitrage_store.py`
- Create: `src/open_trader/prediction_arbitrage_store.py`

**Interfaces:**

```python
class PredictionArbitrageStore:
    def __init__(self, data_dir: Path) -> None: ...
    def write_runtime(self, payload: Mapping[str, object]) -> None: ...
    def load_runtime(self) -> dict[str, object] | None: ...
    def open_signal(self, signal: Mapping[str, object]) -> None: ...
    def update_signal(self, signal: Mapping[str, object]) -> None: ...
    def close_signal(
        self, market_id: str, *, ended_at: str, reason: str
    ) -> None: ...
    def close_all_open(self, *, ended_at: str, reason: str) -> None: ...
    def active_signals(self) -> list[dict[str, object]]: ...
    def history(
        self, window: Literal["24h", "7d", "all"], *, now: datetime
    ) -> list[dict[str, object]]: ...
```

- [ ] **Step 1: Write failing SQLite lifecycle tests**

Cover:

- schema creation under
  `data/prediction_arbitrage/prediction_arbitrage.sqlite3`
- WAL mode and a non-zero busy timeout
- runtime singleton replacement
- open, peak update, close, and restart reads
- two open signals for one market are rejected
- `close_all_open(..., reason="watcher_restarted")`
- exact 24h, 7d, and all boundaries
- history newest-first
- JSON Decimal strings survive process/store recreation
- no table exists for ticks or raw frames

- [ ] **Step 2: Run the store test and verify RED**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_prediction_arbitrage_store.py -q
```

Expected: collection fails because the store module does not exist.

- [ ] **Step 3: Implement the two-table store**

Use stdlib `sqlite3`, parameterized SQL, one short connection per public method,
and explicit transactions. Store canonical JSON with sorted keys.

Create:

```sql
CREATE UNIQUE INDEX one_open_signal_per_market
ON signals(market_id) WHERE ended_at IS NULL;
CREATE INDEX signals_started_at ON signals(started_at DESC);
CREATE INDEX signals_ended_at ON signals(ended_at);
```

Do not add migrations, an ORM, repository interfaces, or tick storage.

- [ ] **Step 4: Run the store tests**

Run the command from Step 2.

Expected: all store tests pass.

- [ ] **Step 5: Commit persistence**

```bash
git add src/open_trader/prediction_arbitrage_store.py \
  tests/test_prediction_arbitrage_store.py
git diff --cached --check
git commit -m "feat: persist prediction signal history"
```

---

### Task 5: Orchestrate Continuous Discovery, Confirmation, and History

**Files:**

- Create: `tests/test_prediction_arbitrage_watch.py`
- Create: `src/open_trader/prediction_arbitrage_watch.py`

**Interfaces:**

```python
class PredictionArbitrageWatcher:
    def refresh_universe(self) -> None: ...
    def handle_updates(self, updates: tuple[MarketStreamUpdate, ...]) -> None: ...
    def publish_runtime(self, *, force: bool = False) -> None: ...
    def run(self) -> None: ...

def run_prediction_arbitrage_watch(data_dir: Path) -> None: ...
```

- [ ] **Step 1: Write failing deterministic watcher tests**

Use fake public data, stream sessions, clock, and real temporary SQLite. Cover:

- startup closes stale open signals as `watcher_restarted`
- first universe contains at most 20 events and all valid tokens
- five-minute refresh sends only token deltas
- fee-unverified markets remain in the runtime event/market counts
- fee-unverified markets never call `/books` for a formal signal
- paired WebSocket asks that meet thresholds trigger one same-batch REST
  confirmation
- every prospective formal signal first re-fetches CLOB market details and
  requires fee/minimum-size facts to remain safe
- a qualifying confirmation opens one signal
- repeated qualifying updates do not duplicate the episode
- later confirmation updates peak edge/profit
- candidate confirmation retries no faster than once per second
- a threshold loss closes the episode
- universe removal closes as `universe_removed`
- stream loss closes as `stream_stale`, records a blocker, and reconnects with
  delays `1, 2, 4, ... 60` seconds
- runtime snapshot is written at most once per second plus heartbeat
- heartbeat contains PID, working directory, fixed process Git SHA,
  `heartbeat_at`, `universe_refreshed_at`, WebSocket state, `last_pong_at`,
  reconnect count, and blocker
- runtime events use the locked eligibility/profit/volume/ID order

- [ ] **Step 2: Run watcher tests and verify RED**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_prediction_arbitrage_watch.py -q
```

Expected: collection fails because the watcher module does not exist.

- [ ] **Step 3: Implement the concrete watcher**

Use one process-lifetime `fcntl.flock` on:

```text
data/prediction_arbitrage/watcher.lock
```

Keep current books and confirmation cooldowns in dictionaries keyed by market
or token. Use a single loop; do not add threads or asyncio.

On every runtime snapshot:

- reduce each event to its best eligible net profit or ineligible gross upper
  bound
- apply `monitored_event_sort_key`
- serialize every `Decimal` as a string
- include exact participation/profit kinds so the Dashboard never guesses

- [ ] **Step 4: Run all prediction core tests**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_prediction_arbitrage.py \
  tests/test_polymarket_public.py \
  tests/test_polymarket_stream.py \
  tests/test_prediction_arbitrage_store.py \
  tests/test_prediction_arbitrage_watch.py -q
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit watcher orchestration**

```bash
git add src/open_trader/prediction_arbitrage_watch.py \
  tests/test_prediction_arbitrage_watch.py
git diff --cached --check
git commit -m "feat: watch prediction arbitrage continuously"
```

---

### Task 6: Add Watch and Status CLI Commands

**Files:**

- Create: `tests/test_prediction_arbitrage_cli.py`
- Modify: `src/open_trader/cli.py`

**CLI:**

```text
open-trader prediction-arb watch --data-dir PATH
open-trader prediction-arb status --data-dir PATH
```

- [ ] **Step 1: Write failing parser and routing tests**

Assert:

- nested command names and help text exist
- default data directory is `data`
- `watch` calls `run_prediction_arbitrage_watch` once
- an already-held watcher lock prints concise stderr and returns `2`
- `status` prints JSON with health, PID, heartbeat, event/market/token counts,
  current signals, and total history
- missing/stale runtime returns `2`; healthy runtime returns `0`
- neither command accepts credentials, wallet paths, execution flags, or
  notification flags

- [ ] **Step 2: Run CLI tests and verify RED**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_prediction_arbitrage_cli.py -q
```

Expected: parser rejects `prediction-arb`.

- [ ] **Step 3: Add the smallest nested CLI**

Place the parser next to other top-level operational commands. Route expected
watcher/public/store errors to one-line stderr and exit code `2`; allow
programming errors to remain visible in tests.

- [ ] **Step 4: Run focused CLI and existing dashboard CLI tests**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_prediction_arbitrage_cli.py tests/test_dashboard_cli.py -q
```

Expected: both files pass.

- [ ] **Step 5: Check real help output**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python \
  -m open_trader prediction-arb --help
```

Expected: only `watch` and `status` are listed.

- [ ] **Step 6: Commit CLI wiring**

```bash
git add src/open_trader/cli.py tests/test_prediction_arbitrage_cli.py
git diff --cached --check
git commit -m "feat: expose prediction watcher commands"
```

---

### Task 7: Add the Read-Only Dashboard API

**Files:**

- Modify: `tests/test_dashboard_web.py`
- Modify: `src/open_trader/dashboard_web.py`

**Interface:**

```python
def build_prediction_arbitrage_payload(
    data_dir: Path,
    *,
    window: Literal["24h", "7d", "all"],
    now: datetime | None = None,
) -> dict[str, object]: ...
```

- [ ] **Step 1: Write failing payload and HTTP tests**

Assert:

- `/api/prediction-arbitrage` defaults to `window=24h`
- `24h`, `7d`, and `all` are accepted; any other value returns HTTP 400
- a healthy store with current signals returns `status=live`
- a healthy store without current signals returns `status=quiet`
- heartbeat or PONG older than 30 seconds, universe older than 10 minutes, or a
  blocker returns `status=degraded`
- a missing database returns schema-valid degraded payload and HTTP 200
- summary counts agree with arrays
- current signals are active only
- history respects the requested window
- event order from the runtime snapshot is preserved
- no endpoint mutates the store

- [ ] **Step 2: Run focused API tests and verify RED**

Run only the new test names, for example:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_dashboard_web.py -q -k prediction_arbitrage_api
```

Expected: HTTP 404 or missing builder failure.

- [ ] **Step 3: Add the builder and GET route**

Parse the existing `urlparse(self.path)` result once and use `parse_qs`.
Instantiate `PredictionArbitrageStore(config.data_dir)` only for the prediction
route.

Keep the API schema exactly
`open_trader.prediction_arbitrage.dashboard.v1`. Do not merge prediction data
into `/api/dashboard`.

- [ ] **Step 4: Run focused and complete Dashboard web tests**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_dashboard_web.py -q
```

Expected: all Dashboard web tests pass.

- [ ] **Step 5: Commit the API**

```bash
git add src/open_trader/dashboard_web.py tests/test_dashboard_web.py
git diff --cached --check
git commit -m "feat: serve prediction arbitrage status"
```

---

### Task 8: Rebuild the Approved A UI in the Existing Dashboard

**Files:**

- Modify: `src/open_trader/dashboard_static/index.html`
- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Modify: `src/open_trader/dashboard_static/dashboard.css`
- Modify: `tests/test_dashboard_web.py`

**Locked UI:** Prototype branch `prototype/prediction-market-ui`, commit
`193fac7`, Variant A.

- [ ] **Step 1: Add failing exact static-contract tests**

Assert:

- top buttons are exactly `持仓`, `预测市场`, `策略回测`, `凯利实验室` in
  that order
- `预测市场` has its own workspace mount
- there is no bottom nav, mobile bottom nav, prototype controller, demo banner,
  or prototype file in production
- production contains the exact fee warning:
  `费用待核验市场仍会监控，但不会产生正式信号`
- required IDs/classes for summary cards, monitored list, current signals,
  history, and history window controls exist exactly once

- [ ] **Step 2: Add failing JavaScript render-contract tests**

Use the existing Dashboard JavaScript test harness patterns in
`tests/test_dashboard_web.py`. In one table-driven test provide deterministic
API payloads for:

- loading
- healthy with a current signal
- healthy with zero current signals
- degraded with retained rows

Assert:

- exact state copy and visible sections
- API event order is retained
- each event contains visible `24h 成交量` plus its value
- eligible rows say `可参与信号` and `最高预计净利润`
- ineligible rows say `仅监控 · 费用待核验` and `毛利润上限`
- missing profit displays `—`
- an event expands to its market rows
- current cards contain YES ask, NO ask, net edge, executable size, and expected
  profit
- `24 小时`, `7 天`, and `全部` request the matching API window
- zero history has explicit Chinese copy
- leaving the workspace stops only prediction polling; returning restarts it
- selected portfolio market/broker state survives navigation

- [ ] **Step 3: Run the new UI tests and verify RED**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_dashboard_web.py -q -k prediction_market
```

Expected: missing navigation/workspace/render functions.

- [ ] **Step 4: Modify the existing top navigation**

Reuse the current `strategy-tools` and `return-to-portfolio` click path:

- make `return-to-portfolio` the always-visible `持仓` top item
- add `open-prediction-market`
- keep existing strategy and Kelly buttons
- use `aria-current="page"` and the existing active-button visual language

Add `prediction_market` to `WORKSPACE_VIEWS`. Do not introduce a router or
client framework.

- [ ] **Step 5: Add the workspace state and polling**

Add only:

```javascript
predictionMarket: {
  payload: null,
  error: "",
  historyWindow: "24h",
  pollId: null,
}
```

Fetch every five seconds only while the workspace is open. Before the first
response render loading. On a later fetch failure retain the last payload and
render degraded/stale.

- [ ] **Step 6: Recreate Variant A**

Translate the approved prototype markup/CSS into production classes while
reusing existing warm-ledger tokens and `escapeHtml`.

Desktop:

```text
monitored events | current signals
                 | history
```

At 760 px and below, stack in reading order. Keep the top navigation and allow
it to wrap. No fixed or floating bottom controls.

- [ ] **Step 7: Run the complete Dashboard static/web tests**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_dashboard_web.py -q
```

Expected: all tests pass.

- [ ] **Step 8: Run a local browser comparison before continuing**

Start a disposable Dashboard from the feature worktree on a non-production
port with a temporary store containing the four deterministic states. Compare
desktop 1440×1000 and mobile 375×844 against the approved prototype.

Verify:

- no horizontal overflow
- no console or HTTP errors
- 44 px mobile controls
- event order and volume labels
- all four state variants

Do not run `make acceptance` yet.

- [ ] **Step 9: Commit the locked UI**

```bash
git add src/open_trader/dashboard_static/index.html \
  src/open_trader/dashboard_static/dashboard.js \
  src/open_trader/dashboard_static/dashboard.css \
  tests/test_dashboard_web.py
git diff --cached --check
git commit -m "feat: add prediction market dashboard"
```

---

### Task 9: Install the Persistent AC-Awake Mac Watcher

**Files:**

- Create: `tests/test_prediction_arbitrage_launchd.py`
- Create: `ops/launchd/com.open-trader.prediction-arbitrage.plist.template`
- Create: `scripts/install_prediction_arbitrage_launchd.sh`
- Create: `scripts/uninstall_prediction_arbitrage_launchd.sh`

- [ ] **Step 1: Write failing plist and dry-run installer tests**

Assert:

- label is exactly `com.open-trader.prediction-arbitrage`
- `RunAtLoad` and `KeepAlive` are true
- executable begins `/usr/bin/caffeinate -s`
- Python is the repository-root `.venv/bin/python`
- `PYTHONPATH` points at the selected worktree `src`
- working directory is the selected worktree
- data and logs use the shared repository root so history survives worktree
  replacement
- command is exactly `prediction-arb watch --data-dir ...`
- no credential, wallet, notification, or order argument exists
- dry-run writes no LaunchAgent and prints a parseable plist
- install stops the old label before replacement, bootstraps the new plist,
  kicks it, then waits for fresh healthy status
- residual old labels or an unmatched watcher process fail closed
- uninstall targets only the prediction label and plist

- [ ] **Step 2: Run launchd tests and verify RED**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_prediction_arbitrage_launchd.py -q
```

Expected: files are missing.

- [ ] **Step 3: Implement one small template and installer**

Resolve:

```text
worktree = git rev-parse --show-toplevel
shared root = parent of git rev-parse --path-format=absolute --git-common-dir
```

Use explicit resolved paths in the rendered plist. Create logs under:

```text
logs/prediction_arbitrage/launchd.out.log
logs/prediction_arbitrage/launchd.err.log
```

Poll status for at most 30 seconds. Do not reuse or extend the unrelated
daily-premarket installer.

- [ ] **Step 4: Run launchd tests and inspect dry-run output**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_prediction_arbitrage_launchd.py -q
scripts/install_prediction_arbitrage_launchd.sh --dry-run
```

Expected: tests pass and `plutil -lint` accepts the rendered plist.

- [ ] **Step 5: Commit deployment files**

```bash
git add ops/launchd/com.open-trader.prediction-arbitrage.plist.template \
  scripts/install_prediction_arbitrage_launchd.sh \
  scripts/uninstall_prediction_arbitrage_launchd.sh \
  tests/test_prediction_arbitrage_launchd.py
git diff --cached --check
git commit -m "feat: keep prediction watcher running on macOS"
```

---

### Task 10: Make Exact UI and Live Watcher Checks Mandatory in Acceptance

**Files:**

- Modify: `tests/test_dashboard_acceptance.py`
- Modify: `src/open_trader/dashboard_acceptance.py`

**New acceptance helpers:**

```python
def validate_prediction_arbitrage_payload(
    payload: Mapping[str, Any], *, now: datetime
) -> list[str]: ...

def _prediction_watcher_errors(
    payload: Mapping[str, Any],
    *,
    expected_root: Path,
    expected_sha: str,
) -> list[str]: ...

def _check_prediction_market(
    page: Any,
    payload: Mapping[str, Any],
    *,
    screenshot_path: Path,
) -> None: ...
```

- [ ] **Step 1: Extend the screenshot matrix in a failing test**

Require these additional fresh non-empty screenshots:

```text
wide_desktop-prediction-market.png
desktop-prediction-market.png
tablet-prediction-market.png
mobile-prediction-market.png
```

Keep the existing 1920×1080, 1440×1000, 760×1000, and 375×844 viewport
matrix.

- [ ] **Step 2: Add failing payload/order validation tests**

Cover:

- schema, venue, runtime fields, summary/array agreement
- heartbeat/PONG/universe freshness
- PID, working directory, Git SHA, and live process
- exact eligibility/profit/volume/ID sort rule
- explicit profit kind for every event
- visible volume fact available for every event
- active/history signal shape and Decimal fields
- fee-unverified market never appears in current/history formal signals

- [ ] **Step 3: Add failing browser-contract unit tests**

Using fake page/locator objects where existing acceptance tests do, assert that
the live browser check:

- clicks top `预测市场`
- rejects any visible bottom navigation
- compares all counts to API
- compares DOM event IDs to API order
- sees `24h 成交量` on every event
- expands one event and sees its market rows
- checks either real current/history rows or explicit zero states
- clicks all three history windows
- checks 44 px mobile targets
- captures the prediction screenshot for every viewport

- [ ] **Step 4: Run focused acceptance tests and verify RED**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_dashboard_acceptance.py -q -k prediction
```

Expected: missing constants/helpers.

- [ ] **Step 5: Implement live API and process checks**

During acceptance:

1. fetch `/api/prediction-arbitrage?window=24h`
2. validate payload and watcher process facts
3. run `launchctl print gui/$UID/com.open-trader.prediction-arbitrage`
4. inspect fresh watcher stdout/stderr from the current PID/start time
5. call real Gamma top events
6. call real CLOB `/books` for one monitored paired market

Classify public network/browser unavailability as `BLOCKED`. Classify stale,
wrong-SHA, wrong-directory, missing-label, malformed-data, ordering, or UI
problems as `FAIL`.

Zero current or historical signals is valid when the UI shows the exact zero
state.

- [ ] **Step 6: Implement the strict live browser check**

Call `_check_prediction_market` once in every existing viewport iteration.
The live check must use the real watcher/API payload and must not inject a
signal.

The pytest portion of `make acceptance` already runs the deterministic
four-state UI tests from Task 8, so together the gate proves all states without
pretending a live opportunity exists.

- [ ] **Step 7: Run focused acceptance and web tests**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_dashboard_acceptance.py tests/test_dashboard_web.py -q
```

Expected: both files pass.

- [ ] **Step 8: Commit the mandatory acceptance contract**

```bash
git add src/open_trader/dashboard_acceptance.py \
  tests/test_dashboard_acceptance.py
git diff --cached --check
git commit -m "test: require prediction dashboard acceptance"
```

---

### Task 11: Document, Deploy, and Run the Final Gate

**Files:**

- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Document operation and explicit non-goals**

Document:

- `prediction-arb watch|status`
- launchd install/uninstall
- top-20 volume universe and five-minute refresh
- WebSocket plus REST confirmation
- fee-unverified monitoring-only policy
- `$0.01` / `$1.00` thresholds
- SQLite location and indefinite signal history
- no wallet/orders/notifications
- Polymarket V1; Kalshi and Predict.fun deferred
- expected API/cloud cost `$0`
- estimated incremental Mac electricity budget `¥2–4/month`

- [ ] **Step 2: Add the dated changelog entry before any merge**

Record:

- persistent Polymarket watcher
- exact prediction Dashboard UI
- formal fee-safe signal/history semantics
- launchd/caffeinate deployment
- strict acceptance coverage

- [ ] **Step 3: Run all focused prediction tests**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_prediction_arbitrage.py \
  tests/test_polymarket_public.py \
  tests/test_polymarket_stream.py \
  tests/test_prediction_arbitrage_store.py \
  tests/test_prediction_arbitrage_watch.py \
  tests/test_prediction_arbitrage_cli.py \
  tests/test_prediction_arbitrage_launchd.py \
  tests/test_dashboard_web.py \
  tests/test_dashboard_acceptance.py -q
```

Expected: all selected tests pass.

- [ ] **Step 4: Run the full automated suite**

Make the ignored worktree-local virtualenv path point at the validated
repository environment if it is absent:

```bash
test -e .venv || ln -s /Users/ray/projects/open_trader/.venv .venv
```

Then run:

```bash
make test
```

Expected: all tests pass. Record the exact count.

- [ ] **Step 5: Commit docs and ensure a clean candidate SHA**

```bash
git add README.md README.zh-CN.md CHANGELOG.md
git diff --cached --check
git commit -m "docs: document prediction arbitrage monitor"
git status --short
git rev-parse HEAD
```

Expected: clean status and one 40-character candidate SHA.

- [ ] **Step 6: Install the candidate watcher and verify the direct workflow**

```bash
scripts/install_prediction_arbitrage_launchd.sh
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python \
  -m open_trader prediction-arb status \
  --data-dir /Users/ray/projects/open_trader/data
```

Inspect:

```bash
launchctl print "gui/$(id -u)/com.open-trader.prediction-arbitrage"
ps -p <watcher-pid> -o pid=,ppid=,lstart=,command=
lsof -a -p <watcher-pid> -d cwd -Fn
tail -n 50 /Users/ray/projects/open_trader/logs/prediction_arbitrage/launchd.out.log
tail -n 50 /Users/ray/projects/open_trader/logs/prediction_arbitrage/launchd.err.log
```

Expected:

- new live PID
- candidate worktree cwd
- candidate Git SHA
- fresh heartbeat/universe/PONG
- real monitored events and books
- no startup traceback or post-start stderr

- [ ] **Step 7: Start the candidate Dashboard for review**

Resolve and stop only the existing listener on review port `8766` after
recording its PID/cwd. Start the feature-worktree Dashboard with
`PYTHONPATH=<feature-worktree>/src`, repository-root Python, shared data, and a
fresh log `/tmp/open_trader_dashboard_8766.log`.

Verify HTTP 200:

```bash
curl -fsS -o /dev/null -w '%{http_code}\n' \
  http://127.0.0.1:8766/
curl -fsS \
  'http://127.0.0.1:8766/api/prediction-arbitrage?window=24h'
```

- [ ] **Step 8: Run the final gate exactly once**

Confirm the ignored `.venv` symlink still resolves to the repository
environment, then run:

```bash
make acceptance
```

Expected final line/result: `PASS`.

On `FAIL`, fix the defect, rerun focused tests, redeploy the new committed SHA,
and rerun `make acceptance`. On `BLOCKED`, report the unavailable browser or
external service and do not substitute fixtures, mocks, curl-only checks, or
screenshots.

- [ ] **Step 9: Redeploy the exact accepted SHA**

Without changing source or data:

1. rerun the prediction launchd installer from the accepted worktree
2. restart the Dashboard from the same accepted worktree
3. verify new watcher and Dashboard PIDs
4. verify both working directories and Git SHAs
5. verify fresh watcher heartbeat/PONG/universe and fresh logs
6. verify HTTP 200 for the Dashboard and prediction API

This exact-SHA restart does not require a second acceptance run.

- [ ] **Step 10: Hand off the review URL**

Only after `PASS` and exact-SHA redeployment, provide:

```text
http://127.0.0.1:8766/
```

Ask the user to open `预测市场` and compare it with the approved A prototype.
Do not describe the feature as merged until the user separately authorizes
merge/integration.

---

## Self-Review Checklist

Before execution begins, confirm the plan contains no unresolved placeholder:

```bash
rg -n "TODO|TBD|FIXME|<[^>]+>" \
  docs/superpowers/plans/2026-07-26-prediction-market-arbitrage-monitor.md
```

Expected: only intentional shell/document notation, no unresolved decision.

Spec coverage:

- continuous Mac runtime: Tasks 5, 9, 11
- top-20 volume universe: Tasks 2, 5
- WebSocket plus REST confirmation: Tasks 2, 3, 5
- formal thresholds and fee fail-closed: Tasks 1, 2, 5
- indefinite history: Task 4
- exact approved A UI: Task 8
- exact ordering and visible 24h volume: Tasks 1, 5, 8, 10
- four UI states: Tasks 7, 8, 10
- real process/data/browser acceptance: Tasks 10, 11
- no execution/notification/authentication: all tasks and final docs

No production implementation begins until the user selects an execution mode.
