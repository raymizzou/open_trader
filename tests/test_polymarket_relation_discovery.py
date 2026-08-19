from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
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

from open_trader.llm_providers import PROVIDER_IDS, LlmCompletion
from open_trader.polymarket_relation_discovery import (
    RELATION_PROMPT_VERSION,
    LlmRelationValidator,
    RelationActivityAssessment,
    ThresholdRelation,
    ThresholdRelationDiscoveryResult,
    _RELATION_SCHEMA,
    _relation_cache_payload,
    assess_threshold_relation_activity,
    discover_threshold_relation_catalog,
    discover_threshold_relations,
    legacy_relation_cache_keys,
    relation_llm_cache_key,
    threshold_relation_from_payload,
    threshold_relation_payload,
)
from open_trader.prediction_arbitrage import BookLevel, ThresholdOrderBook
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore


RULES = (
    'This market resolves "Yes" if the Binance BTC/USDT close at 12:00 ET '
    "is higher than the price specified in the title. Otherwise it resolves "
    '"No". The resolution source is Binance.'
)


def market(
    market_id: str,
    *,
    question: str,
    rules: str = RULES,
    source: str = "Binance",
    end_date: str = "2026-12-31T17:00:00Z",
    group_item_threshold: str = "0",
    condition_id: str | None = None,
    active: bool = True,
    closed: bool = False,
    accepting_orders: bool = True,
    enable_order_book: bool = True,
    neg_risk: bool = False,
    outcomes: object = '["Yes", "No"]',
    token_ids: object | None = None,
) -> dict[str, object]:
    return {
        "id": market_id,
        "conditionId": condition_id or f"condition-{market_id}",
        "question": question,
        "description": rules,
        "resolutionSource": source,
        "endDate": end_date,
        "groupItemThreshold": group_item_threshold,
        "active": active,
        "closed": closed,
        "acceptingOrders": accepting_orders,
        "enableOrderBook": enable_order_book,
        "negRisk": neg_risk,
        "outcomes": outcomes,
        "clobTokenIds": token_ids or f'["yes-{market_id}", "no-{market_id}"]',
        "feesEnabled": True,
        "feeSchedule": {
            "rate": 0.07,
            "exponent": 1,
            "takerOnly": True,
            "rebateRate": 0.2,
        },
        "orderMinSize": 5,
        "orderPriceMinTickSize": 0.001,
    }


def event(
    *markets: dict[str, object],
    event_id: str = "event-1",
    active: bool = True,
    closed: bool = False,
    ended: bool = False,
    neg_risk: bool = False,
) -> dict[str, object]:
    return {
        "id": event_id,
        "title": "Bitcoin above ___ on December 31?",
        "active": active,
        "closed": closed,
        "ended": ended,
        "negRisk": neg_risk,
        "markets": list(markets),
    }


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
    assert isinstance(result, ThresholdRelationDiscoveryResult)
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


def test_exact_above_template_builds_the_logical_relation_and_buy_legs() -> None:
    relations = discover_threshold_relations(
        [
            event(
                market("lower", question="Will Bitcoin be above $90,000 on December 31?"),
                market("higher", question="Will Bitcoin be above $100,000 on December 31?"),
            )
        ]
    )

    assert len(relations) == 1
    relation = relations[0]
    assert relation.market_a.threshold == Decimal("90000")
    assert relation.market_b.threshold == Decimal("100000")
    assert relation.market_a.operator == ">"
    assert relation.market_b.operator == ">"
    assert relation.relation == "B_IMPLIES_A"
    assert (relation.buy_leg_a.outcome, relation.buy_leg_a.token_id) == (
        "YES",
        "yes-lower",
    )
    assert (relation.buy_leg_b.outcome, relation.buy_leg_b.token_id) == (
        "NO",
        "no-higher",
    )


def test_below_template_reverses_the_outcomes_but_keeps_low_high_order() -> None:
    rules = RULES.replace("higher than", "lower than")
    relation = discover_threshold_relations(
        [
            event(
                market(
                    "lower",
                    question="Will Bitcoin be below $90,000 on December 31?",
                    rules=rules,
                ),
                market(
                    "higher",
                    question="Will Bitcoin be below $100,000 on December 31?",
                    rules=rules,
                ),
            )
        ]
    )[0]

    assert relation.relation == "A_IMPLIES_B"
    assert relation.buy_leg_a.outcome == "NO"
    assert relation.buy_leg_a.token_id == "no-lower"
    assert relation.buy_leg_b.outcome == "YES"
    assert relation.buy_leg_b.token_id == "yes-higher"


def test_group_item_threshold_is_never_used_as_the_economic_threshold() -> None:
    relation = discover_threshold_relations(
        [
            event(
                market(
                    "lower",
                    question="Will Bitcoin be above $90k on December 31?",
                    group_item_threshold="0",
                ),
                market(
                    "higher",
                    question="Will Bitcoin be above $100k on December 31?",
                    group_item_threshold="1",
                ),
            )
        ]
    )[0]

    assert relation.market_a.threshold == Decimal("90000")
    assert relation.market_b.threshold == Decimal("100000")
    assert relation.market_a.group_item_threshold == "0"
    assert relation.market_b.group_item_threshold == "1"


def test_identical_rules_or_rules_differing_only_by_threshold_are_certified() -> None:
    embedded = RULES.replace(
        "the price specified in the title",
        "the stated threshold of $90,000",
    )
    higher = embedded.replace("$90,000", "$100,000")

    identical_rules = discover_threshold_relations(
        [
            event(
                market("lower", question="BTC above $90,000?"),
                market("higher", question="BTC above $100,000?"),
            )
        ]
    )
    threshold_rules = discover_threshold_relations(
        [
            event(
                market("lower", question="BTC above $90,000?", rules=embedded),
                market("higher", question="BTC above $100,000?", rules=higher),
            )
        ]
    )

    assert len(identical_rules) == 1
    assert len(threshold_rules) == 1


@pytest.mark.parametrize(
    "events",
    [
        [
            event(market("lower", question="BTC above $90,000?"), event_id="one"),
            event(market("higher", question="BTC above $100,000?"), event_id="two"),
        ],
        [
            event(
                market("lower", question="BTC above $90,000?"),
                market(
                    "higher",
                    question="BTC above $100,000?",
                    rules=RULES + " A disputed source resolves 50-50.",
                ),
            )
        ],
        [
            event(
                market("lower", question="BTC above $90,000?"),
                market("higher", question="BTC above $100,000?", source="Coinbase"),
            )
        ],
        [
            event(
                market("lower", question="BTC above $90,000?"),
                market(
                    "higher",
                    question="BTC above $100,000?",
                    end_date="2027-01-01T17:00:00Z",
                ),
            )
        ],
        [
            event(
                market("lower", question="Will Bitcoin hit $90,000?"),
                market("higher", question="Will Bitcoin hit $100,000?"),
            )
        ],
        [
            event(
                market("lower", question="BTC above $90,000?", neg_risk=True),
                market("higher", question="BTC above $100,000?"),
            )
        ],
        [
            event(
                market("lower", question="BTC above $90,000?", closed=True),
                market("higher", question="BTC above $100,000?"),
            )
        ],
        [
            event(
                market("lower", question="BTC above $90,000?", outcomes='["Up", "Down"]'),
                market("higher", question="BTC above $100,000?"),
            )
        ],
        [
            event(
                market("lower", question="BTC above $90,000?", token_ids='["same", "same"]'),
                market("higher", question="BTC above $100,000?"),
            )
        ],
    ],
)
def test_non_machine_provable_pairs_are_rejected(
    events: list[dict[str, object]],
) -> None:
    assert discover_threshold_relations(events) == ()


@pytest.mark.parametrize(
    "event_state",
    [
        {"active": False},
        {"closed": True},
        {"ended": True},
        {"neg_risk": True},
    ],
)
def test_ineligible_event_state_rejects_the_whole_family(
    event_state: dict[str, bool],
) -> None:
    value = event(
        market("lower", question="BTC above $90,000?"),
        market("higher", question="BTC above $100,000?"),
        **event_state,
    )

    assert discover_threshold_relations([value]) == ()


def threshold_relation():
    return discover_threshold_relations(
        [
            event(
                market(
                    "lower",
                    question="Will Bitcoin be above $90,000 on December 31?",
                    condition_id="condition-lower",
                ),
                market(
                    "higher",
                    question="Will Bitcoin be above $100,000 on December 31?",
                    condition_id="condition-higher",
                ),
            )
        ]
    )[0]


def relation_variant(index: int) -> ThresholdRelation:
    suffix = f" variant {index}"
    relation = threshold_relation()
    return replace(
        relation,
        market_a=replace(
            relation.market_a, question=relation.market_a.question + suffix
        ),
        market_b=replace(
            relation.market_b, question=relation.market_b.question + suffix
        ),
    )


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


def test_activity_assessment_reports_unknown_fee_before_market_cost() -> None:
    relation = activity_relation()
    relation = replace(
        relation,
        market_a=replace(relation.market_a, fees_enabled=True, fee_rate=None),
    )

    assessment = assess_threshold_relation_activity(
        relation,
        activity_books(relation),
    )

    assert isinstance(assessment, RelationActivityAssessment)
    assert assessment.reason == "fee_unknown"
    assert assessment.intent is None


def test_activity_assessment_reports_invalid_tick_before_depth() -> None:
    relation = activity_relation()
    relation = replace(
        relation,
        market_a=replace(relation.market_a, tick_size=Decimal("0.03")),
    )
    books = activity_books(relation, price_a="0.50")

    assessment = assess_threshold_relation_activity(
        relation,
        books,
    )

    assert assessment.reason == "tick_invalid"
    assert assessment.intent is None


def test_activity_assessment_uses_the_smaller_common_executable_depth() -> None:
    relation = activity_relation()
    assessment = assess_threshold_relation_activity(
        relation,
        activity_books(relation, size_a="10", size_b="20"),
    )

    assert assessment.reason == "eligible"
    assert assessment.intent is not None
    assert assessment.intent.quantity == Decimal("10")


def test_activity_assessment_reports_cost_limit_when_minimum_is_too_expensive() -> None:
    relation = activity_relation()
    relation = replace(
        relation,
        market_a=replace(relation.market_a, minimum_order_size=Decimal("20")),
        market_b=replace(relation.market_b, minimum_order_size=Decimal("20")),
    )

    assessment = assess_threshold_relation_activity(
        relation,
        activity_books(relation, price_a="0.60", price_b="0.60"),
    )

    assert assessment.reason == "cost_limit"
    assert assessment.intent is None


def test_activity_assessment_accepts_exact_twenty_dollar_boundary() -> None:
    assessment = assess_threshold_relation_activity(
        activity_relation(),
        activity_books(activity_relation(), price_a="0.50", price_b="0.50"),
    )

    assert assessment.reason == "eligible"
    assert assessment.intent is not None
    assert assessment.intent.total_max_cost == Decimal("20.00")


@pytest.fixture(autouse=True)
def _isolated_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "OPEN_TRADER_PREDICTION_LLM_PROVIDER",
        "OPEN_TRADER_CODEX_MODEL",
        "OPEN_TRADER_LLM_FALLBACK_MODEL",
        "OPEN_TRADER_DEEPSEEK_MODEL",
        "OPEN_TRADER_ZHIPU_MODEL",
        "DEEPSEEK_API_KEY",
        "ZHIPU_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


def codex_market_result(
    *,
    condition_id: str,
    threshold: str,
) -> dict[str, object]:
    return {
        "condition_id": condition_id,
        "subject": "Bitcoin",
        "metric": "Binance BTC/USDT close",
        "operator": ">",
        "threshold": threshold,
        "unit": "USD",
        "currency": "USD",
        "observation_start": "2026-12-31T17:00:00Z",
        "observation_end": "2026-12-31T17:00:00Z",
        "timezone": "America/New_York",
        "resolution_source": "Binance",
        "special_settlement": None,
    }


def codex_result(*, decision: str = "APPROVE") -> dict[str, object]:
    approved = decision == "APPROVE"
    return {
        "schema_version": 1,
        "decision": decision,
        "relation": "B_IMPLIES_A" if approved else "NONE",
        "market_a": codex_market_result(
            condition_id="condition-lower",
            threshold="90000",
        ),
        "market_b": codex_market_result(
            condition_id="condition-higher",
            threshold="100000",
        ),
        "proof": {
            "excluded_state": "A=NO,B=YES" if approved else None,
            "why_excluded": (
                "A has the lower threshold, so B=YES requires A=YES."
                if approved
                else None
            ),
        },
        "reason_codes": [] if approved else ["AMBIGUOUS_RULES"],
        "summary": (
            "高阈值合约为 YES 时，低阈值合约必为 YES。"
            if approved
            else "完整规则仍有歧义，不能证明蕴含关系。"
        ),
        "evidence": (
            [
                {
                    "market": "A",
                    "field": "metric",
                    "quote": "Binance BTC/USDT close at 12:00 ET",
                },
                {
                    "market": "B",
                    "field": "metric",
                    "quote": "Binance BTC/USDT close at 12:00 ET",
                },
            ]
            if approved
            else []
        ),
        "uncertainties": [] if approved else ["规则存在无法消除的歧义"],
    }


DEFAULT_USAGE: dict[str, int] = {
    "input_tokens": 100,
    "cached_input_tokens": 60,
    "output_tokens": 20,
    "reasoning_output_tokens": 5,
}


def make_completer(
    result: dict[str, object] | None = None,
    *,
    content: str | None = None,
    reason: str | None = None,
    usage: dict[str, int] | None = None,
):
    """Build one injectable completer plus its (system, user) call log."""

    calls: list[tuple[str, str]] = []

    def complete(system: str, user: str) -> LlmCompletion:
        calls.append((system, user))
        if reason is not None:
            return LlmCompletion(None, reason, dict(usage or DEFAULT_USAGE))
        payload = content if content is not None else json.dumps(
            result if result is not None else codex_result()
        )
        return LlmCompletion(payload, None, dict(usage or DEFAULT_USAGE))

    return complete, calls


def all_providers(complete) -> dict[str, object]:
    return {provider: complete for provider in PROVIDER_IDS}


def codex_store(tmp_path: Path) -> PredictionArbitrageStore:
    return PredictionArbitrageStore(tmp_path / "data")


def select_provider(db: PredictionArbitrageStore, provider: str) -> None:
    """Persist a provider selection even when it equals the shipped default.

    set_llm_provider skips the write when the effective selection already
    matches, so selecting the default provider with no row present needs a
    pre-write of another provider first.
    """

    if provider == "deepseek":
        db.set_llm_provider("codex")
    db.set_llm_provider(provider)
    assert db.get_llm_provider() == provider


def test_relation_cache_key_uses_only_versioned_semantic_payload() -> None:
    relation = threshold_relation()
    payload = _relation_cache_payload(relation)
    expected = hashlib.sha256(
        f"shared|{RELATION_PROMPT_VERSION}|{payload}".encode()
    ).hexdigest()
    assert relation_llm_cache_key(relation) == expected
    price_only_change = replace(
        relation,
        market_a=replace(relation.market_a, fee_rate=Decimal("0.99")),
    )
    updated_at_change = replace(
        relation,
        market_a=replace(relation.market_a, updated_at="2026-07-31T12:00:00Z"),
    )
    rules_change = replace(
        relation,
        market_a=replace(relation.market_a, rules=relation.market_a.rules + " Extra."),
    )
    condition_change = replace(
        relation,
        market_a=replace(relation.market_a, condition_id="condition-new"),
    )

    assert relation_llm_cache_key(price_only_change) == expected
    assert relation_llm_cache_key(updated_at_change) == expected
    assert relation_llm_cache_key(rules_change) != expected
    assert relation_llm_cache_key(condition_change) != expected
    assert (
        relation_llm_cache_key(relation, prompt_version="polymarket-threshold-relation-v4")
        != expected
    )


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
    assert relation_llm_cache_key(relation) == relation_llm_cache_key(touched)
    assert relation_llm_cache_key(relation) != relation_llm_cache_key(changed_rules)


def test_legacy_relation_cache_keys_cover_old_model_namespaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relation = threshold_relation()
    payload = _relation_cache_payload(relation)
    legacy = legacy_relation_cache_keys(relation)

    assert legacy == [
        hashlib.sha256(
            f"gpt-5.6-sol{RELATION_PROMPT_VERSION}{payload}".encode()
        ).hexdigest(),
        hashlib.sha256(
            f"deepseek-v4-flash{RELATION_PROMPT_VERSION}{payload}".encode()
        ).hexdigest(),
    ]

    monkeypatch.setenv("OPEN_TRADER_CODEX_MODEL", "gpt-custom")
    monkeypatch.setenv("OPEN_TRADER_LLM_FALLBACK_MODEL", "deepseek-custom")

    assert legacy_relation_cache_keys(relation) == legacy + [
        hashlib.sha256(
            f"gpt-custom{RELATION_PROMPT_VERSION}{payload}".encode()
        ).hexdigest(),
        hashlib.sha256(
            f"deepseek-custom{RELATION_PROMPT_VERSION}{payload}".encode()
        ).hexdigest(),
    ]


def test_current_provider_prefers_store_row_over_env_and_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = codex_store(tmp_path)

    assert LlmRelationValidator(db).current_provider() == "deepseek"

    monkeypatch.setenv("OPEN_TRADER_PREDICTION_LLM_PROVIDER", "codex")
    env_default = LlmRelationValidator(db)
    assert env_default.current_provider() == "codex"

    explicit = LlmRelationValidator(db, default_provider="deepseek")
    assert explicit.current_provider() == "deepseek"

    # The durable store row beats both the env default and the constructor
    # default once it has been written.
    select_provider(db, "deepseek")
    assert env_default.current_provider() == "deepseek"
    assert explicit.current_provider() == "deepseek"
    assert env_default.provider_snapshot() == {
        "selected": "deepseek",
        "models": dict(env_default.models),
        "default": "codex",
        "configured": {"codex": True, "deepseek": False, "zhipu": False},
    }

    with pytest.raises(ValueError, match="invalid llm provider"):
        db.set_llm_provider("unknown")


def test_selected_provider_rejects_unknown_completers_and_models(
    tmp_path: Path,
) -> None:
    db = codex_store(tmp_path)
    with pytest.raises(ValueError, match="completer for deepseek is required"):
        LlmRelationValidator(db, completers={"codex": make_completer()[0]})
    with pytest.raises(ValueError, match="unknown llm provider"):
        LlmRelationValidator(db, models={"unknown": "model"})
    with pytest.raises(ValueError, match="non-negative"):
        LlmRelationValidator(db, max_llm_calls=-1)


def test_llm_approve_uses_selected_engine_and_records_usage(
    tmp_path: Path,
) -> None:
    complete, calls = make_completer(codex_result())
    db = codex_store(tmp_path)
    validator = LlmRelationValidator(
        db,
        models={"codex": "gpt-test"},
        default_provider="codex",
        completers=all_providers(complete),
    )

    result = validator.validate(threshold_relation())

    assert result.status == "approved"
    assert result.decision == "APPROVE"
    assert result.relation == "B_IMPLIES_A"
    assert result.cached is False
    assert result.provider == "codex"
    assert result.model == "gpt-test"
    assert len(calls) == 1
    system, user = calls[0]
    assert "untrusted" in system.lower()
    assert json.loads(user) == {
        "market_a": {
            "condition_id": "condition-lower",
            "question": "Will Bitcoin be above $90,000 on December 31?",
            "rules": RULES,
            "resolution_source": "Binance",
            "end_date": "2026-12-31T17:00:00Z",
            "updated_at": "",
        },
        "market_b": {
            "condition_id": "condition-higher",
            "question": "Will Bitcoin be above $100,000 on December 31?",
            "rules": RULES,
            "resolution_source": "Binance",
            "end_date": "2026-12-31T17:00:00Z",
            "updated_at": "",
        },
    }
    assert db.llm_usage_24h() == {
        "calls": 1,
        "successes": 1,
        "failures": 0,
        "cache_hits": 0,
        "input_tokens": 100,
        "cached_input_tokens": 60,
        "output_tokens": 20,
        "reasoning_output_tokens": 5,
    }
    assert db.llm_usage_24h_by_provider()["codex"] == {
        "calls": 1,
        "successes": 1,
        "failures": 0,
        "cache_hits": 0,
        "input_tokens": 100,
        "cached_input_tokens": 60,
        "output_tokens": 20,
        "reasoning_output_tokens": 5,
    }


def test_llm_prompt_embeds_fixed_output_schema(tmp_path: Path) -> None:
    captured: list[str] = []

    def complete(system: str, user: str) -> LlmCompletion:
        captured.append(system)
        return LlmCompletion(json.dumps(codex_result()), None, dict(DEFAULT_USAGE))

    validator = LlmRelationValidator(
        codex_store(tmp_path),
        default_provider="codex",
        completers=all_providers(complete),
    )

    result = validator.validate(threshold_relation())

    assert result.status == "approved"
    assert captured
    prompt = captured[0]
    schema_text = _RELATION_SCHEMA.read_text(encoding="utf-8")
    assert "OUTPUT JSON SCHEMA" in prompt
    assert "Never include schema meta keys" in prompt
    assert schema_text in prompt


def test_cached_validation_never_invokes_completer(tmp_path: Path) -> None:
    relation = threshold_relation()
    db = codex_store(tmp_path)
    complete, calls = make_completer()
    validator = LlmRelationValidator(
        db, default_provider="codex", completers=all_providers(complete)
    )
    assert validator.validate(relation).status == "approved"
    assert len(calls) == 1

    def must_not_complete(*_args: object) -> LlmCompletion:
        raise AssertionError("persistent cache miss")

    restarted = LlmRelationValidator(
        PredictionArbitrageStore(db.data_dir),
        default_provider="codex",
        completers=all_providers(must_not_complete),
    )

    cached = restarted.validate(relation)

    assert cached.status == "approved"
    assert cached.cached is True
    assert cached.provider == "codex"
    assert db.llm_usage_24h()["calls"] == 1
    # Cache-hit accounting is process-local: a new store instance starts at 0,
    # while the llm_cache row itself persists (verified by cached.cached).
    assert db.llm_usage_24h()["cache_hits"] == 0


def test_cached_validation_restores_durable_verdict_for_monitor_restart(
    tmp_path: Path,
) -> None:
    # The monitor's restart fast path restores durable verdicts through the
    # public single-argument cached_validation(relation).  Shared new-key
    # rows are read directly; legacy provider-namespaced rows are migrated
    # once at process startup by migrate_legacy_validations.  Neither path
    # may re-invoke any completer.
    relation = threshold_relation()
    db = codex_store(tmp_path)
    complete, calls = make_completer()
    validator = LlmRelationValidator(
        db, default_provider="codex", completers=all_providers(complete)
    )
    assert validator.validate(relation).status == "approved"
    assert len(calls) == 1

    def must_not_complete(*_args: object) -> LlmCompletion:
        raise AssertionError("restore path must not call the completer")

    # Direct new-key restore: the shared row written by the previous
    # process is read back without any legacy row or completer call.
    direct_store = PredictionArbitrageStore(db.data_dir)
    direct = LlmRelationValidator(
        direct_store,
        default_provider="codex",
        completers=all_providers(must_not_complete),
    )
    restored = direct.cached_validation(relation)

    assert restored is not None
    assert restored.status == "approved"
    assert restored.cached is True
    assert restored.provider == "codex"
    assert restored.model == direct.models["codex"]
    assert len(calls) == 1
    assert direct_store.llm_usage_24h_by_provider()["codex"]["cache_hits"] == 1

    # Legacy restore: an old provider-namespaced row for another relation is
    # migrated once at startup, then restored through the shared new key.
    legacy_relation = relation_variant(1)
    legacy_key = legacy_relation_cache_keys(legacy_relation)[0]
    db.save_llm_cache(
        legacy_key,
        {
            "provider": "codex",
            "model": "gpt-5.6-sol",
            "prompt_version": RELATION_PROMPT_VERSION,
            "structured_result": codex_result(),
        },
    )
    restarted_store = PredictionArbitrageStore(db.data_dir)
    restarted = LlmRelationValidator(
        restarted_store,
        default_provider="codex",
        completers=all_providers(must_not_complete),
    )
    assert restarted.migrate_legacy_validations([legacy_relation]) == 1
    assert restarted_store.llm_usage_24h_by_provider()["codex"]["cache_hits"] == 1

    cached = restarted.cached_validation(legacy_relation)

    assert cached is not None
    assert cached.status == "approved"
    assert cached.cached is True
    assert cached.provider == "codex"
    assert cached.model == "gpt-5.6-sol"
    assert len(calls) == 1
    assert restarted_store.llm_usage_24h_by_provider()["codex"]["cache_hits"] == 2


def test_cached_validations_batch_lookup_uses_one_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = codex_store(tmp_path)
    relations = [threshold_relation(), relation_variant(1), relation_variant(2)]
    keys = [relation_llm_cache_key(item) for item in relations]
    for key, decision in ((keys[0], "APPROVE"), (keys[1], "REJECT")):
        db.save_llm_cache(
            key,
            {
                "provider": "codex",
                "model": "gpt-5.6-sol",
                "prompt_version": RELATION_PROMPT_VERSION,
                "structured_result": codex_result(decision=decision),
            },
        )
    validator = LlmRelationValidator(
        db, default_provider="codex", completers=all_providers(make_completer()[0])
    )
    per_key: list[str] = []
    batch_calls: list[list[str]] = []
    original_load = db.load_llm_cache
    original_entries = db.load_llm_cache_entries
    monkeypatch.setattr(
        db,
        "load_llm_cache",
        lambda key: per_key.append(key) or original_load(key),
    )
    monkeypatch.setattr(
        db,
        "load_llm_cache_entries",
        lambda cache_keys: batch_calls.append(list(cache_keys)) or original_entries(cache_keys),
    )

    result = validator.cached_validations(relations)

    assert set(result) == {keys[0], keys[1]}
    assert result[keys[0]].status == "approved"
    assert result[keys[1]].status == "llm_rejected"
    assert per_key == []  # zero per-key probes
    assert len(batch_calls) == 1  # exactly one batch call


def test_cached_validation_does_not_probe_legacy_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    relation = threshold_relation()
    db = codex_store(tmp_path)
    legacy_key = legacy_relation_cache_keys(relation)[0]
    db.save_llm_cache(
        legacy_key,
        {
            "provider": "codex",
            "model": "gpt-5.6-sol",
            "prompt_version": RELATION_PROMPT_VERSION,
            "structured_result": codex_result(),
        },
    )
    validator = LlmRelationValidator(
        db, default_provider="codex", completers=all_providers(make_completer()[0])
    )
    new_key = relation_llm_cache_key(relation)
    probed: list[str] = []
    original = db.load_llm_cache
    monkeypatch.setattr(
        db,
        "load_llm_cache",
        lambda key: probed.append(key) or original(key),
    )

    assert validator._cached_validation(relation, new_key) is None
    assert probed == [new_key]  # exactly one probe: the new key only
    assert validator.cached_validations([relation]) == {}


def test_migrate_legacy_validations_migrates_once_then_noops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    relation = threshold_relation()
    monkeypatch.setenv("OPEN_TRADER_CODEX_MODEL", "gpt-custom")
    monkeypatch.setenv("OPEN_TRADER_LLM_FALLBACK_MODEL", "deepseek-custom")
    db = codex_store(tmp_path)
    legacy = legacy_relation_cache_keys(relation)
    assert len(legacy) == 4  # two shipped models plus the two env models
    valid_payload = {
        "provider": "codex",
        "model": "gpt-5.6-sol",
        "prompt_version": RELATION_PROMPT_VERSION,
        "structured_result": codex_result(),
    }
    db.save_llm_cache(legacy[0], valid_payload)
    validator = LlmRelationValidator(
        db, default_provider="codex", completers=all_providers(make_completer()[0])
    )
    new_key = relation_llm_cache_key(relation)
    saves: list[str] = []
    hits: list[str] = []
    original_save = db.save_llm_cache
    original_hit = db.record_llm_cache_hit
    monkeypatch.setattr(
        db,
        "save_llm_cache",
        lambda key, payload: saves.append(key) or original_save(key, payload),
    )
    monkeypatch.setattr(
        db,
        "record_llm_cache_hit",
        lambda *, provider="codex": hits.append(provider) or original_hit(provider=provider),
    )

    assert validator.migrate_legacy_validations([relation]) == 1
    assert saves == [new_key]
    assert hits == ["codex"]

    # Idempotent: a second run in the same process migrates nothing.
    assert validator.migrate_legacy_validations([relation]) == 0
    assert saves == [new_key]  # no second save
    assert hits == ["codex"]  # no second hit
    # The migrated row now resolves through the normal restore path.
    assert validator.cached_validation(relation) is not None


def test_migrate_legacy_validations_skips_invalid_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invalid legacy rows (wrong prompt version / bad structured result) are
    actually read and skipped without any save or cache-hit record."""
    relation = threshold_relation()
    monkeypatch.setenv("OPEN_TRADER_CODEX_MODEL", "gpt-custom")
    monkeypatch.setenv("OPEN_TRADER_LLM_FALLBACK_MODEL", "deepseek-custom")
    db = codex_store(tmp_path)
    legacy = legacy_relation_cache_keys(relation)
    assert len(legacy) == 4  # two shipped models plus the two env models
    valid_payload = {
        "provider": "codex",
        "model": "gpt-5.6-sol",
        "prompt_version": RELATION_PROMPT_VERSION,
        "structured_result": codex_result(),
    }
    # Only invalid rows exist, so every legacy key is probed and skipped.
    db.save_llm_cache(
        legacy[0],
        {**valid_payload, "prompt_version": "polymarket-threshold-relation-v9"},
    )
    # Invalid structured result: must be skipped.
    db.save_llm_cache(
        legacy[1],
        {**valid_payload, "structured_result": {"schema_version": 99}},
    )
    validator = LlmRelationValidator(
        db, default_provider="codex", completers=all_providers(make_completer()[0])
    )
    saves: list[str] = []
    hits: list[str] = []
    original_save = db.save_llm_cache
    original_hit = db.record_llm_cache_hit
    monkeypatch.setattr(
        db,
        "save_llm_cache",
        lambda key, payload: saves.append(key) or original_save(key, payload),
    )
    monkeypatch.setattr(
        db,
        "record_llm_cache_hit",
        lambda *, provider="codex": hits.append(provider) or original_hit(provider=provider),
    )

    assert validator.migrate_legacy_validations([relation]) == 0
    assert saves == []  # no save_llm_cache call
    assert hits == []  # no cache hit recorded
    # Nothing was migrated: the restore path still misses.
    assert validator.cached_validation(relation) is None


def test_llm_reject_is_cached_with_operator_visible_reason(
    tmp_path: Path,
) -> None:
    complete, calls = make_completer(codex_result(decision="REJECT"))
    db = codex_store(tmp_path)
    validator = LlmRelationValidator(
        db, default_provider="codex", completers=all_providers(complete)
    )

    first = validator.validate(threshold_relation())
    second = validator.validate(threshold_relation())

    assert first.status == "llm_rejected"
    assert first.reason_codes == ("AMBIGUOUS_RULES",)
    assert "歧义" in first.summary
    assert first.provider == "codex"
    assert second.cached is True
    assert second.status == "llm_rejected"
    assert len(calls) == 1
    assert db.llm_usage_24h()["calls"] == 1


def test_selected_engine_failure_is_strict_without_fallback(
    tmp_path: Path,
) -> None:
    relation = threshold_relation()
    codex, codex_calls = make_completer(reason="CODEX_FAILED")
    deepseek, deepseek_calls = make_completer()
    zhipu, zhipu_calls = make_completer()
    db = codex_store(tmp_path)
    validator = LlmRelationValidator(
        db,
        default_provider="codex",
        completers={"codex": codex, "deepseek": deepseek, "zhipu": zhipu},
    )

    first = validator.validate(relation)

    assert first.status == "llm_unavailable"
    assert first.reason_codes == ("CODEX_FAILED",)
    assert first.provider == "codex"
    assert first.summary == "Codex 语义校验不可用，当前不可下单。"
    assert len(codex_calls) == 1
    assert deepseek_calls == []
    assert zhipu_calls == []
    assert db.llm_usage_24h_by_provider()["codex"]["failures"] == 1
    assert db.llm_usage_24h_by_provider().get("deepseek", {}).get("calls", 0) == 0

    select_provider(db, "deepseek")

    second = validator.validate(relation)

    assert second.status == "approved"
    assert second.provider == "deepseek"
    assert second.model == "deepseek-v4-flash"
    assert len(deepseek_calls) == 1
    assert len(codex_calls) == 1


def test_failure_verdict_keeps_the_model_of_the_engine_that_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPEN_TRADER_ZHIPU_MODEL", raising=False)
    relation = threshold_relation()
    db = codex_store(tmp_path)
    zhipu_calls: list[tuple[str, str]] = []

    def zhipu_fails(system: str, user: str) -> LlmCompletion:
        zhipu_calls.append((system, user))
        # The engine is switched while the zhipu call is in flight, exactly
        # as an operator dashboard click would interleave. The failure
        # verdict must still carry zhipu's model next to zhipu's reason.
        db.set_llm_provider("codex")
        return LlmCompletion(None, "ZHIPU_HTTP_ERROR", dict(DEFAULT_USAGE))

    validator = LlmRelationValidator(
        db,
        default_provider="zhipu",
        completers={
            "codex": make_completer()[0],
            "deepseek": make_completer()[0],
            "zhipu": zhipu_fails,
        },
    )

    result = validator.validate(relation)

    assert result.status == "llm_unavailable"
    assert result.provider == "zhipu"
    assert result.model == "glm-5"
    assert result.reason_codes == ("ZHIPU_HTTP_ERROR",)
    assert len(zhipu_calls) == 1
    assert db.get_llm_provider() == "codex"  # the mid-flight switch did happen


def test_approved_verdict_is_shared_across_engines(tmp_path: Path) -> None:
    relation = threshold_relation()
    codex, codex_calls = make_completer()
    zhipu, zhipu_calls = make_completer()
    db = codex_store(tmp_path)
    validator = LlmRelationValidator(
        db,
        default_provider="codex",
        completers={"codex": codex, "deepseek": codex, "zhipu": zhipu},
    )

    assert validator.validate(relation).status == "approved"

    select_provider(db, "zhipu")
    second = validator.validate(relation)

    assert second.status == "approved"
    assert second.cached is True
    assert second.provider == "codex"  # verdict keeps its producing provider
    assert len(codex_calls) == 1
    assert zhipu_calls == []
    assert db.llm_usage_24h()["cache_hits"] == 1
    assert db.llm_usage_24h_by_provider()["codex"]["cache_hits"] == 1


def test_legacy_cache_row_is_migrated_to_the_shared_key(tmp_path: Path) -> None:
    relation = threshold_relation()
    legacy_model = "gpt-5.6-sol"
    payload = _relation_cache_payload(relation)
    legacy_key = hashlib.sha256(
        f"{legacy_model}{RELATION_PROMPT_VERSION}{payload}".encode()
    ).hexdigest()
    db = codex_store(tmp_path)
    db.save_llm_cache(
        legacy_key,
        {
            "model": legacy_model,
            "prompt_version": RELATION_PROMPT_VERSION,
            "structured_result": codex_result(),
        },
    )

    def must_not_complete(*_args: object) -> LlmCompletion:
        raise AssertionError("legacy cache row must be reused")

    validator = LlmRelationValidator(
        db,
        default_provider="zhipu",
        completers=all_providers(must_not_complete),
    )

    assert validator.migrate_legacy_validations([relation]) == 1
    result = validator.validate(relation)

    assert result.status == "approved"
    assert result.cached is True
    assert result.provider == "codex"  # inferred from the legacy model name
    shared_key = relation_llm_cache_key(relation)
    assert db.load_llm_cache(shared_key) == {
        "model": legacy_model,
        "prompt_version": RELATION_PROMPT_VERSION,
        "structured_result": codex_result(),
    }
    assert db.llm_usage_24h()["cache_hits"] == 2  # migrate plus the restore read
    assert db.llm_usage_24h()["calls"] == 0


def test_circuit_breaker_is_independent_per_provider(tmp_path: Path) -> None:
    codex, codex_calls = make_completer(reason="CODEX_TIMEOUT")
    deepseek, deepseek_calls = make_completer(reason="DEEPSEEK_TIMEOUT")
    db = codex_store(tmp_path)
    validator = LlmRelationValidator(
        db,
        default_provider="codex",
        completers={"codex": codex, "deepseek": deepseek, "zhipu": codex},
    )

    for _ in range(3):
        result = validator.validate(threshold_relation())
        assert result.status == "llm_unavailable"
        assert result.reason_codes == ("CODEX_TIMEOUT",)
    assert len(codex_calls) == 3

    open_circuit = validator.validate(threshold_relation())

    assert open_circuit.status == "llm_unavailable"
    assert open_circuit.reason_codes == ("CODEX_CIRCUIT_OPEN",)
    assert open_circuit.summary == "Codex 连续失败已临时熔断，暂停新校验，稍后自动恢复。"
    assert len(codex_calls) == 3

    select_provider(db, "deepseek")
    switched = validator.validate(threshold_relation())

    assert switched.reason_codes == ("DEEPSEEK_TIMEOUT",)
    assert len(deepseek_calls) == 1


def test_budget_exhaustion_only_limits_the_current_engine(tmp_path: Path) -> None:
    complete, calls = make_completer()
    db = codex_store(tmp_path)
    validator = LlmRelationValidator(
        db,
        default_provider="codex",
        completers=all_providers(complete),
        max_llm_calls=2,
    )

    first = validator.validate(relation_variant(0))
    second = validator.validate(relation_variant(1))
    exhausted = validator.validate(relation_variant(2))

    assert first.status == second.status == "approved"
    assert validator.llm_calls == 2
    assert validator.llm_successes == 2
    assert exhausted.status == "llm_unavailable"
    assert exhausted.reason_codes == ("CODEX_BUDGET_EXHAUSTED",)
    assert exhausted.summary == "Codex 校验额度已耗尽，当前不可下单。"
    assert len(calls) == 2

    # The shared budget still gates the switched engine: there is no
    # cross-engine escape hatch, and the reason code carries the new engine.
    select_provider(db, "zhipu")
    switched = validator.validate(relation_variant(3))

    assert switched.status == "llm_unavailable"
    assert switched.reason_codes == ("ZHIPU_BUDGET_EXHAUSTED",)
    assert switched.provider == "zhipu"
    assert len(calls) == 2


def test_budget_is_not_consumed_by_cache_hits(tmp_path: Path) -> None:
    relation = threshold_relation()
    complete, calls = make_completer()
    db = codex_store(tmp_path)
    seeded = LlmRelationValidator(
        db, default_provider="codex", completers=all_providers(complete)
    )
    assert seeded.validate(relation).status == "approved"

    validator = LlmRelationValidator(
        db,
        default_provider="codex",
        completers=all_providers(complete),
        max_llm_calls=0,
    )

    cached = validator.validate(relation)
    exhausted = validator.validate(relation_variant(1))

    assert cached.status == "approved"
    assert cached.cached is True
    assert exhausted.reason_codes == ("CODEX_BUDGET_EXHAUSTED",)
    assert len(calls) == 1
    assert validator.llm_calls == 0


@pytest.mark.parametrize(
    ("reason", "content"),
    [
        ("CODEX_TIMEOUT", None),
        ("ZHIPU_HTTP_ERROR", None),
        ("DEEPSEEK_AUTH_FAILED", None),
        ("CODEX_FAILED", None),
        (None, "not json"),
        (None, json.dumps({**codex_result(), "unknown": True})),
    ],
)
def test_unavailable_results_are_not_cached(
    tmp_path: Path, reason: str | None, content: str | None
) -> None:
    complete, calls = make_completer(
        content=content if content is not None else None,
        reason=reason,
    )
    db = codex_store(tmp_path)
    provider = (reason or "CODEX").split("_")[0].lower()
    validator = LlmRelationValidator(
        db, default_provider=provider, completers=all_providers(complete)
    )

    first = validator.validate(threshold_relation())
    second = validator.validate(threshold_relation())

    expected = reason or "CODEX_OUTPUT_INVALID"
    assert first.status == second.status == "llm_unavailable"
    assert first.reason_codes == (expected,)
    assert first.provider == provider
    assert len(calls) == 2
    assert db.llm_usage_24h()["failures"] == 2
    assert db.llm_usage_24h()["cache_hits"] == 0


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (
            lambda value: {
                **value,
                "market_a": {
                    **value["market_a"],
                    "condition_id": "forged-condition",
                },
            },
            "CONDITION_ID_MISMATCH",
        ),
        (
            lambda value: {
                **value,
                "evidence": [
                    {
                        "market": "A",
                        "field": "rules",
                        "quote": "text not present in either rule",
                    },
                    value["evidence"][1],
                ],
            },
            "EVIDENCE_NOT_FOUND",
        ),
        (
            lambda value: {
                **value,
                "uncertainties": ["仍可能存在特殊结算"],
            },
            "UNRESOLVED_UNCERTAINTY",
        ),
        (
            lambda value: {**value, "relation": "A_IMPLIES_B"},
            "RELATION_MISMATCH",
        ),
    ],
)
def test_llm_approve_must_pass_deterministic_post_validation(
    tmp_path: Path,
    mutate,
    reason: str,
) -> None:
    structured = mutate(codex_result())
    complete, calls = make_completer(structured)
    validator = LlmRelationValidator(
        codex_store(tmp_path),
        default_provider="codex",
        completers=all_providers(complete),
    )
    first = validator.validate(threshold_relation())
    second = validator.validate(threshold_relation())

    assert first.status == second.status == "deterministic_rejected"
    assert first.reason_codes == (reason,)
    assert len(calls) == 2
    assert validator.store.llm_usage_24h()["cache_hits"] == 0


def test_structured_result_violation_classifies_schema_deviations() -> None:
    from open_trader.polymarket_relation_discovery import (
        _relation_audit_prompt,
        _structured_result_violation,
        _valid_structured_result,
    )

    good = codex_result()
    assert _valid_structured_result(good)
    assert _structured_result_violation(good) is None

    def mutate(**overrides):
        bad = json.loads(json.dumps(good))
        bad.update(overrides)
        return bad

    assert _structured_result_violation(mutate(extra_key="x")) == "top_level_keys"
    assert _structured_result_violation(mutate(schema_version="1")) == "schema_version"
    assert (
        _structured_result_violation(mutate(reason_codes=["NOT_A_CODE"]))
        == "reason_code_enum:NOT_A_CODE"
    )
    reject = mutate(decision="REJECT")
    reject["reason_codes"] = []
    assert _structured_result_violation(reject) == "reject_without_reasons"
    evidence_bad = json.loads(json.dumps(good))
    evidence_bad["evidence"][0]["note"] = "extra"
    assert _structured_result_violation(evidence_bad) == "evidence_row:keys"
    proof_bad = json.loads(json.dumps(good))
    proof_bad["proof"]["extra"] = 1
    assert _structured_result_violation(proof_bad) == "proof_keys"


def test_output_invalid_logs_violation_and_raw_head(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from open_trader.polymarket_relation_discovery import _structured_result_violation

    def invalid(prompt: str, payload: object) -> LlmCompletion:
        return LlmCompletion('{"unexpected": true}', None, {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 3, "reasoning_output_tokens": 0})

    validator = LlmRelationValidator(
        codex_store(tmp_path),
        default_provider="codex",
        completers=all_providers(invalid),
    )
    with caplog.at_level("WARNING", logger="open_trader.polymarket_relation_discovery"):
        result = validator.validate(threshold_relation())

    assert result.status == "llm_unavailable"
    assert result.reason_codes == ("CODEX_OUTPUT_INVALID",)
    record = next(r for r in caplog.records if "llm_output_invalid" in r.message)
    assert "provider=codex" in record.message
    assert "violation=top_level_keys" in record.message
    assert "unexpected" in record.message


def test_relation_prompt_states_output_contract() -> None:
    from open_trader.polymarket_relation_discovery import _relation_audit_prompt

    prompt = _relation_audit_prompt()
    assert "OUTPUT CONTRACT" in prompt
    assert "schema_version must be the JSON number 1" in prompt
    assert "EXACTLY these top-level keys" in prompt
    for key in (
        "schema_version",
        "decision",
        "relation",
        "market_a",
        "market_b",
        "proof",
        "reason_codes",
        "summary",
        "evidence",
        "uncertainties",
    ):
        assert key in prompt
