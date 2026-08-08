from __future__ import annotations

import hashlib
import json
import subprocess
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

from open_trader.polymarket_relation_discovery import (
    CODEX_PROMPT_VERSION,
    _CODEX_SCHEMA,
    _deepseek_completion,
    CodexRelationValidator,
    RelationActivityAssessment,
    ThresholdRelation,
    ThresholdRelationDiscoveryResult,
    assess_threshold_relation_activity,
    codex_relation_cache_key,
    discover_threshold_relation_catalog,
    discover_threshold_relations,
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

    assessment = assess_threshold_relation_activity(relation, books)

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


def codex_jsonl(
    result: dict[str, object],
    *,
    usage: dict[str, int] | None = None,
) -> str:
    return "\n".join(
        (
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": json.dumps(result),
                    },
                }
            ),
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": usage
                    or {
                        "input_tokens": 100,
                        "cached_input_tokens": 60,
                        "output_tokens": 20,
                        "reasoning_output_tokens": 5,
                    },
                }
            ),
        )
    )


def codex_store(tmp_path: Path) -> PredictionArbitrageStore:
    return PredictionArbitrageStore(tmp_path / "data")


def test_codex_fingerprint_uses_only_versioned_semantic_payload() -> None:
    relation = threshold_relation()
    expected = codex_relation_cache_key(
        relation,
        model="gpt-test",
        prompt_version=CODEX_PROMPT_VERSION,
    )
    payload = {
        "market_a": {
            "condition_id": "condition-lower",
            "question": "Will Bitcoin be above $90,000 on December 31?",
            "rules": RULES,
            "resolution_source": "Binance",
            "end_date": "2026-12-31T17:00:00Z",
        },
        "market_b": {
            "condition_id": "condition-higher",
            "question": "Will Bitcoin be above $100,000 on December 31?",
            "rules": RULES,
            "resolution_source": "Binance",
            "end_date": "2026-12-31T17:00:00Z",
        },
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert expected == hashlib.sha256(
        f"gpt-test{CODEX_PROMPT_VERSION}{canonical}".encode()
    ).hexdigest()
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

    assert (
        codex_relation_cache_key(
            price_only_change,
            model="gpt-test",
            prompt_version=CODEX_PROMPT_VERSION,
        )
        == expected
    )
    assert (
        codex_relation_cache_key(
            updated_at_change,
            model="gpt-test",
            prompt_version=CODEX_PROMPT_VERSION,
        )
        == expected
    )
    assert (
        codex_relation_cache_key(
            rules_change,
            model="gpt-test",
            prompt_version=CODEX_PROMPT_VERSION,
        )
        != expected
    )
    assert (
        codex_relation_cache_key(
            condition_change,
            model="gpt-test",
            prompt_version=CODEX_PROMPT_VERSION,
        )
        != expected
    )
    assert (
        codex_relation_cache_key(
            relation,
            model="different-model",
            prompt_version=CODEX_PROMPT_VERSION,
        )
        != expected
    )
    assert (
        codex_relation_cache_key(
            relation,
            model="gpt-test",
            prompt_version="polymarket-threshold-relation-v4",
        )
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


def test_codex_prompt_embeds_fixed_output_schema(tmp_path: Path) -> None:
    relation = threshold_relation()
    captured: list[str] = []

    def runner(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        captured.append(str(kwargs.get("input") or ""))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=codex_jsonl(codex_result()),
            stderr="",
        )

    validator = CodexRelationValidator(
        codex_store(tmp_path), model="gpt-test", runner=runner
    )

    result = validator.validate(relation)

    assert result.status == "approved"
    assert captured
    prompt = captured[0]
    schema_text = _CODEX_SCHEMA.read_text(encoding="utf-8")
    assert "OUTPUT JSON SCHEMA" in prompt
    assert "Never include schema meta keys" in prompt
    assert schema_text in prompt
    assert "INPUT JSON" in prompt


def test_codex_approve_uses_isolated_structured_command_and_records_usage(
    tmp_path: Path,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def runner(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=codex_jsonl(codex_result()),
            stderr="",
        )

    db = codex_store(tmp_path)
    result = CodexRelationValidator(
        db,
        model="gpt-test",
        runner=runner,
    ).validate(threshold_relation())

    assert result.status == "approved"
    assert result.decision == "APPROVE"
    assert result.relation == "B_IMPLIES_A"
    assert result.cached is False
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command[:2] == ["codex", "exec"]
    assert "--ephemeral" in command
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert "--ignore-user-config" in command
    assert "--ignore-rules" in command
    assert "--output-schema" in command
    assert "--json" in command
    assert kwargs["input"].endswith("\n")
    assert kwargs["text"] is True
    assert kwargs["capture_output"] is True
    assert kwargs["timeout"] == 45.0
    assert Path(str(kwargs["cwd"])) != Path.cwd()
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


def test_codex_cache_survives_validator_restart_without_new_process(
    tmp_path: Path,
) -> None:
    db = codex_store(tmp_path)
    relation = threshold_relation()
    first = CodexRelationValidator(
        db,
        model="gpt-test",
        runner=lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout=codex_jsonl(codex_result()),
            stderr="",
        ),
    ).validate(relation)

    def must_not_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("persistent cache miss")

    second = CodexRelationValidator(
        PredictionArbitrageStore(db.data_dir),
        model="gpt-test",
        runner=must_not_run,
    ).validate(relation)

    assert first.status == second.status == "approved"
    assert second.cached is True
    assert db.llm_usage_24h()["calls"] == 1
    # Cache-hit accounting is process-local: a new store instance starts at 0,
    # while llm_cache itself persists (verified by second.cached is True).
    assert db.llm_usage_24h()["cache_hits"] == 0


def test_codex_reject_is_cached_with_operator_visible_reason(
    tmp_path: Path,
) -> None:
    db = codex_store(tmp_path)
    relation = threshold_relation()
    runner = lambda command, **kwargs: subprocess.CompletedProcess(
        command,
        0,
        stdout=codex_jsonl(codex_result(decision="REJECT")),
        stderr="",
    )

    first = CodexRelationValidator(
        db, model="gpt-test", runner=runner
    ).validate(relation)
    second = CodexRelationValidator(
        db, model="gpt-test", runner=runner
    ).validate(relation)

    assert first.status == "llm_rejected"
    assert first.reason_codes == ("AMBIGUOUS_RULES",)
    assert "歧义" in first.summary
    assert second.cached is True
    assert db.llm_usage_24h()["calls"] == 1


def test_codex_failure_falls_back_to_deepseek_and_caches_by_fallback_model(
    tmp_path: Path,
) -> None:
    relation = threshold_relation()
    fallback_calls: list[tuple[str, dict[str, object]]] = []
    runner_calls = 0
    db = codex_store(tmp_path)

    def runner(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        nonlocal runner_calls
        runner_calls += 1
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="401")

    validator = CodexRelationValidator(
        db,
        model="gpt-test",
        fallback_model="deepseek-v4-flash-max",
        runner=runner,
        fallback=lambda prompt, payload: (
            (
                fallback_calls.append((prompt, dict(payload)))
                or json.dumps(codex_result()),
                None,
            )
        ),
    )

    first = validator.validate(relation)

    assert first.status == "approved"
    assert first.model == "deepseek-v4-flash-max"
    assert first.cached is False
    assert len(fallback_calls) == 1
    assert db.llm_usage_24h()["failures"] == 1
    assert db.llm_usage_24h()["successes"] == 1
    assert db.llm_usage_24h_by_provider()["deepseek"]["successes"] == 1
    assert db.llm_usage_24h_by_provider()["codex"]["failures"] == 1

    fallback_calls.clear()
    second = validator.validate(relation)

    assert second.status == "approved"
    assert second.cached is True
    assert second.model == "deepseek-v4-flash-max"
    assert fallback_calls == []
    assert runner_calls == 2
    assert db.llm_usage_24h()["cache_hits"] == 1


def test_codex_circuit_breaker_skips_codex_after_repeated_failures(
    tmp_path: Path,
) -> None:
    relation = threshold_relation()
    runner_calls = 0
    db = codex_store(tmp_path)

    def runner(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        nonlocal runner_calls
        runner_calls += 1
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="401")

    validator = CodexRelationValidator(
        db,
        model="gpt-test",
        fallback_model="deepseek-v4-flash-max",
        runner=runner,
        fallback=lambda prompt, payload: (json.dumps(codex_result()), None),
    )

    for _ in range(3):
        assert validator.validate(relation).status == "approved"
    assert runner_calls == 3

    assert validator.validate(relation).status == "approved"
    assert runner_calls == 3  # circuit open: Codex skipped, DeepSeek cache hit


def test_deepseek_completion_missing_key_reports_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")

    content, reason = _deepseek_completion(
        "prompt", {}, model="deepseek-v4-flash"
    )

    assert content is None
    assert reason == "DEEPSEEK_KEY_MISSING"


def test_deepseek_completion_retries_once_on_empty_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openai
    from types import SimpleNamespace

    calls = {"n": 0}

    class FakeOpenAI:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        @property
        def chat(self) -> "FakeOpenAI":
            return self

        @property
        def completions(self) -> "FakeOpenAI":
            return self

        def create(self, **kwargs: object) -> SimpleNamespace:
            calls["n"] += 1
            content = None if calls["n"] == 1 else '{"ok": true}'
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(message=SimpleNamespace(content=content))
                ]
            )

    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")

    content, reason = _deepseek_completion(
        "prompt", {}, model="deepseek-v4-flash"
    )

    assert content == '{"ok": true}'
    assert reason is None
    assert calls["n"] == 2


def test_deepseek_completion_reports_empty_after_single_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openai
    from types import SimpleNamespace

    calls = {"n": 0}

    class FakeOpenAI:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        @property
        def chat(self) -> "FakeOpenAI":
            return self

        @property
        def completions(self) -> "FakeOpenAI":
            return self

        def create(self, **kwargs: object) -> SimpleNamespace:
            calls["n"] += 1
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(message=SimpleNamespace(content=None))
                ]
            )

    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")

    content, reason = _deepseek_completion(
        "prompt", {}, model="deepseek-v4-flash"
    )

    assert content is None
    assert reason == "DEEPSEEK_EMPTY_CONTENT"
    assert calls["n"] == 2


def test_deepseek_failure_reason_propagates_to_validation(tmp_path: Path) -> None:
    db = codex_store(tmp_path)
    validator = CodexRelationValidator(
        db,
        model="gpt-test",
        fallback_model="deepseek-v4-flash-max",
        runner=lambda command, **kwargs: subprocess.CompletedProcess(
            command, 1, stdout="", stderr="401"
        ),
        fallback=lambda prompt, payload: (None, "DEEPSEEK_KEY_MISSING"),
    )

    result = validator.validate(threshold_relation())

    assert result.status == "llm_unavailable"
    assert result.reason_codes == ("CODEX_FAILED", "DEEPSEEK_KEY_MISSING")


@pytest.mark.parametrize(
    ("response", "expected_reason"),
    [
        (subprocess.TimeoutExpired(["codex"], 45), "CODEX_TIMEOUT"),
        (
            subprocess.CompletedProcess(["codex"], 1, stdout="", stderr="secret"),
            "CODEX_FAILED",
        ),
        (
            subprocess.CompletedProcess(
                ["codex"],
                0,
                stdout=json.dumps({"type": "turn.completed", "usage": {}}),
                stderr="",
            ),
            "CODEX_OUTPUT_INVALID",
        ),
        (
            subprocess.CompletedProcess(
                ["codex"],
                0,
                stdout=codex_jsonl({**codex_result(), "unknown": True}),
                stderr="",
            ),
            "CODEX_OUTPUT_INVALID",
        ),
    ],
)
def test_codex_unavailable_results_are_not_cached(
    tmp_path: Path,
    response: object,
    expected_reason: str,
) -> None:
    calls = 0

    def runner(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        if isinstance(response, BaseException):
            raise response
        assert isinstance(response, subprocess.CompletedProcess)
        return response

    validator = CodexRelationValidator(
        codex_store(tmp_path),
        model="gpt-test",
        runner=runner,
        fallback=lambda prompt, payload: (None, "DEEPSEEK_FAILED"),
    )
    first = validator.validate(threshold_relation())
    second = validator.validate(threshold_relation())

    assert first.status == second.status == "llm_unavailable"
    assert first.reason_codes == (expected_reason, "DEEPSEEK_FAILED")
    assert calls == 2
    assert validator.store.llm_usage_24h()["failures"] == 4
    assert validator.store.llm_usage_24h()["cache_hits"] == 0


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
def test_codex_approve_must_pass_deterministic_post_validation(
    tmp_path: Path,
    mutate,
    reason: str,
) -> None:
    structured = mutate(codex_result())
    calls = 0

    def runner(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=codex_jsonl(structured),
            stderr="",
        )

    validator = CodexRelationValidator(
        codex_store(tmp_path),
        model="gpt-test",
        runner=runner,
    )
    first = validator.validate(threshold_relation())
    second = validator.validate(threshold_relation())

    assert first.status == second.status == "deterministic_rejected"
    assert first.reason_codes == (reason,)
    assert calls == 2
    assert validator.store.llm_usage_24h()["cache_hits"] == 0
