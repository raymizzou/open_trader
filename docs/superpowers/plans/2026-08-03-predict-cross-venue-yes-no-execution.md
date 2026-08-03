# Predict.fun × Polymarket Cross-Venue YES/NO Execution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the existing read-only Predict.fun × Polymarket YES/NO watcher into a protected manual execution path that admits only Codex-proven direct pairs, enforces the approved 15%/20/2/100 USDT rules, submits two bounded FOK legs concurrently, and independently reconciles both venues.

**Architecture:** Keep `PredictSource`, `PredictCrossVenueMonitor`, `PredictionExecutionService`, `PredictionArbitrageStore`, and the existing Dashboard as the owners they already are. Add one focused `PredictTradingClient` for Predict authentication, signing, submit, balance, position, and receipt reads; add only the single-leg methods needed on the existing Polymarket client. Extend the current execution state machine with a cross-venue branch and one small SQLite reservation table. Do not add an exchange framework, queue, scheduler, browser-side trading logic, or a second redemption system.

**Tech Stack:** Python 3.12, `Decimal`, stdlib `urllib`/`threading`/`sqlite3`, `predict-sdk==0.0.22`, existing Polymarket client, macOS Keychain, pytest, vanilla JavaScript/CSS, Playwright, launchd.

**Approved spec:** `docs/superpowers/specs/2026-08-03-predict-cross-venue-yes-no-execution-design.md`

**Official Predict contracts used by this plan:**

- `GET /v1/auth/message`, then `POST /v1/auth` with `signer`, `signature`, and the dynamic `message`.
- `POST /v1/orders` with API key + JWT and the SDK-built signed order.
- `GET /v1/orders/{hash}`, `/v1/orders/matches`, `/v1/account/activity`, and `/v1/positions` for independent reconciliation.
- `OrderBuilder.make(ChainId.BNB_MAINNET, privy_key, OrderBuilderOptions(predict_account=deposit_address))` for a Predict smart account.

## Global Constraints

- Cross-venue only: `Predict YES + Polymarket NO` and `Polymarket YES + Predict NO`.
- Direct polarity only. Missing candidate IDs, inverted propositions, compound contracts, missing rules, or ambiguous canonical cutoffs fail closed.
- Codex runs only in low-frequency admission. Hot monitoring, preview, confirmation, execution, and reconciliation never invoke Codex.
- Predict USDT and Polymarket pUSD are accounted 1:1, with no FX call or haircut.
- Entry requires positive deterministic nominal profit and theoretical simple annualized yield `>= Decimal("0.15")`.
- Entry maximum is 20 USDT equivalent including deterministic fees and calculable gas; remediation worst-case loss maximum is 2; unsettled cross principal maximum is 100.
- The legacy 65 pUSD maximum wallet-balance rule remains unchanged for legacy paths and is never applied to cross-venue execution.
- Only server-owned REST/account facts may authorize or prove a trade. WebSocket and browser payloads are discovery/display inputs only.
- A timeout is ambiguous until account/order reconciliation proves otherwise. Never blind retry a mutation.
- Keep API key and Privy private key in Keychain only. Never persist or log JWTs, signatures, signed orders, raw auth messages, or secrets.
- Acceptance uses deterministic submit doubles and a deterministic notifier. It submits no order and sends no real Feishu message.
- Run `make acceptance` only once, after all focused tests and direct no-submit checks pass.
- The first real cross-venue canary is a separate post-acceptance operator action and requires a new explicit confirmation.

---

### Task 1: Normalize Predict category timing and complete market rules

**Files:**

- Modify: `src/open_trader/predict_source.py`
- Test: `tests/test_predict_source.py`

**Interfaces:**

- `PredictMarket` replaces invented market-level `close_at`/`settlement_at` with `category_slug`, `event_start_at`, `event_end_at`, and `resolution_provider`.
- `PredictSource` caches `GET /v1/categories/{slug}` results and joins each open market to its category before normalization.
- `rules_fingerprint` covers question, complete description/rules, resolution provider, category timing, outcome identities, and explicit Polymarket candidate IDs.

- [ ] **Step 1: Write failing source tests for category joins and fail-closed metadata**

Add fixtures that mirror the official payload shape: the market has `categorySlug`, while the category has `startsAt`, `endsAt`, and `resolutionProvider`. Assert:

```python
market = asyncio.run(source.get_market("896"))
assert market is not None
assert market.category_slug == "btc-year-end"
assert market.event_end_at == datetime(2026, 12, 31, 23, 59, tzinfo=UTC)
assert market.resolution_provider == "PREDICT_DOT_FUN"
assert requested_paths == ["/v1/markets/896", "/v1/categories/btc-year-end"]
```

Add cases proving one category request is reused across sibling markets, missing/unparseable category timing excludes the market from executable candidate resolution, and an empty `polymarketConditionIds` list remains empty without any fallback catalog scan.

- [ ] **Step 2: Run the focused tests and verify they fail**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest \
  tests/test_predict_source.py -k 'category or market_normal' -q
```

Expected: failures show the current normalizer incorrectly expects `closesAt`, `settlementAt`, and `resolutionSource` on the market.

- [ ] **Step 3: Add the minimal category cache and normalization join**

Add `self._categories: dict[str, dict[str, object]]` and one private async loader:

```python
async def _category(self, slug: str) -> dict[str, object] | None:
    cached = self._categories.get(slug)
    if cached is not None:
        return cached
    payload = await self._rest_json(f"/v1/categories/{quote(slug, safe='')}")
    row = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(row, dict) and row.get("slug") == slug:
        self._categories[slug] = row
        return row
    return None
```

Pass the category into `_normalise_market(payload, category)`. Keep the existing binary/standard/non-NegRisk/non-yield-bearing filters, but accept the official default variant spelling used by the API. Do not synthesize a settlement timestamp. Fingerprint the complete deterministic input and keep the existing API key/User-Agent boundary.

- [ ] **Step 4: Run source tests and commit**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest \
  tests/test_predict_source.py tests/test_predict_cross_venue.py -q
```

Expected: PASS.

Commit:

```bash
git add src/open_trader/predict_source.py tests/test_predict_source.py
git commit -m "fix: derive Predict timing from categories"
```

---

### Task 2: Make Codex admission prove direct polarity and one canonical cutoff

**Files:**

- Modify: `src/open_trader/predict_cross_venue.py`
- Modify: `src/open_trader/schemas/cross_exchange_yes_no_equivalence.json`
- Test: `tests/test_predict_cross_venue.py`

**Interfaces:**

- `VenueMarket` carries raw metadata timing and full rules; it does not claim a settlement date.
- `CrossVenueValidation` adds `canonical_cutoff`, `direct_outcome_mapping`, `summary`, `evidence`, `approved_at`, and `cache_key`.
- Schema version becomes 2 and requires direct `YES->YES`, `NO->NO` mapping plus a parseable UTC canonical cutoff.
- `cross_exchange_equivalence_cache_key(pair, prompt_version)` includes both rule/timing/outcome fingerprints and the prompt version.

- [ ] **Step 1: Replace exact-date tests with canonical-cutoff admission tests**

Add one APPROVE result where Predict metadata differs from Polymarket by one minute and another by 29 hours, but both full rules contain evidence for the same UTC cutoff. Assert both pass. Add REJECT cases for incomplete timing, different resolution/cancellation behavior, inverted polarity, compound propositions, evidence not present verbatim in the supplied venue rules, and a non-UTC/unparseable cutoff.

The approved fixture should have this core shape:

```python
{
    "schema_version": 2,
    "decision": "APPROVE",
    "direct_outcome_mapping": {
        "predict_yes": "YES",
        "predict_no": "NO",
        "polymarket_yes": "YES",
        "polymarket_no": "NO",
    },
    "canonical_cutoff": "2026-12-31T23:59:00Z",
    "evidence": [
        {"exchange": "predict.fun", "field": "cutoff", "quote": "at 23:59 UTC on December 31, 2026"},
        {"exchange": "polymarket", "field": "cutoff", "quote": "at 23:59 UTC on December 31, 2026"},
    ],
    "uncertainties": [],
}
```

Add a cache invalidation test that changes, one at a time, rule text, category timing, token identity, candidate ID, and prompt version. Each change must produce a different key and remove the pair from the admitted hot pool.

- [ ] **Step 2: Run the Codex/schema tests and verify they fail**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest \
  tests/test_predict_cross_venue.py -k 'equivalence or canonical or cache' -q
```

Expected: existing schema still requires raw `close_at` and `settlement_at`, and the validator returns `DATE_MISMATCH`.

- [ ] **Step 3: Update the prompt, schema, and one backend validator**

Change the prompt version to `cross-exchange-yes-no-equivalence-v2`. Tell Codex to derive a canonical UTC event cutoff from complete contract text and to return direct polarity only. Remove raw timestamp echoing from the output schema.

In `_equivalence_validation`, validate in this order:

1. schema shape and `decision`
2. exact exchange/market/condition identities and current fingerprints
3. exact direct outcome mapping
4. parseable timezone-aware `canonical_cutoff`
5. both divergent states impossible
6. at least one verbatim evidence quote from each venue, each present in that venue's supplied rules
7. zero uncertainties

Return REJECT on every missing or malformed field. Do not add a timestamp tolerance.

- [ ] **Step 4: Prove Codex is absent from hot paths**

Extend the monitor test double with `calls`. Run one low-frequency admission, several WebSocket/book refreshes, one opportunity refresh, and one monitor snapshot. Assert `calls == 1`; then change a fingerprint and assert exactly one new admission call is queued before the pair can re-enter the hot pool.

- [ ] **Step 5: Run focused tests and commit**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest \
  tests/test_predict_cross_venue.py -q
```

Expected: PASS.

Commit:

```bash
git add src/open_trader/predict_cross_venue.py \
  src/open_trader/schemas/cross_exchange_yes_no_equivalence.json \
  tests/test_predict_cross_venue.py
git commit -m "feat: admit cross venue pairs by canonical rules"
```

---

### Task 3: Add the focused Predict authenticated trading adapter

**Files:**

- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `src/open_trader/polymarket_trading.py`
- Add: `src/open_trader/predict_trading.py`
- Add: `tests/test_predict_trading.py`
- Modify: `tests/test_polymarket_trading.py`

**Interfaces:**

- Pin `predict-sdk==0.0.22`.
- Reuse `load_keychain_secret`; add only `PREDICT_PRIVATE_KEY_ACCOUNT = "privy-private-key"` and `load_predict_private_key()`.
- `PredictTradingClient` exposes synchronous `quote_market_buy`, `account_snapshot`, `no_submit_buy_preflight`, `submit_buy_once`, `reconcile_buy`, and `redeemable_snapshot` methods for the execution service.
- Small dataclasses in `predict_trading.py`: `PredictBuyQuote` and `PredictLegResult`. Do not define a generalized venue interface.

- [ ] **Step 1: Write Keychain and adapter contract tests before adding the dependency**

Test that the private-key loader uses service `com.open-trader.predict`, account `privy-private-key`, captures secret output without placing it in process arguments, and never includes it in exceptions.

In `tests/test_predict_trading.py`, use an injected SDK builder and `urlopen_fn`. Assert:

- the builder is created with BNB mainnet, the Privy signer, and `predict_account=<deposit address>`;
- auth fetches the dynamic message, signs it, and posts `{"signer": deposit_address, "signature": "signature-sentinel", "message": "dynamic-message-sentinel"}` in the test double;
- `x-api-key`, `Authorization: Bearer jwt-sentinel`, and `User-Agent: open-trader/0.1` are present where required;
- `quote_market_buy` returns integer maker/taker amounts and the minimum net redeemable units from the SDK order amounts;
- `no_submit_buy_preflight` builds and signs the exact MARKET+FOK order but makes no `/v1/orders` request;
- `submit_buy_once` posts once and returns order ID/hash without treating HTTP 201 as a fill;
- transport failure returns `ambiguous` and does not retry;
- no stored/logged/test result contains the API key, private key, JWT, signature, or signed order.

- [ ] **Step 2: Run tests and verify import/contract failures**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest \
  tests/test_polymarket_trading.py -k 'predict and keychain' \
  tests/test_predict_trading.py -q
```

Expected: the new module and private-key wrapper do not exist.

- [ ] **Step 3: Pin the official SDK and lock it**

Add `predict-sdk==0.0.22` to project dependencies, then run:

```bash
uv lock
uv sync --extra dev
.venv/bin/python -c 'from predict_sdk import OrderBuilder, ChainId, OrderBuilderOptions; print("predict-sdk-ok")'
```

Expected: dependency resolution succeeds on Python 3.12 and prints `predict-sdk-ok`. If the resolver conflicts with the existing Polymarket stack, stop and report the exact conflict; do not hand-roll EIP-712 signing.

- [ ] **Step 4: Implement auth, quote, preflight, and submit with injected boundaries**

Construct the official builder exactly once per client:

```python
OrderBuilder.make(
    ChainId.BNB_MAINNET,
    private_key,
    OrderBuilderOptions(predict_account=config.wallet_address),
)
```

Use `MarketHelperInput(side=Side.BUY, quantity_wei=10**18, slippage_bps=0, is_min_amount_out=True)` in the one-share contract test, and pass the selected fixed-point quantity in production. Build/sign with `BuildOrderInput` and the market's current `feeRateBps`.

The create-order body must be server-generated and bounded:

```python
{
    "data": {
        "pricePerShare": quote.price_per_share_wei,
        "strategy": "MARKET",
        "slippageBps": "0",
        "isFillOrKill": True,
        "isPostOnly": False,
        "reservedBalancePolicy": "REJECT_MARKET_ORDER",
        "isMinAmountOut": True,
        "selfTradePrevention": "CANCEL_MAKER",
        "order": signed_order_payload,
    }
}
```

Keep JWT only in memory. On 401, discard it and reacquire once before a read; a mutation with an unknown outcome is never replayed merely because auth changed.

- [ ] **Step 5: Implement independent reconciliation reads**

`reconcile_buy` must correlate the intended market/token/signer to:

1. `GET /v1/orders/{hash}` final order status
2. `GET /v1/orders/matches` transaction/executed amount/fee facts
3. `GET /v1/account/activity` authenticated account event
4. `GET /v1/positions?marketId=896` actual outcome amount in the contract test

Return `verified=True` only when identity, order, match/activity, and position all agree. Return `conclusively_absent=True` only when fresh order and account reads prove no order, fill, or position. Everything else is `unknown`; `amountFilled` alone never proves net units.

`account_snapshot` uses SDK `balance_of("USDT")` and scoped BUY approval checks. It returns the public deposit address, available USDT, allowance readiness, fresh timestamp, open orders, and positions—never signer material.

- [ ] **Step 6: Run adapter tests and commit**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest \
  tests/test_polymarket_trading.py tests/test_predict_trading.py tests/test_predict_source.py -q
```

Expected: PASS.

Commit:

```bash
git add pyproject.toml uv.lock \
  src/open_trader/polymarket_trading.py src/open_trader/predict_trading.py \
  tests/test_polymarket_trading.py tests/test_predict_trading.py
git commit -m "feat: add protected Predict trading adapter"
```

---

### Task 4: Build fee-aware cross-venue intents within the 15% and 20 USDT gates

**Files:**

- Modify: `src/open_trader/predict_cross_venue.py`
- Modify: `src/open_trader/prediction_arbitrage.py`
- Test: `tests/test_predict_cross_venue.py`
- Test: `tests/test_prediction_arbitrage.py`

**Interfaces:**

- Reuse `CrossVenueIntent`; make `quantity` the aligned net redeemable units and add `canonical_cutoff` plus deterministic all-in cost fields rather than creating another intent type. Each `CrossVenueLeg` keeps both `requested_quantity` and `net_quantity`; no SDK quote object is persisted in the intent.
- `build_cross_venue_intents(pair, predict_book, polymarket_books, now=now, predict_quote_fn=quote_fn)` remains pure under tests through an injected quote function.
- `PredictCrossVenueMonitor.__init__` receives the existing Predict client's `quote_market_buy` callable; it does not load signing material itself.
- Cross-venue policy constants live beside existing prediction-arbitrage constants: `MAX_CROSS_UNSETTLED_PRINCIPAL = Decimal("100")`; reuse `MAX_NORMAL_COST`, `MAX_EMERGENCY_LOSS`, and `MIN_THRESHOLD_ANNUALIZED_YIELD`.

- [ ] **Step 1: Write failing tests for both directions and exact net units**

For each direction, have the Predict quote return a gross requested amount and a smaller deterministic minimum net output. Assert the Polymarket leg is sized to that exact net output, not the requested Predict quantity or API `amountFilled`.

Add cases for:

- unavailable/nondeterministic Predict quote: stage-4 observation exists, no executable intent;
- USDT and pUSD accounting: no FX collaborator is called and value is 1:1;
- positive nominal profit but 14.9999% annualized: observable but not actionable;
- exactly 15%: actionable;
- largest common depth costs over 20 but a smaller quantity fits: select the largest valid smaller quantity;
- high available balances: no 65 pUSD rejection;
- deterministic fees/gas push total over 20 or profit to zero: reject/size down;
- canonical cutoff missing or in the past: no actionable intent.

- [ ] **Step 2: Run economics tests and verify they fail**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest \
  tests/test_predict_cross_venue.py -k 'intent or annualized or net or cap' -q
```

Expected: current builder aligns gross quantities, uses raw settlement dates, and examines the largest candidate as a terminal choice.

- [ ] **Step 3: Implement the smallest correct sizing loop**

For each protected Predict book quantity, request a deterministic quote. Use its minimum net shares as the Polymarket quantity. `PredictBuyQuote` and the Polymarket calculation each expose an all-in maximum collateral debit, a fee amount/asset, and net units so fees are included exactly once. Calculate the Polymarket protected FOK cost for that quantity and then:

```python
total_max_cost = predict_quote.max_collateral_debit + polymarket_all_in_debit + calculable_gas
minimum_payout = predict_quote.net_units
minimum_profit = minimum_payout - total_max_cost
annualized = simple_annualized_yield_from_values(
    minimum_profit, total_max_cost, now=now, resolution_at=validation.canonical_cutoff
)
```

Continue descending when a larger candidate exceeds 20 or fails exact net-unit sizing; do not `break` until a valid largest candidate is selected. Require `minimum_profit > 0` and `annualized >= 0.15` only for stage 5. Preserve lower-yield positive rows as stage 4 observations.

- [ ] **Step 4: Run economics tests and commit**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest \
  tests/test_prediction_arbitrage.py tests/test_predict_cross_venue.py -q
```

Expected: PASS.

Commit:

```bash
git add src/open_trader/prediction_arbitrage.py \
  src/open_trader/predict_cross_venue.py \
  tests/test_prediction_arbitrage.py tests/test_predict_cross_venue.py
git commit -m "feat: size cross venue intents by net units"
```

---

### Task 5: Reserve unsettled cross principal atomically

**Files:**

- Modify: `src/open_trader/prediction_arbitrage_store.py`
- Test: `tests/test_prediction_arbitrage_store.py`

**Interfaces:**

- Add one purpose-built table `cross_execution_reservations(execution_id, amount, state, created_at, released_at, release_reason)`; no generic ledger.
- `consume_preview_and_create_execution(preview_id, idempotency_key)` detects cross payloads and reserves inside the same `BEGIN IMMEDIATE` transaction.
- Add `cross_unsettled_principal()` and `release_cross_reservation(execution_id, *, reason)`.
- Re-consuming one preview returns its existing execution; it never creates a second execution or reservation.

- [ ] **Step 1: Write atomic reservation and idempotency tests**

Test two store instances against one SQLite file. With 90 already reserved, race two 20-cost confirmations and assert both cannot succeed. Add assertions that:

- the accepted reservation and execution are committed together;
- a failed cap check leaves no execution and no reservation;
- repeated same preview or idempotency key returns one execution;
- `holding_to_resolution`, unknown orders, incidents, and dust stay reserved;
- final `both_rejected` with proven zero positions and observed redemption release capacity;
- ordinary legacy previews/executions remain byte-for-byte compatible and create no cross reservation.

- [ ] **Step 2: Run store tests and verify they fail**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest \
  tests/test_prediction_arbitrage_store.py -k 'cross or preview' -q
```

Expected: the store currently raises `cross_venue_observation_only` and has no durable reservation.

- [ ] **Step 3: Add the table and reserve in the existing transaction**

Inside `consume_preview_and_create_execution`:

1. return the existing execution if `preview_id` is already linked;
2. parse the server-owned preview payload;
3. for `market_type == "cross_venue_yes_no"`, sum rows whose state is `reserved`;
4. reject with `cross_unsettled_cap` if `current + total_max_cost > 100`;
5. insert execution and reservation before commit.

Store decimal amounts as fixed-point text and parse with `Decimal`. `release_cross_reservation` is idempotent and accepts only `no_submit`, `both_rejected`, or `redeemed`; every incident, holding, remediation, and unknown state remains reserved. It records no venue payloads or credentials.

- [ ] **Step 4: Run store tests and commit**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest \
  tests/test_prediction_arbitrage_store.py -q
```

Expected: PASS.

Commit:

```bash
git add src/open_trader/prediction_arbitrage_store.py \
  tests/test_prediction_arbitrage_store.py
git commit -m "feat: reserve cross venue unsettled principal"
```

---

### Task 6: Enable protected cross-venue preview and final confirmation refresh

**Files:**

- Modify: `src/open_trader/prediction_arbitrage_execution.py`
- Modify: `src/open_trader/dashboard_web.py`
- Test: `tests/test_prediction_arbitrage_execution.py`
- Test: `tests/test_dashboard_web.py`

**Interfaces:**

- `PredictionExecutionService.__init__` adds the keyword-only `predict_trading: object | None = None` collaborator without replacing the existing Polymarket client.
- Add `set_cross_venue_monitor(monitor)` because the service is constructed before the cross monitor; `_fresh_opportunity` routes only `cross:` IDs to that monitor and leaves every legacy ID on `PolymarketMonitor`.
- `ExecutionIntent` includes the existing `CrossVenueIntent`.
- Cross preview uses the existing `/api/prediction-arbitrage/preview` and confirmation uses the existing `/api/prediction-arbitrage/executions` route.
- Cross breaker state is scoped to cross entry; legacy breaker behavior remains unchanged.

- [ ] **Step 1: Replace observation-only tests with complete preview tests**

Replace `test_cross_venue_opportunities_cannot_be_previewed_or_confirmed` and the server rejection test. Assert a current stage-5 opportunity returns one preview containing:

```python
assert preview["market_type"] == "cross_venue_yes_no"
assert [leg["exchange"] for leg in preview["buy_legs"]] == ["predict.fun", "polymarket"]
assert preview["net_quantity"] == "5"
assert preview["maximum_total_cost"] == "4.70"
assert preview["minimum_payout"] == "5"
assert preview["minimum_profit"] == "0.30"
assert preview["annualized_yield"] >= "0.15"
assert preview["canonical_cutoff"].endswith("Z")
assert preview["codex_approval"]["decision"] == "APPROVE"
assert preview["balances"]["predict.fun"]["asset"] == "USDT"
assert preview["balances"]["polymarket"]["asset"] == "pUSD"
assert preview["unsettled"]["limit"] == "100"
assert preview["policy_limits"]["max_normal_cost"] == "20"
assert preview["policy_limits"]["max_emergency_loss"] == "2"
```

Add failures for stale books, changed fingerprints, withdrawn Codex approval, missing cutoff/evidence, insufficient balance/allowance, high-but-sufficient Polymarket balance, active cross execution, cross breaker, and incomplete Predict quote.

- [ ] **Step 2: Run preview/server tests and verify they fail**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest \
  tests/test_prediction_arbitrage_execution.py tests/test_dashboard_web.py \
  -k 'cross_venue or prediction_ids' -q
```

Expected: both service and HTTP boundary still reject cross opportunities as observation-only.

- [ ] **Step 3: Parse and validate the existing cross intent**

Import `CrossVenueIntent`/`CrossVenueLeg`; extend `_intent_payload` and `_intent_from_payload` without duplicating the dataclasses. Add a cross branch to `_validate_opportunity` that checks:

- explicit stage-5 actionability and fresh dual REST confirmation;
- current approval IDs/fingerprints and direct polarity;
- exact two venues and complementary outcomes;
- equal positive net units;
- every max price/cost/fee is finite and positive;
- total maximum cost `<= 20`, profit `> 0`, annualized `>= 0.15`;
- canonical cutoff is valid/future.

Do not reuse legacy `minimum_profit >= 1`, `net_edge >= 0.01`, or maximum wallet-balance checks.

Handle `CrossVenueIntent` before shared code reads legacy-only attributes such as `event_id`, `net_edge`, `relation_id`, or `condition_id`. Use `pair_id`, `direction`, `quantity`, and the explicit legs for cross serialization.

- [ ] **Step 4: Branch volatile account checks by intent type**

For cross intent, independently call current Polymarket and Predict account snapshots and require enough balance/allowance for each displayed leg. Return a two-venue account payload. Keep the old `_volatile_checks` path untouched for `PairIntent` and `ThresholdHedgeIntent`.

- [ ] **Step 5: Build the cross preview from server facts**

Add one branch in `_preview_payload`; include the exact approved fields and an immutable `execution_id` generated by the server. The full Codex evidence is persisted/displayed, but no prompt, token usage, secret, JWT, signature, or signed order is included.

Remove the cross-ID rejection in `dashboard_web.py`; continue treating browser-supplied economics as untrusted.

Wire the existing objects in `serve_dashboard` without making Predict failure take down Polymarket:

1. build `PredictTradingClient` only when Predict config/Keychain are available;
2. pass it to `PredictionExecutionService` and its quote callable to `PredictCrossVenueMonitor`;
3. call `prediction_execution.set_cross_venue_monitor(cross_venue_monitor)` after construction;
4. if Predict construction fails, expose an unavailable Predict venue card/cross monitor while the existing Polymarket monitor and LLM tab continue running.

- [ ] **Step 6: Enforce confirmation ceilings and no-submit rejection**

During `_run_execution`, fetch a fresh opportunity and compare both current legs to the preview:

- same pair/direction/condition/token IDs and fingerprints;
- same Codex approval/cache key and canonical cutoff;
- each refreshed price is at or below its displayed ceiling;
- fresh total cost/profit/annualized/depth/balances/allowances/cap still pass.

Any failure transitions to rejected, releases the reservation as `no_submit`, and calls neither venue submitter. A better price may proceed; a worse price always requires a new preview.

- [ ] **Step 7: Run preview/final-validation tests and commit**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest \
  tests/test_prediction_arbitrage_execution.py tests/test_dashboard_web.py -q
```

Expected: PASS, including all legacy execution tests.

Commit:

```bash
git add src/open_trader/prediction_arbitrage_execution.py \
  src/open_trader/dashboard_web.py \
  tests/test_prediction_arbitrage_execution.py tests/test_dashboard_web.py
git commit -m "feat: preview protected cross venue execution"
```

---

### Task 7: Submit concurrently, reconcile independently, and contain incidents

**Files:**

- Modify: `src/open_trader/polymarket_trading.py`
- Modify: `src/open_trader/predict_trading.py`
- Modify: `src/open_trader/prediction_arbitrage_execution.py`
- Test: `tests/test_polymarket_trading.py`
- Test: `tests/test_predict_trading.py`
- Test: `tests/test_prediction_arbitrage_execution.py`

**Interfaces:**

- Add focused Polymarket methods `no_submit_cross_leg_preflight`, `submit_cross_leg_once`, and `reconcile_cross_leg`; implement them by reusing `_sign_leg`, `_threshold_leg_result`, and `_reconcile_threshold_leg`.
- Add `_run_cross_venue_execution(execution_id, intent, opportunity, accounts, preview_payload)` to the existing execution service.
- Use stdlib `ThreadPoolExecutor(max_workers=2)` for the two synchronous venue posts.
- No new redemption transaction loop: observe the already-automatic venue redemption and release capacity only after positions/collateral prove it completed.

- [ ] **Step 1: Write one-leg Polymarket reuse tests**

Assert the new preflight signs one FOK BUY without submitting, and `submit_cross_leg_once` posts exactly one order after that preflight. Assert the returned result retains exchange, condition, outcome, token, order, trade, and filled facts. Transport failure must be ambiguous and must not retry.

- [ ] **Step 2: Write concurrent execution and idempotency tests**

Use two blocking submit doubles with a barrier. Confirm one preview and assert both venue submit calls start before either is released. Assert they share one local execution ID and each venue is called at most once.

Double-confirm with the same preview using both the same and a different UI idempotency key. Assert one durable execution and at most one order per venue leg.

- [ ] **Step 3: Write final REST reconciliation cases**

Add deterministic cases for:

- both legs fill and account positions prove equal net units -> `holding_to_resolution`;
- both legs conclusively reject with zero positions -> `both_rejected` and reservation released;
- one submit times out but reconciliation finds the created order -> continue without retry;
- one submit is conclusively absent -> at most one bounded retry using the same execution identity;
- one submit remains unknown -> cross breaker opens, reservation remains, no retry;
- order says filled but position amount disagrees -> reconciliation incident;
- both fills leave a below-minimum residual whose total loss is `<= 2` -> visible dust evidence;
- the same residual or remediation costs `> 2` -> no automatic mutation and cross breaker.

- [ ] **Step 4: Run execution tests and verify they fail**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest \
  tests/test_polymarket_trading.py tests/test_predict_trading.py \
  tests/test_prediction_arbitrage_execution.py \
  -k 'cross or concurrent or unknown or dust' -q
```

Expected: cross preview exists after Task 6, but no cross submission branch exists.

- [ ] **Step 5: Add the concurrent branch with evidence-first transitions**

In `_run_execution`, dispatch `CrossVenueIntent` to `_run_cross_venue_execution`. The branch must:

1. complete both no-submit preflights;
2. persist `submitting` before POST;
3. start exactly two futures, one per venue;
4. persist each venue result under a stable venue leg label;
5. reconcile both via fresh REST/account reads;
6. compare actual position units, not response `amountFilled`;
7. transition to holding, both-rejected, remediation, or cross incident.

Never have one future submit the other venue. Never retry from an exception handler.

- [ ] **Step 6: Reuse bounded remediation math**

Reuse the existing completion/unwind selection rules and single-leg submitters. Compute the complete worst-case loss from executable price, fees, calculable gas, and any residual dust. Permit one action only when the total is `<= Decimal("2")`; otherwise record the exposure and open the cross breaker.

Keep the normal 20 reservation separate from the 2 remediation budget. Record actual dust as `unhedged_units` and `worst_case_loss`; never label it hedged.

- [ ] **Step 7: Reconcile automatic redemption without adding a transaction loop**

Add an idempotent `reconcile_cross_holdings_once()` on the execution service and pass it as a `holding_reconciler` callback to `PredictCrossVenueMonitor`; invoke it from the existing 15-minute `_discover` cycle, not a new scheduler. For each `holding_to_resolution` cross execution:

- read both current venue positions and collateral balances;
- if positions are still present or redemption is pending, leave state/reservation unchanged and expose `待兑付`;
- if both positions are gone and the corresponding redeemed collateral increase is independently observed, transition to `complete` and release the reservation once;
- on stale/unknown/failure, retain capacity and alert once; never send a redemption transaction or loop blindly.

- [ ] **Step 8: Run backend tests and commit**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest \
  tests/test_polymarket_trading.py tests/test_predict_trading.py \
  tests/test_prediction_arbitrage_store.py tests/test_prediction_arbitrage_execution.py -q
```

Expected: PASS.

Commit:

```bash
git add src/open_trader/polymarket_trading.py \
  src/open_trader/predict_trading.py \
  src/open_trader/prediction_arbitrage_execution.py \
  tests/test_polymarket_trading.py tests/test_predict_trading.py \
  tests/test_prediction_arbitrage_execution.py
git commit -m "feat: execute and reconcile cross venue legs"
```

---

### Task 8: Notify only the first transition into stage 5

**Files:**

- Modify: `src/open_trader/predict_cross_venue.py`
- Modify: `src/open_trader/prediction_arbitrage_execution.py`
- Modify: `src/open_trader/notifications.py`
- Test: `tests/test_predict_cross_venue.py`
- Test: `tests/test_prediction_arbitrage_execution.py`
- Test: `tests/test_notifications.py`

**Interfaces:**

- Cross notification dedupe key is `pair_id + direction + predict_fingerprint + polymarket_fingerprint`.
- Reuse `reserve_notification_attempt`/`complete_notification_attempt` and the existing asynchronous observer path.
- `render_yes_no_signal_notification(signal)` receives a cross stage-5 signal and returns a Dashboard deep link; opening it never submits.

- [ ] **Step 1: Replace observation-only notification assertions**

Replace `test_cross_venue_yes_no_signal_notification_is_link_free_and_observation_only`. Assert stages 1–4 schedule no call. On first stage-5 transition assert exactly one call and a message containing both exchange/outcome legs, maximum cost, minimum profit, theoretical annualized yield, canonical cutoff, and `/?prediction_signal=<signal_id>`.

Repeat stage 5 without fingerprint change and assert no second notification. Change a fingerprint, require fresh Codex approval, and assert one new notification only after the new pair reaches stage 5.

- [ ] **Step 2: Run notification tests and verify they fail**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest \
  tests/test_notifications.py tests/test_prediction_arbitrage_execution.py \
  tests/test_predict_cross_venue.py -k 'notification or stage_5' -q
```

Expected: current cross message is observation-only and link-free.

- [ ] **Step 3: Reuse the existing notification lease**

Schedule only when `actionable` changes false -> true. Persist the dedupe identity with the signal. Before delivery, use the same no-submit preparation path as preview to ensure the signal is still stage 5; do not call Codex and do not create a preview or reservation.

Keep notification failure isolated from signal persistence and trading actionability.

- [ ] **Step 4: Run notification tests and commit**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest \
  tests/test_notifications.py tests/test_prediction_arbitrage_execution.py \
  tests/test_predict_cross_venue.py -q
```

Expected: PASS.

Commit:

```bash
git add src/open_trader/predict_cross_venue.py \
  src/open_trader/prediction_arbitrage_execution.py \
  src/open_trader/notifications.py \
  tests/test_predict_cross_venue.py tests/test_prediction_arbitrage_execution.py \
  tests/test_notifications.py
git commit -m "feat: notify actionable cross venue signals"
```

---

### Task 9: Merge the approved cross-venue UI into the existing YES/NO page

**Files:**

- Modify: `src/open_trader/dashboard_web.py`
- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Modify: `src/open_trader/dashboard_static/dashboard.css`
- Modify: `tests/test_dashboard_web.py`
- Modify: `tests/e2e/serve_dashboard_fixture.py`
- Modify: `tests/e2e/prediction-market.spec.ts`

**Interfaces:**

- Reuse the current prediction page, strategy tabs, venue cards, funnel, history, modal, and mutation endpoints.
- Shared venue header is rendered above both YES/NO and LLM tabs.
- YES/NO funnel remains exactly five stages and the four duplicate metric cards are removed.
- Cross modal is a branch of `predictionModalHtml`, not a new modal system.

- [ ] **Step 1: Extend the deterministic fixture with actionable and blocked cross rows**

Add one stage-5 fixture whose preview route returns explicit Predict/Polymarket legs and one stage-4 below-15% fixture that remains visible without a button. Add execution fixture states for submitting, reconciling, holding, dust incident, breaker, and `待兑付`. Keep all submit responses deterministic and local.

- [ ] **Step 2: Write desktop and mobile Playwright assertions**

At 1440px and 375px assert:

- both strategy tabs share the same Polymarket/Predict health header with REST, WebSocket, mode, masked wallet, balance/asset, and last success;
- no duplicate four-card metrics block appears;
- funnel labels are exactly `两所对应标的`, `正在监视`, `Codex 认为可以`, `有套利空间`, `可下单明确信号`;
- stage-2 copy says low-frequency/Codex queue, while only stage 3 is real-time admitted;
- each candidate leg names its exchange and outcome;
- below-threshold observations remain visible without an execution action;
- stage-5 action opens the existing modal and never submits before confirmation;
- the modal shows two ceilings, net units, currencies, total cost, payout, profit, annualized yield, cutoff, Codex summary/evidence, balances, unsettled capacity, 20/2 risk limits, automatic redemption copy, and non-atomic warning;
- desktop two-column and mobile single-column/fixed footer have no horizontal overflow;
- action targets are at least 44px;
- Escape closes and restores focus;
- one confirmation request is sent even on rapid double click;
- history shows explicit exchange legs, holding state, dust, breaker, and `待兑付`.

- [ ] **Step 3: Run Playwright and verify the new assertions fail**

Run:

```bash
OPEN_TRADER_PYTHON="$PWD/.venv/bin/python" npm exec playwright test \
  tests/e2e/prediction-market.spec.ts --project=chromium
```

Expected: the current UI still presents cross rows as read-only and lacks the approved confirmation branch.

- [ ] **Step 4: Project truthful two-venue API fields**

In `dashboard_web.py`, return current source/trading health and balances without inventing readiness. Preserve `unknown`, `pending`, `stale`, and `blocked` states. Include current/post-reservation unsettled values and cross breaker scope in the existing prediction state payload.

- [ ] **Step 5: Reuse the current renderers and modal**

Update the existing functions around `predictionCrossVenueFunnel`, candidate rendering, `predictionPreviewIsComplete`, and `predictionModalHtml`. Add a `market_type === "cross_venue_yes_no"` branch before the threshold branch. Keep the existing warm CSS tokens and 720px modal; add only the responsive rules needed for the approved mobile stack/footer.

Do not add another page, route, framework, icon library, chart package, or client-side economics.

- [ ] **Step 6: Run UI/API tests and commit**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest \
  tests/test_dashboard_web.py -q
OPEN_TRADER_PYTHON="$PWD/.venv/bin/python" npm exec playwright test \
  tests/e2e/prediction-market.spec.ts --project=chromium
```

Expected: PASS at both viewport sizes.

Commit:

```bash
git add src/open_trader/dashboard_web.py \
  src/open_trader/dashboard_static/dashboard.js \
  src/open_trader/dashboard_static/dashboard.css \
  tests/test_dashboard_web.py tests/e2e/serve_dashboard_fixture.py \
  tests/e2e/prediction-market.spec.ts
git commit -m "feat: add cross venue execution UI"
```

---

### Task 10: Prove no-submit readiness, pass acceptance, deploy the exact SHA, and stop before the live canary

**Files:**

- Modify: `src/open_trader/prediction_arbitrage_acceptance.py`
- Modify: `tests/test_prediction_arbitrage_acceptance.py`
- Modify: `CHANGELOG.md`
- Review only: `Makefile`
- Review only: `scripts/install_dashboard_launchd.sh`

**Interfaces:**

- Acceptance performs real read-only Predict and Polymarket source/account checks plus signed-not-submitted preflight when credentials are available.
- Acceptance never calls `/v1/orders`, any Polymarket order post, any redemption mutation, or a real notifier.
- Final review URL remains `http://127.0.0.1:8766/`.

- [ ] **Step 1: Add deterministic acceptance tests for Predict readiness**

Extend the acceptance runner so its JSON/report distinguishes:

- Predict REST/WS market and book readiness;
- Predict account/JWT/balance/allowance readiness;
- Predict order signed but not submitted;
- Polymarket source/account/preflight readiness;
- zero mutation calls and zero live notifications.

Missing external/browser/Keychain environment returns `BLOCKED`, never fixture PASS. Auth/read failures return FAIL with a redacted reason.

- [ ] **Step 2: Run acceptance-module tests**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest \
  tests/test_prediction_arbitrage_acceptance.py -q
```

Expected: deterministic tests PASS and prove zero submissions/deliveries.

- [ ] **Step 3: Run all focused regressions before the final gate**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest \
  tests/test_predict_source.py \
  tests/test_predict_cross_venue.py \
  tests/test_predict_trading.py \
  tests/test_polymarket_trading.py \
  tests/test_prediction_arbitrage_store.py \
  tests/test_prediction_arbitrage_execution.py \
  tests/test_notifications.py \
  tests/test_dashboard_web.py \
  tests/test_prediction_arbitrage_acceptance.py -q
OPEN_TRADER_PYTHON="$PWD/.venv/bin/python" npm exec playwright test \
  tests/e2e/prediction-market.spec.ts --project=chromium
```

Expected: PASS. Confirm the deterministic notifier has no real delivery assertion and mutation doubles report no unexpected call.

- [ ] **Step 4: Update the operator changelog before any merge**

Add a dated `2026-08-03` entry describing cross-venue Codex admission, protected manual execution, risk caps, Predict adapter, UI funnel/header/modal, and no-submit acceptance. Do not include wallet secrets, order payloads, or credential details.

Commit:

```bash
git add src/open_trader/prediction_arbitrage_acceptance.py \
  tests/test_prediction_arbitrage_acceptance.py CHANGELOG.md
git commit -m "test: accept protected cross venue execution"
```

- [ ] **Step 5: Reconcile with current local main and deploy the candidate SHA for the gate**

Inspect both worktrees and preserve unrelated dirty-root files. If local `main` advanced since this branch was created, integrate it into the feature branch, resolve only in-scope conflicts, and rerun Step 3 focused checks before continuing. Do not reset, clean, or discard another session's work.

With a clean feature worktree, record the candidate SHA and deploy that exact worktree so acceptance can verify real PID/cwd/SHA state:

```bash
git status --short
git log --oneline --decorate -12
CANDIDATE_SHA="$(git rev-parse HEAD)"
./scripts/install_dashboard_launchd.sh \
  --mode stack \
  --repo-root "$PWD" \
  --runtime-root /Users/ray/projects/open_trader \
  --python "$PWD/.venv/bin/python"
PYTHONPATH=src .venv/bin/python -m open_trader.prediction_arbitrage_acceptance \
  --url http://127.0.0.1:8766 \
  --expected-root "$PWD" \
  --config "$PWD/config/prediction_arbitrage.json"
```

Expected: clean source, candidate SHA recorded, the live stack is serving from this worktree, and the direct workflow PASSes real read-only/signed-not-submitted checks with zero submissions. An unavailable required external environment is a blocker; do not continue to the final gate.

- [ ] **Step 6: Run the only final Dashboard gate**

Run once, after all source commits and the candidate deployment are complete:

```bash
make acceptance
```

Expected: the terminal result is exactly `PASS`. On FAIL, fix, redeploy the new candidate SHA, and rerun the complete gate. On BLOCKED, report the blocker and do not present, merge, or deploy the task as complete.

- [ ] **Step 7: Fast-forward main and origin to the exact accepted SHA**

After PASS, verify `git rev-parse HEAD` still equals the candidate SHA recorded in Step 5; this is `ACCEPTED_SHA`. Ensure local `main` has not advanced since Step 5. Then fast-forward only:

```bash
ACCEPTED_SHA="$(git rev-parse HEAD)"
git -C /Users/ray/projects/open_trader status --short
git -C /Users/ray/projects/open_trader merge --ff-only fix/keychain-secret-write
git -C /Users/ray/projects/open_trader rev-parse HEAD
git -C /Users/ray/projects/open_trader push origin main
git ls-remote origin refs/heads/main
```

Expected: local `main` and `origin/main` equal `ACCEPTED_SHA`; no merge commit changed the accepted SHA, and unrelated dirty-root files remain untouched. If fast-forward is impossible because main advanced, stop, integrate the new main into the feature branch, rerun focused checks, redeploy, and rerun `make acceptance` on the new final SHA.

- [ ] **Step 8: Redeploy the exact accepted SHA after acceptance**

From the accepted feature worktree, run the existing installer again with explicit roots:

```bash
./scripts/install_dashboard_launchd.sh \
  --mode stack \
  --repo-root "$PWD" \
  --runtime-root /Users/ray/projects/open_trader \
  --python "$PWD/.venv/bin/python"
```

This required post-acceptance restart changes no source or data and therefore does not require another acceptance run.

- [ ] **Step 9: Prove the fresh live process**

Verify:

```bash
launchctl print "gui/$UID/com.open-trader.frontend-gateway"
launchctl print "gui/$UID/com.open-trader.legacy-dashboard"
curl --fail --silent http://127.0.0.1:8766/healthz
curl --fail --silent --output /dev/null --write-out '%{http_code}\n' http://127.0.0.1:8766/
tail -n 100 logs/frontend_gateway/launchd.out.log
tail -n 100 logs/legacy_dashboard/launchd.out.log
```

Expected: new PIDs, cwd at the exact accepted worktree, runtime SHA equal to the accepted SHA, fresh startup timestamps/logs with no traceback, gateway upstream `ok`, and HTTP 200.

- [ ] **Step 10: Capture the user-requested visual proof after PASS**

Capture 1440px YES/NO page, 375px YES/NO page, desktop cross confirmation modal, and mobile cross confirmation modal from the deployed review URL. Screenshots prove presentation only and are supplied alongside—not instead of—the PASS/runtime evidence.

- [ ] **Step 11: Stop before any real cross-venue order**

Report the deployed URL, accepted SHA, PIDs, no-submit readiness result, and screenshots. Do not execute a live cross-venue order in this implementation run.

For the later explicitly approved canary, select a currently actionable pair using the normal system, then show the exact pair/direction/net units/ceilings/fees/maximum cost. Require smallest common executable quantity, combined maximum cost `<= 5`, all permanent gates, and a fresh user confirmation. If none qualifies, wait; never relax the 15%/5/2/20/100 limits.

---

## Acceptance Coverage Map

| Spec criteria | Plan evidence |
| --- | --- |
| CV-01–CV-02 | Task 1 category/candidate tests |
| CV-03–CV-06 | Task 2 schema, canonical cutoff, cache, and hot-path call-count tests |
| CV-07–CV-13 | Tasks 3–5 Predict quote, net units, annualization, sizing, wallet, and atomic reservation tests |
| CV-14–CV-18 | Tasks 6–7 preview completeness, ceiling refresh, concurrency, idempotency, and ambiguous-submit tests |
| CV-19–CV-24 | Task 7 independent position reconciliation, remediation/dust, breaker, holding, and automatic-redemption observation tests |
| CV-25–CV-27 | Task 9 API/Playwright desktop/mobile header, funnel, candidate, modal, focus, and overflow tests |
| CV-28 | Task 8 stage-transition/dedupe/deterministic-notifier tests |
| CV-29 | Tasks 6–10 full legacy pytest and Playwright regressions |
| Final Dashboard gate | Task 10 `make acceptance`, exact-SHA redeploy, runtime proof, and requested screenshots |
| First live canary | Separate explicit post-acceptance action; Task 10 stops before mutation |

## Plan Self-Review Checklist

- [x] Every CV-01 through CV-29 criterion maps to a task and runnable check.
- [x] No task adds same-venue product focus, global semantic scan, inverse matching, automatic entry, exchange framework, queue, scheduler, FX feed, or redemption transaction loop.
- [x] Money fields use `Decimal`; time fields are timezone-aware UTC; public token IDs remain persistable while credentials/signatures remain redacted.
- [x] Cross-specific rules do not alter legacy 65 pUSD, minimum-profit, edge, LLM hedge, merge, or notification behavior.
- [x] Every mutation-capable test proves call count and ambiguous-state behavior.
- [x] `make acceptance` is the final gate, and the exact accepted SHA is redeployed before review.
- [x] No placeholder markers remain in implementation instructions.
