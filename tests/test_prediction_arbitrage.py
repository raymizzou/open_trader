from __future__ import annotations

import time
import json
import subprocess
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from open_trader.prediction_arbitrage import (
    MAX_CROSS_UNSETTLED_PRINCIPAL,
    MAX_EMERGENCY_LOSS,
    PROTECTED_BUY_SHARE_PRECISION,
    BookLevel,
    ConfirmedBooks,
    MarketFacts,
    ThresholdOrderBook,
    build_pair_intent,
    estimated_unwind_loss,
    monitored_event_sort_key,
    _book_segments,
    _protected_buy_candidates,
)


def test_cross_venue_unsettled_principal_policy_is_one_hundred_usdt() -> None:
    assert MAX_CROSS_UNSETTLED_PRINCIPAL == Decimal("100")
from open_trader.polymarket_relation_discovery import (
    CodexRelationValidator,
    ThresholdBuyLeg,
    ThresholdMarket,
    ThresholdRelation,
    build_threshold_hedge_intent,
    discover_threshold_relations,
    positive_edge_depth,
    simple_annualized_yield,
    simple_annualized_yield_from_values,
    _fee,
)
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore


def _shadow_relation(index: int) -> ThresholdRelation:
    suffix = f" candidate {index}"
    markets = []
    for market_id, threshold in (("lower", "90,000"), ("higher", "100,000")):
        markets.append(
            {
                "id": market_id,
                "conditionId": f"condition-{market_id}",
                "question": f"Will Bitcoin be above ${threshold} on December 31?{suffix}",
                "description": "This market resolves from the Binance BTC/USDT close at 12:00 ET.",
                "resolutionSource": "Binance",
                "endDate": "2026-12-31T17:00:00Z",
                "active": True,
                "closed": False,
                "acceptingOrders": True,
                "enableOrderBook": True,
                "negRisk": False,
                "outcomes": '["Yes", "No"]',
                "clobTokenIds": f'["yes-{market_id}", "no-{market_id}"]',
                "feesEnabled": False,
                "orderMinSize": 1,
                "orderPriceMinTickSize": 0.01,
            }
        )
    return discover_threshold_relations(
        [{"id": "shadow-event", "title": "Bitcoin", "active": True, "closed": False, "ended": False, "negRisk": False, "markets": markets}]
    )[0]


def _shadow_relation_result() -> dict[str, object]:
    def market(condition_id: str, threshold: str) -> dict[str, object]:
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

    return {
        "schema_version": 1,
        "decision": "REJECT",
        "relation": "NONE",
        "market_a": market("condition-lower", "90000"),
        "market_b": market("condition-higher", "100000"),
        "proof": {"excluded_state": None, "why_excluded": None},
        "reason_codes": ["AMBIGUOUS_RULES"],
        "summary": "Not approved.",
        "evidence": [],
        "uncertainties": ["ambiguous"],
    }


def _shadow_relation_jsonl() -> str:
    return "\n".join(
        (
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(_shadow_relation_result())}}),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1}}),
        )
    )


class _ShadowRelationRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(
        self, command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(command)
        return subprocess.CompletedProcess(
            command, 0, stdout=_shadow_relation_jsonl(), stderr=""
        )


def test_relation_codex_rejects_negative_budget(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="non-negative"):
        CodexRelationValidator(
            PredictionArbitrageStore(tmp_path), model="gpt-test", max_codex_calls=-1
        )


def test_relation_codex_budget_caps_only_uncached_calls(tmp_path: Path) -> None:
    runner = _ShadowRelationRunner()
    fallback_calls: list[str] = []
    validator = CodexRelationValidator(
        PredictionArbitrageStore(tmp_path),
        model="gpt-test",
        runner=runner,
        fallback_enabled=False,
        max_codex_calls=3,
        fallback=lambda *_: (fallback_calls.append("called") or None, "disabled"),
    )

    results = [validator.validate(_shadow_relation(index)) for index in range(4)]

    assert len(runner.calls) == 3
    assert validator.codex_calls == 3
    assert validator.codex_successes == 3
    assert results[3].reason_codes == ("CODEX_BUDGET_EXHAUSTED",)
    assert fallback_calls == []


def test_relation_codex_cached_hit_does_not_consume_budget(tmp_path: Path) -> None:
    store = PredictionArbitrageStore(tmp_path)
    relation = _shadow_relation(0)
    assert CodexRelationValidator(store, model="gpt-test", runner=_ShadowRelationRunner()).validate(relation).cached is False
    runner = _ShadowRelationRunner()
    validator = CodexRelationValidator(
        store, model="gpt-test", runner=runner, fallback_enabled=False, max_codex_calls=0
    )

    cached = validator.validate(relation)
    exhausted = validator.validate(_shadow_relation(1))

    assert cached.cached is True
    assert exhausted.reason_codes == ("CODEX_BUDGET_EXHAUSTED",)
    assert runner.calls == []
    assert validator.codex_calls == validator.codex_successes == 0


def test_relation_codex_timeout_without_fallback_records_no_deepseek_usage(tmp_path: Path) -> None:
    fallback_calls: list[str] = []

    def timeout(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(command, 1)

    store = PredictionArbitrageStore(tmp_path)
    result = CodexRelationValidator(
        store,
        model="gpt-test",
        runner=timeout,
        fallback_enabled=False,
        fallback=lambda *_: (fallback_calls.append("called") or None, "disabled"),
    ).validate(_shadow_relation(0))

    assert result.reason_codes == ("CODEX_TIMEOUT",)
    assert fallback_calls == []
    assert store.llm_usage_24h_by_provider().get("deepseek", {}) == {}


def test_relation_codex_default_fallback_is_preserved(tmp_path: Path) -> None:
    fallback_calls: list[str] = []
    result = CodexRelationValidator(
        PredictionArbitrageStore(tmp_path),
        model="gpt-test",
        runner=lambda command, **_kwargs: subprocess.CompletedProcess(command, 1, stdout="", stderr="failed"),
        fallback=lambda *_: (fallback_calls.append("called") or json.dumps(_shadow_relation_result()), None),
    ).validate(_shadow_relation(0))

    assert result.status == "llm_rejected"
    assert fallback_calls == ["called"]


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


def test_protected_buy_candidate_scan_stays_fast_for_deep_books() -> None:
    asks = tuple(
        BookLevel(Decimal(cents).scaleb(-2), Decimal("1"))
        for cents in range(1, 101)
    )
    segments = _book_segments(asks, Decimal("0.01"))
    assert segments is not None

    started = time.process_time()
    for _ in range(5):
        candidates = _protected_buy_candidates(segments, Decimal("0.01"))
    elapsed = time.process_time() - started

    assert len(candidates) == 1010
    assert candidates[Decimal("1.0000")] == Decimal("0.01")
    assert candidates[Decimal("44.4445")] == Decimal("20.00")
    assert elapsed < 0.25


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


def threshold_relation(
    *,
    price_tick_a: str = "0.01",
    price_tick_b: str = "0.01",
    fee_rate_a: str = "0",
    fee_rate_b: str = "0",
    minimum_order_size: str = "5",
) -> ThresholdRelation:
    def row(
        label: str,
        threshold: str,
        tick: str,
        fee_rate: str,
    ) -> ThresholdMarket:
        return ThresholdMarket(
            event_id="event-threshold",
            market_id=f"market-{label}",
            condition_id=f"condition-{label}",
            question=f"BTC above ${threshold}?",
            rules=RULES_FOR_THRESHOLD,
            resolution_source="Binance",
            end_date="2027-01-01T00:00:00Z",
            operator=">",
            threshold=Decimal(threshold),
            yes_token_id=f"yes-{label}",
            no_token_id=f"no-{label}",
            group_item_threshold="0" if label == "a" else "1",
            fees_enabled=Decimal(fee_rate) > 0,
            fee_rate=Decimal(fee_rate),
            minimum_order_size=Decimal(minimum_order_size),
            tick_size=Decimal(tick),
        )

    a = row("a", "90000", price_tick_a, fee_rate_a)
    b = row("b", "100000", price_tick_b, fee_rate_b)
    return ThresholdRelation(
        relation_id="threshold:one",
        event_id="event-threshold",
        market_a=a,
        market_b=b,
        relation="B_IMPLIES_A",
        buy_leg_a=ThresholdBuyLeg(
            "A", a.market_id, a.condition_id, "YES", a.yes_token_id
        ),
        buy_leg_b=ThresholdBuyLeg(
            "B", b.market_id, b.condition_id, "NO", b.no_token_id
        ),
        rules_hash_a="rules-a",
        rules_hash_b="rules-b",
    )


RULES_FOR_THRESHOLD = (
    "The Binance BTC/USDT close at 12:00 ET must be above the title threshold."
)


def threshold_book(
    token_id: str,
    *,
    asks: list[tuple[str, str]],
    bids: list[tuple[str, str]],
) -> ThresholdOrderBook:
    return ThresholdOrderBook(
        token_id=token_id,
        asks=tuple(BookLevel(Decimal(price), Decimal(size)) for price, size in asks),
        bids=tuple(BookLevel(Decimal(price), Decimal(size)) for price, size in bids),
        confirmed_at=datetime.now(UTC),
    )


def test_threshold_hedge_builds_equal_cross_condition_legs_and_includes_fees() -> None:
    relation = threshold_relation(fee_rate_a="0.07", fee_rate_b="0.04")
    books = {
        "yes-a": threshold_book(
            "yes-a", asks=[("0.40", "20")], bids=[("0.35", "20")]
        ),
        "no-b": threshold_book(
            "no-b", asks=[("0.50", "20")], bids=[("0.42", "20")]
        ),
    }

    intent = build_threshold_hedge_intent(relation, books)

    assert intent is not None
    assert intent.quantity == Decimal("20")
    assert intent.leg_a.condition_id == "condition-a"
    assert intent.leg_b.condition_id == "condition-b"
    assert intent.leg_a.outcome == "YES"
    assert intent.leg_b.outcome == "NO"
    assert intent.leg_a.max_cost == Decimal("8.00")
    assert intent.leg_b.max_cost == Decimal("10.00")
    assert intent.maximum_fee == Decimal("0.5360")
    assert intent.total_max_cost == Decimal("18.5360")
    assert intent.minimum_payout == Decimal("20")
    assert intent.minimum_profit == Decimal("1.4640")


def test_threshold_hedge_keeps_tiny_positive_profit_instead_of_one_dollar_gate() -> None:
    relation = threshold_relation(price_tick_a="0.001", price_tick_b="0.001")
    books = {
        "yes-a": threshold_book(
            "yes-a", asks=[("0.490", "10")], bids=[("0.489", "10")]
        ),
        "no-b": threshold_book(
            "no-b", asks=[("0.509", "10")], bids=[("0.508", "10")]
        ),
    }

    intent = build_threshold_hedge_intent(relation, books)

    assert intent is not None
    assert intent.quantity == Decimal("10")
    assert intent.minimum_profit == Decimal("0.010")
    assert intent.net_edge == Decimal("0.001")


@pytest.mark.parametrize(
    ("ask_a", "ask_b"),
    [("0.50", "0.50"), ("0.51", "0.50")],
)
def test_zero_or_negative_threshold_profit_is_not_executable(
    ask_a: str, ask_b: str
) -> None:
    relation = threshold_relation()
    books = {
        "yes-a": threshold_book(
            "yes-a", asks=[(ask_a, "20")], bids=[("0.45", "20")]
        ),
        "no-b": threshold_book(
            "no-b", asks=[(ask_b, "20")], bids=[("0.45", "20")]
        ),
    }

    assert build_threshold_hedge_intent(relation, books) is None


def test_threshold_hedge_rejects_a_current_unwind_path_above_two_dollars() -> None:
    relation = threshold_relation()
    books = {
        "yes-a": threshold_book(
            "yes-a", asks=[("0.90", "20")], bids=[("0.01", "20")]
        ),
        "no-b": threshold_book(
            "no-b", asks=[("0.05", "20")], bids=[("0.04", "20")]
        ),
    }

    assert build_threshold_hedge_intent(relation, books) is None
    visible_candidate = build_threshold_hedge_intent(
        relation,
        books,
        require_safe_unwind=False,
    )
    assert visible_candidate is not None
    assert visible_candidate.minimum_profit > 0


@pytest.mark.parametrize(
    "books",
    [
        {
            "yes-a": threshold_book(
                "yes-a", asks=[], bids=[("0.35", "20")]
            ),
            "no-b": threshold_book(
                "no-b", asks=[("0.50", "20")], bids=[("0.42", "20")]
            ),
        },
        {
            "yes-a": threshold_book(
                "yes-a", asks=[("0.40", "20")], bids=[]
            ),
            "no-b": threshold_book(
                "no-b", asks=[("0.50", "20")], bids=[("0.42", "20")]
            ),
        },
    ],
)
def test_threshold_hedge_requires_both_asks_and_bids(
    books: dict[str, ThresholdOrderBook],
) -> None:
    assert build_threshold_hedge_intent(threshold_relation(), books) is None


def test_threshold_hedge_supports_different_tick_sizes_per_leg() -> None:
    relation = threshold_relation(price_tick_a="0.01", price_tick_b="0.001")
    books = {
        "yes-a": threshold_book(
            "yes-a", asks=[("0.40", "20")], bids=[("0.35", "20")]
        ),
        "no-b": threshold_book(
            "no-b", asks=[("0.499", "20")], bids=[("0.42", "20")]
        ),
    }

    intent = build_threshold_hedge_intent(relation, books)

    assert intent is not None
    assert intent.leg_a.tick_size == Decimal("0.01")
    assert intent.leg_b.tick_size == Decimal("0.001")


def test_threshold_annualized_yield_uses_capital_and_remaining_days() -> None:
    relation = threshold_relation(price_tick_a="0.001", price_tick_b="0.001")
    books = {
        "yes-a": threshold_book(
            "yes-a", asks=[("0.490", "10")], bids=[("0.489", "10")]
        ),
        "no-b": threshold_book(
            "no-b", asks=[("0.509", "10")], bids=[("0.508", "10")]
        ),
    }
    intent = build_threshold_hedge_intent(relation, books)
    assert intent is not None

    annualized = simple_annualized_yield(
        intent,
        now=datetime(2026, 1, 1, tzinfo=UTC),
        resolution_at=datetime(2027, 1, 1, tzinfo=UTC),
    )

    assert annualized == intent.minimum_profit / intent.total_max_cost


def test_simple_annualized_yield_from_values_uses_profit_capital_and_days() -> None:
    annualized = simple_annualized_yield_from_values(
        Decimal("2"),
        Decimal("100"),
        now=datetime(2026, 1, 1, tzinfo=UTC),
        resolution_at=datetime(2026, 1, 11, tzinfo=UTC),
    )

    assert annualized == Decimal("0.73")


def test_positive_edge_depth_returns_largest_common_positive_edge() -> None:
    segments_a = [
        (Decimal("0.98"), Decimal("0"), Decimal("100")),
        (Decimal("0.99"), Decimal("100"), Decimal("500")),
    ]
    segments_b = [
        (Decimal("0.005"), Decimal("0"), Decimal("1000")),
    ]
    depth = positive_edge_depth(
        segments_a,
        segments_b,
        tick_size_a=Decimal("0.01"),
        tick_size_b=Decimal("0.005"),
        fee_rate_a=Decimal("0.002"),
        fee_rate_b=Decimal("0.002"),
        minimum_order_size=Decimal("1"),
    )
    assert depth is not None
    assert depth.quantity == Decimal("500")
    expected_cost = (
        Decimal("500") * Decimal("0.99")
        + Decimal("500") * Decimal("0.005")
        + _fee(Decimal("500"), Decimal("0.002"), Decimal("0.99"))
        + _fee(Decimal("500"), Decimal("0.002"), Decimal("0.005"))
    )
    assert depth.cost == expected_cost


def test_positive_edge_depth_returns_none_when_edge_turns_negative() -> None:
    segments_a = [(Decimal("0.999"), Decimal("0"), Decimal("1000"))]
    segments_b = [(Decimal("0.001"), Decimal("0"), Decimal("1000"))]
    assert (
        positive_edge_depth(
            segments_a,
            segments_b,
            tick_size_a=Decimal("0.001"),
            tick_size_b=Decimal("0.001"),
            fee_rate_a=Decimal("0.01"),
            fee_rate_b=Decimal("0.01"),
            minimum_order_size=Decimal("1"),
        )
        is None
    )


def test_positive_edge_depth_includes_extra_cost_for_cross_venue_gas() -> None:
    segments_a = [(Decimal("0.98"), Decimal("0"), Decimal("100"))]
    segments_b = [(Decimal("0.01"), Decimal("0"), Decimal("100"))]
    depth = positive_edge_depth(
        segments_a,
        segments_b,
        tick_size_a=Decimal("0.01"),
        tick_size_b=Decimal("0.01"),
        fee_rate_a=Decimal("0"),
        fee_rate_b=Decimal("0"),
        minimum_order_size=Decimal("1"),
        extra_cost=Decimal("1.50"),
    )
    assert depth is None


def test_positive_edge_depth_respects_minimum_order_size() -> None:
    segments_a = [(Decimal("0.98"), Decimal("0"), Decimal("100"))]
    segments_b = [(Decimal("0.01"), Decimal("0"), Decimal("100"))]
    assert (
        positive_edge_depth(
            segments_a,
            segments_b,
            tick_size_a=Decimal("0.01"),
            tick_size_b=Decimal("0.01"),
            fee_rate_a=Decimal("0"),
            fee_rate_b=Decimal("0"),
            minimum_order_size=Decimal("200"),
        )
        is None
    )
