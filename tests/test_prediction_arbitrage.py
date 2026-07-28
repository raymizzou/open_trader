from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from open_trader.prediction_arbitrage import (
    MAX_EMERGENCY_LOSS,
    PROTECTED_BUY_SHARE_PRECISION,
    BookLevel,
    ConfirmedBooks,
    MarketFacts,
    build_pair_intent,
    estimated_unwind_loss,
    monitored_event_sort_key,
)


def market_facts(
    *,
    minimum_order_size: str = "1",
    tick_size: str = "0.01",
    fee_verified_zero: bool = True,
    neg_risk: bool = False,
    volume_24h: str = "100",
) -> MarketFacts:
    return MarketFacts(
        event_id="event-1",
        market_id="market-1",
        condition_id="condition-1",
        slug="market-1",
        question="Will it happen?",
        volume_24h=Decimal(volume_24h),
        minimum_order_size=Decimal(minimum_order_size),
        tick_size=Decimal(tick_size),
        fee_verified_zero=fee_verified_zero,
        neg_risk=neg_risk,
    )


def confirmed_books(
    *,
    yes: list[tuple[str, str]],
    no: list[tuple[str, str]],
) -> ConfirmedBooks:
    return ConfirmedBooks(
        yes_token_id="yes-token",
        no_token_id="no-token",
        yes_asks=tuple(BookLevel(Decimal(price), Decimal(size)) for price, size in yes),
        no_asks=tuple(BookLevel(Decimal(price), Decimal(size)) for price, size in no),
        confirmed_at=datetime.now(UTC),
    )


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


def test_largest_common_requested_amount_uses_cent_spends_and_both_depths() -> None:
    facts = market_facts(minimum_order_size="1")
    books = confirmed_books(
        yes=[("0.40", "5"), ("0.45", "15")],
        no=[("0.48", "20")],
    )

    intent = build_pair_intent(
        facts, books, balance=Decimal("50"), allowance=Decimal("50")
    )

    assert intent is not None
    assert intent.quantity == Decimal("20.00")
    assert intent.yes_max_price == Decimal("0.45")
    assert intent.no_max_price == Decimal("0.48")
    assert intent.yes_max_cost == Decimal("9.00")
    assert intent.no_max_cost == Decimal("9.60")


def test_worst_ask_price_is_the_price_cap_for_full_depth() -> None:
    facts = market_facts(minimum_order_size="1")
    books = confirmed_books(
        yes=[("0.40", "10"), ("0.41", "10")],
        no=[("0.45", "20")],
    )

    intent = build_pair_intent(
        facts, books, balance=Decimal("50"), allowance=Decimal("50")
    )

    assert intent is not None
    assert intent.quantity == Decimal("20")
    assert intent.yes_max_price == Decimal("0.41")
    assert intent.no_max_price == Decimal("0.45")


@pytest.mark.parametrize("tick_size", tuple(PROTECTED_BUY_SHARE_PRECISION))
def test_supported_tick_sizes_use_pinned_protected_buy_precision(
    tick_size: Decimal,
) -> None:
    facts = market_facts(minimum_order_size="1", tick_size=str(tick_size))
    price = tick_size
    books = confirmed_books(
        yes=[(str(price), "100")],
        no=[(str(price), "100")],
    )

    intent = build_pair_intent(
        facts, books, balance=Decimal("50"), allowance=Decimal("50")
    )

    assert intent is not None
    assert -intent.quantity.as_tuple().exponent <= PROTECTED_BUY_SHARE_PRECISION[tick_size]


def test_unsupported_tick_size_is_rejected() -> None:
    facts = market_facts(tick_size="0.03")
    books = confirmed_books(yes=[("0.45", "20")], no=[("0.48", "20")])

    assert build_pair_intent(facts, books, balance=Decimal("50"), allowance=Decimal("50")) is None


def test_signaling_nan_tick_size_fails_closed() -> None:
    facts = market_facts(tick_size="sNaN")
    books = confirmed_books(yes=[("0.45", "20")], no=[("0.48", "20")])

    assert build_pair_intent(facts, books, balance=Decimal("50"), allowance=Decimal("50")) is None


def test_threshold_equality_is_accepted() -> None:
    facts = market_facts(minimum_order_size="1")
    books = confirmed_books(yes=[("0.09", "100")], no=[("0.10", "100")])

    intent = build_pair_intent(
        facts, books, balance=Decimal("50"), allowance=Decimal("50")
    )

    assert intent is not None
    assert intent.net_edge >= Decimal("0.01")
    assert intent.minimum_profit >= Decimal("1.00")


@pytest.mark.parametrize(
    ("yes_price", "no_price"),
    (("0.496", "0.495"), ("0.49", "0.511")),
)
def test_edge_or_profit_below_threshold_is_rejected(
    yes_price: str,
    no_price: str,
) -> None:
    facts = market_facts(minimum_order_size="1", tick_size="0.001")
    books = confirmed_books(
        yes=[(yes_price, "100")], no=[(no_price, "100")]
    )

    assert build_pair_intent(facts, books, balance=Decimal("50"), allowance=Decimal("50")) is None


def test_balance_or_allowance_below_total_cost_is_rejected() -> None:
    facts = market_facts(minimum_order_size="20")
    books = confirmed_books(yes=[("0.45", "20")], no=[("0.48", "20")])

    assert build_pair_intent(facts, books, balance=Decimal("18.59"), allowance=Decimal("50")) is None
    assert build_pair_intent(facts, books, balance=Decimal("50"), allowance=Decimal("18.59")) is None


@pytest.mark.parametrize(
    ("balance", "accepted"),
    (("65.00", True), ("65.01", False)),
)
def test_wallet_funding_cap_boundary(balance: str, accepted: bool) -> None:
    facts = market_facts(minimum_order_size="5")
    books = confirmed_books(yes=[("0.45", "20")], no=[("0.48", "20")])

    intent = build_pair_intent(
        facts,
        books,
        balance=Decimal(balance),
        allowance=Decimal("65.00"),
    )

    assert (intent is not None) is accepted


def test_minimum_size_and_book_tick_grid_violations_are_rejected() -> None:
    facts = market_facts(minimum_order_size="21")
    books = confirmed_books(yes=[("0.45", "20")], no=[("0.48", "20")])
    assert build_pair_intent(facts, books, balance=Decimal("50"), allowance=Decimal("50")) is None

    facts = market_facts(minimum_order_size="1", tick_size="0.01")
    books = confirmed_books(yes=[("0.451", "20")], no=[("0.48", "20")])
    assert build_pair_intent(facts, books, balance=Decimal("50"), allowance=Decimal("50")) is None


@pytest.mark.parametrize(
    "facts",
    (
        market_facts(fee_verified_zero=False),
        market_facts(neg_risk=True),
    ),
)
def test_fee_unknown_or_neg_risk_markets_are_monitor_only(
    facts: MarketFacts,
) -> None:
    books = confirmed_books(yes=[("0.45", "20")], no=[("0.48", "20")])
    assert build_pair_intent(facts, books, balance=Decimal("50"), allowance=Decimal("50")) is None


@pytest.mark.parametrize(
    "bad_facts",
    (
        market_facts(volume_24h="NaN"),
        market_facts(volume_24h="-1"),
        market_facts(minimum_order_size="NaN"),
        market_facts(minimum_order_size="-1"),
        market_facts(tick_size="NaN"),
    ),
)
def test_invalid_nonfinite_or_negative_inputs_fail_closed(
    bad_facts: MarketFacts,
) -> None:
    books = confirmed_books(yes=[("0.45", "20")], no=[("0.48", "20")])
    assert build_pair_intent(bad_facts, books, balance=Decimal("50"), allowance=Decimal("50")) is None

    assert estimated_unwind_loss(
        filled_cost=Decimal("NaN"), sell_price=Decimal("0.50"), quantity=Decimal("10")
    ).is_infinite()
    assert estimated_unwind_loss(
        filled_cost=Decimal("-1"), sell_price=Decimal("0.50"), quantity=Decimal("10")
    ).is_infinite()


def test_emergency_loss_limit_is_inclusive() -> None:
    assert estimated_unwind_loss(
        filled_cost=Decimal("10"), sell_price=Decimal("0.80"), quantity=Decimal("10")
    ) == MAX_EMERGENCY_LOSS
    assert estimated_unwind_loss(
        filled_cost=Decimal("10.000001"), sell_price=Decimal("0.80"), quantity=Decimal("10")
    ) > MAX_EMERGENCY_LOSS


def test_monitored_event_order_is_actionable_profit_volume_then_id() -> None:
    events = [
        {"event_id": "z", "actionable": False, "profit": Decimal("99"), "volume_24h": Decimal("999")},
        {"event_id": "b", "actionable": True, "profit": Decimal("2"), "volume_24h": Decimal("1")},
        {"event_id": "a", "actionable": True, "profit": Decimal("2"), "volume_24h": Decimal("1")},
        {"event_id": "c", "actionable": True, "profit": Decimal("3"), "volume_24h": Decimal("1")},
    ]

    assert [event["event_id"] for event in sorted(events, key=monitored_event_sort_key)] == [
        "c",
        "a",
        "b",
        "z",
    ]


def test_missing_profit_sorts_after_finite_profit_in_its_group() -> None:
    events = [
        {"event_id": "missing", "actionable": False, "volume_24h": Decimal("999")},
        {"event_id": "finite", "actionable": False, "profit": Decimal("0.01"), "volume_24h": Decimal("1")},
    ]

    assert [event["event_id"] for event in sorted(events, key=monitored_event_sort_key)] == [
        "finite",
        "missing",
    ]


def test_negative_finite_profit_sorts_before_missing_profit() -> None:
    events = [
        {"event_id": "z", "actionable": False, "profit": Decimal("-1"), "volume_24h": Decimal("1")},
        {"event_id": "a", "actionable": False, "volume_24h": Decimal("1")},
    ]

    assert [event["event_id"] for event in sorted(events, key=monitored_event_sort_key)] == [
        "z",
        "a",
    ]
