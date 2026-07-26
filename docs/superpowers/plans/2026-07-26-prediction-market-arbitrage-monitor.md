# Prediction-Market Arbitrage Execution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a continuously running, local-only Polymarket binary-arbitrage monitor and explicitly confirmed two-leg execution workflow to Open Trader, with automatic merge, bounded incident recovery, durable audit history, and the exact approved Dashboard UI.

**Architecture:** One in-process `PolymarketMonitor` uses the official async public client for top-20 discovery, WebSocket books, and paired REST confirmation. One concrete `PolymarketTradingClient` wraps the official secure client and macOS Keychain; one serialized `PredictionExecutionService` persists every transition in stdlib SQLite before making an external mutation. The existing stdlib Dashboard server owns those services, serves the approved vanilla UI, and is kept alive by one launchd job through `caffeinate -s`.

**Tech Stack:** Python 3.12, official `polymarket-client==0.2.0`, stdlib `Decimal`/`sqlite3`/`fcntl`/`threading`/`secrets`/`urllib`, existing vanilla HTML/CSS/JavaScript Dashboard, pytest, existing Playwright dependency, macOS Keychain and launchd.

## Global Constraints

- Work only in `/Users/ray/projects/open_trader/.worktrees/prediction-arbitrage-scanner` on branch `feat/prediction-arbitrage-scanner`, based on local `main` at `f98812d`.
- The approved design is
  `docs/superpowers/specs/2026-07-26-prediction-market-arbitrage-monitor-design.md`.
- The mandatory UI baseline is prototype commit `e0d5083`, file
  `src/open_trader/dashboard_static/prediction-market-execution-prototype.html`.
- Production must match the prototype at `1440x1100` and `375x812` with at
  most `0.1%` changed pixels and no semantic layout, component, color, or copy
  mismatch.
- The only approved prototype omission is its scenario selector.
- Any new user-visible state, component, copy, or layout not in the approved
  prototype requires a new design artifact and user confirmation before coding.
- Top navigation is exactly `持仓`, `预测市场`, `策略回测`, `凯利实验室`.
  There is no bottom navigation.
- Every event row visibly includes `24h 成交量`.
- Event order is exactly: actionable first, profit descending, 24-hour volume
  descending, stable event ID ascending.
- `可参与` means every current data, market, wallet, region, relayer, breaker,
  and lock check passed.
- V1 trades only standard, explicitly fee-free binary markets. NegRisk and
  fee-enabled/unknown markets remain monitor-only.
- Fixed limits are code constants, not configuration:
  - minimum net edge `$0.01`
  - minimum estimated profit `$1.00`
  - maximum normal cost `$20.00`
  - wallet funding policy `$50.00`
  - maximum emergency expected loss `$2.00`
- One confirmation creates one batch POST containing exactly two equal-share
  FOK BUY orders. Batch results are independent.
- The CLOB API has no application client-order-id. Each execution attempts its
  batch POST exactly once; an ambiguous result is reconciled and never resent.
- Only one execution can be non-terminal across the whole app.
- Any one-leg or merge incident opens the breaker and requires manual
  acknowledgement after fresh clean reconciliation.
- Private key and Builder credentials live only in macOS Keychain and process
  memory. They never enter files, browser storage, SQLite, command arguments,
  logs, notifications, tracebacks, or API bodies.
- The trading Dashboard binds only to `127.0.0.1`.
- Use the existing notification classes and result recorder. Do not build a
  second notification framework.
- Use the official SDK boundary only. Do not handwrite EIP-712, contract call
  encoding, a relayer client, or a fallback CLI.
- Use stdlib SQLite directly. Add no ORM, task queue, message bus, venue base
  class, repository interface, or speculative Kalshi/Predict.fun structure.
- Retain signal, execution, merge, and incident history indefinitely. Do not
  retain raw WebSocket ticks or signed payloads.
- Use red-green-refactor for every behavior. Observe each focused test fail for
  the intended reason before production changes.
- Do not run `make acceptance` during intermediate work. Run it only as the
  final review-readiness gate.
- Only `make acceptance` `PASS` is complete. `FAIL` must be fixed; required
  external/browser unavailability is `BLOCKED`.
- After `PASS`, redeploy the exact accepted SHA and verify new PID, cwd, SHA,
  fresh logs/heartbeat, and HTTP 200 before providing the review URL.
- Before merge, commit the dated operator-facing `CHANGELOG.md` entry.
- Do not spawn subagents unless the user explicitly selects subagent-driven
  execution.

---

## File Map

### Add

- `src/open_trader/prediction_arbitrage.py` — immutable domain values, Decimal
  sizing, eligibility, risk calculations, and deterministic sorting.
- `src/open_trader/polymarket_trading.py` — Keychain access, geoblock,
  official secure-client wrapper, no-submit compatibility preflight, one-shot
  FOK batch, reconciliation, remediation orders, and merge.
- `src/open_trader/prediction_arbitrage_store.py` — SQLite schema and durable
  signal, preview, execution, leg, and incident operations.
- `src/open_trader/polymarket_monitor.py` — top-20 discovery, WebSocket books,
  paired REST confirmation, readiness freshness, signal lifecycle, snapshot.
- `src/open_trader/prediction_arbitrage_execution.py` — serialized execution,
  remediation, merge, breaker, alerts, restart reconciliation, reset.
- `src/open_trader/prediction_arbitrage_acceptance.py` — fixed scenario registry
  and aggregation of Python, Playwright, live, and process evidence.
- `config/prediction_arbitrage.json.example` — non-secret signer and wallet
  address example.
- `ops/launchd/com.open-trader.dashboard.plist.template` — loopback Dashboard,
  `caffeinate -s`, restart, and shared logs.
- `scripts/install_dashboard_launchd.sh` — render, install, restart, and verify
  the exact worktree Dashboard.
- `scripts/uninstall_dashboard_launchd.sh` — remove only that launchd job.
- `tests/test_prediction_arbitrage.py`
- `tests/test_polymarket_trading.py`
- `tests/test_prediction_arbitrage_store.py`
- `tests/test_polymarket_monitor.py`
- `tests/test_prediction_arbitrage_execution.py`
- `tests/test_prediction_arbitrage_launchd.py`
- `acceptance/test_prediction_arbitrage_scenarios.py`
- `tests/e2e/prediction-market.spec.ts`
- `tests/e2e/prediction-market.spec.ts-snapshots/*.png` — approved golden images.

### Modify

- `.gitignore` — ignore `config/prediction_arbitrage.json`.
- `pyproject.toml` — pin the one official SDK dependency.
- `src/open_trader/cli.py` — wallet setup/status, no-submit preflight, status,
  and Dashboard configuration.
- `src/open_trader/dashboard.py` — carry the non-secret prediction config path.
- `src/open_trader/dashboard_web.py` — service lifecycle, state/history GET,
  protected preview/execute/reset POST routes.
- `src/open_trader/dashboard_static/index.html` — exact approved navigation and
  workspace mounts.
- `src/open_trader/dashboard_static/dashboard.js` — render/poll, modal,
  execution, breaker acknowledgement, histories.
- `src/open_trader/dashboard_static/dashboard.css` — exact approved responsive
  visuals.
- `src/open_trader/dashboard_acceptance.py` — live prediction page/process
  checks in the existing final Dashboard gate.
- `tests/e2e/serve_dashboard_fixture.py` — deterministic prediction UI states
  and non-live mutation responses.
- `tests/test_dashboard_web.py`
- `tests/test_dashboard_acceptance.py`
- `tests/test_polymarket_trading.py` — also owns the prediction CLI contract
  tests so no second generic CLI test module is introduced.
- `Makefile` — call the fixed 54-scenario runner before existing Dashboard
  acceptance.
- `README.md`, `README.zh-CN.md`, `CHANGELOG.md`.

---

### Task 1: Lock Decimal Sizing, Eligibility, and Ordering

**Files:**

- Create: `tests/test_prediction_arbitrage.py`
- Create: `src/open_trader/prediction_arbitrage.py`

**Interfaces:**

```python
@dataclass(frozen=True, slots=True)
class BookLevel:
    price: Decimal
    size: Decimal

@dataclass(frozen=True, slots=True)
class ConfirmedBooks:
    yes_token_id: str
    no_token_id: str
    yes_asks: tuple[BookLevel, ...]
    no_asks: tuple[BookLevel, ...]
    confirmed_at: datetime

@dataclass(frozen=True, slots=True)
class MarketFacts:
    event_id: str
    market_id: str
    condition_id: str
    slug: str
    question: str
    volume_24h: Decimal
    minimum_order_size: Decimal
    tick_size: Decimal
    fee_verified_zero: bool
    neg_risk: bool

@dataclass(frozen=True, slots=True)
class PairIntent:
    event_id: str
    market_id: str
    condition_id: str
    yes_token_id: str
    no_token_id: str
    quantity: Decimal
    yes_max_price: Decimal
    no_max_price: Decimal
    yes_max_cost: Decimal
    no_max_cost: Decimal
    total_max_cost: Decimal
    minimum_profit: Decimal
    net_edge: Decimal

def build_pair_intent(
    facts: MarketFacts,
    books: ConfirmedBooks,
    *,
    balance: Decimal,
    allowance: Decimal,
) -> PairIntent | None: ...

def estimated_unwind_loss(
    *,
    filled_cost: Decimal,
    sell_price: Decimal,
    quantity: Decimal,
) -> Decimal: ...

def monitored_event_sort_key(event: Mapping[str, object]) -> tuple[object, ...]: ...
```

- [ ] **Step 1: Write the failing domain tests**

Use exact Decimal inputs:

```python
def test_sizes_equal_fok_pair_under_all_fixed_limits() -> None:
    facts = market_facts(minimum_order_size="5", tick_size="0.01")
    books = confirmed_books(
        yes=[("0.45", "20")],
        no=[("0.48", "20")],
    )

    intent = build_pair_intent(
        facts, books, balance=Decimal("50"), allowance=Decimal("50")
    )

    assert intent is not None
    assert intent.quantity == Decimal("20")
    assert intent.yes_max_cost == Decimal("9.00")
    assert intent.no_max_cost == Decimal("9.60")
    assert intent.total_max_cost == Decimal("18.60")
    assert intent.minimum_profit == Decimal("1.40")
    assert intent.net_edge == Decimal("0.07")
```

Add separate tests proving:

- quantity is the largest common protected-BUY requested amount produced by
  cent-denominated YES and NO spends under `$20`
- the same quantity is available on both books
- price caps are the worst levels required for the chosen quantity
- all six SDK-supported tick sizes use the pinned SDK's protected-BUY requested
  share precision and reject unsupported tick sizes
- threshold equality at `$0.01` edge and `$1.00` profit is accepted
- `$0.009999` edge or `$0.999999` profit is rejected
- balance or allowance below total cost is rejected
- minimum order size and tick-grid violations are rejected
- fee-unverified and NegRisk markets are rejected for execution but retain a
  gross upper bound
- every invalid/non-finite/negative input fails closed
- emergency loss equality at `$2.00` is allowed and `$2.000001` is not
- actionable event first, then profit descending, volume descending, ID
  ascending
- missing profit sorts after a finite profit in its own group

- [ ] **Step 2: Run focused tests and verify RED**

```bash
cd /Users/ray/projects/open_trader/.worktrees/prediction-arbitrage-scanner
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_prediction_arbitrage.py -q
```

Expected: collection fails because `open_trader.prediction_arbitrage` does not
exist.

- [ ] **Step 3: Implement the minimum pure module**

Use these exact constants:

```python
MIN_NET_EDGE = Decimal("0.01")
MIN_ESTIMATED_PROFIT = Decimal("1.00")
MAX_NORMAL_COST = Decimal("20.00")
MAX_WALLET_BALANCE = Decimal("50.00")
MAX_EMERGENCY_LOSS = Decimal("2.00")
COLLATERAL_SPEND_QUANTUM = Decimal("0.01")
PROTECTED_BUY_SHARE_PRECISION = {
    Decimal("0.1"): 3,
    Decimal("0.01"): 4,
    Decimal("0.005"): 5,
    Decimal("0.0025"): 6,
    Decimal("0.001"): 5,
    Decimal("0.0001"): 6,
}
```

Walk each ask book once to derive conservative maximum prices and depth. Enumerate
at most 2,000 cent-denominated spend amounts per leg, calculate each protected
BUY's requested shares with `Decimal` and `ROUND_CEILING` at the pinned SDK's
tick-specific precision, intersect the two small maps, and choose the largest
common quantity satisfying depth, minimum size, cost, balance, allowance, edge,
and profit. The resulting `yes_max_cost` and `no_max_cost` are the exact amounts
passed to the official client. Task 2 compares this pure calculation against
real signed SDK orders for every supported tick size, so an SDK rounding change
fails closed. Do not import SDK private helpers or introduce NumPy, a solver, or
configurable strategy rules.

- [ ] **Step 4: Run the focused test and verify GREEN**

Run the Step 2 command.

Expected: all domain tests pass.

- [ ] **Step 5: Commit the domain rules**

```bash
git add src/open_trader/prediction_arbitrage.py \
  tests/test_prediction_arbitrage.py
git diff --cached --check
git commit -m "feat: define prediction arbitrage rules"
```

---

### Task 2: Pass the Official SDK and Keychain Compatibility Gate

Nothing after this task may enable execution unless the real no-submit preflight
passes on the target Mac.

**Files:**

- Modify: `pyproject.toml`
- Modify: `.gitignore`
- Create: `config/prediction_arbitrage.json.example`
- Create: `src/open_trader/polymarket_trading.py`
- Modify: `src/open_trader/cli.py`
- Create: `tests/test_polymarket_trading.py`

**Interfaces:**

```python
@dataclass(frozen=True, slots=True)
class TradingConfig:
    signer_address: str
    wallet_address: str

@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    wallet_address: str
    p_usd_balance: Decimal
    p_usd_allowance: Decimal
    open_order_ids: tuple[str, ...]
    positions: tuple[dict[str, str], ...]
    checked_at: datetime

@dataclass(frozen=True, slots=True)
class LegResult:
    leg: Literal["YES", "NO"]
    accepted: bool
    status: str
    order_id: str
    filled_quantity: Decimal
    trade_ids: tuple[str, ...]
    error_code: str

@dataclass(frozen=True, slots=True)
class PairSubmission:
    yes: LegResult
    no: LegResult

class PolymarketTradingClient:
    @classmethod
    def from_keychain(cls, config: TradingConfig) -> "PolymarketTradingClient": ...
    def geoblock_allowed(self) -> bool: ...
    def account_snapshot(self) -> AccountSnapshot: ...
    def no_submit_preflight(self, intent: PairIntent) -> dict[str, object]: ...
    def submit_pair_once(self, intent: PairIntent) -> PairSubmission: ...
    def reconcile(self, *, condition_id: str, since: datetime) -> dict[str, object]: ...
    def cancel_orders(self, order_ids: tuple[str, ...]) -> tuple[str, ...]: ...
    def submit_remediation_once(self, order: dict[str, object]) -> LegResult: ...
    def merge_once(self, *, condition_id: str, quantity: Decimal) -> dict[str, object]: ...

def store_keychain_secret(account: str, secret: str) -> None: ...
def load_keychain_secret(account: str) -> str: ...
def load_trading_config(path: Path) -> TradingConfig: ...
```

- [ ] **Step 1: Pin the current official SDK and install it**

Add exactly:

```toml
"polymarket-client==0.2.0",
```

Then run:

```bash
/Users/ray/projects/open_trader/.venv/bin/python -m pip install \
  "polymarket-client==0.2.0"
/Users/ray/projects/open_trader/.venv/bin/python -c \
  'import importlib.metadata; assert importlib.metadata.version("polymarket-client") == "0.2.0"'
```

Expected: import succeeds and prints no assertion failure.

- [ ] **Step 2: Write failing Keychain and adapter contract tests**

Inject `subprocess.run`, `urlopen`, and an official-client factory. Assert:

```python
def test_keychain_write_never_places_secret_in_process_arguments() -> None:
    calls: list[tuple[list[str], str | None]] = []

    def run(args: list[str], **kwargs: object) -> CompletedProcess[str]:
        calls.append((args, kwargs.get("input")))
        return CompletedProcess(args, 0, "", "")

    store_keychain_secret("signing-private-key", "secret-sentinel", run=run)

    assert all("secret-sentinel" not in item for item in calls[0][0])
    assert calls[0][1] == "secret-sentinel\n"
```

Also prove:

- service name is exactly `com.open-trader.polymarket`
- Keychain read captures stdout without logging it
- config accepts only canonical 20-byte hex signer/wallet addresses
- geoblock blocked, timeout, malformed, or error returns fail-closed
- `no_submit_preflight` creates two signed `FOK` BUY market orders using the
  intent's exact cent-denominated leg cost as both `amount` and `max_spend`,
  plus the intent's `max_price`
- both signed orders have equal requested/taker share amounts
- each supported tick's signed requested amount matches Task 1's pure rounding
  result; a mismatch makes readiness fail closed
- signature/order payload never appears in the returned summary or log
- `submit_pair_once` calls official `post_orders` exactly once with two signed
  orders and preserves independent responses
- it calls `post_orders`, not `place_market_order`, so the SDK cannot silently
  send an allowance mutation
- an exception after the POST begins is returned as ambiguous and is never
  retried
- account reads cover collateral balance/allowance, open orders, account
  trades, and positions
- merge calls the official `merge_positions` once and waits at most 60 seconds
- official SDK exceptions are redacted to safe categories

- [ ] **Step 3: Run the adapter tests and verify RED**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_polymarket_trading.py -q
```

Expected: collection fails because `polymarket_trading` does not exist.

- [ ] **Step 4: Implement Keychain storage with no secret argv**

Use `/usr/bin/security` directly:

```python
SECURITY = "/usr/bin/security"
KEYCHAIN_SERVICE = "com.open-trader.polymarket"
KEYCHAIN_ACCOUNTS = (
    "signing-private-key",
    "builder-key",
    "builder-secret",
    "builder-passphrase",
)
```

`store_keychain_secret` runs:

```python
[
    SECURITY, "add-generic-password", "-U",
    "-a", account, "-s", KEYCHAIN_SERVICE, "-w",
]
```

with `input=f"{secret}\n"`, `text=True`, `capture_output=True`, and `check=True`.
Read with `find-generic-password -a account -s service -w`. Never include a
`CompletedProcess` representation in exceptions.

- [ ] **Step 5: Implement the one official-client boundary**

Use `SecureClient.create`, `BuilderApiKey`, `get_balance_allowance`,
`list_open_orders`, `list_account_trades`, `list_positions`,
`create_market_order`, `post_orders`, `cancel_orders`, and `merge_positions`.

For each BUY leg:

```python
amount = intent.yes_max_cost  # use no_max_cost for the NO leg
signed = client.create_market_order(
    token_id=token_id,
    side="BUY",
    amount=amount,
    max_spend=amount,
    max_price=max_price,
    order_type="FOK",
)
```

Assert each signed order's requested/taker amount equals `intent.quantity`, and
that the two are equal, before calling `post_orders`. The adapter makes no
second `post_orders` call on any code path.

Use stdlib `urllib` only for
`GET https://polymarket.com/api/geoblock`, with a finite timeout and explicit
allow response. Do not cache an allow result inside final execution.

- [ ] **Step 6: Add the one-time wallet CLI**

Add:

```text
prediction-arb wallet setup --config config/prediction_arbitrage.json
prediction-arb wallet status --config config/prediction_arbitrage.json
prediction-arb preflight --config config/prediction_arbitrage.json --no-submit
```

`wallet setup` accepts non-secret signer/wallet addresses as options, prompts
four secrets through `getpass`, writes JSON mode `0600`, and writes secrets to
Keychain. It has no option that accepts a secret.

The example file contains only:

```json
{
  "signer_address": "0x0000000000000000000000000000000000000000",
  "wallet_address": "0x0000000000000000000000000000000000000000"
}
```

Add `config/prediction_arbitrage.json` to `.gitignore`.

- [ ] **Step 7: Run focused tests and safe CLI checks**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_prediction_arbitrage.py tests/test_polymarket_trading.py -q
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python \
  -m open_trader prediction-arb wallet setup --help
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python \
  -m open_trader prediction-arb preflight --help
```

Expected: tests pass; help contains no private-key or secret option.

- [ ] **Step 8: Run the target-Mac no-submit compatibility gate**

The user completes the one-time hidden-input setup. The command selects the
highest-volume active standard binary fee-free market and constructs the
smallest venue-valid, SDK-rounding-compatible equal-share pair solely as an
in-memory signing probe. It does not require a live arbitrage opportunity, does
not persist the probe, and does not call any mutation method. Then run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python \
  -m open_trader prediction-arb preflight \
  --config /Users/ray/projects/open_trader/config/prediction_arbitrage.json \
  --no-submit
```

The command must prove and print only safe facts:

```text
sdk_version: 0.2.0
signer_match: yes
wallet_match: yes
geoblock: allowed
account_reads: pass
fok_pair_signed_not_submitted: pass
equal_requested_shares: pass
merge_capability: present_not_invoked
relayer_readiness: pass
secret_scan: pass
result: PASS
```

If any line fails, stop implementation and report `BLOCKED`. Do not enable an
execution API or continue to the UI.

- [ ] **Step 9: Commit the proven dependency boundary**

```bash
git add pyproject.toml .gitignore \
  config/prediction_arbitrage.json.example \
  src/open_trader/polymarket_trading.py src/open_trader/cli.py \
  tests/test_polymarket_trading.py
git diff --cached --check
git commit -m "feat: add safe Polymarket client boundary"
```

---

### Task 3: Persist Signals, Previews, Executions, Legs, and Incidents

**Files:**

- Create: `src/open_trader/prediction_arbitrage_store.py`
- Create: `tests/test_prediction_arbitrage_store.py`

**Interfaces:**

```python
class PredictionArbitrageStore:
    def __init__(self, data_dir: Path) -> None: ...
    def write_runtime(self, payload: Mapping[str, object]) -> None: ...
    def load_runtime(self) -> dict[str, object] | None: ...
    def upsert_signal(self, payload: Mapping[str, object]) -> str: ...
    def close_signal(self, market_id: str, *, ended_at: str, reason: str) -> None: ...
    def signal_history(self, window: Literal["24h", "7d", "all"]) -> list[dict[str, object]]: ...
    def create_preview(self, payload: Mapping[str, object], *, expires_at: str) -> str: ...
    def consume_preview_and_create_execution(
        self, preview_id: str, idempotency_key: str
    ) -> dict[str, object]: ...
    def transition_execution(
        self, execution_id: str, *, state: str, evidence: Mapping[str, object]
    ) -> None: ...
    def record_leg(self, execution_id: str, payload: Mapping[str, object]) -> None: ...
    def open_incident(self, execution_id: str, payload: Mapping[str, object]) -> str: ...
    def acknowledge_incident(self, incident_id: str, payload: Mapping[str, object]) -> None: ...
    def active_execution(self) -> dict[str, object] | None: ...
    def unacknowledged_incident(self) -> dict[str, object] | None: ...
    def histories(self, kind: Literal["signals", "executions", "incidents"]) -> list[dict[str, object]]: ...
```

- [ ] **Step 1: Write failing SQLite tests**

Create real temporary databases and assert:

- file is `data/prediction_arbitrage/prediction_arbitrage.sqlite3`
- WAL and non-zero busy timeout are enabled
- tables are exactly `runtime`, `signals`, `previews`, `executions`,
  `execution_legs`, and `incidents` plus SQLite internals
- one open signal per market
- one non-terminal execution globally
- preview expiry is 10 seconds and consumption is atomic/one-use
- duplicate application idempotency key returns the existing execution
- transition evidence is appended before the next action and survives restart
- leg identity is unique per execution plus `YES`/`NO`/remediation label
- acknowledgement never deletes incident evidence
- 24h/7d/all boundaries and newest-first history are exact
- raw ticks, signed orders, signatures, API secrets, and private keys have no
  column/table and fail a sentinel scan

Use a concurrency test with two threads and two store instances; only one can
consume the preview/create a non-terminal execution.

- [ ] **Step 2: Run store tests and verify RED**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_prediction_arbitrage_store.py -q
```

Expected: collection fails because the store module does not exist.

- [ ] **Step 3: Implement six direct SQLite tables**

Use `sqlite3`, parameterized SQL, explicit transactions, canonical JSON, and
short-lived connections. Add partial unique indexes:

```sql
CREATE UNIQUE INDEX one_open_signal_per_market
ON signals(market_id) WHERE ended_at IS NULL;

CREATE UNIQUE INDEX one_nonterminal_execution
ON executions(singleton)
WHERE state NOT IN (
  'both_rejected', 'complete', 'neutralized_incident',
  'directional_incident', 'merge_incident'
);

CREATE UNIQUE INDEX one_execution_per_idempotency_key
ON executions(idempotency_key);
```

Every execution row uses `singleton=1`. Store amounts as canonical decimal
strings inside JSON. Do not add a migration framework; a `schema_version`
PRAGMA/user_version check and explicit idempotent DDL are sufficient.

- [ ] **Step 4: Run the focused store tests**

Run the Step 2 command.

Expected: all store tests pass.

- [ ] **Step 5: Commit persistence**

```bash
git add src/open_trader/prediction_arbitrage_store.py \
  tests/test_prediction_arbitrage_store.py
git diff --cached --check
git commit -m "feat: persist prediction execution state"
```

---

### Task 4: Run the Top-20 Public Monitor and Signal Lifecycle

**Files:**

- Create: `src/open_trader/polymarket_monitor.py`
- Create: `tests/test_polymarket_monitor.py`

**Interfaces:**

```python
class PolymarketMonitor:
    def __init__(
        self,
        *,
        store: PredictionArbitrageStore,
        trading: PolymarketTradingClient,
        public_client_factory: Callable[[], object] = AsyncPublicClient,
    ) -> None: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def snapshot(self) -> dict[str, object]: ...
    def opportunity(self, opportunity_id: str) -> dict[str, object] | None: ...
    def run_forever(self) -> None: ...
```

- [ ] **Step 1: Write failing monitor tests with the official model shapes**

Inject a fake `AsyncPublicClient`, fake stream handle, deterministic clock, real
temporary store, and fake trading readiness. Assert:

- `list_events(closed=False, ended=False, order="volume24hr",
  ascending=False, page_size=20)` is used
- results are revalidated as active, not closed/ended, finite non-negative
  volume, then limited to 20
- only active, order-accepting, order-book-enabled, exactly YES/NO tokenized
  markets enter subscriptions
- malformed items are counted and skipped without aborting valid events
- `MarketSpec` contains exactly the current sorted token IDs
- reconnect resubscribes the full current set
- each apparent candidate calls one `get_order_books(token_ids=[yes, no])`
- returned books are matched by token ID, not tuple position
- candidate/actionability confirmation is at most 10 seconds old
- wallet/geoblock/relayer readiness is at most 60 seconds old
- readiness is refreshed without signing or submitting
- fee-enabled/unknown and NegRisk markets stay visible but never actionable
- healthy/no opportunity is quiet, not degraded
- heartbeat over 30 seconds, stream disruption over 15 seconds, universe over
  10 minutes, or store write failure is degraded and disables action
- open/improve/close signal episode updates peaks once and survives restart
- event order uses the domain sort key and every event contains volume
- runtime is written no more than once per second plus heartbeat
- monitor-only operation never calls `submit_pair_once`, remediation, or merge

- [ ] **Step 2: Run monitor tests and verify RED**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_polymarket_monitor.py -q
```

Expected: collection fails because `polymarket_monitor` does not exist.

- [ ] **Step 3: Implement one async monitor thread**

`start()` creates one daemon thread whose target is `asyncio.run(run_forever())`.
`run_forever()` uses `AsyncPublicClient`, `MarketSpec`, one stream, and one
five-minute universe timer. Keep current books and opportunities in dicts under
one `threading.RLock`; return serialized copies from `snapshot()`.

On each relevant stream update:

1. update the in-memory book
2. calculate whether thresholds might be reachable
3. paired REST-confirm both books
4. refresh typed market fee/tick/minimum/NegRisk facts
5. combine latest account readiness
6. build/remove the actionable opportunity
7. update the durable signal episode and runtime snapshot

Do not create a generic reconnect service or publish raw stream messages.

- [ ] **Step 4: Run core monitoring tests**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_prediction_arbitrage.py \
  tests/test_prediction_arbitrage_store.py \
  tests/test_polymarket_monitor.py -q
```

Expected: all selected tests pass.

- [ ] **Step 5: Run the exact one-shot public monitor diagnostic**

Add `monitor-once` as a non-mutating operator diagnostic. It performs one
top-20 refresh, opens the actual public WebSocket until heartbeat or timeout,
and makes one paired REST book read for the highest-volume eligible binary
market even when no arbitrage candidate exists:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python \
  -m open_trader prediction-arb monitor-once \
  --config /Users/ray/projects/open_trader/config/prediction_arbitrage.json \
  --data-dir /Users/ray/projects/open_trader/data \
  --timeout 30
```

Expected safe output:

```text
event_count: 20
volumes: present
websocket_heartbeat: pass
paired_book_read: pass
mutations: 0
result: PASS
```

The diagnostic must neither sign nor submit.

- [ ] **Step 6: Commit monitoring**

```bash
git add src/open_trader/polymarket_monitor.py \
  tests/test_polymarket_monitor.py src/open_trader/cli.py
git diff --cached --check
git commit -m "feat: monitor Polymarket arbitrage signals"
```

---

### Task 5: Execute One Two-Leg Request and Merge Normal Success

**Files:**

- Create: `src/open_trader/prediction_arbitrage_execution.py`
- Create: `tests/test_prediction_arbitrage_execution.py`

**Interfaces:**

```python
class PredictionExecutionService:
    def __init__(
        self,
        *,
        store: PredictionArbitrageStore,
        monitor: PolymarketMonitor,
        trading: PolymarketTradingClient,
        notifier: Notifier,
        lock_path: Path,
    ) -> None: ...
    def preview(self, opportunity_id: str) -> dict[str, object]: ...
    def confirm(self, preview_id: str, idempotency_key: str) -> dict[str, object]: ...
    def execution(self, execution_id: str) -> dict[str, object]: ...
    def reconcile_startup(self) -> dict[str, object]: ...
    def reset_breaker(self, incident_id: str) -> dict[str, object]: ...
```

- [ ] **Step 1: Write failing normal-flow and serialization tests**

Use the real store, a fake monitor, and a fake concrete trading object. Assert:

```python
def test_one_confirm_posts_exactly_one_equal_fok_batch_and_merges() -> None:
    service, trading, store = execution_fixture(result="both_filled")
    preview = service.preview("opp-1")

    execution = service.confirm(preview["id"], "browser-request-1")
    wait_until_terminal(service, execution["id"])

    assert trading.batch_calls == 1
    assert trading.batch_leg_names == ("YES", "NO")
    assert trading.batch_quantities == (Decimal("10"), Decimal("10"))
    assert trading.merge_calls == 1
    assert store.execution(execution["id"])["state"] == "complete"
```

Cover:

- opening preview rechecks paired books, wallet, geoblock, relayer, lock, and
  breaker but does not sign/submit
- preview expires at exactly 10 seconds
- confirm consumes preview atomically and repeats all volatile checks
- price worsening below `$1` rejects with zero external mutation
- each execution creates local leg IDs `execution_id:YES` and
  `execution_id:NO`
- double-click/same idempotency key returns the same execution
- different opportunity while active returns busy
- two independent FOK rejections end `both_rejected`, no retry/merge/breaker
- two fills reconcile equal actual shares before exactly one merge
- complete is written only after merge confirmation and pUSD reconciliation
- ambiguous POST performs zero second POST and opens reconciliation
- delayed order polls at least once per second for 30 injected-clock seconds,
  then locks as incident
- browser-supplied prices, quantity, wallet, and limits are absent from the
  service method signature
- the OS `fcntl` lock blocks a second process/service instance

- [ ] **Step 2: Run execution tests and verify RED**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_prediction_arbitrage_execution.py -q
```

Expected: collection fails because the execution module does not exist.

- [ ] **Step 3: Implement preview and one-shot execution**

`preview()` stores only the server-computed intent and safe display fields.
`confirm()`:

1. atomically consumes preview and creates execution
2. acquires one process `threading.Lock` plus `fcntl.flock`
3. starts one daemon execution thread
4. final-validates with current monitor/trading data
5. persists `submitting`
6. calls `submit_pair_once` exactly once
7. persists both independent leg responses
8. reconciles actual trades/positions
9. either ends both rejected, routes one-leg to Task 6 logic, or merges

Every transition is committed before the next external mutation. Never persist
the signed objects returned by the SDK.

- [ ] **Step 4: Implement confirmed normal merge**

When equal filled quantities are proven, call:

```python
trading.merge_once(
    condition_id=intent.condition_id,
    quantity=actual_equal_quantity,
)
```

Wait at most 60 seconds. Only confirmed collateral balance increase yields
`complete`. Any other result routes to an incident implemented in Task 6.

- [ ] **Step 5: Run focused normal-flow tests**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_prediction_arbitrage_execution.py -q \
  -k 'preview or serialization or both_rejected or both_filled or ambiguous or delayed'
```

Expected: selected tests pass.

- [ ] **Step 6: Commit the normal state machine**

```bash
git add src/open_trader/prediction_arbitrage_execution.py \
  tests/test_prediction_arbitrage_execution.py
git diff --cached --check
git commit -m "feat: execute one prediction arbitrage pair"
```

---

### Task 6: Handle One-Leg Risk, Alerts, Restart Recovery, and Reset

**Files:**

- Modify: `src/open_trader/prediction_arbitrage_execution.py`
- Modify: `tests/test_prediction_arbitrage_execution.py`
- Modify: `src/open_trader/polymarket_trading.py`
- Modify: `tests/test_polymarket_trading.py`

- [ ] **Step 1: Add failing one-leg remediation tests**

Table-drive these exact outcomes:

| Filled state | Fresh options | Required action | Terminal state |
|---|---|---|---|
| YES only | complete NO loss `$1.20`; unwind YES loss `$1.50` | one NO FOK | `neutralized_incident` after merge |
| NO only | complete YES loss `$1.80`; unwind NO loss `$0.90` | one NO SELL FOK | `neutralized_incident` |
| YES only | both options over `$2` | no order | `directional_incident` |
| one leg ambiguous | no proven neutral state | no second pair | incident/reconcile |
| equal pair | merge rejected/timeout | no second merge | `merge_incident` |

For every one-leg case assert:

- breaker opens before remediation
- fresh books/positions are read
- lower-loss executable option is chosen
- equality at `$2` is allowed; above `$2` sends no order
- at most one remediation FOK is submitted
- normal executions remain disabled after neutralization
- macOS and exactly one configured Feishu channel are attempted
- each channel result is persisted independently
- notification failure never blocks risk work or unlocks trading

- [ ] **Step 2: Add failing startup reconciliation tests**

Cover:

- clean account: starts locked, reads live state, then ready
- known open orders: cancel once, confirm cancellation, open incident
- equal pair: merge once, reconcile, remain locked until acknowledgement
- imbalance: no directional repair order, urgent incident
- unknown external order/position: incident
- already-confirmed merge: mark reconciled, no duplicate merge
- stale local state never overrides live orders/trades/positions
- first-live-order flag changes only for a real adapter result containing venue
  fill references plus confirmed merge transaction; controlled fakes cannot set
  it

- [ ] **Step 3: Add failing reset tests**

`reset_breaker` must make fresh live reads. It succeeds only with:

```python
open_orders == ()
directional_imbalance == Decimal("0")
pending_merge is False
readiness_is_fresh is True
```

Any failure returns a precise blocking reason, records the denial, keeps the
incident/history, and sends no order.

- [ ] **Step 4: Run incident tests and verify RED**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_prediction_arbitrage_execution.py \
  tests/test_polymarket_trading.py -q \
  -k 'one_leg or remediation or merge_incident or restart or reset or notification'
```

Expected: new cases fail because incident paths are absent.

- [ ] **Step 5: Implement the bounded incident paths**

Use existing `send_notification_with_results` and record its returned
`NotificationAttempt` values. Build the incident notifier from the existing
daily config with mandatory `macos` and one available `feishu` or `feishu_app`
channel; fail readiness if no Feishu channel is configured.

Remediation is one exact FOK request:

- complete missing BUY: choose a cent-denominated protected spend whose signed
  requested amount equals the proven missing quantity, then use
  `amount=max_spend=that spend`, `max_price`, `FOK`
- unwind filled SELL: `shares=quantity`, `min_price`, `FOK`

Do not loop or fall through to the second option after an ambiguous first
attempt.

- [ ] **Step 6: Implement startup and manual reset**

`reconcile_startup()` runs synchronously before the monitor can publish
actionable opportunities. It records the current account snapshot and returns
readiness. Old equal pairs may merge; old imbalances never cause a new
directional order.

`reset_breaker()` acknowledges rather than deletes. There is no CLI force flag
or hidden override endpoint.

- [ ] **Step 7: Run the complete execution and adapter tests**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_polymarket_trading.py \
  tests/test_prediction_arbitrage_execution.py -q
```

Expected: all tests pass.

- [ ] **Step 8: Commit incident safety**

```bash
git add src/open_trader/polymarket_trading.py \
  src/open_trader/prediction_arbitrage_execution.py \
  tests/test_polymarket_trading.py \
  tests/test_prediction_arbitrage_execution.py
git diff --cached --check
git commit -m "feat: contain prediction execution incidents"
```

---

### Task 7: Add Local-Only Dashboard Services and Protected APIs

**Files:**

- Modify: `src/open_trader/dashboard.py`
- Modify: `src/open_trader/dashboard_web.py`
- Modify: `src/open_trader/cli.py`
- Modify: `tests/test_dashboard_web.py`

**HTTP contract:**

```text
GET  /api/prediction-arbitrage/state
GET  /api/prediction-arbitrage/history?kind=signals|executions|incidents
POST /api/prediction-arbitrage/preview
POST /api/prediction-arbitrage/executions
POST /api/prediction-arbitrage/circuit-breaker/reset
```

- [ ] **Step 1: Write failing service-lifecycle and GET tests**

Inject store, monitor, execution service, and tokens into
`create_dashboard_server`. Assert:

- state includes readiness, masked wallet, policy limits, heartbeat, sorted
  events, opportunities, current execution, breaker, and CSRF token
- histories are separate and paginated; unknown kind is HTTP 400
- missing/unready prediction configuration returns a schema-valid unavailable
  state, not a server traceback
- server startup calls `reconcile_startup` before `monitor.start`
- shutdown calls `monitor.stop` and releases resources
- production CLI rejects non-loopback `--host` when prediction config is
  supplied
- no prediction route merges data into `/api/dashboard`

- [ ] **Step 2: Write failing mutation security tests**

For each POST, prove HTTP 403 and zero service calls when any is wrong:

- client address is non-loopback
- `Host` differs from the actual loopback listener
- `Origin` differs from `http://host:port`
- HttpOnly `ot_prediction_session` cookie is missing/wrong
- `X-CSRF-Token` is missing/wrong

Then prove one valid same-origin request works. Also assert:

- `SameSite=Strict`, `HttpOnly`, and `Path=/` on the random session cookie
- request body maximum remains 1 MiB
- preview accepts only `opportunity_id`
- execution accepts only `preview_id` and `idempotency_key`
- reset accepts only `incident_id`
- any extra price/quantity/wallet/limit field returns HTTP 400
- duplicate execution returns the existing durable execution

- [ ] **Step 3: Run focused API tests and verify RED**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_dashboard_web.py -q -k prediction_arbitrage
```

Expected: routes return 404 or injected services are unsupported.

- [ ] **Step 4: Add the minimum lifecycle wiring**

Add `prediction_config_path: Path | None = None` to `DashboardConfig` and
`--prediction-config` to `dashboard`.

When configured, `serve_dashboard` creates:

```python
store = PredictionArbitrageStore(config.data_dir)
trading = PolymarketTradingClient.from_keychain(load_trading_config(path))
monitor = PolymarketMonitor(store=store, trading=trading)
execution = PredictionExecutionService(...)
execution.reconcile_startup()
monitor.start()
```

Reuse existing daily config loading and notification construction. Do not add a
sidecar process or task queue.

- [ ] **Step 5: Add one process-random browser session**

At server construction:

```python
session_token = secrets.token_urlsafe(32)
csrf_token = secrets.token_urlsafe(32)
```

The state GET sets the session cookie and returns the CSRF token. Mutations
compare using `secrets.compare_digest`, exact Host/Origin, and loopback client
address before reading the body.

- [ ] **Step 6: Run complete Dashboard web tests**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_dashboard_web.py -q
```

Expected: all existing and prediction web tests pass.

- [ ] **Step 7: Commit the protected API**

```bash
git add src/open_trader/dashboard.py src/open_trader/dashboard_web.py \
  src/open_trader/cli.py tests/test_dashboard_web.py
git diff --cached --check
git commit -m "feat: serve protected prediction execution APIs"
```

---

### Task 8: Reproduce the Approved UI and Lock It with Golden Screenshots

**Files:**

- Modify: `src/open_trader/dashboard_static/index.html`
- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Modify: `src/open_trader/dashboard_static/dashboard.css`
- Modify: `tests/test_dashboard_web.py`
- Modify: `tests/e2e/serve_dashboard_fixture.py`
- Create: `tests/e2e/prediction-market.spec.ts`
- Create: `tests/e2e/prediction-market.spec.ts-snapshots/*.png`

- [ ] **Step 1: Add failing exact static-contract tests**

Parse the production files and assert:

- top navigation labels and order are exact
- `预测市场` has one workspace
- there is no bottom nav or prototype scenario selector
- required readiness strip, summary, event list, opportunity, three histories,
  confirmation modal, execution progress, incident detail, and reset modal
  mounts exist exactly once
- required visible policy values are `$50`, `$20`, `$2`, fee-free-only, and
  possible real loss
- every event template includes literal `24h 成交量`
- browser private-key/API-secret input strings do not exist

- [ ] **Step 2: Add deterministic fixture routes**

Extend the existing fixture server with:

```text
GET  /api/prediction-arbitrage/state?scenario=...
GET  /api/prediction-arbitrage/history?kind=...
POST /api/prediction-arbitrage/preview
POST /api/prediction-arbitrage/executions
POST /api/prediction-arbitrage/circuit-breaker/reset
```

Support exact scenarios:

```text
loading
ready
quiet
executing
success
incident
degraded
confirmation
reset
history-signals
history-executions
history-incidents
```

The fixture cannot import or call `PolymarketTradingClient`.

- [ ] **Step 3: Write failing Playwright interaction tests**

For desktop and mobile:

```typescript
const viewports = [
  { name: 'desktop', width: 1440, height: 1100 },
  { name: 'mobile', width: 375, height: 812 },
];
```

Assert:

- exact top navigation and no bottom navigation
- event DOM order equals actionable/profit/volume/ID order
- every event visibly contains `24h 成交量` and its value
- `参与` opens the exact confirmation modal
- modal focus is trapped; Escape cancels; focus returns to invoker
- one confirm sends one POST and disables every other action
- success shows both legs, merge, realized result, and trade history
- incident shows breaker, both leg outcomes, alert states, and reset control
- denied reset remains locked; allowed reset returns ready
- all three history tabs render exact empty/populated states
- no horizontal overflow
- all interactive mobile targets are at least `44x44`
- no actionable console or HTTP errors

- [ ] **Step 4: Run UI tests and verify RED**

```bash
OPEN_TRADER_PYTHON=/Users/ray/projects/open_trader/.venv/bin/python \
  npx playwright test tests/e2e/prediction-market.spec.ts
```

Expected: missing workspace/selectors or screenshot failures.

- [ ] **Step 5: Implement the approved production UI exactly**

Port the layout and states from commit `e0d5083` into the existing three static
files. Reuse existing warm-ledger tokens, `escapeHtml`, and workspace
navigation. Add no framework, router, component library, chart library, or
client state abstraction.

Keep only this state:

```javascript
predictionMarket: {
  payload: null,
  historyKind: "signals",
  error: "",
  pollId: null,
  csrfToken: "",
  activeExecutionId: "",
}
```

Poll only while the prediction workspace is open. Keep last-known rows and mark
them stale on later fetch failure.

- [ ] **Step 6: Generate the approved golden screenshots from `e0d5083`**

Start the approved prototype at its worktree URL. Run the same Playwright test
with `PREDICTION_UI_BASE_URL` pointing at the prototype and update snapshots:

```bash
PREDICTION_UI_BASE_URL=http://127.0.0.1:8772/prediction-market-execution-prototype.html \
  npx playwright test tests/e2e/prediction-market.spec.ts \
  --update-snapshots
```

Before each prototype capture, the test injects CSS that hides only the
prototype scenario selector. Commit 24 named images: 12 states times 2
viewports. Record `e0d5083` in the test as the golden provenance.

- [ ] **Step 7: Compare production to the fixed goldens**

Normal test execution targets the production fixture and uses:

```typescript
await expect(page).toHaveScreenshot(name, {
  animations: 'disabled',
  fullPage: true,
  maxDiffPixelRatio: 0.001,
});
```

In addition to the pixel threshold, exact DOM/copy/component assertions from
Step 3 must pass so a small semantic change cannot hide inside the threshold.

- [ ] **Step 8: Run static, browser, and accessibility checks**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_dashboard_web.py -q -k 'prediction or static'
OPEN_TRADER_PYTHON=/Users/ray/projects/open_trader/.venv/bin/python \
  npx playwright test tests/e2e/prediction-market.spec.ts
```

Expected: all tests and all 24 golden comparisons pass.

- [ ] **Step 9: Commit the exact UI**

```bash
git add src/open_trader/dashboard_static/index.html \
  src/open_trader/dashboard_static/dashboard.js \
  src/open_trader/dashboard_static/dashboard.css \
  tests/test_dashboard_web.py tests/e2e/serve_dashboard_fixture.py \
  tests/e2e/prediction-market.spec.ts \
  tests/e2e/prediction-market.spec.ts-snapshots
git diff --cached --check
git commit -m "feat: add approved prediction execution UI"
```

---

### Task 9: Keep the Accepted Dashboard Running on macOS

**Files:**

- Create: `ops/launchd/com.open-trader.dashboard.plist.template`
- Create: `scripts/install_dashboard_launchd.sh`
- Create: `scripts/uninstall_dashboard_launchd.sh`
- Create: `tests/test_prediction_arbitrage_launchd.py`
- Modify: `src/open_trader/cli.py`

- [ ] **Step 1: Write failing plist and installer tests**

Assert:

- label is exactly `com.open-trader.dashboard`
- `RunAtLoad` and `KeepAlive` are true
- program begins `/usr/bin/caffeinate`, `-s`, then repository `.venv` Python
- command is exactly `-m open_trader dashboard`
- host is `127.0.0.1`, review port is `8766`
- selected worktree is `WorkingDirectory` and `PYTHONPATH`
- data/reports/config use shared repository paths
- prediction config path is the ignored shared config
- logs are
  `logs/dashboard/launchd.out.log` and `logs/dashboard/launchd.err.log`
- no secret appears in plist or environment
- dry-run modifies no LaunchAgent and passes `plutil -lint`
- install bootouts only the exact label, writes one plist, bootstraps,
  kickstarts, and waits for HTTP/readiness
- an occupied port from an unknown cwd fails without killing it
- uninstall removes only the exact label/plist

- [ ] **Step 2: Run launchd tests and verify RED**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_prediction_arbitrage_launchd.py -q
```

Expected: files are absent.

- [ ] **Step 3: Implement the single Dashboard job**

Follow the existing launchd installer conventions for path resolution,
XML/sed escaping, `plutil`, `launchctl bootout/bootstrap/kickstart`, and fresh
status checks. Keep this installer separate from daily premarket jobs.

Add `prediction-arb status` output with safe process/runtime facts:

```text
health, pid, heartbeat_at, universe_refreshed_at, websocket,
event_count, market_count, actionable_count, breaker, masked_wallet
```

- [ ] **Step 4: Run focused tests and inspect dry-run output**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_prediction_arbitrage_launchd.py -q
scripts/install_dashboard_launchd.sh --dry-run | plutil -lint -
```

Expected: tests pass and plist is valid.

- [ ] **Step 5: Commit macOS operation**

```bash
git add ops/launchd/com.open-trader.dashboard.plist.template \
  scripts/install_dashboard_launchd.sh \
  scripts/uninstall_dashboard_launchd.sh \
  tests/test_prediction_arbitrage_launchd.py src/open_trader/cli.py
git diff --cached --check
git commit -m "feat: keep prediction dashboard running"
```

---

### Task 10: Enforce All 54 Scenarios in `make acceptance`

**Files:**

- Create: `src/open_trader/prediction_arbitrage_acceptance.py`
- Create: `acceptance/test_prediction_arbitrage_scenarios.py`
- Modify: `src/open_trader/dashboard_acceptance.py`
- Modify: `tests/test_dashboard_acceptance.py`
- Modify: `Makefile`

**Fixed scenario registry:**

```python
SCENARIO_IDS = (
    "MON-01", "MON-02", "MON-03", "MON-04", "MON-05",
    "MON-06", "MON-07", "MON-08", "MON-09", "MON-10",
    "PRE-01", "PRE-02", "PRE-03", "PRE-04", "PRE-05",
    "PRE-06", "PRE-07", "PRE-08", "PRE-09",
    "SEC-01", "SEC-02", "SEC-03", "SEC-04",
    "EXE-01", "EXE-02", "EXE-03", "EXE-04", "EXE-05",
    "EXE-06", "EXE-07", "EXE-08", "EXE-09", "EXE-10",
    "REC-01", "REC-02", "REC-03", "REC-04", "REC-05",
    "RST-01", "RST-02",
    "HIS-01", "HIS-02", "HIS-03",
    "UI-01", "UI-02", "UI-03", "UI-04", "UI-05",
    "LIVE-01", "LIVE-02", "LIVE-03",
    "OPS-01", "OPS-02", "OPS-03",
)
```

- [ ] **Step 1: Write a failing registry completeness test**

Parse Section 16 of the approved spec and assert the ordered IDs exactly equal
`SCENARIO_IDS`, with length 54 and no duplicates. This prevents a scenario from
silently disappearing from the gate.

- [ ] **Step 2: Implement deterministic scenario tests**

Create one pytest test per non-UI scenario under `acceptance/`. Add a
`scenario_id` JUnit property and implement the exact precondition/action/UI or
API result/backend evidence/forbidden behavior from the corresponding Section
16 row.

Map helpers as follows:

| Scenario group | Controlled evidence |
|---|---|
| `MON-01`–`MON-10` | fake official public client + real monitor/store |
| `PRE-01`–`PRE-09` | protected HTTP server + fake trading client |
| `SEC-01`–`SEC-04` | real HTTP listener + sentinel secrets |
| `EXE-01`–`EXE-10` | real state machine/store + controlled execution outcomes |
| `REC-01`–`REC-05` | recreated service/store + controlled live account facts |
| `RST-01`–`RST-02` | real reset service + fresh controlled reconciliation |
| `HIS-01`–`HIS-03` | real SQLite restart and API/browser reads |
| `LIVE-01`–`LIVE-03` | actual target services; skip reason begins `BLOCKED:` |
| `OPS-01`–`OPS-03` | actual launchctl/PID/cwd/SHA/log/HTTP facts |

Every deterministic test asserts both the required evidence and all “Forbidden
behavior” assertions from the spec. Controlled execution methods count every
external mutation.

- [ ] **Step 3: Mark the five Playwright tests with UI IDs**

Test titles are exactly:

```text
[UI-01] desktop prototype parity
[UI-02] mobile prototype parity
[UI-03] keyboard modal behavior
[UI-04] status semantics
[UI-05] cost disclosure
```

Each test includes its required golden comparisons and interactions from Task
8.

- [ ] **Step 4: Write the failing acceptance aggregator test**

The runner:

1. executes the Python scenario file with JUnit XML output
2. executes the one Playwright file with JSON reporter
3. runs live Dashboard/process validation against `--url`
4. maps pass/fail/skip to `PASS`/`FAIL`/`BLOCKED`
5. prints every ID exactly once in registry order

Expected output format:

```text
SCENARIO MON-01 PASS loading state
SCENARIO EXE-06 PASS unsafe remediation blocked
SCENARIO LIVE-02 BLOCKED Keychain item unavailable
SCENARIO UI-01 FAIL desktop golden mismatch
```

Any missing/duplicate ID is `FAIL`. A `FAIL` dominates; otherwise a required
`BLOCKED` makes the final result `BLOCKED`; only 54 `PASS` lines produce
`PASS`.

- [ ] **Step 5: Run focused acceptance code and verify RED**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_dashboard_acceptance.py -q -k prediction
```

Expected: registry/runner helpers are missing.

- [ ] **Step 6: Extend the existing live Dashboard verifier**

Add live checks for:

- actual prediction state/history APIs
- real top-20 data, volumes, paired book read, and WebSocket heartbeat
- real Keychain retrieval and secret-clean no-submit signed pair
- geoblock, balance, allowance, open orders, trades, positions, and relayer
  readiness
- no live POST/merge/approval during acceptance
- prediction browser workspace and actual quiet/actionable/degraded semantics
- launchd label, current PID, loopback bind, cwd, clean exact SHA, start time,
  `caffeinate -s`, fresh logs, and heartbeat

External network, browser, Keychain, or account unavailability is `BLOCKED`.
Wrong data, UI, process, SHA, cwd, logs, or security is `FAIL`.

- [ ] **Step 7: Wire the runner into the final Make target**

Keep existing pytest, drawdown preflight, and Dashboard acceptance. Add before
the existing Dashboard verifier:

```make
cd "$(WORKTREE_ROOT)" && \
  PYTHONPATH=src .venv/bin/python -m open_trader.prediction_arbitrage_acceptance \
  --url "$(DASHBOARD_URL)" \
  --expected-root "$(WORKTREE_ROOT)"
```

The runner invokes only the dedicated acceptance scenario file and prediction
Playwright file; normal `make test` remains unchanged.

- [ ] **Step 8: Run all focused acceptance tests, not the final gate**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_dashboard_acceptance.py \
  tests/test_dashboard_web.py \
  tests/test_prediction_arbitrage_execution.py -q
OPEN_TRADER_PYTHON=/Users/ray/projects/open_trader/.venv/bin/python \
  npx playwright test tests/e2e/prediction-market.spec.ts
```

Expected: selected tests and all goldens pass. Do not run `make acceptance`.

- [ ] **Step 9: Commit the mandatory gate**

```bash
git add src/open_trader/prediction_arbitrage_acceptance.py \
  acceptance/test_prediction_arbitrage_scenarios.py \
  src/open_trader/dashboard_acceptance.py \
  tests/test_dashboard_acceptance.py Makefile
git diff --cached --check
git commit -m "test: enforce prediction execution acceptance"
```

---

### Task 11: Document, Deploy, Run the Final Gate, and Hand Off

**Files:**

- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Document setup, operation, risk, and cost**

Document exact commands for:

- hidden-input wallet setup/status
- no-submit preflight
- launchd install/uninstall
- runtime status
- UI preview/confirm/reset
- top-20/5-minute monitoring
- fee-free standard-binary scope
- `$0.01`, `$1`, `$20`, `$50`, and `$2` policies
- indefinite histories and SQLite location
- local-only `127.0.0.1`
- first-live-order pending status
- no automated canary order
- `$0` API/cloud expectation and `¥2–4/month` estimated electricity
- possible slippage, remediation loss, and venue/merge risk
- Kalshi/Predict.fun/cross-venue deferrals

- [ ] **Step 2: Add the dated changelog entry before merge**

The 2026-07-26 entry records:

- persistent Polymarket monitor
- exact approved UI
- explicit two-step, one-request two-FOK execution
- merge and bounded one-leg incident policy
- Keychain/local-only security
- durable signal/trade/incident history
- launchd deployment and 54-scenario acceptance

- [ ] **Step 3: Run all focused prediction tests**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_prediction_arbitrage.py \
  tests/test_polymarket_trading.py \
  tests/test_prediction_arbitrage_store.py \
  tests/test_polymarket_monitor.py \
  tests/test_prediction_arbitrage_execution.py \
  tests/test_prediction_arbitrage_launchd.py \
  tests/test_dashboard_web.py \
  tests/test_dashboard_acceptance.py -q
```

Expected: all selected tests pass. Record the exact count.

- [ ] **Step 4: Run the complete automated suite**

If the worktree-local ignored `.venv` path is absent, link it to the validated
repository environment:

```bash
test -e .venv || ln -s /Users/ray/projects/open_trader/.venv .venv
make test
OPEN_TRADER_PYTHON=/Users/ray/projects/open_trader/.venv/bin/python \
  npm run test:e2e
```

Expected: all Python and browser tests pass.

- [ ] **Step 5: Commit docs and create a clean candidate SHA**

```bash
git add README.md README.zh-CN.md CHANGELOG.md
git diff --cached --check
git commit -m "docs: document prediction execution"
git status --short
git rev-parse HEAD
```

Expected: clean worktree and one committed 40-character SHA.

- [ ] **Step 6: Deploy the committed candidate**

```bash
scripts/install_dashboard_launchd.sh
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python \
  -m open_trader prediction-arb status \
  --data-dir /Users/ray/projects/open_trader/data
```

Inspect directly:

```bash
launchctl print "gui/$(id -u)/com.open-trader.dashboard"
lsof -nP -iTCP:8766 -sTCP:LISTEN
ps -p "$(lsof -tiTCP:8766 -sTCP:LISTEN)" -o pid=,ppid=,lstart=,command=
lsof -a -p "$(lsof -tiTCP:8766 -sTCP:LISTEN)" -d cwd -Fn
tail -n 80 /Users/ray/projects/open_trader/logs/dashboard/launchd.out.log
tail -n 80 /Users/ray/projects/open_trader/logs/dashboard/launchd.err.log
```

Verify candidate PID, worktree cwd, exact candidate SHA, `caffeinate -s`,
fresh startup reconciliation, real universe/books/heartbeat, loopback listener,
and no fresh traceback/stderr.

- [ ] **Step 7: Verify the candidate review endpoint**

```bash
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8766/
```

Expected: `200`. Curl is only a direct workflow check, not a substitute for the
browser phase.

- [ ] **Step 8: Run the final gate**

```bash
make acceptance
```

Expected:

- exactly 54 `SCENARIO ... PASS` lines
- existing automated/live/browser/process gates pass
- final result `PASS`

On `FAIL`, fix, run focused tests, commit, redeploy the new SHA, then rerun the
gate. On `BLOCKED`, report the unavailable required environment and do not
substitute mocks, fixtures, curl, or screenshots.

- [ ] **Step 9: Redeploy the exact accepted SHA**

Without modifying source or data:

```bash
scripts/install_dashboard_launchd.sh
```

Then re-verify:

- new PID
- accepted worktree cwd
- exact accepted Git SHA
- fresh launchd/log start timestamp
- fresh startup reconciliation and monitor heartbeat
- loopback listener
- HTTP 200

This exact-SHA restart does not require a second acceptance run.

- [ ] **Step 10: Hand off the deployed review URL**

Only after Step 9, provide:

```text
http://127.0.0.1:8766/
```

Ask the user to review `预测市场`. Do not say merged until the user separately
authorizes integration into `main`.

---

## Self-Review Checklist

### Spec coverage

- official SDK/Keychain/no-submit hard gate: Task 2
- Decimal thresholds, limits, equal shares, ordering: Task 1
- top-20/5-minute/WebSocket/paired REST/freshness: Task 4
- indefinite signals/trades/incidents: Task 3
- one preview, one confirmation, one two-leg batch: Tasks 5 and 7
- both-rejected, both-filled, merge: Task 5
- one-leg `$2` policy, alerts, breaker: Task 6
- restart reconciliation and manual reset: Task 6
- local-only session/CSRF/trust boundary: Task 7
- exact approved UI and golden diff: Task 8
- launchd/caffeinate Mac operation: Task 9
- all 54 scenario lines and real non-mutating integration: Task 10
- cost/risk/operator docs, changelog, exact-SHA deployment: Task 11

### Placeholder scan

```bash
rg -n 'TODO|TBD|FIXME|implement later|fill in details|similar to Task' \
  docs/superpowers/plans/2026-07-26-prediction-market-arbitrage-monitor.md
```

Expected: no matches.

### Type consistency

- `PairIntent` is defined once in Task 1 and consumed unchanged by Tasks 2,
  4, 5, and 6.
- `PolymarketTradingClient` is the only official secure-client boundary.
- `PredictionArbitrageStore` owns all durable state.
- `PolymarketMonitor` owns only public monitoring/current opportunity state.
- `PredictionExecutionService` owns all mutations and breaker policy.
- Browser POSTs contain only opaque server IDs and application idempotency key.

No production implementation begins until the user selects an execution mode.
