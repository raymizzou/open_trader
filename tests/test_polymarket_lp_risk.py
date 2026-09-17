from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from open_trader import polymarket_lp_risk, polymarket_lp_views
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore


NOW = datetime(2026, 9, 15, 1, 0, tzinfo=UTC)


def _direction(
    *, reward_min_size: str = "20", minimum_order_size: str = "5"
) -> dict[str, object]:
    return {
        "market": {
            "market_id": "market-a",
            "condition_id": "condition-a",
            "token_id": "token-yes",
            "outcome": "YES",
            "metadata_checked_at": NOW,
            "accepting_orders": True,
            "exchange_type": "CLOB",
            "tick_size": Decimal("0.01"),
            "minimum_order_size": Decimal(minimum_order_size),
            "reward_min_size": Decimal(reward_min_size),
            "reward_max_spread": Decimal("0.10"),
            "fees_enabled": False,
            "fee": Decimal("0"),
            "taker_fee_rate": Decimal("0"),
            "fee_exponent": Decimal("1"),
        },
        "book": {
            "condition_id": "condition-a",
            "token_id": "token-yes",
            "received_at": NOW,
            "bids": [
                {"price": Decimal("0.51"), "size": Decimal("1")},
                {"price": Decimal("0.50"), "size": Decimal("100")},
            ],
            "asks": [{"price": Decimal("0.52"), "size": Decimal("100")}],
        },
        "reward_active": True,
        "daily_pool_usd": Decimal("100"),
        "reward_checked_at": NOW,
    }


def _account() -> dict[str, object]:
    return {
        "authenticated": True,
        "balance": Decimal("1000"),
        "allowance": Decimal("1000"),
        "open_orders": [],
        "positions": [],
        "checked_at": NOW,
    }


def test_lp_entry_uses_best_bid_and_minimum_eligible_quantity() -> None:
    regular = polymarket_lp_risk.evaluate_lp_entry(
        _direction(), account=_account(), now=NOW
    )
    fractional_reward = polymarket_lp_risk.evaluate_lp_entry(
        _direction(reward_min_size="20.001"), account=_account(), now=NOW
    )
    platform_minimum = polymarket_lp_risk.evaluate_lp_entry(
        _direction(minimum_order_size="30"), account=_account(), now=NOW
    )

    regular_guidance = regular["guidance"]
    fractional_guidance = fractional_reward["guidance"]
    platform_guidance = platform_minimum["guidance"]
    assert regular["state"] == "eligible"
    assert fractional_reward["state"] == "eligible"
    assert platform_minimum["state"] == "eligible"
    assert isinstance(regular_guidance, dict)
    assert isinstance(fractional_guidance, dict)
    assert isinstance(platform_guidance, dict)
    assert regular_guidance["price"] == Decimal("0.51")
    assert regular_guidance["quantity"] == Decimal("20")
    assert regular_guidance["required_capital"] == Decimal("10.20")
    assert fractional_guidance["quantity"] == Decimal("20.01")
    assert platform_guidance["quantity"] == Decimal("30")

    out_of_range = _direction()
    out_of_range_market = dict(out_of_range["market"])
    out_of_range_market.update(
        {
            "tick_size": Decimal("0.01"),
            "minimum_order_size": Decimal("20"),
            "reward_min_size": Decimal("20"),
            "reward_max_spread": Decimal("0.10"),
        }
    )
    out_of_range["market"] = out_of_range_market
    out_of_range["book"] = {
        "condition_id": "condition-a",
        "token_id": "token-yes",
        "received_at": NOW,
        "bids": [
            {"price": Decimal("0.95"), "size": Decimal("100")},
            {"price": Decimal("0.94"), "size": Decimal("100")},
        ],
        "asks": [{"price": Decimal("0.97"), "size": Decimal("100")}],
    }
    out_of_range_result = polymarket_lp_risk.evaluate_lp_entry(
        out_of_range, account=_account(), now=NOW
    )

    thin_bbo = _direction()
    thin_bbo_market = dict(thin_bbo["market"])
    thin_bbo_market.update(
        {
            "tick_size": Decimal("0.001"),
            "minimum_order_size": Decimal("20"),
            "reward_min_size": Decimal("20"),
            "reward_max_spread": Decimal("0.01"),
        }
    )
    thin_bbo["market"] = thin_bbo_market
    thin_bbo["book"] = {
        "condition_id": "condition-a",
        "token_id": "token-yes",
        "received_at": NOW,
        "bids": [
            {"price": Decimal("0.51"), "size": Decimal("1")},
            {"price": Decimal("0.50"), "size": Decimal("100")},
        ],
        "asks": [
            {"price": Decimal("0.515"), "size": Decimal("1")},
            {"price": Decimal("0.57"), "size": Decimal("100")},
        ],
    }
    thin_bbo_result = polymarket_lp_risk.evaluate_lp_entry(
        thin_bbo, account=_account(), now=NOW
    )

    assert out_of_range_result["state"] == "rejected"
    assert "midpoint_out_of_range" in out_of_range_result["reason_codes"]
    assert thin_bbo_result["state"] == "rejected"
    assert "reward_distance_invalid" in thin_bbo_result["reason_codes"]


def test_lp_stress_exit_removes_own_orders_and_the_entire_best_level() -> None:
    direction = _direction()
    market = direction["market"]
    assert isinstance(market, dict)

    def book(*, last_level_size: str = "10") -> dict[str, object]:
        return {
            "condition_id": "condition-a",
            "token_id": "token-yes",
            "received_at": NOW,
            "bids": [
                {"price": Decimal("0.50"), "size": Decimal("5")},
                {"price": Decimal("0.50"), "size": Decimal("5")},
                {"price": Decimal("0.46"), "size": Decimal("15")},
                {"price": Decimal("0.44"), "size": Decimal(last_level_size)},
            ],
        }

    own_orders = [
        {
            "condition_id": "condition-a",
            "token_id": "token-yes",
            "side": "BUY",
            "status": "LIVE",
            "price": Decimal("0.46"),
            "remaining_size": Decimal("5"),
        }
    ]
    covered = polymarket_lp_risk.estimate_lp_stress_exit(
        book(),
        market=market,
        price=Decimal("0.50"),
        quantity=Decimal("20"),
        own_orders=own_orders,
    )
    insufficient = polymarket_lp_risk.estimate_lp_stress_exit(
        book(last_level_size="9.99"),
        market=market,
        price=Decimal("0.50"),
        quantity=Decimal("20"),
        own_orders=own_orders,
    )

    assert covered["state"] == "eligible"
    assert covered["fully_covered"] is True
    assert covered["gross_exit_value"] == Decimal("9.00")
    assert covered["exit_fee"] == Decimal("0")
    assert covered["net_loss"] == Decimal("1.00")
    assert covered["loss_ratio"] == Decimal("0.10")
    assert insufficient["state"] == "rejected"
    assert insufficient["fully_covered"] is False
    assert "exit_liquidity_insufficient" in insufficient["reason_codes"]


def test_lp_exposure_uses_actual_cost_and_remaining_orders() -> None:
    direction = _direction()
    market = direction["market"]
    assert isinstance(market, dict)
    account = {
        "authenticated": True,
        "checked_at": NOW,
        "open_orders_complete": True,
        "positions_complete": True,
        "open_orders": [
            {
                "condition_id": "condition-a",
                "token_id": "token-yes",
                "side": "BUY",
                "status": "LIVE",
                "price": Decimal("0.50"),
                "original_size": Decimal("100"),
                "size_matched": Decimal("40"),
                "remaining_size": Decimal("60"),
            }
        ],
        "positions": [
            {
                "condition_id": "condition-a",
                "token_id": "token-yes",
                "size": Decimal("40"),
                "average_price": Decimal("0.50"),
            }
        ],
    }

    def book(next_bid: str, *, next_size: str = "100") -> dict[str, object]:
        return {
            "condition_id": "condition-a",
            "token_id": "token-yes",
            "received_at": NOW,
            "bids": [
                {"price": Decimal("0.51"), "size": Decimal("20")},
                {"price": Decimal("0.50"), "size": Decimal("60")},
                {"price": Decimal(next_bid), "size": Decimal(next_size)},
            ],
        }

    for next_bid, expected_loss, expected_ratio, warning in (
        ("0.46", Decimal("4"), Decimal("0.08"), False),
        ("0.45", Decimal("5"), Decimal("0.10"), True),
        ("0.44", Decimal("6"), Decimal("0.12"), True),
    ):
        result = polymarket_lp_risk.evaluate_lp_exposure(
            book(next_bid), market=market, account=account, now=NOW
        )
        assert result["state"] == "known"
        assert result["risk_quantity"] == Decimal("100")
        assert result["risk_principal"] == Decimal("50")
        assert result["stress_loss"] == expected_loss
        assert result["loss_ratio"] == expected_ratio
        assert result["warning"] is warning

    for age in (11, 120):
        delayed_account = {**account, "checked_at": NOW - timedelta(seconds=age)}
        result = polymarket_lp_risk.evaluate_lp_exposure(
            book("0.46"), market=market, account=delayed_account, now=NOW
        )
        assert result["state"] == "known"
        assert result["loss_ratio"] == Decimal("0.08")
    expired_account = {**account, "checked_at": NOW - timedelta(seconds=121)}
    expired_account_result = polymarket_lp_risk.evaluate_lp_exposure(
        book("0.46"), market=market, account=expired_account, now=NOW
    )
    assert expired_account_result["state"] == "unknown"
    assert "account_freshness_stale" in expired_account_result["reason_codes"]
    stale_book = {**book("0.46"), "received_at": NOW - timedelta(seconds=11)}
    stale_book_result = polymarket_lp_risk.evaluate_lp_exposure(
        stale_book, market=market, account=account, now=NOW
    )
    assert stale_book_result["state"] == "unknown"
    assert "book_freshness_stale" in stale_book_result["reason_codes"]

    insufficient = polymarket_lp_risk.evaluate_lp_exposure(
        book("0.45", next_size="99.99"),
        market=market,
        account=account,
        now=NOW,
    )
    assert insufficient["state"] == "unknown"
    assert insufficient["warning"] is None
    assert insufficient["loss_ratio"] is None

    unknown_fee_market = {
        **market,
        "fees_enabled": True,
        "taker_fee_rate": None,
        "fee_exponent": Decimal("1"),
    }
    unknown_fee = polymarket_lp_risk.evaluate_lp_exposure(
        book("0.45"),
        market=unknown_fee_market,
        account=account,
        now=NOW,
    )
    assert unknown_fee["state"] == "unknown"
    assert unknown_fee["warning"] is None
    assert "exit_fee_unknown" in unknown_fee["reason_codes"]

    fee_market = {
        **market,
        "fees_enabled": True,
        "taker_fee_rate": Decimal("0.05"),
        "fee_exponent": Decimal("1"),
    }
    fee_account = {
        **account,
        "open_orders": [],
        "positions": [
            {
                "condition_id": "condition-a",
                "token_id": "token-yes",
                "size": Decimal("20"),
                "average_price": Decimal("0.50"),
            }
        ],
    }
    fee_book = {
        "condition_id": "condition-a",
        "token_id": "token-yes",
        "received_at": NOW,
        "bids": [
            {"price": Decimal("0.51"), "size": Decimal("10")},
            {"price": Decimal("0.46"), "size": Decimal("20")},
        ],
    }
    with_fees = polymarket_lp_risk.evaluate_lp_exposure(
        fee_book, market=fee_market, account=fee_account, now=NOW
    )
    assert with_fees["risk_principal"] == Decimal("10")
    assert with_fees["exit_fee"] == Decimal("0.24840")
    assert with_fees["stress_loss"] == Decimal("1.04840")


def test_lp_entry_loss_limit_includes_exit_fees() -> None:
    direction = _direction()
    market = dict(direction["market"])
    fee_market = {
        **market,
        "fees_enabled": True,
        "taker_fee_rate": Decimal("0.05"),
        "fee_exponent": Decimal("1"),
    }
    no_fee_market = {**market, "fees_enabled": False}
    unknown_fee_market = {
        **fee_market,
        "taker_fee_rate": None,
    }
    book = {
        "condition_id": "condition-a",
        "token_id": "token-yes",
        "received_at": NOW,
        "bids": [
            {"price": Decimal("0.50"), "size": Decimal("10")},
            {"price": Decimal("0.46"), "size": Decimal("20")},
        ],
    }

    with_fees = polymarket_lp_risk.estimate_lp_stress_exit(
        book,
        market=fee_market,
        price=Decimal("0.50"),
        quantity=Decimal("20"),
    )
    without_fees = polymarket_lp_risk.estimate_lp_stress_exit(
        book,
        market=no_fee_market,
        price=Decimal("0.50"),
        quantity=Decimal("20"),
    )
    fee_unknown = polymarket_lp_risk.estimate_lp_stress_exit(
        book,
        market=unknown_fee_market,
        price=Decimal("0.50"),
        quantity=Decimal("20"),
    )

    assert with_fees["state"] == "rejected"
    assert with_fees["gross_exit_value"] == Decimal("9.20")
    assert with_fees["exit_fee"] == Decimal("0.24840")
    assert with_fees["net_loss"] == Decimal("1.04840")
    assert with_fees["loss_ratio"] == Decimal("0.10484")
    assert without_fees["state"] == "eligible"
    assert without_fees["net_loss"] == Decimal("0.80")
    assert without_fees["loss_ratio"] == Decimal("0.08")
    assert fee_unknown["state"] == "unknown"


def test_lp_entry_respects_account_reservations_and_market_participation() -> None:
    direction = _direction()
    market = dict(direction["market"])
    market.update(
        {
            "tick_size": Decimal("0.01"),
            "minimum_order_size": Decimal("5"),
            "reward_min_size": Decimal("20"),
        }
    )
    direction["market"] = market
    direction["book"] = {
        "condition_id": "condition-a",
        "token_id": "token-yes",
        "received_at": NOW,
        "bids": [
            {"price": Decimal("0.50"), "size": Decimal("1")},
            {"price": Decimal("0.49"), "size": Decimal("100")},
        ],
        "asks": [{"price": Decimal("0.52"), "size": Decimal("100")}],
    }

    other_buy = {
        "order_id": "other-order",
        "market_id": "other-market",
        "condition_id": "other-condition",
        "token_id": "other-token",
        "side": "BUY",
        "status": "LIVE",
        "price": Decimal("0.50"),
        "remaining_size": Decimal("20"),
    }
    funded = _account()
    funded.update(
        {
            "balance": Decimal("20"),
            "allowance": Decimal("20"),
            "open_orders": [other_buy],
        }
    )
    duplicate_reservation = polymarket_lp_risk.evaluate_lp_entry(
        direction,
        account=funded,
        now=NOW,
        reservations=[{"order_id": "other-order", "amount": Decimal("10")}],
    )
    extra_reservation = polymarket_lp_risk.evaluate_lp_entry(
        direction,
        account=funded,
        now=NOW,
        reservations=[
            {"order_id": "other-order", "amount": Decimal("10")},
            {"order_id": "separate-reservation", "amount": Decimal("0.01")},
        ],
    )

    other_outcome_order = _account()
    other_outcome_order["open_orders"] = [
        {
            **other_buy,
            "condition_id": "condition-a",
            "token_id": "token-no",
        }
    ]
    participating_order = polymarket_lp_risk.evaluate_lp_entry(
        direction, account=other_outcome_order, now=NOW
    )
    other_outcome_position = _account()
    other_outcome_position["positions"] = [
        {
            "condition_id": "condition-a",
            "token_id": "token-no",
            "size": Decimal("1"),
        }
    ]
    participating_position = polymarket_lp_risk.evaluate_lp_entry(
        direction, account=other_outcome_position, now=NOW
    )

    missing_orders = _account()
    missing_orders.pop("open_orders")
    missing_orders_result = polymarket_lp_risk.evaluate_lp_entry(
        direction, account=missing_orders, now=NOW
    )
    missing_positions = _account()
    missing_positions.pop("positions")
    missing_positions_result = polymarket_lp_risk.evaluate_lp_entry(
        direction, account=missing_positions, now=NOW
    )
    unknown_balance = _account()
    unknown_balance["balance"] = None
    unknown_balance_result = polymarket_lp_risk.evaluate_lp_entry(
        direction, account=unknown_balance, now=NOW
    )
    unknown_allowance = _account()
    unknown_allowance["allowance"] = None
    unknown_allowance_result = polymarket_lp_risk.evaluate_lp_entry(
        direction, account=unknown_allowance, now=NOW
    )
    stale_account = _account()
    stale_account["checked_at"] = NOW - timedelta(seconds=11)
    stale_account_result = polymarket_lp_risk.evaluate_lp_entry(
        direction, account=stale_account, now=NOW
    )

    stable_history = [
        {
            "condition_id": "condition-a",
            "token_id": "token-yes",
            "received_at": NOW - timedelta(seconds=3600 - elapsed),
            "best_bid_price": Decimal("0.50"),
            "best_bid_size": Decimal("100"),
            "best_ask_price": Decimal("0.52"),
            "best_ask_size": Decimal("100"),
        }
        for elapsed in range(0, 3601, 5)
    ]
    screened_before_account_change = polymarket_lp_views.screen_lp_direction(
        direction, history=stable_history, now=NOW
    )
    screened_after_account_change = polymarket_lp_views.screen_lp_direction(
        direction, history=stable_history, now=NOW
    )

    assert duplicate_reservation["state"] == "eligible"
    assert extra_reservation["state"] == "rejected"
    assert "balance_insufficient" in extra_reservation["reason_codes"]
    assert participating_order["state"] == "rejected"
    assert participating_position["state"] == "rejected"
    assert missing_orders_result["state"] == "unknown"
    assert missing_positions_result["state"] == "unknown"
    assert unknown_balance_result["state"] == "unknown"
    assert unknown_allowance_result["state"] == "unknown"
    assert stale_account_result["state"] == "unknown"
    assert screened_before_account_change["state"] == "eligible"
    assert screened_after_account_change == screened_before_account_change


def test_lp_entry_event_window_requires_a_fresh_post_event_hour(tmp_path) -> None:
    event_start = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
    event_end = datetime(2026, 9, 15, 13, 0, tzinfo=UTC)
    direction = _direction()
    market = dict(direction["market"])
    market.update(
        {
            "game_id": "game-7",
            "game_start_time": event_start,
            "event_start_time": event_start,
            "event_ended": False,
            "event_finished_at": None,
        }
    )
    direction["market"] = market

    def history_ending_at(end: datetime) -> list[dict[str, object]]:
        start = end - timedelta(hours=1)
        return [
            {
                "condition_id": "condition-a",
                "token_id": "token-yes",
                "received_at": start + timedelta(seconds=elapsed),
                "best_bid_price": Decimal("0.50"),
                "best_bid_size": Decimal("100"),
                "best_ask_price": Decimal("0.52"),
                "best_ask_size": Decimal("100"),
            }
            for elapsed in range(0, 3601, 5)
        ]

    def evaluate_at(
        checked_at: datetime,
        history: object,
        *,
        market_changes: dict[str, object] | None = None,
        event_end_confirmation: dict[str, object] | None = None,
    ) -> dict[str, object]:
        current_market = {
            **market,
            "metadata_checked_at": checked_at,
            **(market_changes or {}),
        }
        current = {
            **direction,
            "market": current_market,
            "book": {**direction["book"], "received_at": checked_at},
            "reward_checked_at": checked_at,
        }
        current["screening"] = polymarket_lp_views.screen_lp_direction(
            current, history=history, now=checked_at
        )
        if event_end_confirmation is not None:
            current["event_end_confirmation"] = event_end_confirmation
        account = _account()
        account["checked_at"] = checked_at
        return polymarket_lp_risk.evaluate_lp_entry(
            current, account=account, now=checked_at
        )

    before_start_at = event_start - timedelta(minutes=30, seconds=1)
    before_start = evaluate_at(
        before_start_at,
        history_ending_at(before_start_at),
        market_changes={"event_ended": False},
    )
    starting_boundary_at = event_start - timedelta(minutes=30)
    starting_boundary = evaluate_at(
        starting_boundary_at,
        history_ending_at(starting_boundary_at),
        market_changes={"event_ended": False},
    )
    unknown_end_state = evaluate_at(
        before_start_at,
        history_ending_at(before_start_at),
        market_changes={"event_ended": None},
    )

    before_recovery_at = event_end + timedelta(hours=1) - timedelta(seconds=1)
    before_recovery = evaluate_at(
        before_recovery_at,
        history_ending_at(before_recovery_at),
        market_changes={"event_ended": True, "event_finished_at": event_end},
    )
    recovery_at = event_end + timedelta(hours=1)
    post_event_history = history_ending_at(recovery_at)
    recovered = evaluate_at(
        recovery_at,
        post_event_history,
        market_changes={"event_ended": True, "event_finished_at": event_end},
    )
    pre_event_only = evaluate_at(
        recovery_at,
        history_ending_at(event_start + timedelta(hours=1)),
        market_changes={"event_ended": True, "event_finished_at": event_end},
    )

    data_dir = tmp_path / "screening"
    first_store = PredictionArbitrageStore(data_dir)
    first_store.lp_save_screening_snapshot(
        {
            "event_end_confirmations": {
                "condition-a": {
                    "event_id": "event-1",
                    "game_id": "game-7",
                    "confirmed_end_at": "2026-09-15T13:00:00Z",
                }
            }
        }
    )
    reopened_store = PredictionArbitrageStore(data_dir)
    snapshot = reopened_store.lp_screening_snapshot()
    assert isinstance(snapshot, dict)
    confirmations = snapshot["event_end_confirmations"]
    assert isinstance(confirmations, dict)
    confirmation = confirmations["condition-a"]
    assert isinstance(confirmation, dict)
    assert confirmation["confirmed_end_at"] == "2026-09-15T13:00:00Z"
    confirmation_recovered = evaluate_at(
        recovery_at,
        post_event_history,
        market_changes={"event_ended": True, "event_finished_at": None},
        event_end_confirmation=confirmation,
    )
    reset_confirmation = evaluate_at(
        recovery_at,
        post_event_history,
        market_changes={"event_ended": True, "event_finished_at": None},
        event_end_confirmation={
            **confirmation,
            "confirmed_end_at": "2026-09-15T14:00:00Z",
        },
    )
    non_sports_event = {
        "game_id": None,
        "game_start_time": None,
        "event_id": "scheduled-event-2",
        "event_start_time": event_start,
        "event_ended": True,
        "event_finished_at": None,
    }
    non_sports_confirmation = {
        "event_id": "scheduled-event-2",
        "game_id": None,
        "confirmed_end_at": "2026-09-15T13:00:00Z",
    }
    non_sports_recovered = evaluate_at(
        recovery_at,
        post_event_history,
        market_changes=non_sports_event,
        event_end_confirmation=non_sports_confirmation,
    )
    non_sports_mismatched_confirmation = evaluate_at(
        recovery_at,
        post_event_history,
        market_changes=non_sports_event,
        event_end_confirmation={
            **non_sports_confirmation,
            "event_id": "different-event",
        },
    )

    no_known_event = evaluate_at(
        recovery_at,
        post_event_history,
        market_changes={
            "event_id": "parent-event-1",
            "game_id": None,
            "game_start_time": None,
            "event_start_time": None,
            "event_ended": None,
            "event_finished_at": None,
        },
    )
    known_game_time_unknown = evaluate_at(
        recovery_at,
        post_event_history,
        market_changes={
            "game_start_time": None,
            "event_start_time": None,
            "event_ended": None,
            "event_finished_at": None,
        },
    )

    assert before_start["state"] == "eligible"
    before_start_guidance = before_start["guidance"]
    assert isinstance(before_start_guidance, dict)
    assert before_start_guidance["expires_at"] == "2026-09-15T11:30:00.000000Z"
    assert starting_boundary["state"] == "rejected"
    assert "event_starting_soon" in starting_boundary["reason_codes"]
    assert unknown_end_state["state"] == "unknown"
    assert before_recovery["state"] == "rejected"
    assert recovered["state"] == "eligible"
    assert pre_event_only["state"] == "unknown"
    assert confirmation_recovered["state"] == "eligible"
    assert reset_confirmation["state"] == "rejected"
    assert non_sports_recovered["state"] == "eligible"
    assert non_sports_mismatched_confirmation["state"] == "unknown"
    assert no_known_event["state"] == "eligible"
    assert "event_coverage_incomplete" in no_known_event["reason_codes"]
    assert known_game_time_unknown["state"] == "unknown"


@pytest.mark.parametrize("age_seconds", [60, 61, 3661, 43200])
def test_lp_entry_metadata_requires_fresh_dynamic_observation(
    age_seconds: int,
) -> None:
    direction = _direction()
    direction["market"] = {
        **direction["market"],
        "metadata_checked_at": NOW - timedelta(seconds=age_seconds),
    }

    result = polymarket_lp_risk.evaluate_lp_entry(
        direction, account=_account(), now=NOW
    )

    if age_seconds == 60:
        assert result["state"] == "eligible"
        assert "market_metadata_stale" not in result["reason_codes"]
    else:
        assert result["state"] == "unknown"
        assert result["reason_codes"] == ["market_metadata_stale"]


def test_lp_entry_metadata_beyond_cache_window_is_stale() -> None:
    direction = _direction()
    direction["market"] = {
        **direction["market"],
        "metadata_checked_at": NOW - timedelta(seconds=61),
    }

    result = polymarket_lp_risk.evaluate_lp_entry(
        direction, account=_account(), now=NOW
    )

    assert result["state"] == "unknown"
    assert result["reason_codes"] == ["market_metadata_stale"]


def test_lp_entry_reward_data_stale_bound_unchanged() -> None:
    direction = _direction()
    direction["reward_checked_at"] = NOW - timedelta(seconds=61)

    result = polymarket_lp_risk.evaluate_lp_entry(
        direction, account=_account(), now=NOW
    )

    assert result["state"] == "unknown"
    assert result["reason_codes"] == ["reward_data_stale"]
