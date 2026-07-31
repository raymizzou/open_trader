# Polymarket Relation Discovery Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reliably discover all Polymarket threshold relations, narrow them every minute to a near-executable live pool, expose both funnels, record observed opportunity windows, and send one Feishu notification only when the exact intent is ready to submit without submitting it.

**Architecture:** `polymarket_relation_discovery.py` remains the deterministic domain boundary for SDK normalization, relation serialization, activity economics, and Codex fingerprints. `PredictionArbitrageStore` durably owns relation snapshots, scan summaries, and signal episodes. `PolymarketMonitor` owns the daily catalog task, minute activity scan, one-worker Codex queue, targeted WebSocket subscriptions, rule refresh, and opportunity episodes; `PredictionExecutionService` reuses the real execution checks for read-only no-submit notification proof.

**Tech Stack:** Python 3.12, Pydantic models from the installed `polymarket` SDK, `asyncio`, `sqlite3`, `Decimal`, existing notification classes, vanilla JavaScript/CSS, pytest, Node-based Dashboard JS tests, Playwright acceptance.

## Global Constraints

- Start and remain on `fix/polymarket-relation-discovery` in `/Users/ray/projects/open_trader/.worktrees/polymarket-relation-discovery`, based on local `main`.
- Do not add dependencies, services, queues, generic frameworks, or a Top N relation cap.
- Full relation discovery succeeds at most once per 24 hours; `new_market` gets an immediate event-only refresh.
- The activity scan considers the complete persisted relation catalog every 60 seconds.
- Activity eligibility requires both buy books, common minimum executable depth, known ticks/fees, total cost at most `$20`, and net edge at least `-5%`.
- Volume and Gamma liquidity are display/sort facts only, never hard eligibility gates.
- WebSocket quote age is at most 10 seconds; only affected relations are recalculated per message.
- Codex uses one background worker. Final approve/reject results persist; unchanged relations never rerun because of pool churn or generic `updated_at`.
- Codex transient failures retry at most once per relation per hour.
- Signal episodes begin at `minimum_profit > 0`, not at Codex approval, and describe only the system-observed window.
- Existing `$20` normal cost, `$2` emergency loss, wallet, region, allowance, relayer, circuit breaker, concurrency, and deterministic review rules do not change.
- Observation mode never submits an order and never self-promotes to automatic execution.
- A Feishu opportunity notification means that relation review, Codex, account checks, no-submit preflight, and a final fresh intent check all passed at `order_ready_at`.
- Each episode succeeds at notification once; at most three attempts occur, and only while the same episode is still order-ready.
- Production data has one writer: the Dashboard on port 8766. Stop the stale 18766 process before production verification.
- Run focused tests while developing. Do not run `make acceptance` until the
  branch is merged and it is the final Dashboard gate; rerun it only after
  fixing an actual `FAIL`.
- Before merging, commit the dated operator-facing `CHANGELOG.md` entry.
- After `make acceptance` returns `PASS`, deploy that exact SHA, verify PID/cwd/SHA/fresh logs/HTTP 200, and capture live desktop and mobile screenshots of the changed prediction view.

---

### Task 1: Normalize Official SDK Models and Stabilize Relation Fingerprints

**Files:**
- Modify: `src/open_trader/polymarket_relation_discovery.py:191-675`
- Modify: `tests/test_polymarket_relation_discovery.py:1-510`

**Interfaces:**
- Produces:
  `ThresholdRelationDiscoveryResult(relations, events_seen, events_eligible,
  markets_seen, markets_normalized, threshold_markets, unique_tokens,
  rejection_counts)`.
- Produces:
  `discover_threshold_relation_catalog(events: Sequence[object]) ->
  ThresholdRelationDiscoveryResult`.
- Preserves:
  `discover_threshold_relations(events: Sequence[object]) ->
  tuple[ThresholdRelation, ...]` as a wrapper returning `.relations`.
- Produces: `threshold_relation_payload(relation: ThresholdRelation) -> dict[str, object]`
- Produces: `threshold_relation_from_payload(payload: Mapping[str, object]) -> ThresholdRelation`
- Produces: `CodexRelationValidator.cached_validation(relation: ThresholdRelation) -> RelationValidation | None`
- Changes: `codex_relation_cache_key()` excludes generic market `updated_at`.
- Changes: `ThresholdRelation` carries `event_title`, `event_slug`,
  `event_volume_24h`, and `event_liquidity`; `ThresholdMarket` carries
  `volume_24h` and `liquidity`. Missing display metrics remain `None` and never
  affect relation or activity eligibility.

- [ ] **Step 1: Add failing official-model and round-trip tests**

Use actual SDK model classes so the regression covers attribute outcomes and `datetime` dates:
Extend the existing `open_trader.polymarket_relation_discovery` imports with
`ThresholdRelationDiscoveryResult`,
`discover_threshold_relation_catalog`, `threshold_relation_payload`, and
`threshold_relation_from_payload`.

```python
from datetime import UTC, datetime
from polymarket.models.gamma.event import Event, EventState
from polymarket.models.gamma.market import (
    FeeSchedule,
    Market,
    MarketOutcome,
    MarketOutcomes,
    MarketResolution,
    MarketState,
    MarketTrading,
)


def sdk_market(market_id: str, question: str, token_suffix: str) -> Market:
    return Market.model_construct(
        id=market_id,
        condition_id=f"condition-{market_id}",
        question=question,
        description=RULES,
        state=MarketState(
            active=True,
            closed=False,
            accepting_orders=True,
            enable_order_book=True,
            neg_risk=False,
            end_date=datetime(2026, 12, 31, 17, tzinfo=UTC),
        ),
        outcomes=MarketOutcomes(
            yes=MarketOutcome(label="Yes", token_id=f"yes-{token_suffix}"),
            no=MarketOutcome(label="No", token_id=f"no-{token_suffix}"),
        ),
        trading=MarketTrading(
            minimum_order_size=Decimal("5"),
            minimum_tick_size=Decimal("0.001"),
            fees_enabled=True,
            fee_schedule=FeeSchedule(
                exponent=1,
                rate=Decimal("0.07"),
                taker_only=True,
                rebate_rate=Decimal("0.2"),
            ),
        ),
        resolution=MarketResolution(source="Binance"),
    )


def test_official_sdk_event_matches_json_dump() -> None:
    sdk_event = Event.model_construct(
        id="event-sdk",
        slug="btc-thresholds",
        title="Bitcoin thresholds",
        state=EventState(active=True, closed=False, ended=False),
        markets=(
            sdk_market("lower", "Will Bitcoin be above $90,000 on December 31?", "lower"),
            sdk_market("higher", "Will Bitcoin be above $100,000 on December 31?", "higher"),
        ),
    )
    sdk_relations = discover_threshold_relations([sdk_event])
    json_relations = discover_threshold_relations(
        [sdk_event.model_dump(by_alias=True, mode="json")]
    )
    assert sdk_relations == json_relations
    assert len(sdk_relations) == 1
    assert sdk_relations[0].market_a.end_date == "2026-12-31T17:00:00Z"
    assert sdk_relations[0].event_slug == "btc-thresholds"


def test_catalog_result_reports_each_first_funnel_stage_once() -> None:
    result = discover_threshold_relation_catalog(
        [
            event(
                market("ordinary", question="Will Bitcoin rise?"),
                market(
                    "lower",
                    question="Will Bitcoin be above $90,000 on December 31?",
                ),
                market(
                    "higher",
                    question="Will Bitcoin be above $100,000 on December 31?",
                ),
            ),
            event(
                market("closed-market", question="Will Bitcoin rise?"),
                active=False,
            ),
        ]
    )
    assert result.events_seen == 2
    assert result.events_eligible == 1
    assert result.markets_seen == 4
    assert result.markets_normalized == 3
    assert result.threshold_markets == 2
    assert len(result.relations) == 1
    assert result.unique_tokens == 2
    assert result.rejection_counts["event_ineligible"] == 1
    assert result.rejection_counts["not_threshold"] == 1


def test_relation_payload_round_trips_without_type_loss() -> None:
    relation = discover_threshold_relations([
        event(
            market("lower", question="Will Bitcoin be above $90,000 on December 31?"),
            market("higher", question="Will Bitcoin be above $100,000 on December 31?"),
        )
    ])[0]
    assert threshold_relation_from_payload(
        threshold_relation_payload(relation)
    ) == relation
```

- [ ] **Step 2: Add failing Codex cache tests**

```python
def test_cache_key_ignores_generic_updated_at_but_not_rules() -> None:
    relation = threshold_relation()
    touched = replace(
        relation,
        market_a=replace(relation.market_a, updated_at="2026-07-31T12:00:00Z"),
    )
    changed_rules = replace(
        relation,
        market_a=replace(relation.market_a, rules=relation.market_a.rules + " Changed."),
    )
    assert codex_relation_cache_key(relation, model="gpt-test") == (
        codex_relation_cache_key(touched, model="gpt-test")
    )
    assert codex_relation_cache_key(relation, model="gpt-test") != (
        codex_relation_cache_key(changed_rules, model="gpt-test")
    )


def test_cached_validation_never_invokes_runner(tmp_path: Path) -> None:
    relation = threshold_relation()
    validator = CodexRelationValidator(
        codex_store(tmp_path),
        model="gpt-test",
        runner=lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout=codex_jsonl(codex_result()),
            stderr="",
        ),
    )
    assert validator.validate(relation).status == "approved"
    validator.runner = lambda *args, **kwargs: pytest.fail("runner called")
    cached = validator.cached_validation(relation)
    assert cached is not None
    assert cached.status == "approved"
    assert cached.cached is True
```

- [ ] **Step 3: Run the focused tests and confirm they fail**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_polymarket_relation_discovery.py \
  -k 'official_sdk_event or relation_payload_round or cache_key_ignores or cached_validation' -q
```

Expected: failures showing SDK models produce no relations, round-trip helpers are missing, and `updated_at` changes the cache key.

- [ ] **Step 4: Implement one boundary normalization and strict relation serialization**

Normalize the whole SDK event once before field parsing:

```python
def _json_model(value: object) -> object:
    model_dump = getattr(value, "model_dump", None)
    if not callable(model_dump):
        return value
    dumped = model_dump(by_alias=True, mode="json")
    return dumped if isinstance(dumped, Mapping) else value


for raw_event in events:
    event = _json_model(raw_event)
    if not _eligible_event(event):
        continue
```

Add the audit/link/display fields declared above and pass them from normalized
event/market metrics into `_relation()`. Accept only finite non-negative display
metrics; preserve missing metrics as `None`. Implement explicit
mapping-to-dataclass reconstruction; reject missing IDs, non-finite trading
decimals, unsupported operators, unsupported outcomes, or duplicate tokens with
`ValueError`.

Move the existing discovery loop into
`discover_threshold_relation_catalog()`. Increment exactly one safe reason per
rejected event/market/relation using `event_ineligible`, `market_ineligible`,
`market_unparseable`, `not_threshold`, `duplicate_condition`, and
`duplicate_token`; do not retain raw SDK values or exception text. Keep
`discover_threshold_relations()` as the compatibility wrapper so existing
callers and tests do not change unnecessarily.

`markets_seen` counts all raw markets attached to scanned events.
`markets_normalized` counts schema-readable markets under eligible events,
including those later classified `not_threshold`. `threshold_markets` counts
only markets admitted to relation grouping.

- [ ] **Step 5: Split prompt payload from cache fingerprint**

Keep `updated_at` in the Codex prompt if useful, but exclude it from the hash:

```python
def _codex_cache_market_payload(market: ThresholdMarket) -> dict[str, str]:
    return {
        "condition_id": market.condition_id,
        "question": market.question,
        "rules": market.rules,
        "resolution_source": market.resolution_source,
        "end_date": market.end_date,
    }


def cached_validation(
    self, relation: ThresholdRelation
) -> RelationValidation | None:
    cache_key = codex_relation_cache_key(
        relation, model=self.model, prompt_version=self.prompt_version
    )
    cached = self.store.load_llm_cache(cache_key)
    if not isinstance(cached, Mapping):
        return None
    structured = cached.get("structured_result")
    if (
        cached.get("model") != self.model
        or cached.get("prompt_version") != self.prompt_version
        or not _valid_structured_result(structured)
    ):
        return None
    assert isinstance(structured, Mapping)
    validation = self._validated(
        relation,
        cache_key,
        structured,
        cached=True,
    )
    if validation.status not in {"approved", "llm_rejected"}:
        return None
    self.store.record_llm_cache_hit()
    return validation
```

Make `validate()` call `cached_validation()` first so terminal cache parsing has one implementation.
Update the existing
`test_codex_fingerprint_uses_only_versioned_semantic_payload` expected JSON to
remove `updated_at`; retain its fee/price-only stability assertions and add an
explicit `updated_at` mutation.

- [ ] **Step 6: Run all relation-discovery tests**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_polymarket_relation_discovery.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/open_trader/polymarket_relation_discovery.py \
  tests/test_polymarket_relation_discovery.py
git commit -m "fix: normalize Polymarket relation inputs"
```

---

### Task 2: Persist Relation Catalogs, Funnel Runs, and Immutable Episodes

**Files:**
- Modify: `src/open_trader/prediction_arbitrage_store.py:1-490`
- Modify: `tests/test_prediction_arbitrage_store.py:1-250`

**Interfaces:**
- Produces: `_dump_relation_payload(payload: Mapping[str, object]) -> str`,
  the only persistence path that permits public outcome `token_id` fields
  while still dropping credential names.
- Produces: `save_relation_state(payload: Mapping[str, object], *, full_scanned_at: str) -> None`
- Produces: `load_relation_state() -> dict[str, object] | None`
- Produces: `record_relation_scan(*, scope: Literal["full", "event", "activity"], status: Literal["completed", "failed"], started_at: str, completed_at: str, payload: Mapping[str, object], event_id: str | None = None) -> str`
- Produces: `relation_scan_history(*, scope: str | None = None, limit: int = 20) -> list[dict[str, object]]`
- Produces: `signal(signal_id: str) -> dict[str, object] | None`
- Produces: `update_signal(signal_id: str, changes: Mapping[str, object]) -> dict[str, object]`
- Changes: `close_signal(market_id: str, *, ended_at: str, reason: str, updates: Mapping[str, object] | None = None)`.

- [ ] **Step 1: Add failing schema, catalog, and retention tests**

```python
def test_relation_state_and_scan_tables_survive_restart(tmp_path: Path) -> None:
    db = store(tmp_path)
    db.save_relation_state(
        {"relations": [{"relation_id": "r-1", "token_id": "public-token"}]},
        full_scanned_at="2026-07-31T00:00:00Z",
    )
    db.record_relation_scan(
        scope="full",
        status="completed",
        started_at="2026-07-31T00:00:00Z",
        completed_at="2026-07-31T00:00:02Z",
        payload={"relations_discovered": 1},
    )
    restarted = PredictionArbitrageStore(tmp_path / "data")
    assert restarted.load_relation_state()["relations"][0]["relation_id"] == "r-1"
    assert restarted.load_relation_state()["relations"][0]["token_id"] == "public-token"
    assert restarted.relation_scan_history(limit=1)[0]["scope"] == "full"


def test_activity_retention_does_not_delete_full_or_event_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 7, 31, 12, tzinfo=UTC)
    monkeypatch.setattr(
        "open_trader.prediction_arbitrage_store._utc_now", lambda: iso(now)
    )
    db = store(tmp_path)
    for scope in ("full", "event", "activity"):
        db.record_relation_scan(
            scope=scope,
            status="completed",
            event_id="e-1" if scope == "event" else None,
            started_at=iso(now - timedelta(days=8)),
            completed_at=iso(now - timedelta(days=8)),
            payload={"scope": scope},
        )
    db.record_relation_scan(
        scope="activity",
        status="completed",
        started_at=iso(now),
        completed_at=iso(now),
        payload={"scope": "new"},
    )
    assert [row["scope"] for row in db.relation_scan_history(limit=10)] == [
        "activity", "event", "full"
    ]
```

- [ ] **Step 2: Add failing immutable episode and update tests**

```python
def test_open_signal_keeps_first_observation_and_initial_profit(tmp_path: Path) -> None:
    db = store(tmp_path)
    signal_id = db.upsert_signal({
        **signal_payload("relation-1", "2026-07-31T00:00:00Z"),
        "first_positive_at": "2026-07-31T00:00:00Z",
        "initial_profit": Decimal("0.10"),
        "peak_profit": Decimal("0.10"),
    })
    db.upsert_signal({
        **signal_payload("relation-1", "2026-07-31T00:00:01Z"),
        "first_positive_at": "2026-07-31T00:00:01Z",
        "initial_profit": Decimal("0.05"),
        "peak_profit": Decimal("0.20"),
    })
    row = db.signal(signal_id)
    assert row["started_at"] == "2026-07-31T00:00:00.000000Z"
    assert row["first_positive_at"] == "2026-07-31T00:00:00.000000Z"
    assert row["initial_profit"] == "0.10"
    assert row["peak_profit"] == "0.20"


def test_close_signal_persists_final_episode_values(tmp_path: Path) -> None:
    db = store(tmp_path)
    signal_id = db.upsert_signal(
        signal_payload("relation-1", "2026-07-31T00:00:00Z")
    )
    db.close_signal(
        "relation-1",
        ended_at="2026-07-31T00:00:00.250Z",
        reason="profit_non_positive",
        updates={
            "observed_duration_ms": 250,
            "final_profit": Decimal("-0.01"),
        },
    )
    assert db.signal(signal_id)["observed_duration_ms"] == 250
```

- [ ] **Step 3: Run tests and confirm they fail**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_prediction_arbitrage_store.py \
  -k 'relation_state or activity_retention or first_observation or final_episode' -q
```

Expected: missing-table/missing-method failures and mutable `started_at`.

- [ ] **Step 4: Add the two narrow tables and version 2 schema**

```sql
CREATE TABLE IF NOT EXISTS relation_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    payload TEXT NOT NULL,
    full_scanned_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS relation_scan_runs (
    scan_id TEXT PRIMARY KEY,
    scope TEXT NOT NULL CHECK (scope IN ('full', 'event', 'activity')),
    event_id TEXT,
    status TEXT NOT NULL CHECK (status IN ('completed', 'failed')),
    payload TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL
);
```

Set `PRAGMA user_version=2`. Keep short `BEGIN IMMEDIATE` transactions and canonical JSON. In `record_relation_scan()`, delete only `scope='activity' AND completed_at < now - 7 days`.

Do not pass relation snapshots through the existing generic `_dump_payload()`,
which intentionally strips all token-shaped fields. Add a relation-only
serializer that delegates to `_safe_value()` with an
`allow_public_token_ids=True` flag propagated recursively; that flag permits
only normalized keys `token_id`, `yes_token_id`, and `no_token_id`. All other
private-name rules remain active. `save_relation_state()` alone uses this
serializer. Add a test proving public tokens survive only in `relation_state`,
while `record_relation_scan()`, `upsert_signal()`, Dashboard projection, and
Feishu payloads still remove them.

- [ ] **Step 5: Preserve immutable signal fields and add atomic updates**

When an open signal exists, merge only mutable fields:

```python
for immutable in ("started_at", "first_positive_at", "initial_profit"):
    if immutable in previous:
        clean[immutable] = previous[immutable]
previous.update(clean)
```

`update_signal()` must load, merge, sanitize, and update in one transaction. `close_signal()` applies final updates and the close reason in the same transaction.

- [ ] **Step 6: Run store tests**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_prediction_arbitrage_store.py -q
```

Expected: all tests pass, including concurrent writers and credential stripping.

- [ ] **Step 7: Commit**

```bash
git add src/open_trader/prediction_arbitrage_store.py \
  tests/test_prediction_arbitrage_store.py
git commit -m "feat: persist relation scan state"
```

---

### Task 3: Add the Pure Five-Percent Activity Assessment

**Files:**
- Modify: `src/open_trader/polymarket_relation_discovery.py:1099-1270`
- Modify: `tests/test_polymarket_relation_discovery.py:1-end`

**Interfaces:**
- Produces: `RelationActivityAssessment(reason: str, intent: ThresholdHedgeIntent | None)`
- Produces: `assess_threshold_relation_activity(relation: ThresholdRelation, books: Mapping[str, ThresholdOrderBook], *, minimum_net_edge: Decimal = Decimal("-0.05")) -> RelationActivityAssessment`
- Preserves: `build_threshold_hedge_intent()` positive-only behavior and safe-unwind checks.

- [ ] **Step 1: Add table-driven failing tests for every funnel reason**

Extend the test imports with `UTC`/`datetime`,
`BookLevel`/`ThresholdOrderBook`, `ThresholdRelation`,
`RelationActivityAssessment`, and `assess_threshold_relation_activity`. Add
this helper beside the existing `threshold_relation()` fixture:

```python
def activity_relation() -> ThresholdRelation:
    relation = threshold_relation()
    return replace(
        relation,
        market_a=replace(
            relation.market_a,
            fees_enabled=False,
            fee_rate=None,
        ),
        market_b=replace(
            relation.market_b,
            fees_enabled=False,
            fee_rate=None,
        ),
    )


def activity_books(
    relation: ThresholdRelation,
    *,
    price_a: str = "0.50",
    price_b: str = "0.50",
    size_a: str = "20",
    size_b: str = "20",
) -> dict[str, ThresholdOrderBook]:
    def book(token_id: str, price: str, size: str) -> ThresholdOrderBook:
        return ThresholdOrderBook(
            token_id=token_id,
            asks=(BookLevel(price=Decimal(price), size=Decimal(size)),),
            bids=(),
            confirmed_at=datetime(2026, 7, 31, tzinfo=UTC),
        )

    return {
        relation.buy_leg_a.token_id: book(
            relation.buy_leg_a.token_id, price_a, size_a
        ),
        relation.buy_leg_b.token_id: book(
            relation.buy_leg_b.token_id, price_b, size_b
        ),
    }


@pytest.mark.parametrize(
    ("changes", "expected_reason"),
    [
        ({"missing": "both"}, "book_unavailable"),
        ({"missing": "b"}, "book_unavailable"),
        ({"size_a": "0.5"}, "minimum_depth"),
        ({"price_a": "0.55", "price_b": "0.51"}, "outside_5pct"),
        ({"price_a": "0.55", "price_b": "0.50"}, "eligible"),
    ],
)
def test_activity_assessment_has_exact_reason(
    changes: dict[str, str],
    expected_reason: str,
) -> None:
    relation = activity_relation()
    missing = changes.get("missing", "")
    prices_and_sizes = {
        key: value for key, value in changes.items() if key != "missing"
    }
    books = activity_books(relation, **prices_and_sizes)
    if missing == "both":
        books.clear()
    elif missing == "b":
        books.pop(relation.buy_leg_b.token_id)
    assert assess_threshold_relation_activity(relation, books).reason == expected_reason


def test_activity_assessment_does_not_consume_volume() -> None:
    relation = replace(
        activity_relation(),
        event_volume_24h=Decimal("0"),
        event_liquidity=Decimal("0"),
    )
    assessment = assess_threshold_relation_activity(
        relation,
        activity_books(relation, price_a="0.50", price_b="0.51"),
    )
    assert assessment.reason == "eligible"
```

Add separate cases for `fee_unknown`, `tick_invalid`, unequal common executable
depth, `cost_limit`, and the exact `$20` boundary. Build each by replacing the
relevant frozen relation field or book level; do not hide those mutations in
additional fixture helpers.

- [ ] **Step 2: Run tests and confirm they fail**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_polymarket_relation_discovery.py -k activity_assessment -q
```

Expected: import error for the missing assessment.

- [ ] **Step 3: Extract one shared candidate calculator**

Implement one internal calculator used by both activity assessment and the existing positive intent builder:

```python
@dataclass(frozen=True, slots=True)
class RelationActivityAssessment:
    reason: str
    intent: ThresholdHedgeIntent | None


def assess_threshold_relation_activity(
    relation: ThresholdRelation,
    books: Mapping[str, ThresholdOrderBook],
    *,
    minimum_net_edge: Decimal = Decimal("-0.05"),
) -> RelationActivityAssessment:
    candidate, reason = _threshold_candidate(
        relation,
        books,
        require_safe_unwind=False,
    )
    if candidate is None:
        return RelationActivityAssessment(reason=reason, intent=None)
    if candidate.net_edge < minimum_net_edge:
        return RelationActivityAssessment(reason="outside_5pct", intent=None)
    return RelationActivityAssessment(reason="eligible", intent=candidate)
```

`_threshold_candidate()` returns the exact fail-closed reason code while sharing
the existing quantity/cost/fee arithmetic. Do not add volume parameters. Keep
`build_threshold_hedge_intent()` as a wrapper requiring positive profit and, by
default, safe unwind bids.

Use this precedence so each rejected relation increments exactly one funnel
reason: `book_unavailable`, `fee_unknown`, `tick_invalid`, `minimum_depth`,
`cost_limit`, `outside_5pct`, then `eligible`. Choose the largest common
equal-share quantity whose fee-inclusive total cost is at most `$20`; if even
the common minimum quantity breaches the cap, return `cost_limit`.

- [ ] **Step 4: Run relation domain tests**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_polymarket_relation_discovery.py -q
```

Expected: all tests pass and prior positive-hedge economics are unchanged.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/polymarket_relation_discovery.py \
  tests/test_polymarket_relation_discovery.py
git commit -m "feat: assess near-executable relations"
```

---

### Task 4: Run Daily Full Discovery Outside the Top-20 Refresh

**Files:**
- Modify: `src/open_trader/polymarket_monitor.py:243-730`
- Modify: `src/open_trader/dashboard_web.py:1350-1395`
- Modify: `tests/test_polymarket_monitor.py:1-760`

**Interfaces:**
- Consumes: `discover_threshold_relation_catalog()`, relation serialization,
  and store methods from Tasks 1-2.
- Produces: `_load_relation_catalog() -> None`
- Produces: `_run_full_relation_scan(client: object) -> None`
- Produces: `_refresh_relation_event(client: object, event_id: str) -> bool`
- Snapshot contract: `relation_discovery.catalog` contains status, timestamps, age, counts, duration, and last full/event run.

- [ ] **Step 1: Add failing restart, paginator, and atomic-publication tests**

Import `discover_threshold_relation_catalog` and
`threshold_relation_payload` from the relation module, and make the
production-shaped `make_monitor()` fixture use the catalog function.

```python
def test_restart_loads_fresh_relation_catalog_without_full_scan(tmp_path: Path) -> None:
    relation = discover_threshold_relations([threshold_event()])[0]
    setup_public([])
    db = PredictionArbitrageStore(tmp_path / "data")
    db.save_relation_state(
        {"relations": [threshold_relation_payload(relation)]},
        full_scanned_at=NOW.isoformat(),
    )
    monitor = make_monitor(tmp_path)
    monitor._load_relation_catalog()
    assert set(monitor._relations) == {relation.relation_id}
    assert FakePublicClient.list_events_calls == []


def test_full_scan_consumes_every_paginator_page_and_publishes_once(
    tmp_path: Path,
) -> None:
    rows = [
        event(
            f"ordinary-{index}",
            markets=(market(f"market-{index}"),),
        )
        for index in range(21)
    ] + [threshold_event()]
    setup_public(rows)
    FakePublicClient.page_mode = True
    monitor = make_monitor(tmp_path)
    asyncio.run(monitor._run_full_relation_scan(FakePublicClient()))
    state = monitor._store.load_relation_state()
    assert state["relations"]
    assert PagePaginator.iter_calls == 1
    assert monitor.snapshot()["relation_discovery"]["catalog"]["status"] == "healthy"


def test_failed_full_scan_keeps_previous_catalog(tmp_path: Path) -> None:
    setup_public([threshold_event()])
    monitor = make_monitor(tmp_path)
    asyncio.run(monitor._run_full_relation_scan(FakePublicClient()))
    before = set(monitor._relations)
    FakePublicClient.fail_list_events = True
    asyncio.run(monitor._run_full_relation_scan(FakePublicClient()))
    assert set(monitor._relations) == before
    assert monitor.snapshot()["relation_discovery"]["catalog"]["status"] == "degraded"
```

- [ ] **Step 2: Add failing 24-hour and event-only tests**

Extend `make_monitor()` with an injectable clock. Use it at ages `23:59:59`
and `24:00:00`; assert the first case schedules no scan, the second schedules
exactly one background task, a second scheduler tick does not overlap it, and
neither blocks `_refresh_universe_bounded()`. Adapt the existing
`test_new_market_refreshes_only_its_event_for_relation_discovery` to assert the
exact event ID is fetched, only that event's relations are replaced, failure
preserves its old relations, `full_scanned_at` is unchanged, and a completed
event scan marks the activity scan immediately due.

- [ ] **Step 3: Run tests and confirm they fail**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_polymarket_monitor.py \
  -k 'relation_catalog or full_scan or event_only or twenty_four' -q
```

Expected: no catalog load, first-page-only behavior, and relation scan still inside the 30-second universe path.

- [ ] **Step 4: Move full discovery into its own async task**

At monitor startup:

```python
self._load_relation_catalog()
if self._catalog_due(self._now()):
    self._full_scan_task = asyncio.create_task(
        self._run_full_relation_scan(client)
    )
```

Remove `_refresh_relation_universe()` from ordinary Top-20
`_refresh_universe()`. Use `_collect()` rather than `_collect_first_page()` for
the full task and exhaust the keyset paginator until `next_cursor` is empty.
Build the entire tuple, serialize it, persist it, then swap `_relations` under
the monitor lock. Failed fetch/discovery/persistence leaves the old catalog
untouched. Derive catalog status as `healthy`, `scanning`, `stale`, or
`degraded`; a catalog older than 24 hours remains visible but sets
`relation_discovery_stale` on every relation opportunity so Task 7 cannot mark
it order-ready.

Update `serve_dashboard()` to inject
`discover_threshold_relation_catalog` into `PolymarketMonitor`. For test
compatibility, normalize either `ThresholdRelationDiscoveryResult` or the
legacy relation tuple returned by an explicitly injected fake; production full
and event scans must always use the result object and its first-funnel counts.

- [ ] **Step 5: Make `new_market` event-only and durable**

Fetch the exact event ID, normalize it, replace only relations sharing that event ID, save the whole atomic snapshot without changing `full_scanned_at`, record `scope="event"`, and mark activity refresh due immediately.

- [ ] **Step 6: Run monitor and existing focused tests**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_polymarket_monitor.py \
  tests/test_polymarket_relation_discovery.py \
  tests/test_prediction_arbitrage_store.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/open_trader/polymarket_monitor.py tests/test_polymarket_monitor.py
git add src/open_trader/dashboard_web.py
git commit -m "feat: persist daily Polymarket relation catalog"
```

---

### Task 5: Build the Minute Activity Funnel, Codex Worker, and Targeted Stream

**Files:**
- Modify: `src/open_trader/polymarket_monitor.py:243-930`
- Modify: `tests/test_polymarket_monitor.py:390-1100`

**Interfaces:**
- Consumes: `assess_threshold_relation_activity()` and `cached_validation()`.
- Produces: `_refresh_relation_activity(client: object) -> None`
- Produces: `_poll_relation_validation(client: object) -> None`
- Produces: `_refresh_relation_opportunities(client: object, relation_ids: set[str]) -> tuple[dict[str, object], ...]`
- Snapshot contract: `relation_discovery.activity`, `relation_discovery.codex_queue`, and `relation_discovery.websocket`.

- [ ] **Step 1: Add failing minute-funnel tests**

Extend the existing `threshold_event()` fixture with keyword-only `event_id`
and `token_prefix` arguments, and extend `setup_threshold_books()` with the
same `token_prefix`. Then add these exact cases:

- Persist two distinct relations. Give one books at `0.50 + 0.51` and the
  other at `0.60 + 0.60`; after the first activity scan assert
  `relations_considered == 2` and `relations_within_5pct == 1`. Change only the
  second pair to `0.50 + 0.52`, run a second scan, and assert both relations are
  now in the 5% pool. This proves full reconsideration rather than incremental
  exclusion.
- Replace the first relation's `event_volume_24h`, `event_liquidity`, and both
  market display metrics with `Decimal("0")`. With valid `0.50 + 0.51` books,
  assert it still enters `_active_relation_ids`.
- After a successful scan, make `FakePublicClient.get_order_books()` raise.
  Assert `status == "degraded"` and all last-completed counts and active
  subscription IDs are unchanged.
- Hold the first scan open with two `asyncio.Event` barriers, advance the fake
  clock past 60 seconds, and tick the scheduler twice. Assert no second scan
  starts concurrently, status becomes `lagging`, and exactly one catch-up scan
  begins immediately after the first completes.
- After a successful full or event-only catalog publication, assert activity
  becomes due immediately instead of waiting for the next minute boundary.

- [ ] **Step 2: Add failing Codex queue/cache tests**

Use the two parameterized threshold events from Step 1 and an extended
`FakeRelationValidator` that records relation IDs and exposes
`cached_validation()`:

- Put both relations in the 5% pool, one at `net_edge=-0.01` and one at
  `net_edge=0.02`. Tick once and assert the positive relation ID is the sole
  inflight item; finish it, tick again, and assert the other ID starts.
- Parameterize terminal status as `approved` and `llm_rejected`: enter, finish
  validation, move the relation outside 5%, re-enter it, and assert validator
  call count remains one after restart with the same SQLite directory.
- Return `llm_unavailable`, advance the fake clock to `59:59`, then `60:00`;
  assert call counts `1`, `1`, then `2`.
- Assert pending and approved IDs contribute subscription tokens, rejected IDs
  do not, and the complete synthetic set of 301 eligible relations is present
  without slicing or a Top-N cap.
- Hold the fake validator runner on a `threading.Event`. While it is inflight,
  complete another minute activity scan and process a price message; assert
  both finish before releasing the validator. This proves the single Codex
  worker does not block REST or WebSocket work.

- [ ] **Step 3: Add failing targeted WebSocket tests**

Create two active relations with distinct token prefixes using Step 1's
fixtures. Send
`ns(type="price_change", payload=ns(asset_id=relation_a.buy_leg_a.token_id,
price_changes=()))` to `_process_stream_event()`. Assert
`FakePublicClient.book_calls` contains exactly one two-token request for
relation A and no relation B token. Then assert a completed activity scan
resubscribes to the union of Top-20 tokens plus pending/approved 5% tokens,
deduplicated and split into groups of at most 250.

Use the fake clock to timestamp message receipt. Make relation A cross from
non-positive to positive and back on two messages; after each awaited handler,
assert the Dashboard snapshot changed on the same event-loop turn and the
measured handler-to-snapshot delay is at most 10 seconds. Keep this as test
evidence; do not add a second public latency metric.

- [ ] **Step 4: Run tests and confirm they fail**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_polymarket_monitor.py \
  -k 'activity_scan or zero_volume or codex_worker or pool_churn or transient_codex or affected_relations' -q
```

Expected: missing activity state/queue and all-relation refresh behavior.

- [ ] **Step 5: Implement the minute scheduler and durable funnel summary**

Add constants:

```python
RELATION_ACTIVITY_REFRESH_SECONDS = 60
RELATION_ACTIVITY_MIN_EDGE = Decimal("-0.05")
RELATION_VALIDATION_RETRY_SECONDS = 60 * 60
```

Batch all catalog buy tokens in existing 100-token chunks with concurrency 8.
Assess every relation, count exact reason codes, and atomically replace active
relation IDs only after a complete scan. Record one `scope="activity"` row and
expose last completed values while the next scan is running. Keep one task;
when a minute boundary is missed set `lagging`, queue one catch-up run, and
never overlap scans.

- [ ] **Step 6: Implement the one-task Codex worker**

Do not create a generic queue class. Keep one `_codex_task`, one relation ID, and a retry timestamp dictionary. On each loop, reap the finished task, update status, then select the highest net-edge uncached relation. Use `asyncio.to_thread(validator.validate, relation)`.

- [ ] **Step 7: Restrict subscriptions and relation refresh**

Build `_relation_by_token` from pending plus approved 5% relations only. Rejected relations remain in funnel counts but not subscriptions. On a price message, fetch/recalculate only the relation IDs mapped from changed tokens. The 60-second activity scan remains the full REST recovery path.

- [ ] **Step 8: Run monitor tests**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_polymarket_monitor.py -q
```

Expected: all monitor tests pass.

- [ ] **Step 9: Commit**

```bash
git add src/open_trader/polymarket_monitor.py tests/test_polymarket_monitor.py
git commit -m "feat: add live relation activity funnel"
```

---

### Task 6: Record Millisecond Opportunity Windows and Recheck Live Rules

**Files:**
- Modify: `src/open_trader/polymarket_monitor.py:778-930, 1273-1430`
- Modify: `src/open_trader/dashboard_web.py:300-350`
- Modify: `tests/test_polymarket_monitor.py:760-930`
- Modify: `tests/test_dashboard_web.py:2100-2210`

**Interfaces:**
- Consumes: immutable signal store updates from Task 2.
- Opportunity contract adds: `rules_verified_at`, `rules_fingerprint`, both book timestamps, both local receive timestamps.
- Signal contract adds: `first_positive_at`, `last_positive_at`, `observed_duration_ms`, initial/peak/final profit, book timestamps, receive timestamps, and exact `ended_reason`.

- [ ] **Step 1: Add failing episode-boundary tests**

Use `setup_public([threshold_event()])`, the parameterized
`setup_threshold_books()` from Task 5, an approved `FakeRelationValidator`, and
the injectable clock:

- At `00:00:00.000`, use a positive book pair and refresh; capture the signal
  ID and assert `first_positive_at` and `initial_profit`.
- Move the clock to `00:00:00.100`, improve one ask, refresh, and assert
  `last_positive_at` and `peak_profit` move while first/initial values do not.
- Move to `00:00:00.275`, set the pair one cent beyond break-even, refresh, and
  assert `observed_duration_ms == 275`, the exact negative `final_profit`, and
  `ended_reason == "profit_non_positive"`.
- In a separate case, advance the newest required quote to age `10.001`
  seconds and tick the one-second maintenance path. Assert
  `data_unavailable`; restore fresh positive books and assert a different
  signal ID opens.
- Parameterize the other terminal causes as `rules_changed` and
  `relation_discovery_stale`. Assert every close updates the existing episode;
  it never mutates its first/initial values or opens a history table.

- [ ] **Step 2: Add failing rule-refresh tests**

Start with a fresh positive relation, then replace the event returned by
`FakePublicClient.get_event()` with a copy whose market description differs by
one semantic clause. On the first positive refresh assert the exact event ID
was fetched, the episode closes as `rules_changed`, `rules_verified_at` is
absent, and the opportunity is not order-ready. Parameterize mutations of event
ID, either condition ID, either outcome token, source, end date, active state,
either rules hash, and relation direction. The unchanged control case must set
`rules_verified_at` and the recomputed `rules_fingerprint`.

- [ ] **Step 3: Run tests and confirm they fail**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_polymarket_monitor.py tests/test_dashboard_web.py \
  -k 'observed_milliseconds or recovery_opens or first_positive_refetches or rules_changed' -q
```

Expected: current signal `started_at` shifts, no millisecond duration, and no live rule refresh.

- [ ] **Step 4: Implement episode state without another history table**

Have `_upsert_signal()` return the durable `signal_id`. Preserve first fields,
update last/peak fields, and store both exchange/book times and local receive
times. On every one-second event-loop timeout, close open signals whose
required quotes are older than 10 seconds or whose stream is disconnected.

- [ ] **Step 5: Recheck the source event only on first positive**

Fetch the exact event, rediscover only that event, compare the relation fingerprint, and update the catalog if unchanged metadata is fresher. A changed semantic identity closes the signal before Codex/actionability/notification.

- [ ] **Step 6: Project complete history fields**

Extend the `kind == "signals"` branch of `_prediction_history_aliases()` to
preserve observed duration, three profits, end reason, and notification state.
Do not call a planned amount an actual amount.

- [ ] **Step 7: Run focused tests**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_polymarket_monitor.py \
  tests/test_prediction_arbitrage_store.py \
  tests/test_dashboard_web.py -q
```

Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add src/open_trader/polymarket_monitor.py \
  src/open_trader/dashboard_web.py \
  tests/test_polymarket_monitor.py tests/test_dashboard_web.py
git commit -m "feat: record observed arbitrage windows"
```

---

### Task 7: Send Feishu Only After Real Read-Only Execution Proof

**Files:**
- Modify: `src/open_trader/notifications.py:1-160`
- Modify: `src/open_trader/prediction_arbitrage_execution.py:125-220, 1163-1270, 2065-2290`
- Modify: `src/open_trader/polymarket_monitor.py:243-450, 1273-1430`
- Modify: `src/open_trader/dashboard_web.py:1338-1405`
- Modify: `tests/test_notifications.py`
- Modify: `tests/test_prediction_arbitrage_execution.py`
- Modify: `tests/test_polymarket_monitor.py`
- Modify: `tests/test_dashboard_web.py:2870-2930`

**Interfaces:**
- Produces: `render_prediction_opportunity_notification(opportunity: Mapping[str, object], signal: Mapping[str, object], *, dashboard_url: str) -> tuple[str, str]`
- Produces: `PredictionExecutionService.notify_ready_opportunity(opportunity_id: str, signal_id: str) -> dict[str, object]`
- Produces: `PolymarketMonitor.set_ready_observer(observer: Callable[[str, str], Mapping[str, object]]) -> None`

- [ ] **Step 1: Add an exact renderer test**

```python
def test_prediction_notification_contains_ready_order_facts_only() -> None:
    title, message = render_prediction_opportunity_notification(
        {
            "event_title": "2026 年美联储会降息多少次？",
            "event_slug": "fed-cuts-2026",
            "leg_a": {
                "question": "至少降息 2 次",
                "outcome": "YES",
                "quantity": "10.00",
                "max_price": "0.53",
                "max_cost": "5.30",
            },
            "leg_b": {
                "question": "至少降息 3 次",
                "outcome": "NO",
                "quantity": "10.00",
                "max_price": "0.42",
                "max_cost": "4.20",
            },
            "planned_amount": "9.50",
            "maximum_fee": "0.12",
            "total_max_cost": "9.62",
            "minimum_payout": "10.00",
            "minimum_profit": "0.38",
            "net_edge": "0.0395",
            "order_ready_at": "2026-07-31T10:46:54.896000+08:00",
            "confirmed_age_seconds": "0.184",
            "rules_verified_at": "2026-07-31T10:46:54.700000+08:00",
        },
        {
            "signal_id": "pm-01",
            "first_positive_at": "2026-07-31T10:46:53.696000+08:00",
        },
        dashboard_url="http://127.0.0.1:8766/",
    )
    assert title == "【仅观察·未下单】Polymarket 正收益机会｜+$0.38"
    for text in (
        "事件：2026 年美联储会降息多少次？",
        "买入「至少降息 2 次」YES：10.00 份 × $0.53 = $5.30",
        "买入「至少降息 3 次」NO：10.00 份 × $0.42 = $4.20",
        "拟下单金额：$9.50",
        "预计费用：$0.12",
        "最大总成本：$9.62",
        "最低兑付：$10.00",
        "保底净利润：+$0.38（+3.95%）",
        "发现时间：2026-07-31 10:46:53.696 +08:00",
        "信号→发送：1.2 秒",
        "盘口年龄：184 毫秒",
        "关系复核：通过",
        "机会状态：观察中",
        "机会编号：pm-01",
        "https://polymarket.com/event/fed-cuts-2026",
        "Dashboard：http://127.0.0.1:8766/",
    ):
        assert text in message
    for forbidden in (
        "token_id",
        "wallet",
        "Codex 待确认",
        "余额正常",
        "自动下单关闭",
    ):
        assert forbidden not in message
```

- [ ] **Step 2: Add failing readiness/no-submit tests**

Parameterized tests must prove that notification is absent when any one check fails: breaker, active execution, rule verification, Codex terminal approval, book age, positive economics, account freshness, balance, allowance, geoblock, relayer, emergency unwind, or `no_submit_threshold_preflight`.
Add `replace` to the existing `dataclasses` test import.

Extend the existing `ThresholdMonitor.opportunity()` fixture with
`rules_verified_at`, `rules_fingerprint`, `relation_validation.status ==
"approved"`, fresh book/receive timestamps, event title/slug, and the two
display legs. In the ready control:

```python
service, trading, store, _ = threshold_execution_fixture(tmp_path)
signal_id = store.upsert_signal(
    {
        "market_id": "relation-1",
        "event_id": "event-threshold",
        "question": "Fed cuts",
        "started_at": datetime.now(UTC).isoformat(),
        "first_positive_at": datetime.now(UTC).isoformat(),
        "net_edge": Decimal("0.788"),
        "estimated_profit": Decimal("7.88"),
        "notification_state": "pending",
        "notification_attempts": 0,
    }
)
result = service.notify_ready_opportunity("threshold-opp-1", signal_id)
assert result["state"] == "sent"
assert trading.threshold_preflight_calls == 1
assert trading.threshold_submit_calls == 0
assert trading.batch_calls == 0
assert store.active_execution() is None
assert store.signal(signal_id)["notification_state"] == "sent"
```

Expose the existing macOS and Feishu `ChannelNotifier` objects from the fixture
and assert macOS calls remain zero while exactly one Feishu target succeeds.

For the final-intent race, define a local `ChangingThresholdMonitor` subclass
whose first `opportunity()` call returns `_threshold_intent()` and whose second
returns `replace(_threshold_intent(), total_max_cost=Decimal("2.13"),
minimum_profit=Decimal("7.87"))`. Assert the result is
`{"state": "failed", "reason": "opportunity_changed"}`, no notifier is called,
and all submit counters remain zero.

- [ ] **Step 3: Add failing dedupe/retry tests**

Assert one successful Feishu call per signal, no macOS opportunity call, at most three failed calls, no retry after the episode closes, and a new signal ID can notify again.

- [ ] **Step 4: Run tests and confirm they fail**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_notifications.py \
  tests/test_prediction_arbitrage_execution.py \
  tests/test_polymarket_monitor.py \
  -k 'prediction_notification or ready_notification or final_intent or notification_retry' -q
```

Expected: renderer and read-only notification entrypoint are missing.

- [ ] **Step 5: Extract shared read-only preparation from `preview()`**

Add
`_prepare_opportunity(opportunity_id: str) ->
tuple[dict[str, object], ExecutionIntent, dict[str, object]] |
dict[str, object]`. Move the existing breaker, active execution, process-lock
probe, `_fresh_opportunity()`, `_intent_from_opportunity()`,
`_validate_opportunity()`, and `_volatile_checks()` block from `preview()` into
it without changing any returned `state` or `reason`. A successful return is
the `(opportunity, intent, account)` tuple; a failed return is the exact current
API response dictionary. `preview()` consumes the tuple and is solely
responsible for persisting a preview. `notify_ready_opportunity()` consumes the
same tuple but creates neither preview nor execution.

- [ ] **Step 6: Implement final intent proof and Feishu delivery**

For a threshold intent:

1. Check open signal and attempts `< 3`.
2. Run shared current validation.
3. Require `rules_verified_at` and Codex approved.
4. Call `no_submit_threshold_preflight(intent)`.
5. Read the opportunity again and require identical serialized intent, quote age `<= 10`, and positive economics.
6. Render the approved template.
7. Call
   `send_notification_with_results(self._notifier, title, message,
   channels={"feishu", "feishu_app"})`.
8. Persist `sent` only when Feishu reports success; otherwise persist a redacted failure code.

Increment `notification_attempts` in the same store transaction that verifies
the signal is still open, not sent, and below three attempts. This reserves the
attempt before network I/O and prevents two monitor ticks from sending the same
episode concurrently.

- [ ] **Step 7: Wire the monitor observer after both services exist**

Add the keyword
`dashboard_url=f"http://127.0.0.1:{port}/"` to the existing
`PredictionExecutionService` construction in `serve_dashboard()`. Immediately
after that constructor and before startup reconciliation, call
`prediction_monitor.set_ready_observer(
prediction_execution.notify_ready_opportunity)`.

The monitor calls the observer through `asyncio.to_thread()` once a positive, rules-verified, Codex-approved episode needs an attempt. It rechecks the signal before retries.
Keep a single `_notification_task` and its signal ID; reap it before scheduling
another attempt. A closed episode is never retried, and notification completion
does not send a separate close message.

- [ ] **Step 8: Run notification and execution tests**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_notifications.py \
  tests/test_prediction_arbitrage_execution.py \
  tests/test_polymarket_monitor.py \
  tests/test_dashboard_web.py -q
```

Expected: all pass; submit counts remain zero on every observation-mode path.

- [ ] **Step 9: Commit**

```bash
git add src/open_trader/notifications.py \
  src/open_trader/prediction_arbitrage_execution.py \
  src/open_trader/polymarket_monitor.py \
  src/open_trader/dashboard_web.py \
  tests/test_notifications.py \
  tests/test_prediction_arbitrage_execution.py \
  tests/test_polymarket_monitor.py \
  tests/test_dashboard_web.py
git commit -m "feat: notify order-ready prediction opportunities"
```

---

### Task 8: Render the Two-Layer Funnel and Complete Window History

**Files:**
- Modify: `src/open_trader/dashboard_web.py:402-630`
- Modify: `src/open_trader/dashboard_static/dashboard.js:2487-2650`
- Modify: `src/open_trader/dashboard_static/dashboard.css:5705-5805`
- Modify: `tests/test_dashboard_web.py:2210-2520`

**Interfaces:**
- Consumes: `relation_discovery.catalog`, `.activity`, `.codex_queue`, `.websocket`, `.scan_logs`.
- Produces: `predictionRelationFunnel(payload)` and revised `predictionRelationDiscoveryPanel(payload)`.
- Changes: signal history displays initial/peak/final profit, observed duration, end reason, and Feishu status.

- [ ] **Step 1: Add failing API projection tests**

Build a monitor snapshot containing exact funnel numbers and assert `_prediction_state_payload()` preserves counts/times/reasons while dropping wallet addresses, raw rules, prompts, token IDs, and raw errors.

- [ ] **Step 2: Add failing JS funnel tests**

```javascript
const payload = {relation_discovery:{
  catalog:{status:"healthy",events_seen:16058,events_eligible:15980,markets_seen:28000,markets_normalized:27800,threshold_markets:412,relations_discovered:4879,unique_tokens:1989,completed_at:"2026-07-31T12:00:00Z",duration_ms:32495,rejection_counts:{event_ineligible:78,market_unparseable:200}},
  activity:{status:"healthy",relations_considered:4879,tokens_expected:1989,tokens_probed:1989,relations_with_books:3200,relations_with_minimum_depth:2900,relations_within_5pct:341,codex_pending:12,codex_approved:301,codex_rejected:28,subscribed_relations:313,subscribed_tokens:374,positive_candidates:2,order_ready:1,notifications_sent:1,duration_ms:1290,next_scan_at:"2026-07-31T12:01:00Z",rejection_counts:{book_unavailable:1679,minimum_depth:300,cost_limit:0,outside_5pct:2559,codex_rejected:28,codex_unavailable:0,rules_changed:0,readiness_blocked:1}},
  websocket:{status:"connected",subscribed_tokens:374,last_message_age_seconds:0.184},
  codex_queue:{pending:12,inflight:1,oldest_wait_seconds:45},
  scan_logs:[{scope:"activity",status:"completed"}],
  codex_usage_24h:{calls:10,successes:9,failures:1,cache_hits:200},
}};
const html = predictionRelationDiscoveryPanel(payload);
for (const text of ["第一层 · 关系目录","第二层 · 成交候选","16,058","15,980","4,879","341","374","正收益 2","可下单 1","飞书 1","1.29 秒","WebSocket 正常","盘口缺失 1,679"]) {
  if (!html.includes(text)) throw new Error(`missing ${text}`);
}
if (html.includes("内存")) throw new Error("stale in-memory label");
```

Add a `scanning` case that keeps last counts, a `lagging` case, a `degraded`
case, and all three exact empty states from the design:

- `relations_discovered == 0`: “本轮未发现可验证关系”.
- relations exist but `positive_candidates == 0`: render the complete second
  funnel and rejection reasons.
- positive candidates exist but `order_ready == 0`: render each existing
  `eligibility_reason`.

- [ ] **Step 3: Add failing responsive/history tests**

Assert mobile funnel markup has no fixed-width overflow. Assert signal history renders `250 ms`, initial/peak/final profit, `data_unavailable` label, and `飞书已发/发送失败/未发送`.

- [ ] **Step 4: Run tests and confirm they fail**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_dashboard_web.py \
  -k 'relation_funnel or funnel_projection or responsive_funnel or observed_window_history' -q
```

Expected: old single log panel, “内存” label, and incomplete signal history.

- [ ] **Step 5: Implement the smallest live funnel UI**

Render two compact ordered stage grids in the existing left panel. Use the persisted last-completed activity numbers while `scanning`. Add concise status rows for scan duration/next run, Codex queue, and WebSocket age. Keep rejection counts folded under `<details>`.

Remove token IDs from threshold candidate leg rows:

```javascript
`<small>最大成本 ${predictionMoney(leg.max_cost)}</small>`
```

- [ ] **Step 6: Add responsive CSS**

Use CSS grid with `minmax(0, 1fr)` and collapse to one column under the existing mobile breakpoint. Do not add JavaScript layout measurement.

- [ ] **Step 7: Run Dashboard tests**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_dashboard_web.py -q
```

Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add src/open_trader/dashboard_web.py \
  src/open_trader/dashboard_static/dashboard.js \
  src/open_trader/dashboard_static/dashboard.css \
  tests/test_dashboard_web.py
git commit -m "feat: show prediction relation funnels"
```

---

### Task 9: Prove the Real Workflow, Merge, Accept, and Deploy

**Files:**
- Modify: `CHANGELOG.md`
- Modify only if a discovered acceptance regression requires it: relevant source/test file from Tasks 1-8.

**Interfaces:**
- Consumes all prior tasks.
- Produces the final operator evidence: tests, real scan timing, real notifier smoke, unique process ownership, final-SHA acceptance, deployment proof, and screenshots.

- [ ] **Step 1: Run the complete focused prediction suite**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_prediction_arbitrage.py \
  tests/test_polymarket_relation_discovery.py \
  tests/test_polymarket_monitor.py \
  tests/test_polymarket_trading.py \
  tests/test_prediction_arbitrage_store.py \
  tests/test_prediction_arbitrage_execution.py \
  tests/test_prediction_arbitrage_launchd.py \
  tests/test_notification_policy.py \
  tests/test_notifications.py \
  tests/test_dashboard_web.py -q
```

Expected: zero failures. Record the exact passed count.

- [ ] **Step 2: Run a real read-only full catalog and activity scan**

Start the worktree Dashboard with the main runtime config/data on an isolated review port/data directory first. Verify from `/api/dashboard`:

```text
catalog.status=healthy
catalog.relations_discovered>0
activity.status=healthy
activity.relations_considered=catalog.relations_discovered
activity.duration_ms<60000
activity.relations_within_5pct>0
websocket.subscribed_tokens=activity.subscribed_tokens
```

Also verify restart loads the same catalog without another full scan when its age is under 24 hours.

- [ ] **Step 3: Prove no-submit and send one clearly labeled notifier smoke**

Run the existing configured notification command once:

```bash
cd /Users/ray/projects/open_trader
PYTHONPATH=src .venv/bin/python -m open_trader test-notification \
  --config config/daily_premarket.env
```

Expected: Feishu attempt succeeds. This proves channel configuration only; do not manufacture an “order-ready opportunity” message. Inspect the observation-mode test evidence again and confirm all submit call counts are zero.

- [ ] **Step 4: Stop the stale shared-data preview writer**

Resolve listeners before touching them:

```bash
lsof -nP -iTCP:8766 -sTCP:LISTEN
lsof -nP -iTCP:18766 -sTCP:LISTEN
```

Stop port 18766 only if its PID/cwd/command prove it is the old Open Trader preview using the production data directory. Do not kill an unknown process.

- [ ] **Step 5: Add and commit the dated changelog before merge**

Add a `2026-07-31` operator-facing entry covering:

```text
- 修复 Polymarket 官方 SDK 模型导致关系候选恒为 0；关系目录改为每日全量扫描并持久化。
- 新增每分钟 5% 成交候选漏斗、Codex 前置缓存、定向 WebSocket、机会窗口历史，以及“已可下单但观察模式未提交”的飞书通知。
- Dashboard 实时展示两层漏斗、淘汰原因、扫描耗时、Codex 队列和 WebSocket 健康。
```

Commit:

```bash
git add CHANGELOG.md
git commit -m "docs: log Polymarket relation funnel"
```

- [ ] **Step 6: Re-run focused tests on the final branch SHA**

Repeat Step 1 and record:

```bash
git rev-parse HEAD
git status --short
```

Expected: focused tests pass and the branch has no unintended changes.

- [ ] **Step 7: Merge into local `main` without disturbing unrelated files**

Inspect the root worktree first:

```bash
git -C /Users/ray/projects/open_trader status --short
git -C /Users/ray/projects/open_trader branch --show-current
```

Preserve unrelated user changes. Merge only when the root is on `main` and no overlapping dirty files exist:

```bash
git -C /Users/ray/projects/open_trader merge --no-ff \
  fix/polymarket-relation-discovery
```

Record the merge SHA. Do not push unless the user separately authorizes it.

- [ ] **Step 8: Run the final acceptance gate on the merge SHA**

Ensure the main worktree has its configured `.venv`, runtime config, data, and port 8766 ownership, then run:

```bash
cd /Users/ray/projects/open_trader
make acceptance
```

Expected: `PASS`. On `FAIL`, return to the feature worktree, add the failing
acceptance case as a focused regression test, fix and recommit there, merge the
additional commits into `main`, then rerun. Do not patch the dirty/root
worktree directly. On `BLOCKED`, report the blocker and do not substitute curl,
fixtures, or screenshots.

- [ ] **Step 9: Redeploy the exact accepted SHA**

```bash
cd /Users/ray/projects/open_trader
scripts/install_dashboard_launchd.sh \
  --repo-root /Users/ray/projects/open_trader \
  --runtime-root /Users/ray/projects/open_trader \
  --python /Users/ray/projects/open_trader/.venv/bin/python
```

Verify:

```bash
launchctl print "gui/$UID/com.open-trader.dashboard"
lsof -nP -iTCP:8766 -sTCP:LISTEN
curl --fail --silent --show-error -o /dev/null -w '%{http_code}\n' \
  http://127.0.0.1:8766/
git -C /Users/ray/projects/open_trader rev-parse HEAD
tail -n 100 /Users/ray/projects/open_trader/logs/dashboard/launchd.out.log
tail -n 100 /Users/ray/projects/open_trader/logs/dashboard/launchd.err.log
```

The PID cwd and Dashboard runtime SHA must equal `/Users/ray/projects/open_trader` and the accepted merge SHA. Logs must be newer than the PID start.

- [ ] **Step 10: Capture live desktop and mobile screenshots**

Open `http://127.0.0.1:8766/`, select `预测市场` → `LLM对冲套利`, and capture:

- Desktop: both funnel stages, counts, Codex queue, WebSocket health, and candidate area.
- Mobile: the same funnel without horizontal overflow.

The screenshots must come from the live deployed exact accepted SHA and be included inline in the final handoff.

- [ ] **Step 11: Final evidence report**

Report only after all gates pass:

```text
focused tests: copy the final pytest summary verbatim
make acceptance: PASS
accepted/deployed SHA: copy `git rev-parse HEAD`
dashboard PID/cwd: copy the verified listener values
catalog/activity: copy the live API counts and `duration_ms`
Feishu smoke: success
submit calls in observation mode: 0
review URL: http://127.0.0.1:8766/
```

Include desktop and mobile screenshots. State explicitly that real opportunity notifications will occur only when a naturally observed episode reaches complete order-ready status.
