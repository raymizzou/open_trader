from __future__ import annotations

from decimal import Decimal

import pytest

from open_trader.polymarket_relation_discovery import discover_threshold_relations


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
