# Predict Cross-Venue YES/NO Read-Only Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Predict.fun as a read-only information source, explicitly match its binary markets to Polymarket, validate exact settlement equivalence before real-time monitoring, and expose durable cross-venue YES/NO signals in the existing Dashboard without enabling Predict.fun mainnet orders.

**Architecture:** Keep `PolymarketMonitor` as the concrete Polymarket book owner and add only two production modules: `PredictSource` for Predict-specific REST/WebSocket/auth health, and `PredictCrossVenueMonitor` for explicit mapping, Codex validation, two-direction Decimal calculations, confirmation, and signal episodes. Reuse the existing SQLite store, notification lease, Polymarket client, annualized-yield helper, Dashboard runtime, and `.pm-*` UI. Do not add a generic venue interface, daemon, database table, or mainnet submit path.

**Tech Stack:** Python 3.12, stdlib `urllib`/`asyncio`/`decimal`, already-installed `websockets`, existing `polymarket-client`, SQLite, vanilla JavaScript/CSS, pytest, Playwright, existing `make acceptance` gate.

**Approved design:** `docs/superpowers/specs/2026-08-02-predict-cross-venue-yes-no-design.md`

## Global constraints

- Start implementation from a fresh isolated worktree based on local `main` after the separate annualized-entry branch has landed. Reuse `MIN_THRESHOLD_ANNUALIZED_YIELD` and `simple_annualized_yield`; do not define a second 15% rule.
- Predict.fun mainnet is observation-only. No Predict.fun signer, JWT, approval, order builder, order endpoint, execution button, or automatic execution may enter this plan.
- Preserve existing Polymarket same-venue YES/NO behavior and the existing `polymarket-threshold-relation-v1` prompt byte-for-byte.
- Match only through `polymarketConditionIds[]`; do not add title matching, embeddings, preclassification, or all-pairs search.
- Every market and leg must carry `exchange`, native `market_id`, native `condition_id`, `outcome`, `token_id`, and `settlement_asset`.
- Codex is slow-path only. WebSocket updates use local `Decimal` arithmetic; a positive candidate is confirmed by concurrent REST book refreshes from both venues.
- At most one confirmation task and one open signal episode per pair/direction. Stale books, reconnects, health loss, and rule-fingerprint changes fail closed.
- Treat supported settlement assets nominally as 1:1 and display both assets. Do not add FX pricing.
- Predict fees use a conservative maximum fee ceiling, `quantity * fee_rate_bps / 10_000`, until the venue exposes a deterministic pre-trade fee amount. Mark this with a `ponytail:` comment so it is replaced only when real fee-quote evidence exists.
- Keep secrets out of config, arguments, logs, API responses, stored payloads, screenshots, and tests. The only committed Predict address is the public wallet `0xcE23B341C888A88C4C44D8B5Aa6D04A8615Ff435`.
- Run focused tests while developing. Run `make acceptance` only once as the final Dashboard gate. After `PASS`, redeploy the exact accepted SHA and verify PID, cwd, SHA, fresh logs, and HTTP 200 before review.

## Locked file structure

New production files:

- `src/open_trader/predict_source.py`
- `src/open_trader/predict_cross_venue.py`
- `src/open_trader/schemas/cross_exchange_yes_no_equivalence.json`

New focused tests:

- `tests/test_predict_source.py`
- `tests/test_predict_cross_venue.py`

Existing files modified:

- `config/prediction_arbitrage.json.example`
- `src/open_trader/cli.py`
- `src/open_trader/polymarket_trading.py`
- `src/open_trader/polymarket_monitor.py`
- `src/open_trader/polymarket_relation_discovery.py`
- `src/open_trader/prediction_arbitrage_store.py`
- `src/open_trader/prediction_arbitrage_execution.py`
- `src/open_trader/notifications.py`
- `src/open_trader/dashboard_web.py`
- `src/open_trader/dashboard_static/dashboard.js`
- `src/open_trader/dashboard_static/dashboard.css`
- `tests/test_polymarket_trading.py`
- `tests/test_polymarket_monitor.py`
- `tests/test_prediction_arbitrage_store.py`
- `tests/test_prediction_arbitrage_execution.py`
- `tests/test_notifications.py`
- `tests/test_dashboard_web.py`
- `tests/e2e/serve_dashboard_fixture.py`
- `tests/e2e/prediction-market.spec.ts`
- `CHANGELOG.md`

Do not add a third source/adapter layer, migration, service plist, generic matching module, or Predict mainnet trading file. The isolated BNB Testnet canary is covered by its own plan.

---

### Task 0: Establish the implementation baseline

**Files:** No source changes.

- [ ] **Step 1: Confirm the prerequisite branch has landed on local main**

Run from `/Users/ray/projects/open_trader`:

```bash
git status --short --branch
git log -1 --oneline main
git grep -n 'MIN_THRESHOLD_ANNUALIZED_YIELD = Decimal("0.15")' main -- src/open_trader/prediction_arbitrage.py
git grep -n '^def simple_annualized_yield' main -- src/open_trader/polymarket_relation_discovery.py
```

Expected: both symbols exist on `main`. If either is absent, stop; this plan must not duplicate the separately owned annualized gate.

- [ ] **Step 2: Create the isolated implementation worktree from local main**

```bash
git worktree add .worktrees/predict-cross-venue-read-only \
  -b feat/predict-cross-venue-read-only main
cd .worktrees/predict-cross-venue-read-only
git status --short --branch
```

Expected: clean `feat/predict-cross-venue-read-only` worktree.

- [ ] **Step 3: Run the focused pre-change baseline**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_prediction_arbitrage.py \
  tests/test_polymarket_monitor.py \
  tests/test_prediction_arbitrage_store.py \
  tests/test_prediction_arbitrage_execution.py \
  tests/test_dashboard_web.py -q
```

Expected: PASS. Record the exact count before editing.

---

### Task 1: Add Predict public configuration, Keychain API key, and read-only source

**Files:**

- Create: `src/open_trader/predict_source.py`
- Modify: `src/open_trader/polymarket_trading.py:36-247`
- Modify: `src/open_trader/cli.py:1247-1465`
- Modify: `config/prediction_arbitrage.json.example`
- Create: `tests/test_predict_source.py`
- Modify: `tests/test_polymarket_trading.py:1380-1485`

**Interfaces:**

```python
@dataclass(frozen=True, slots=True)
class PredictConfig:
    wallet_address: str
    environment: Literal["mainnet"] = "mainnet"


@dataclass(frozen=True, slots=True)
class PredictMarket:
    market_id: str
    condition_id: str
    question: str
    rules: str
    resolution_source: str
    close_at: datetime
    settlement_at: datetime
    yes_token_id: str
    no_token_id: str
    settlement_asset: str
    minimum_order_size: Decimal
    tick_size: Decimal
    fee_rate_bps: Decimal
    polymarket_condition_ids: tuple[str, ...]
    rules_fingerprint: str


@dataclass(frozen=True, slots=True)
class PredictBook:
    market_id: str
    yes_asks: tuple[BookLevel, ...]
    no_asks: tuple[BookLevel, ...]
    source_timestamp: datetime
    received_at: datetime
```

`PredictSource` exposes only `list_open_markets()`, `get_market()`, `get_order_book()`, `stream_books()`, `get_balance_snapshot()`, and `snapshot()`. There is no order method.

- [ ] **Step 1: Write failing config and secret-boundary tests**

Cover:

1. the legacy two-key config still loads unchanged;
2. an optional `predict` object accepts only the canonical public wallet and `environment="mainnet"`;
3. unknown Predict fields and malformed addresses fail;
4. `store_predict_api_key()` sends the secret through stdin to `/usr/bin/security`, never process arguments;
5. `load_predict_api_key()` returns a stripped value and redacts all failure details;
6. CLI `prediction-arb predict setup` uses `getpass`, preserves existing Polymarket config, and never accepts `--api-key`.

Use the concrete config shape:

```json
{
  "signer_address": "0x0000000000000000000000000000000000000000",
  "wallet_address": "0x0000000000000000000000000000000000000000",
  "predict": {
    "wallet_address": "0xcE23B341C888A88C4C44D8B5Aa6D04A8615Ff435",
    "environment": "mainnet"
  }
}
```

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_predict_source.py tests/test_polymarket_trading.py \
  -k 'predict or trading_config' -q
```

Expected: FAIL because Predict config and Keychain functions do not exist.

- [ ] **Step 2: Implement backward-compatible config and Predict Keychain functions**

Keep the existing `TradingConfig` return type and add `predict: PredictConfig | None = None`. Permit exactly the old two keys or those two keys plus `predict`; do not loosen other validation.

Use a separate Keychain service and account:

```python
PREDICT_KEYCHAIN_SERVICE = "com.open-trader.predict"
PREDICT_API_KEY_ACCOUNT = "api-key"
```

Reuse the existing `_run_security`, canonical address validation, and redacted `KeychainError` pattern. Do not make a generic credential registry.

- [ ] **Step 3: Write failing REST normalization, WS, and health tests**

Inject URL opener and WebSocket connector fakes. Prove:

- REST calls use only `https://api.predict.fun` and header `x-api-key`;
- pagination keeps only open, standard binary, non-NegRisk markets with exact YES/NO outcomes;
- `polymarketConditionIds` remains a tuple of external IDs and is never confused with native `conditionId`;
- all price, size, tick, and fee fields become `Decimal`;
- a full YES-side book derives NO asks by complementing YES bids at the market tick precision;
- the subscription frame is exactly `{"method":"subscribe","requestId":1,"params":["predictOrderbook/<id>"]}`;
- heartbeat replies echo the exact supplied timestamp;
- malformed/out-of-order books are dropped and mark the source stale;
- missing key reports `pending`, 401/403 reports `auth_blocked` without a retry storm, and 429/network failures use bounded backoff;
- snapshots mask the wallet as `0xcE23…f435`, expose REST/WS separately, and never contain the API key.

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_predict_source.py -q
```

Expected: FAIL on the missing source behavior.

- [ ] **Step 4: Implement the smallest read-only source**

Use stdlib `urllib.request` inside `asyncio.to_thread()` for REST and the already-installed `websockets.connect` for streaming. Keep fixed mainnet URLs in this source; do not add config knobs for hosts.

Normalize complete books only. Derive the NO side with:

```python
no_asks = tuple(
    BookLevel(price=Decimal("1") - level.price, size=level.size)
    for level in reversed(yes_bids)
)
```

Quantize to the market tick and reject prices outside `[0, 1]`. A reconnect clears cached freshness before resubscribing.

- [ ] **Step 5: Run focused tests and commit**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_predict_source.py tests/test_polymarket_trading.py -q
git diff --check
git add config/prediction_arbitrage.json.example \
  src/open_trader/predict_source.py src/open_trader/polymarket_trading.py \
  src/open_trader/cli.py tests/test_predict_source.py \
  tests/test_polymarket_trading.py
git commit -m "feat: add read-only Predict source"
```

Expected: PASS; commit contains no secret and no Predict order method.

---

### Task 2: Resolve explicit pairs and validate exact cross-venue equivalence

**Files:**

- Create: `src/open_trader/predict_cross_venue.py`
- Create: `src/open_trader/schemas/cross_exchange_yes_no_equivalence.json`
- Create: `tests/test_predict_cross_venue.py`

**Interfaces:**

```python
Direction = Literal[
    "PREDICT_YES_POLYMARKET_NO",
    "POLYMARKET_YES_PREDICT_NO",
]


@dataclass(frozen=True, slots=True)
class VenueMarket:
    exchange: Literal["predict.fun", "polymarket"]
    market_id: str
    condition_id: str
    question: str
    rules: str
    resolution_source: str
    close_at: datetime
    settlement_at: datetime
    yes_token_id: str
    no_token_id: str
    settlement_asset: str
    minimum_order_size: Decimal
    tick_size: Decimal
    fee_rate_bps: Decimal
    rules_fingerprint: str


@dataclass(frozen=True, slots=True)
class ExplicitMarketPair:
    pair_id: str
    predict: VenueMarket
    polymarket: VenueMarket


@dataclass(frozen=True, slots=True)
class CrossVenueValidation:
    approved: bool
    reason: str
    prompt_version: str
    predict_fingerprint: str
    polymarket_fingerprint: str
```

- [ ] **Step 1: Write failing explicit-mapping tests**

Use fake Predict rows and fake Gamma/CLOB clients to prove:

1. every non-empty `polymarketConditionIds[]` value is requested;
2. Gamma is queried with repeated `condition_ids` for `closed=false` and `closed=true`;
3. CLOB `/markets/{condition_id}` is used only when Gamma has no exact row;
4. returned condition IDs must equal the requested external ID;
5. Predict native `conditionId` never matches or replaces the Polymarket condition ID;
6. empty/unresolved mappings are skipped and counted;
7. `pair_id` is deterministic from venue-qualified native identities.

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_predict_cross_venue.py -k mapping -q
```

Expected: FAIL because pair resolution does not exist.

- [ ] **Step 2: Implement explicit resolution without a matcher framework**

Add private functions in `predict_cross_venue.py` for Gamma batch lookup, exact CLOB fallback, Polymarket normalization, and deterministic pair ID. Use injected callables in tests. Do not add title similarity or a general adapter protocol.

- [ ] **Step 3: Write failing Codex schema/cache/post-check tests**

The schema must require:

- `decision` equal to `APPROVE` or `REJECT`;
- both venue-qualified IDs and both rule fingerprints;
- two divergent-state checks, each with `possible: false` for approval;
- evidence rows for both exchanges containing a `field` and exact `quote`;
- `uncertainties` as an array that must be empty for approval.

Tests must reject approval when IDs/exchanges/fingerprints mismatch, either divergent state is possible, evidence is missing, a quote is absent from the supplied rules, or uncertainty remains. Verify the cache namespace is exactly `cross-exchange-yes-no-equivalence-v1` and the existing threshold prompt/schema files are unchanged.

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_predict_cross_venue.py -k 'codex or equivalence or fingerprint' -q
```

Expected: FAIL because the validator and schema do not exist.

- [ ] **Step 4: Implement one dedicated validator**

Follow the existing `CodexRelationValidator` subprocess, JSON-schema, `llm_cache`, and usage-accounting pattern, but keep a separate prompt constant and deterministic result checker in `predict_cross_venue.py`. The input and output must name exchanges explicitly. Do not modify `polymarket_relation_discovery.py` or reuse its threshold prompt.

- [ ] **Step 5: Run focused tests and commit**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_predict_cross_venue.py \
  tests/test_polymarket_relation_discovery.py -q
git diff --check
git add src/open_trader/predict_cross_venue.py \
  src/open_trader/schemas/cross_exchange_yes_no_equivalence.json \
  tests/test_predict_cross_venue.py
git commit -m "feat: validate explicit cross-venue market pairs"
```

Expected: PASS, including the existing threshold validator regression suite.

---

### Task 3: Calculate the two executable cross-venue directions

**Files:**

- Modify: `src/open_trader/predict_cross_venue.py`
- Modify: `src/open_trader/polymarket_relation_discovery.py:1837-1860`
- Modify: `tests/test_predict_cross_venue.py`
- Modify: `tests/test_prediction_arbitrage.py`

**Interfaces:**

```python
@dataclass(frozen=True, slots=True)
class CrossVenueLeg:
    exchange: Literal["predict.fun", "polymarket"]
    market_id: str
    condition_id: str
    outcome: Literal["YES", "NO"]
    token_id: str
    settlement_asset: str
    quantity: Decimal
    max_price: Decimal
    max_cost: Decimal
    maximum_fee: Decimal
    book_timestamp: datetime
    settlement_at: datetime


@dataclass(frozen=True, slots=True)
class CrossVenueIntent:
    pair_id: str
    direction: Direction
    legs: tuple[CrossVenueLeg, CrossVenueLeg]
    quantity: Decimal
    total_max_cost: Decimal
    maximum_fee: Decimal
    minimum_payout: Decimal
    minimum_profit: Decimal
    annualized_yield: Decimal
    resolution_at: datetime
```

- [ ] **Step 1: Write failing Decimal/depth/fee tests**

Cover both directions and prove:

- executable asks and common depth determine quantity and worst prices;
- minimum order size and tick protection fail closed;
- `total_max_cost + maximum_fee < minimum_payout` is required;
- Predict's conservative fee ceiling is used, while Polymarket reuses the existing fee calculation;
- the later of the two settlement times is supplied to `simple_annualized_yield`;
- the imported `MIN_THRESHOLD_ANNUALIZED_YIELD` gates the clear signal;
- nominally different asset labels are retained and no FX conversion occurs;
- all serialized legs include the required exchange and native IDs;
- floats, stale books, negative depth, and crossed invalid books are rejected.

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_predict_cross_venue.py -k 'intent or direction or annualized or fee' -q
```

Expected: FAIL because the calculator does not exist.

- [ ] **Step 2: Implement the calculator with existing primitives**

Reuse `_book_segments`, `_protected_buy_candidates`, and `_worst_price` from `prediction_arbitrage.py`; do not copy them. Calculate only the two approved directions.

The Predict fee ceiling is:

```python
predict_maximum_fee = (
    quantity * predict_market.fee_rate_bps / Decimal("10000")
)
# ponytail: conservative payout-base ceiling; replace only when Predict exposes
# a deterministic pre-trade fee quote.
```

Use the later settlement timestamp and the existing annualized helper/constant. Do not add a configurable threshold or FX client.

- [ ] **Step 3: Run focused tests and commit**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_predict_cross_venue.py tests/test_prediction_arbitrage.py -q
git diff --check
git add src/open_trader/predict_cross_venue.py \
  src/open_trader/polymarket_relation_discovery.py \
  tests/test_predict_cross_venue.py tests/test_prediction_arbitrage.py
git commit -m "feat: calculate cross-venue yes-no signals"
```

Expected: PASS.

---

### Task 4: Reuse Polymarket books and orchestrate the slow/hot paths

**Files:**

- Modify: `src/open_trader/polymarket_monitor.py:293-513,950-985,1761-1856,3006-3135`
- Modify: `src/open_trader/predict_cross_venue.py`
- Modify: `tests/test_polymarket_monitor.py`
- Modify: `tests/test_predict_cross_venue.py`

**Interfaces:**

Add only these two public methods to the concrete Polymarket monitor:

```python
def set_cross_venue_tokens(self, token_ids: Sequence[str]) -> None:
    """Replace the externally requested token set and force a fresh subscription."""


def cross_venue_books(self, token_ids: Sequence[str]) -> dict[str, ThresholdOrderBook]:
    """Return fresh cached books for requested tokens; omit stale or absent rows."""
```

`PredictCrossVenueMonitor` owns one async task and exposes `start()`, `stop()`, and `snapshot()` only.

- [ ] **Step 1: Write failing Polymarket external-book tests**

Prove that:

- setting cross-venue tokens adds only those tokens to the existing subscription union;
- a changed set causes one resubscribe and a fresh REST snapshot;
- full-book and supported delta messages update the local `ThresholdOrderBook` cache;
- malformed/out-of-order events invalidate freshness rather than trigger synchronous REST work in the WebSocket callback;
- removing tokens drops their books;
- existing standard and threshold subscriptions remain unchanged.

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_polymarket_monitor.py -k cross_venue -q
```

Expected: FAIL because the external token set and cache API do not exist.

- [ ] **Step 2: Add the narrow external-book surface**

Extend the existing subscription union and stream-book update path. Do not import Predict code into `polymarket_monitor.py`, start a second Polymarket stream, or add a source interface. Existing relation events may retain their current REST behavior; cross-venue events must update/invalidate the local cache only.

- [ ] **Step 3: Write failing orchestrator tests**

Use fake clocks/sources/validator/store and cover this exact state flow:

```text
explicitly mapped
  -> deterministic eligible / candidate monitored
  -> Codex approved
  -> WebSocket subscribed
  -> positive local candidate
  -> concurrent two-venue REST confirmation
  -> annualized gate passed
  -> one open signal episode
```

Assert:

- discovery runs at a fixed 15-minute interval and is not in the hot path;
- Codex runs sequentially before subscription and never after a book update;
- only approved pairs contribute tokens to either venue subscription;
- both directions are evaluated locally;
- one positive direction creates at most one confirmation task;
- the two REST refreshes overlap under `asyncio.gather`;
- no signal opens from WebSocket data alone;
- stale/changed/rejected books close the episode and permit a later fresh rearm;
- reconnect or fingerprint change removes the pair from subscriptions immediately;
- Predict failure does not stop or alter same-venue Polymarket behavior;
- missing API key produces pending health and zero cross subscriptions.

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_predict_cross_venue.py -k monitor -q
```

Expected: FAIL because the orchestrator does not exist.

- [ ] **Step 4: Implement the single concrete orchestrator**

Keep discovery, validation queue, subscriptions, confirmations, and episodes in `PredictCrossVenueMonitor`; do not split them into managers. Its snapshot must include:

```python
{
    "status": "ready",
    "mode": "observe_only",
    "funnel": {
        "matched_pairs": 12,
        "monitored_pairs": 8,
        "codex_approved_pairs": 5,
        "arbitrage_space_pairs": 2,
        "clear_signal_pairs": 1,
    },
    "opportunities": [],
    "events": [],
}
```

Counts are pair counts, not leg or market counts. Every emitted opportunity uses `market_type="cross_venue_yes_no"`, `execution_mode="observe_only"`, `actionable=False`, and `clear_signal=True` only after REST confirmation and the annualized gate.

- [ ] **Step 5: Run focused tests and commit**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_predict_cross_venue.py tests/test_polymarket_monitor.py -q
git diff --check
git add src/open_trader/predict_cross_venue.py \
  src/open_trader/polymarket_monitor.py \
  tests/test_predict_cross_venue.py tests/test_polymarket_monitor.py
git commit -m "feat: monitor approved cross-venue pairs"
```

Expected: PASS.

---

### Task 5: Persist and notify one observation-only signal episode

**Files:**

- Modify: `src/open_trader/prediction_arbitrage_store.py:29-126,583-845`
- Modify: `src/open_trader/prediction_arbitrage_execution.py:217-405`
- Modify: `src/open_trader/notifications.py:401-465`
- Modify: `src/open_trader/predict_cross_venue.py`
- Modify: `tests/test_prediction_arbitrage_store.py`
- Modify: `tests/test_prediction_arbitrage_execution.py`
- Modify: `tests/test_notifications.py`
- Modify: `tests/test_predict_cross_venue.py`

- [ ] **Step 1: Write failing store redaction/episode tests**

Prove that cross signal payloads retain public `token_id`, `yes_token_id`, and `no_token_id` values inside venue-qualified legs while still deleting API keys, private keys, signatures, JWTs, secrets, passphrases, and credentials at any nesting depth. Assert deterministic opportunity IDs:

```text
cross:<pair_id>:PREDICT_YES_POLYMARKET_NO
cross:<pair_id>:POLYMARKET_YES_PREDICT_NO
```

Assert one open row per opportunity ID, immutable trigger economics, close reason persistence, and rearm only after closure.

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_prediction_arbitrage_store.py -k cross_venue -q
```

Expected: FAIL because ordinary signal serialization currently drops public token IDs.

- [ ] **Step 2: Reuse the relation-safe serializer for signals**

Route signal create/update/close payloads through the existing recursive secret redaction plus the existing public token allowlist. Do not add columns or a migration.

- [ ] **Step 3: Write failing observation-notification tests**

Prove that `notify_ready_opportunity()` branches on `market_type="cross_venue_yes_no"` before any preview, preflight, or trading method. The renderer must display each leg as `Predict.fun · YES` or `Polymarket · NO`, include confirmed maximum cost/profit and settlement assets, and exclude action links, wallet details, internal rule traces, credentials, and order language.

Also assert `/preview` and `/executions` reject a crafted cross opportunity even when `clear_signal=True`.

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_notifications.py tests/test_prediction_arbitrage_execution.py \
  -k cross_venue -q
```

Expected: FAIL because the cross type is unsupported.

- [ ] **Step 4: Add one early observation-only branch**

Reuse the existing notification lease/delivery mechanism and extend the current YES/NO renderer to render `legs`. The new branch returns before `_prepare_opportunity`, preview, preflight, or any trading call. Keep current standard-binary and threshold branches unchanged.

- [ ] **Step 5: Run focused tests and commit**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_prediction_arbitrage_store.py \
  tests/test_notifications.py \
  tests/test_prediction_arbitrage_execution.py \
  tests/test_predict_cross_venue.py -q
git diff --check
git add src/open_trader/prediction_arbitrage_store.py \
  src/open_trader/prediction_arbitrage_execution.py \
  src/open_trader/notifications.py src/open_trader/predict_cross_venue.py \
  tests/test_prediction_arbitrage_store.py \
  tests/test_prediction_arbitrage_execution.py tests/test_notifications.py \
  tests/test_predict_cross_venue.py
git commit -m "feat: retain cross-venue signal episodes"
```

Expected: PASS and no execution request from the cross branch.

---

### Task 6: Project source-specific health and cross funnel through the Dashboard API

**Files:**

- Modify: `src/open_trader/dashboard_web.py:532-890,1109-1200,1593-1715`
- Modify: `tests/test_dashboard_web.py:1880-2075,3180-3450`

- [ ] **Step 1: Write failing payload and lifecycle tests**

Cover:

- `venues` always contains configured Polymarket and Predict entries with separate REST/WS, masked wallet, asset-labelled balance, mode, last success, and concise reason;
- missing Predict API key is `pending` and does not change Polymarket health;
- `cross_venue.funnel` contains exactly the five approved pair counters;
- cross opportunities/events merge into the existing current/history projections with explicit legs;
- a live cross signal has `signal_live_now=True` but `actionable_now=False`;
- server startup starts Polymarket first and cross monitoring second; shutdown stops cross first and Polymarket second;
- if Predict construction fails, the page remains available with source-specific degraded health;
- existing preview/execution endpoints reject cross IDs without invoking execution.

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_dashboard_web.py -k 'prediction and (venue or cross or lifecycle)' -q
```

Expected: FAIL because the Dashboard owns only one source today.

- [ ] **Step 2: Add optional cross monitor parameters and merge projections**

Extend `_prediction_state_payload`, `_prediction_history_payload`, `create_dashboard_server`, and `serve_dashboard` with `cross_venue_monitor: PredictCrossVenueMonitor | None = None`. Keep defaults so existing tests and callers remain valid.

The top-level shape must add, not replace, these keys:

```python
{
    "venues": [polymarket_venue, predict_venue],
    "cross_venue": cross_monitor.snapshot(),
}
```

Do not let Predict readiness overwrite the existing Polymarket/controller readiness fields.

- [ ] **Step 3: Run focused tests and commit**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_dashboard_web.py tests/test_predict_cross_venue.py -q
git diff --check
git add src/open_trader/dashboard_web.py tests/test_dashboard_web.py
git commit -m "feat: expose cross-venue dashboard state"
```

Expected: PASS.

---

### Task 7: Update the existing YES/NO page in place

**Files:**

- Modify: `src/open_trader/dashboard_static/dashboard.js:2317-2915`
- Modify: `src/open_trader/dashboard_static/dashboard.css:5550-5855`
- Modify: `tests/e2e/serve_dashboard_fixture.py:217-285`
- Modify: `tests/e2e/prediction-market.spec.ts`

- [ ] **Step 1: Write failing Playwright assertions**

Fixture data must include two venue cards, all five funnel counts, one same-venue row, and one observation-only cross row with explicit legs.

Assert on desktop and mobile:

- the shared exchange header appears above the two existing strategy tabs;
- both exchange cards show independent REST/WS, wallet, asset balance, and mode;
- balances are never summed;
- the old four summary cards are absent from YES/NO;
- the funnel labels are exactly `两所对应标的`, `正在监视`, `Codex 认为可以`, `有套利空间`, `明确下单信号`;
- `正在监视` help copy identifies low-frequency candidate monitoring, not WebSocket monitoring;
- the existing LLM funnel remains inside the LLM tab;
- current/history cross rows show `Predict.fun · YES` and `Polymarket · NO` (and the inverse case);
- observation-only cross rows have no `参与`, `重新检查`, preview, or submit control;
- existing Polymarket same-venue preview/confirm behavior still works;
- no page action emits a Predict mutation request.

Run:

```bash
npm exec playwright test tests/e2e/prediction-market.spec.ts --project=chromium
```

Expected: FAIL on the missing shared header/funnel and old metric strip.

- [ ] **Step 2: Implement the minimum in-place UI change**

Replace `predictionReadinessStrip()` with a venue-card renderer backed by `payload.venues`, move it above `predictionStrategyTabs()`, remove `predictionMetricStrip()` from `predictionYesNoWorkspace()`, and add one five-stage cross-funnel renderer backed by `payload.cross_venue.funnel`.

Reuse existing `.pm-readiness-*`, `.pm-funnel-*`, row, badge, table, and responsive styles. Add CSS only for the extra venue-card wrapping and five-column funnel when existing selectors cannot express it. Do not rename tabs, alter page navigation, or create a new visual language.

- [ ] **Step 3: Run renderer, E2E, and focused backend tests**

```bash
npm exec playwright test tests/e2e/prediction-market.spec.ts --project=chromium
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_dashboard_web.py -q
```

Expected: PASS. Do not capture new screenshots unless the user requests them.

- [ ] **Step 4: Commit the UI update**

```bash
git diff --check
git add src/open_trader/dashboard_static/dashboard.js \
  src/open_trader/dashboard_static/dashboard.css \
  tests/e2e/serve_dashboard_fixture.py tests/e2e/prediction-market.spec.ts
git commit -m "feat: show cross-venue yes-no funnel"
```

---

### Task 8: Prove regression safety, live read-only behavior, and review readiness

**Files:**

- Modify: `CHANGELOG.md`

- [ ] **Step 1: Run security and scope scans**

```bash
rg -n 'private.?key|api.?key|jwt|signature|secret|passphrase' \
  config src/open_trader tests \
  -g '!tests/test_predict_source.py' \
  -g '!tests/test_prediction_arbitrage_store.py'
rg -n 'predict.*(submit|order|approve)|api\.predict\.fun.*/orders' \
  src/open_trader -i
rg -n 'cross-exchange-yes-no-equivalence-v1' \
  src/open_trader tests
git diff main -- src/open_trader/polymarket_relation_discovery.py \
  src/open_trader/schemas/polymarket_threshold_relation.json
```

Expected: no committed secret, no Predict mainnet mutation path, the new prompt version appears where expected, and the old threshold prompt/schema have no diff.

- [ ] **Step 2: Run the complete automated suite**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q
npm exec playwright test tests/e2e/prediction-market.spec.ts --project=chromium
```

Expected: PASS. Record exact counts.

- [ ] **Step 3: Run the direct read-only workflow**

With no Predict API key, start the candidate Dashboard command and verify the state endpoint reports Predict `pending`, Polymarket remains healthy, and no Predict subscription or mutation occurs.

After the API key is allocated and stored in Keychain, restart the candidate and verify fresh logs show:

- Predict mainnet REST catalogue success;
- mapping counters for explicit/resolved/unmapped references;
- Codex queue outside the WebSocket hot path;
- approved-pair subscription counts;
- source-specific REST/WS timestamps;
- zero Predict mutation requests.

Use only sanitized IDs and masked wallet output. If the API key is still unavailable, record this direct mainnet check as externally blocked; do not replace it with fixtures and do not claim live Predict readiness.

- [ ] **Step 4: Update and commit the operator-facing changelog before merge**

Add a dated `2026-08-02` entry covering the read-only Predict source, explicit matching, separate Codex equivalence gate, cross-venue funnel, observation-only signals, and the explicit absence of Predict mainnet execution.

```bash
git add CHANGELOG.md
git commit -m "docs: log Predict cross-venue observation"
git status --short
```

Expected: clean worktree.

- [ ] **Step 5: Run the final Dashboard acceptance gate once**

```bash
make acceptance
```

Expected: `PASS`. `FAIL` must be fixed and rerun. `BLOCKED` must be reported as blocked and cannot be replaced with curl, tests, fixtures, or screenshots.

- [ ] **Step 6: Redeploy the exact accepted SHA and verify runtime ownership**

After `PASS`, record the accepted SHA, redeploy that exact SHA, then verify:

```bash
git rev-parse HEAD
curl -fsS http://127.0.0.1:8766/api/dashboard-runtime
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8766/
lsof -nP -iTCP:8766 -sTCP:LISTEN
```

Also inspect the live PID's cwd/SHA and fresh launchd logs. Expected: accepted SHA equals deployed SHA, a new process owns port 8766, logs are fresh, and HTTP status is 200.

- [ ] **Step 7: Self-review against the approved design**

Check every in-scope and out-of-scope item in the approved spec. Scan for placeholders and accidental abstractions:

```bash
rg -n 'TODO|FIXME|NotImplemented|pass$|VenueAdapter|SourceFactory|MatcherFactory' \
  src/open_trader tests
git diff --check main...HEAD
git log --oneline main..HEAD
```

Expected: no task placeholder, no generic venue framework, and only intentional commits.

## Completion boundary

This plan is complete only when focused tests, the full suite, direct source-specific runtime checks, final `make acceptance`, and exact-SHA redeployment all satisfy the project gates. A pending Predict API key may leave live Predict verification externally blocked; it must not be reported as a working mainnet connection until observed. Predict mainnet submission remains impossible by construction.
