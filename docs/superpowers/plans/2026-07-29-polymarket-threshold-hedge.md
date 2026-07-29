# Polymarket Threshold Hedge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Discover same-event Polymarket threshold hedges, validate each positive-profit semantic pair once through Codex, show every positive opportunity, and let the operator manually confirm two cross-condition FOK orders.

**Architecture:** Add one deep relation-discovery module that owns threshold parsing, template certificates, Codex validation, and opportunity projection. Reuse the existing public SDK, monitor, SQLite store, trading signer, execution lock, incident handling, Dashboard endpoints, and notification path. Cross-condition execution gets an explicit intent and never enters the same-condition merge path.

**Tech Stack:** Python 3.12, existing `polymarket` SDK, stdlib `subprocess`/`hashlib`/`json`/`sqlite3`, existing vanilla Dashboard JavaScript/CSS, pytest, Playwright acceptance.

## Global Constraints

- Start from local `main` in the isolated `feat/polymarket-threshold-hedge` worktree.
- Do not add a package, database file, daemon, provider SDK, or automatic order-confirmation path.
- Only same-event binary contracts with complete rules and a deterministic `>`, `>=`, `<`, or `<=` threshold template are eligible.
- `groupItemThreshold` is auxiliary ordering metadata and never equals or replaces the economic threshold.
- Use local `codex exec`; do not use DeepSeek for threshold validation.
- Invoke Codex only for a positive-profit cache miss; cache APPROVE and REJECT by one SHA-256 fingerprint.
- Maximum cost is `$20` per combination; minimum net profit is strictly greater than `$0`; `$1`, `1%`, and `20%` annualized are display labels only.
- Require current-book unwind or completion remediation within `$2` before preview and confirmation.
- Allow multiple `holding_to_resolution` executions while keeping one in-flight execution lock.
- The operator must click preview and final confirmation; both orders remain FOK and non-atomic.
- Scan logs stay in a `deque(maxlen=20)` and are not persisted.
- `make acceptance` runs once as the final Dashboard gate, followed by redeploying the exact accepted SHA.

---

### Task 1: Pure Threshold Relation and Economics Domain

**Files:**
- Create: `src/open_trader/polymarket_relation_discovery.py`
- Modify: `src/open_trader/prediction_arbitrage.py`
- Test: `tests/test_polymarket_relation_discovery.py`
- Test: `tests/test_prediction_arbitrage.py`

**Interfaces:**
- Consumes: normalized Gamma event/market objects through the existing `_value`-style field access pattern.
- Produces: `ThresholdRelation`, `ThresholdHedgeLeg`, `ThresholdHedgeIntent`, `discover_threshold_relations(events)`, and `build_threshold_hedge_intent`.

- [ ] **Step 1: Write failing parser and certificate tests**

```python
def test_exact_above_template_builds_one_relation() -> None:
    relations = discover_threshold_relations([
        event(
            market(question="BTC above $90,000?", rules=RULES.replace("$X", "$90,000")),
            market(question="BTC above $100,000?", rules=RULES.replace("$X", "$100,000")),
        )
    ])
    assert [(row.lower_threshold, row.higher_threshold) for row in relations] == [
        (Decimal("90000"), Decimal("100000"))
    ]
    assert relations[0].relation == "B_IMPLIES_A"


def test_group_item_threshold_never_replaces_question_threshold() -> None:
    relation = discover_threshold_relations([
        event(
            market(question="BTC above $90,000?", group_item_threshold="0"),
            market(question="BTC above $100,000?", group_item_threshold="1"),
        )
    ])[0]
    assert relation.lower_threshold == Decimal("90000")
    assert relation.higher_threshold == Decimal("100000")
```

Also assert rejection for different event IDs, rule text differing outside one threshold, different source/end time/timezone, missing condition/token IDs, equal thresholds, neg-risk, closed/ended, non-binary outcomes, and `hit`/`reach` wording.

- [ ] **Step 2: Run relation tests and verify RED**

Run:

```bash
/Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_polymarket_relation_discovery.py -q
```

Expected: collection/import failure because `polymarket_relation_discovery` does not exist.

- [ ] **Step 3: Implement the minimum deterministic certificate**

Use frozen dataclasses and Decimal-only parsing. Normalize a question/rule by replacing exactly one parsed numeric threshold with `<threshold>`; require all remaining normalized text and semantic metadata to match byte-for-byte after whitespace normalization. Keep candidate ordering stable by `(event_id, lower_threshold, higher_threshold, condition_id)`.

`ThresholdRelation` is a frozen dataclass with `relation_id`, `event_id`,
`market_a`, `market_b`, `relation`, `rules_hash_a`, and `rules_hash_b`.
`discover_threshold_relations(events: Sequence[object])` returns a tuple of
these rows in stable order.

For an `above` ladder, `market_a` is the lower threshold, `market_b` the higher threshold, relation is `B_IMPLIES_A`, and the independent buy legs are `YES(A) + NO(B)`. For a `below` ladder, relation is `A_IMPLIES_B`, and buy legs are `NO(A) + YES(B)`.

- [ ] **Step 4: Write failing intent economics tests**

Cover:

- equal requested shares on both cross-condition legs;
- current taker fee formula `q * rate * p * (1 - p)` per leg;
- `total_max_cost <= 20`;
- `minimum_profit > 0`;
- a `$0.001` positive profit remains visible/eligible;
- zero or negative profit returns no intent;
- different tick sizes are validated per leg;
- missing asks/bids or unwind loss above `$2` returns no executable intent;
- annualized yield uses `minimum_profit / total_max_cost`.

- [ ] **Step 5: Run economics tests and verify RED**

Run:

```bash
/Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_prediction_arbitrage.py -k threshold_hedge -q
```

Expected: failure because `ThresholdHedgeIntent` and its builder do not exist.

- [ ] **Step 6: Implement the smallest cross-condition intent**

```python
@dataclass(frozen=True, slots=True)
class ThresholdHedgeLeg:
    label: Literal["A", "B"]
    condition_id: str
    market_id: str
    outcome: Literal["YES", "NO"]
    token_id: str
    quantity: Decimal
    max_price: Decimal
    max_cost: Decimal
    tick_size: Decimal


@dataclass(frozen=True, slots=True)
class ThresholdHedgeIntent:
    relation_id: str
    event_id: str
    relation: Literal["A_IMPLIES_B", "B_IMPLIES_A"]
    leg_a: ThresholdHedgeLeg
    leg_b: ThresholdHedgeLeg
    quantity: Decimal
    maximum_fee: Decimal
    total_max_cost: Decimal
    minimum_payout: Decimal
    minimum_profit: Decimal
    net_edge: Decimal
```

Reuse the existing protected-buy cent/share math. Add no generic strategy interface.

- [ ] **Step 7: Run focused tests and commit**

```bash
/Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_polymarket_relation_discovery.py \
  tests/test_prediction_arbitrage.py -q
git add src/open_trader/polymarket_relation_discovery.py \
  src/open_trader/prediction_arbitrage.py \
  tests/test_polymarket_relation_discovery.py \
  tests/test_prediction_arbitrage.py
git commit -m "feat: discover deterministic threshold hedges"
```

---

### Task 2: Persistent Codex Cache, Usage, and Opportunity Episodes

**Files:**
- Modify: `src/open_trader/prediction_arbitrage_store.py`
- Modify: `tests/test_prediction_arbitrage_store.py`

**Interfaces:**
- Consumes: one cache fingerprint, structured result payload, Codex usage payload, and threshold opportunity episode payload.
- Produces: `load_llm_cache`, `save_llm_cache`, `record_llm_call`, `record_llm_cache_hit`, and `llm_usage_24h`.

- [ ] **Step 1: Write failing persistence tests**

```python
def test_llm_cache_survives_restart_and_unavailable_is_not_saved(tmp_path: Path) -> None:
    db = store(tmp_path)
    db.save_llm_cache("abc", {"decision": "APPROVE"})
    assert PredictionArbitrageStore(db.data_dir).load_llm_cache("abc") == {
        "decision": "APPROVE"
    }


def test_llm_usage_24h_counts_calls_failures_hits_and_tokens(tmp_path: Path) -> None:
    db = store(tmp_path)
    db.record_llm_call(status="success", usage={
        "input_tokens": 100, "cached_input_tokens": 60,
        "output_tokens": 20, "reasoning_output_tokens": 5,
    })
    db.record_llm_call(status="failed", usage={})
    db.record_llm_cache_hit()
    assert db.llm_usage_24h() == {
        "calls": 2, "successes": 1, "failures": 1, "cache_hits": 1,
        "input_tokens": 100, "cached_input_tokens": 60,
        "output_tokens": 20, "reasoning_output_tokens": 5,
    }
```

Add a boundary test proving records exactly 24 hours old count and older records do not. Extend the schema-name test with the new tables.

- [ ] **Step 2: Run store tests and verify RED**

Run:

```bash
/Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_prediction_arbitrage_store.py -q
```

Expected: attribute errors for the new store methods.

- [ ] **Step 3: Add two minimal tables to the existing SQLite file**

```sql
CREATE TABLE IF NOT EXISTS llm_cache (
    cache_key TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_usage (
    usage_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);
```

Use existing `_dump_payload`, `_load_payload`, `_utc_now`, and short-lived transaction helpers. Store cache hits as `kind='cache_hit'`; actual process starts as `kind='call'`. Do not add a cleanup job or TTL.

- [ ] **Step 4: Add threshold opportunity episode identity**

Reuse `signals` rather than create another episode table. Use the stable `relation_id` as `market_id` and include `market_type='threshold_hedge'`, `annualized_yield`, `minimum_profit`, `total_max_cost`, and `resolution_at` in the payload. Existing 24h/7d/all history supplies distribution inputs; add `30d` to `SignalHistoryWindow`.

- [ ] **Step 5: Run store tests and commit**

```bash
/Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_prediction_arbitrage_store.py -q
git add src/open_trader/prediction_arbitrage_store.py \
  tests/test_prediction_arbitrage_store.py
git commit -m "feat: persist codex validation usage"
```

---

### Task 3: Codex Structured Validator

**Files:**
- Modify: `src/open_trader/polymarket_relation_discovery.py`
- Modify: `tests/test_polymarket_relation_discovery.py`
- Create: `src/open_trader/schemas/polymarket_threshold_relation.json`

**Interfaces:**
- Consumes: `ThresholdRelation`, store cache methods, and a subprocess runner compatible with `subprocess.run`.
- Produces: `CodexRelationValidator.validate(relation) -> RelationValidation`.

- [ ] **Step 1: Write failing fingerprint and subprocess tests**

Assert:

- fingerprint equals SHA-256 of `model + prompt_version + canonical_json(payload)`;
- identical payload hits persistent cache after a new validator instance;
- price changes do not change the fingerprint;
- rules, condition ID, prompt version, or model changes do;
- `codex exec` command includes `--ephemeral`, read-only sandbox, ignored config/rules, and output schema;
- APPROVE and REJECT are cached;
- timeout, nonzero exit, missing final message, invalid JSON, unknown fields, wrong IDs, evidence not found, or nonempty uncertainties produce `llm_unavailable`/`deterministic_rejected` and are not cached;
- each process start records one call and each cache read records one hit.

- [ ] **Step 2: Run validator tests and verify RED**

```bash
/Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_polymarket_relation_discovery.py -k codex -q
```

Expected: failures because `CodexRelationValidator` does not exist.

- [ ] **Step 3: Implement one subprocess adapter**

`CodexRelationValidator` accepts `store`, `model`, `runner=subprocess.run`, and
`timeout_seconds=45.0`; it exposes
`validate(relation: ThresholdRelation) -> RelationValidation`. The injected
runner has the same keyword arguments and `CompletedProcess[str]` result as
`subprocess.run`.

Run in a fresh `TemporaryDirectory`, pass the JSON payload through stdin, parse JSONL stdout, accept only the final `agent_message`, and capture `turn.completed.usage`. The static JSON Schema mirrors the approved Prompt contract. Do not expose prompt payloads, raw exceptions, credentials, or Codex home paths in public results.

- [ ] **Step 4: Implement deterministic post-validation**

Validate exact keys/enums/Decimal thresholds, original condition IDs, verbatim evidence from both rule texts, empty uncertainties, semantic field equality, and the independently computed implication direction. The model never supplies token IDs or order legs.

- [ ] **Step 5: Run tests and commit**

```bash
/Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_polymarket_relation_discovery.py -q
git add src/open_trader/polymarket_relation_discovery.py \
  src/open_trader/schemas/polymarket_threshold_relation.json \
  tests/test_polymarket_relation_discovery.py
git commit -m "feat: validate threshold relations with codex"
```

---

### Task 4: Full-Universe Monitor Integration

**Files:**
- Modify: `src/open_trader/polymarket_monitor.py`
- Modify: `tests/test_polymarket_monitor.py`

**Interfaces:**
- Consumes: existing `AsyncPublicClient.list_events().iter_items()`, relation discovery, batch `get_order_books`, existing stream subscription, store, and validator.
- Produces: threshold rows in `snapshot()["opportunities"]`, relation health, scan logs, usage metrics, and annualized distributions.

- [ ] **Step 1: Write failing universe and gating tests**

Cover:

- the existing Top 20 same-market monitor remains unchanged;
- threshold discovery consumes every keyset page using `iter_items`;
- filters use `closed=False, ended=False` and do not set `live=True`;
- startup and five-minute refresh scan all events;
- `new_market` refreshes only its event;
- all deterministic relation token IDs join stream subscription chunks;
- positive economics triggers one Codex cache lookup/call;
- nonpositive economics never calls Codex;
- APPROVE becomes actionable only when fresh, ready, funded, and remediation-safe;
- REJECT and unavailable remain visible with reason but disabled;
- `$0.001` profit remains visible;
- book receipt time, not last price-change timestamp, determines freshness;
- logs cap at 20 and disappear on a new monitor instance.

- [ ] **Step 2: Run monitor tests and verify RED**

```bash
/Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_polymarket_monitor.py -k 'threshold or relation or codex' -q
```

Expected: missing constructor collaborators and missing threshold snapshot rows.

- [ ] **Step 3: Add optional discovery/validator collaborators**

Keep existing constructor callers working:

Add optional `relation_discovery: PolymarketRelationDiscovery | None = None`
and `relation_validator: CodexRelationValidator | None = None` keyword
parameters to the existing constructor.

Production wiring supplies both; tests and unavailable configurations may omit them. Use the existing monitor thread and stream—no second daemon.

- [ ] **Step 4: Project threshold state into existing snapshot**

Add `market_type='threshold_hedge'`, both questions/conditions/tokens, relation proof, LLM status/summary/reasons, two BUY legs, fee/cost/profit, simple annualized yield, volume, received timestamp, remediation proof, and `actionable`. Persist positive episodes through `upsert_signal(relation_id)` and close them when profit is nonpositive, books go stale, or markets close.

- [ ] **Step 5: Add health/log/usage/distribution fields**

```python
snapshot["relation_discovery"] = {
    "status": "healthy|degraded|stale|unavailable",
    "scan_logs": copy.deepcopy(list(self._relation_scan_logs)),
    "codex_usage_24h": store.llm_usage_24h(),
    "annualized_distribution": {
        "current": distribution(current_rows),
        "7d": distribution(store.signal_history("7d")),
        "30d": distribution(store.signal_history("30d")),
    },
}
```

The distribution contains count, min, median, p75, p90, and max using stdlib sorting only.

- [ ] **Step 6: Run monitor regression tests and commit**

```bash
/Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_polymarket_monitor.py \
  tests/test_prediction_arbitrage.py -q
git add src/open_trader/polymarket_monitor.py \
  tests/test_polymarket_monitor.py
git commit -m "feat: monitor cross-market threshold hedges"
```

---

### Task 5: Cross-Condition Trading and Execution

**Files:**
- Modify: `src/open_trader/polymarket_trading.py`
- Modify: `src/open_trader/prediction_arbitrage_execution.py`
- Modify: `src/open_trader/prediction_arbitrage_store.py`
- Modify: `tests/test_polymarket_trading.py`
- Modify: `tests/test_prediction_arbitrage_execution.py`
- Modify: `tests/test_prediction_arbitrage_store.py`

**Interfaces:**
- Consumes: `ThresholdHedgeIntent` and the existing authenticated signer/account/geoblock/preflight collaborators.
- Produces: `no_submit_threshold_preflight`, `submit_threshold_hedge_once`, `reconcile_threshold_hedge`, threshold preview, final confirmation, remediation, and `holding_to_resolution`.

- [ ] **Step 1: Write failing signer/preflight tests**

Assert the trading client:

- signs exactly the two intent token IDs with BUY/FOK;
- validates each leg's independent condition ID and tick size;
- proves equal requested shares;
- includes fee-inclusive total cost in balance/allowance checks;
- does not require or call merge capability;
- refuses without prior no-submit preflight;
- posts the two signed orders exactly once;
- treats ambiguous POST results as ambiguous and never retries.

- [ ] **Step 2: Run trading tests and verify RED**

```bash
/Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_polymarket_trading.py -k threshold -q
```

Expected: missing threshold preflight/submission methods.

- [ ] **Step 3: Add explicit threshold methods without changing same-market methods**

Reuse `_sign_leg`, `_signed_quantity`, `account_snapshot`, and `post_orders`. Do not coerce threshold legs into `PairIntent`, fabricate a shared condition ID, or call `merge_once`.

- [ ] **Step 4: Write failing execution tests**

Cover:

- preview re-reads both rule hashes, books, fees, remediation paths, account, geoblock, and relayer;
- preview and confirm reject changed rule hash/cache fingerprint;
- final confirm rebuilds intent from server state;
- equal fills transition directly to `holding_to_resolution`;
- two existing holdings do not block a new preview;
- one in-flight execution still blocks another;
- startup recognizes known holdings without merging them;
- each condition reconciles against its own orders/trades/token;
- both rejected closes;
- one filled leg uses only a verified `<= $2` remediation option, opens breaker, and notifies;
- unknown state never retries and opens breaker.

- [ ] **Step 5: Run execution tests and verify RED**

```bash
/Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_prediction_arbitrage_execution.py -k threshold -q
```

Expected: threshold opportunities cannot be parsed and same-market merge behavior fails the expected holding assertion.

- [ ] **Step 6: Add a narrow threshold branch**

Parse intent by `intent_type`. Share preview TTL, locks, idempotency, volatile checks, evidence persistence, remediation selection, incidents, and notifications. Branch only at trading preflight/submission/reconciliation and successful terminal handling:

```text
same_market_pair → existing merge path
threshold_hedge  → holding_to_resolution, merge never invoked
```

Add `holding_to_resolution` to terminal/non-in-flight store states while retaining it in execution history.

- [ ] **Step 7: Run execution/trading/store regressions and commit**

```bash
/Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_polymarket_trading.py \
  tests/test_prediction_arbitrage_execution.py \
  tests/test_prediction_arbitrage_store.py -q
git add src/open_trader/polymarket_trading.py \
  src/open_trader/prediction_arbitrage_execution.py \
  src/open_trader/prediction_arbitrage_store.py \
  tests/test_polymarket_trading.py \
  tests/test_prediction_arbitrage_execution.py \
  tests/test_prediction_arbitrage_store.py
git commit -m "feat: execute cross-condition threshold hedges"
```

---

### Task 6: Dashboard Truthful Display and Manual Confirmation

**Files:**
- Modify: `src/open_trader/dashboard_web.py`
- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Modify: `src/open_trader/dashboard_static/dashboard.css`
- Modify: `tests/test_dashboard_web.py`
- Modify: `tests/e2e/prediction-market.spec.ts`

**Interfaces:**
- Consumes: monitor/store threshold snapshot and the unchanged preview/confirm HTTP routes.
- Produces: threshold opportunity cards, structured LLM decisions/reasons, annualized distribution, usage summary, folded logs, and a cross-condition confirmation modal.

- [ ] **Step 1: Write failing API projection tests**

Assert threshold fields survive `_prediction_state_payload` redaction/aliasing, full wallet/token secrets remain masked, Codex raw prompt/stdout never appears, and unavailable state disables ordering while preserving reasons and metrics.

- [ ] **Step 2: Run API tests and verify RED**

```bash
/Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_dashboard_web.py -k 'prediction_arbitrage and threshold' -q
```

Expected: missing threshold aliases/fields.

- [ ] **Step 3: Add threshold card and modal rendering**

Show:

- both market questions and shortened condition IDs;
- relation and independent BUY outcomes;
- LLM APPROVE/REJECT/unavailable plus Chinese summary/reasons/evidence;
- deterministic certificate status;
- quantity, leg max prices, fee-inclusive cost, minimum payout/profit;
- simple annualized yield and `$1`/`1%`/`20%` labels;
- 24h combined volume and freshness;
- disabled reason when not actionable.

The preview modal says the two orders are non-atomic, belong to separate conditions, will not merge, and authorize at most `$2` estimated remediation.

- [ ] **Step 4: Add distribution, usage, and folded scan logs**

Use native `<details>` for the nonpersistent logs. Render current/7d/30d distributions and:

```text
Codex 24h: calls · successes · failures · cache hits
input tokens · cached input tokens · output tokens
```

Do not add a charting dependency.

- [ ] **Step 5: Add Playwright acceptance states**

Cover APPROVE/actionable, REJECT/reason, unavailable, stale, tiny positive profit, multiple holdings, modal/cancel/confirm, and `<details>` collapsed by default at 1920, 1440, 768, and 375 widths.

- [ ] **Step 6: Run Dashboard tests and commit**

```bash
/Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_dashboard_web.py -q
npx playwright test tests/e2e/prediction-market.spec.ts
git add src/open_trader/dashboard_web.py \
  src/open_trader/dashboard_static/dashboard.js \
  src/open_trader/dashboard_static/dashboard.css \
  tests/test_dashboard_web.py \
  tests/e2e/prediction-market.spec.ts
git commit -m "feat: show threshold hedge validation"
```

---

### Task 7: Production Wiring, Operator Log, and Final Verification

**Files:**
- Modify: `src/open_trader/dashboard_web.py`
- Modify: `src/open_trader/cli.py`
- Modify: `tests/test_dashboard_web.py`
- Modify: `tests/test_prediction_arbitrage_launchd.py`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: existing Dashboard/launchd monitor construction.
- Produces: production discovery/validator wiring using the local Codex CLI.

- [ ] **Step 1: Write failing production wiring tests**

Assert configured prediction-arbitrage startup creates discovery and Codex validator once, does not require `DEEPSEEK_API_KEY`, and reports `unavailable` rather than crashing when `codex` is absent or not logged in.

- [ ] **Step 2: Run wiring tests and verify RED**

```bash
/Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_dashboard_web.py \
  tests/test_prediction_arbitrage_launchd.py -k 'prediction and codex' -q
```

- [ ] **Step 3: Wire production collaborators and update changelog**

Construct both collaborators beside the existing monitor/store/trading services. Add the dated operator-facing entry to `CHANGELOG.md` before any merge.

- [ ] **Step 4: Run all focused automated tests**

```bash
/Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_polymarket_relation_discovery.py \
  tests/test_prediction_arbitrage.py \
  tests/test_prediction_arbitrage_store.py \
  tests/test_polymarket_monitor.py \
  tests/test_polymarket_trading.py \
  tests/test_prediction_arbitrage_execution.py \
  tests/test_dashboard_web.py \
  tests/test_prediction_arbitrage_launchd.py -q
```

- [ ] **Step 5: Run live read-only workflows**

Run one real full Gamma scan, one real CLOB batch read, and one real Codex cache-miss validation. Confirm the second validation is a cache hit and the 24h usage counter increments once. Run `prediction-arb preflight --no-submit`; do not post orders.

- [ ] **Step 6: Inspect long-running process state before final gate**

Record current monitor/dashboard PIDs, working directories, Git SHAs, screen sessions, launchd jobs, and fresh logs. Stop any review process that still has pre-change code loaded.

- [ ] **Step 7: Commit final wiring and changelog**

```bash
git add src/open_trader/dashboard_web.py src/open_trader/cli.py \
  tests/test_dashboard_web.py tests/test_prediction_arbitrage_launchd.py \
  CHANGELOG.md \
  docs/superpowers/specs/2026-07-29-polymarket-threshold-hedge-design.md \
  docs/superpowers/plans/2026-07-29-polymarket-threshold-hedge.md
git commit -m "feat: wire polymarket threshold hedge discovery"
```

- [ ] **Step 8: Run the final Dashboard acceptance gate once**

Run:

```bash
make acceptance
```

Required result: `PASS`. On `FAIL`, fix and rerun. On `BLOCKED`, report the blocker and do not present the task for review.

- [ ] **Step 9: Redeploy the exact accepted SHA and verify**

Restart the Dashboard/monitor from the accepted worktree SHA without source or data changes. Verify new PID, working directory, Git SHA, fresh logs, and HTTP 200 from the review URL. Only then ask the user to review.
