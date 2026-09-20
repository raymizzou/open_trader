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
            "fees_checked_at": NOW,
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


def test_lp_entry_cumulative_midpoint_qualification() -> None:
    direction = _direction()
    direction["market"] = {
        **direction["market"],
        "reward_min_size": Decimal("20"),
        "reward_max_spread": Decimal("0.01"),
    }
    direction["book"] = {
        "condition_id": "condition-a",
        "token_id": "token-yes",
        "received_at": NOW,
        "bids": [
            {"price": Decimal("0.45"), "size": Decimal("10")},
            {"price": Decimal("0.44"), "size": Decimal("10")},
            {"price": Decimal("0.40"), "size": Decimal("100")},
        ],
        "asks": [{"price": Decimal("0.47"), "size": Decimal("20")}],
    }
    account = _account()
    account.update(
        {
            "wallet_address": "wallet-a",
            "open_orders_complete": True,
            "positions_complete": True,
        }
    )

    result = polymarket_lp_risk.evaluate_lp_entry(
        direction, account=account, now=NOW, candidate=True
    )

    assert result["state"] == "eligible"
    guidance = result["guidance"]
    assert isinstance(guidance, dict)
    assert guidance["price"] == Decimal("0.45")
    assert guidance["quantity"] == Decimal("20")
    assert guidance["required_capital"] == Decimal("9.00")
    assert guidance["estimated_exit_loss"] == Decimal("0.60")


def test_lp_entry_candidate_fact_freshness_and_identity() -> None:
    direction = _direction()
    market = dict(direction["market"])
    market["account_wallet_address"] = "wallet-a"
    direction["market"] = market
    account = _account()
    account.update(
        {
            "wallet_address": "wallet-a",
            "open_orders_complete": True,
            "positions_complete": True,
        }
    )

    def evaluate(
        current_direction: dict[str, object] | None = None,
        current_account: dict[str, object] | None = None,
    ) -> dict[str, object]:
        return polymarket_lp_risk.evaluate_lp_entry(
            current_direction or direction,
            account=current_account or account,
            now=NOW,
            candidate=True,
        )

    at_boundary = {
        **direction,
        "market": {
            **market,
            "metadata_checked_at": NOW - timedelta(seconds=60),
            "fees_checked_at": NOW - timedelta(seconds=60),
        },
        "book": {**direction["book"], "received_at": NOW - timedelta(seconds=60)},
        "reward_checked_at": NOW - timedelta(seconds=60),
    }
    boundary_account = {
        **account,
        "checked_at": NOW - timedelta(seconds=60),
    }
    boundary = evaluate(at_boundary, boundary_account)
    assert boundary["state"] == "eligible"

    for changed in (
        {"book": {**direction["book"], "received_at": NOW - timedelta(seconds=60, microseconds=1)}},
        {"reward_checked_at": NOW - timedelta(seconds=60, microseconds=1)},
        {"market": {**market, "metadata_checked_at": NOW + timedelta(microseconds=1)}},
        {"market": {**market, "fees_checked_at": NOW - timedelta(seconds=60, microseconds=1)}},
        {"market": {**market, "fees_checked_at": NOW + timedelta(microseconds=1)}},
        {"market": {**market, "account_wallet_address": "wallet-b"}},
    ):
        result = evaluate({**direction, **changed})
        assert result["state"] == "unknown"
        assert result["guidance"] is None

    missing_reward = {**direction}
    missing_reward.pop("reward_checked_at")
    result = evaluate(missing_reward)
    assert result["state"] == "unknown"
    assert result["guidance"] is None

    missing_fees = {**direction, "market": dict(market)}
    missing_fees["market"].pop("fees_checked_at")
    result = evaluate(missing_fees)
    assert result["state"] == "unknown"
    assert result["guidance"] is None

    incomplete_account = {**account, "open_orders_complete": False}
    result = evaluate(current_account=incomplete_account)
    assert result["state"] == "unknown"
    assert result["guidance"] is None

    unknown_fee_market = {
        **market,
        "fees_enabled": True,
        "taker_fee_rate": None,
        "fee_exponent": Decimal("1"),
    }
    result = evaluate({**direction, "market": unknown_fee_market})
    assert result["state"] == "unknown"
    assert result["guidance"] is None

    known_zero_fee = evaluate()
    assert known_zero_fee["state"] == "eligible"


def test_lp_entry_minimum_capital_and_stress_boundaries() -> None:
    direction = _direction(reward_min_size="20", minimum_order_size="5")
    direction["market"] = {
        **direction["market"],
        "reward_max_spread": Decimal("0.10"),
    }
    direction["book"] = {
        "condition_id": "condition-a",
        "token_id": "token-yes",
        "received_at": NOW,
        "bids": [
                {"price": Decimal("0.45"), "size": Decimal("21")},
                {"price": Decimal("0.44"), "size": Decimal("21")},
            ],
            "asks": [{"price": Decimal("0.47"), "size": Decimal("21")}],
    }
    account = _account()
    account.update(
        {
            "open_orders_complete": True,
            "positions_complete": True,
            "wallet_address": "wallet-a",
            "balance": Decimal("9"),
            "allowance": Decimal("9"),
        }
    )
    direction["market"] = {
        **direction["market"],
        "account_wallet_address": "wallet-a",
    }

    funded = polymarket_lp_risk.evaluate_lp_entry(
        direction, account=account, now=NOW, candidate=True
    )
    assert funded["state"] == "eligible"
    funded_guidance = funded["guidance"]
    assert isinstance(funded_guidance, dict)
    assert funded_guidance["quantity"] == Decimal("20")
    assert funded_guidance["required_capital"] == Decimal("9")

    overfunded = {**account, "balance": Decimal("9"), "allowance": Decimal("9")}
    overfunded_direction = {
        **direction,
        "book": {
            **direction["book"],
            "bids": [
                    {"price": Decimal("0.46"), "size": Decimal("21")},
                    {"price": Decimal("0.45"), "size": Decimal("21")},
            ],
        },
    }
    overfunded_result = polymarket_lp_risk.evaluate_lp_entry(
        overfunded_direction, account=overfunded, now=NOW, candidate=True
    )
    assert overfunded_result["state"] == "rejected"
    assert "balance_insufficient" in overfunded_result["reason_codes"]

    fractional_direction = {
        **direction,
        "market": {**direction["market"], "reward_min_size": Decimal("20.001")},
    }
    fractional = polymarket_lp_risk.evaluate_lp_entry(
        fractional_direction, account={**account, "balance": Decimal("20"), "allowance": Decimal("20")}, now=NOW, candidate=True
    )
    assert fractional["state"] == "eligible"
    fractional_guidance = fractional["guidance"]
    assert isinstance(fractional_guidance, dict)
    assert fractional_guidance["quantity"] == Decimal("20.01")

    unknown_funds = polymarket_lp_risk.evaluate_lp_entry(
        direction,
        account={**account, "balance": None},
        now=NOW,
        candidate=True,
    )
    assert unknown_funds["state"] == "unknown"
    assert unknown_funds["guidance"] is None

    market = direction["market"]
    assert isinstance(market, dict)
    stress_market = {**market, "fees_enabled": False}
    stress_book = {
        "condition_id": "condition-a",
        "token_id": "token-yes",
        "received_at": NOW,
        "bids": [
            {"price": Decimal("0.50"), "size": Decimal("20")},
            {"price": Decimal("0.45"), "size": Decimal("20")},
        ],
    }
    at_limit = polymarket_lp_risk.estimate_lp_stress_exit(
        stress_book,
        market=stress_market,
        price=Decimal("0.50"),
        quantity=Decimal("20"),
    )
    below_limit = polymarket_lp_risk.estimate_lp_stress_exit(
        {
            **stress_book,
            "bids": [
                {"price": Decimal("0.50"), "size": Decimal("20")},
                {"price": Decimal("0.4495"), "size": Decimal("20")},
            ],
        },
        market=stress_market,
        price=Decimal("0.50"),
        quantity=Decimal("20"),
    )
    assert at_limit["state"] == "eligible"
    assert at_limit["net_loss"] == Decimal("1.00")
    assert below_limit["state"] == "rejected"
    assert below_limit["loss_ratio"] == Decimal("0.101")


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


def test_lp_book_share_reports_both_sides_with_literal_arithmetic() -> None:
    book = {
        "condition_id": "condition-a",
        "token_id": "token-yes",
        "bids": [
            {"price": Decimal("0.50"), "size": Decimal("1000")},
            {"price": Decimal("0.45"), "size": Decimal("7050")},
        ],
        "asks": [{"price": Decimal("0.55"), "size": Decimal("4200")}],
    }
    own_orders = [
        {
            "condition_id": "condition-a",
            "token_id": "token-yes",
            "side": "BUY",
            "status": "LIVE",
            "price": Decimal("0.50"),
            "remaining_quantity": Decimal("1000"),
        },
        {
            "condition_id": "condition-a",
            "token_id": "token-yes",
            "side": "SELL",
            "status": "LIVE",
            "price": Decimal("0.55"),
            "remaining_quantity": Decimal("100"),
        },
    ]

    result = polymarket_lp_risk.evaluate_lp_book_share(
        book,
        condition_id="condition-a",
        token_id="token-yes",
        own_orders=own_orders,
    )

    assert result["BUY"] == {
        "own_side_quantity": Decimal("1000"),
        "side_total_quantity": Decimal("8050"),
        "book_share_pct": Decimal("12.42"),
    }
    assert result["SELL"] == {
        "own_side_quantity": Decimal("100"),
        "side_total_quantity": Decimal("4200"),
        "book_share_pct": Decimal("2.38"),
    }


def test_lp_book_share_missing_book_keeps_fields_none_not_zero() -> None:
    result = polymarket_lp_risk.evaluate_lp_book_share(
        None,
        condition_id="condition-a",
        token_id="token-yes",
        own_orders=(),
    )

    assert result["state"] == "unknown"
    assert result["BUY"] == {
        "own_side_quantity": None,
        "side_total_quantity": None,
        "book_share_pct": None,
    }
    assert result["SELL"] == {
        "own_side_quantity": None,
        "side_total_quantity": None,
        "book_share_pct": None,
    }


def _target_share_book(
    bids: list[tuple[str, str]], asks: list[tuple[str, str]]
) -> dict[str, object]:
    return {
        "condition_id": "condition-a",
        "token_id": "token-yes",
        "received_at": NOW,
        "bids": [
            {"price": Decimal(price), "size": Decimal(size)}
            for price, size in bids
        ],
        "asks": [
            {"price": Decimal(price), "size": Decimal(size)}
            for price, size in asks
        ],
    }


def _target_share_estimate(book: object, *, price: str = "0.50") -> dict[str, object]:
    return polymarket_lp_risk.estimate_lp_target_share_yield(
        book,
        price=Decimal(price),
        reward_min_size=Decimal("20"),
        reward_max_spread=Decimal("0.03"),
        daily_pool_usd=Decimal("24"),
        now=NOW,
    )


def test_estimate_target_share_worked_example_one() -> None:
    """B1: bids .50×570, asks .52×570, v=.03, min 20, T=$24.

    Independent arithmetic (issue #138): midpoint .51; each side's weighted
    quantity is 570×(1−.01/.03)² = 570×(2/3)², so C = 570×4/9;
    q = 3C/(19w) with w = 4/9 gives 3×570/19 = 90 shares; capital
    90×$0.50 = $45; hourly gross reward $24×5%/24 = $0.05; yield
    0.05/45×100 = 1/9 ≈ 0.111111%/h.
    """

    result = _target_share_estimate(_target_share_book([("0.50", "570")], [("0.52", "570")]))

    assert result["state"] == "known"
    assert result["target_quantity"] == Decimal("90")
    assert result["target_capital_usd"] == Decimal("45")
    assert result["hourly_reward_usd"] == Decimal("0.05")
    assert Decimal("0.11111") < result["yield_pct_per_hour"] < Decimal("0.11112")
    assert result["yield_pct_per_hour_display"] == Decimal("0.111111")
    assert result["midpoint"] == Decimal("0.51")
    assert result["competition_upper_bound"] > 0
    assert result["checked_at"] == NOW


def test_estimate_target_share_worked_example_doubled_depth() -> None:
    """B2: both sides' depth doubled → 180 shares, $90, ≈1/18 %/h.

    C doubles while w stays the same, so q doubles: 3×1140/19 = 180;
    yield 0.05/90×100 = 1/18 ≈ 0.055556%/h — deeper books earn less per
    dollar at the same 5% target share.
    """

    result = _target_share_estimate(
        _target_share_book([("0.50", "1140")], [("0.52", "1140")])
    )

    assert result["state"] == "known"
    assert result["target_quantity"] == Decimal("180")
    assert result["target_capital_usd"] == Decimal("90")
    assert Decimal("0.05555") < result["yield_pct_per_hour"] < Decimal("0.05556")
    assert result["yield_pct_per_hour_display"] == Decimal("0.055556")


def test_estimate_target_share_leaves_minimum_trial_to_entry_checks() -> None:
    """B3: with only $20 available, a passing minimum trial still guides
    20 shares / $10 — the $45 hypothetical target capital neither rejects
    the trial nor inflates its quantity (evaluate_lp_entry semantics)."""

    direction = _direction()
    direction["market"]["tick_size"] = Decimal("0.01")
    direction["market"]["minimum_order_size"] = Decimal("20")
    direction["market"]["reward_min_size"] = Decimal("20")
    direction["market"]["reward_max_spread"] = Decimal("0.03")
    # A second bid level below the quote keeps the stress-exit depth check
    # green once the entire best-bid level is removed.
    direction["book"] = _target_share_book(
        [("0.50", "570"), ("0.49", "100")], [("0.52", "570")]
    )
    account = _account()
    account["balance"] = Decimal("20")
    account["allowance"] = Decimal("20")

    result = polymarket_lp_risk.evaluate_lp_entry(
        direction, account=account, now=NOW
    )

    assert result["state"] == "eligible"
    guidance = result["guidance"]
    assert isinstance(guidance, dict)
    assert guidance["quantity"] == Decimal("20")
    assert guidance["required_capital"] == Decimal("10.00")


def test_estimate_target_share_unknown_boundary_cases() -> None:
    """B4: w=0 quote, out-of-range midpoint, and missing book are UNKNOWN
    with no numeric fallback and never a fake zero."""

    # Quote exactly v away from the midpoint → unit weight zero.
    far = _target_share_estimate(
        _target_share_book([("0.48", "570")], [("0.54", "570")]), price="0.48"
    )
    # Midpoint (0.04+0.06)/2 = 0.05 outside [0.10, 0.90].
    low_mid = _target_share_estimate(
        _target_share_book([("0.04", "570")], [("0.06", "570")]), price="0.04"
    )
    # Missing book entirely.
    no_book = _target_share_estimate(None)

    for result in (far, low_mid, no_book):
        assert result["state"] == "unknown"
        assert result["yield_pct_per_hour"] is None
        assert result["yield_pct_per_hour_display"] is None
        assert result["target_quantity"] is None
        assert result["target_capital_usd"] is None
        assert result["hourly_reward_usd"] is None
        assert result["midpoint"] is None
        assert result["competition_upper_bound"] is None
    assert "reward_score_zero" in far["reason_codes"]
    assert "midpoint_out_of_range" in low_mid["reason_codes"]
    assert "book_unknown" in no_book["reason_codes"]


# ---- Issue 152: LP BUY 队列位置保护估算（Seam 1 验收用例，票面独立真值） ----


def _queue_book(
    bids: list[tuple[str, str]],
    *,
    received_at: object = NOW,
    condition_id: str = "condition-a",
    token_id: str = "token-yes",
) -> dict[str, object]:
    book: dict[str, object] = {
        "condition_id": condition_id,
        "token_id": token_id,
        "bids": [{"price": Decimal(price), "size": Decimal(size)} for price, size in bids],
        "asks": [{"price": Decimal("0.52"), "size": Decimal("100")}],
    }
    if received_at is not None:
        book["received_at"] = received_at
    return book


def _queue_estimate(
    book: object,
    *,
    price: str,
    own_remaining: str | None,
    baseline_front: str,
    **kwargs: object,
) -> dict[str, object]:
    return polymarket_lp_risk.estimate_lp_queue_position(
        book,
        price=Decimal(price),
        own_remaining=None if own_remaining is None else Decimal(own_remaining),
        baseline_front=Decimal(baseline_front),
        **kwargs,
    )


def test_queue_position_monitoring_above_threshold() -> None:
    """T1: baseline=8000, C=10000, own=2000 → front 8000, ratio 0.80, monitoring."""
    result = _queue_estimate(
        _queue_book([("0.50", "10000")]),
        price="0.50",
        own_remaining="2000",
        baseline_front="8000",
    )
    assert result["state"] == "monitoring"
    assert result["front_estimate"] == Decimal("8000")
    assert result["level_total"] == Decimal("10000")
    assert result["ratio"] == Decimal("0.80")
    assert result["threshold"] == Decimal("0.5")
    assert result["data_time"] == NOW
    assert result["reason_codes"] == []


def test_queue_position_same_price_reduction_stays_monitoring() -> None:
    """T2: baseline=8000, C=6000, own=2000 → front 4000, ratio 2/3, monitoring."""
    result = _queue_estimate(
        _queue_book([("0.50", "6000")]),
        price="0.50",
        own_remaining="2000",
        baseline_front="8000",
    )
    assert result["state"] == "monitoring"
    assert result["front_estimate"] == Decimal("4000")
    assert result["level_total"] == Decimal("6000")
    assert result["ratio"] == Decimal("2") / Decimal("3")


def test_queue_position_exactly_half_triggers() -> None:
    """T3: baseline=8000, C=4000, own=2000 → front 2000, ratio 0.50, triggered."""
    result = _queue_estimate(
        _queue_book([("0.50", "4000")]),
        price="0.50",
        own_remaining="2000",
        baseline_front="8000",
    )
    assert result["state"] == "triggered"
    assert result["front_estimate"] == Decimal("2000")
    assert result["level_total"] == Decimal("4000")
    assert result["ratio"] == Decimal("0.50")


def test_queue_position_behind_growth_ignores_new_depth() -> None:
    """T4: baseline=8000, C=16000, own=2000 → front 8000, ratio 0.50, triggered."""
    result = _queue_estimate(
        _queue_book([("0.50", "16000")]),
        price="0.50",
        own_remaining="2000",
        baseline_front="8000",
    )
    assert result["state"] == "triggered"
    assert result["front_estimate"] == Decimal("8000")
    assert result["level_total"] == Decimal("16000")
    assert result["ratio"] == Decimal("0.50")


def test_queue_position_empty_baseline_triggers_first_tick() -> None:
    """T5: baseline=0（空档位）, C=2000, own=2000 → front 0, ratio 0, triggered."""
    result = _queue_estimate(
        _queue_book([("0.50", "2000")]),
        price="0.50",
        own_remaining="2000",
        baseline_front="0",
    )
    assert result["state"] == "triggered"
    assert result["front_estimate"] == Decimal("0")
    assert result["level_total"] == Decimal("2000")
    assert result["ratio"] == Decimal("0")


def test_queue_position_missing_receipt_is_unknown() -> None:
    """T6: own_remaining=None → unknown/remaining_unknown，数值全 None."""
    result = _queue_estimate(
        _queue_book([("0.50", "10000")]),
        price="0.50",
        own_remaining=None,
        baseline_front="8000",
    )
    assert result["state"] == "unknown"
    assert result["front_estimate"] is None
    assert result["level_total"] is None
    assert result["ratio"] is None
    assert result["data_time"] == NOW
    assert result["reason_codes"] == ["remaining_unknown"]


def test_queue_position_unknown_book_identity_and_stamp() -> None:
    """T7: 身份不符 → book_identity_mismatch；缺 received_at → book_unknown."""
    mismatch = _queue_estimate(
        _queue_book([("0.50", "10000")], condition_id="condition-other"),
        price="0.50",
        own_remaining="2000",
        baseline_front="8000",
        condition_id="condition-a",
        token_id="token-yes",
    )
    assert mismatch["state"] == "unknown"
    assert mismatch["front_estimate"] is None
    assert mismatch["level_total"] is None
    assert mismatch["ratio"] is None
    assert mismatch["reason_codes"] == ["book_identity_mismatch"]

    no_stamp = _queue_estimate(
        _queue_book([("0.50", "10000")], received_at=None),
        price="0.50",
        own_remaining="2000",
        baseline_front="8000",
    )
    assert no_stamp["state"] == "unknown"
    assert no_stamp["front_estimate"] is None
    assert no_stamp["level_total"] is None
    assert no_stamp["ratio"] is None
    assert no_stamp["data_time"] is None
    assert no_stamp["reason_codes"] == ["book_unknown"]

    not_a_book = _queue_estimate(
        "not-a-book",
        price="0.50",
        own_remaining="2000",
        baseline_front="8000",
    )
    assert not_a_book["state"] == "unknown"
    assert not_a_book["reason_codes"] == ["book_unknown"]


def test_queue_position_inconsistent_and_missing_level_are_unknown() -> None:
    """T8: own=2500 > C=2000 → data_inconsistent；档位缺失 → book_level_missing."""
    inconsistent = _queue_estimate(
        _queue_book([("0.50", "2000")]),
        price="0.50",
        own_remaining="2500",
        baseline_front="8000",
    )
    assert inconsistent["state"] == "unknown"
    assert inconsistent["front_estimate"] is None
    assert inconsistent["level_total"] is None
    assert inconsistent["ratio"] is None
    assert inconsistent["reason_codes"] == ["data_inconsistent"]

    zero_total = _queue_estimate(
        _queue_book([("0.49", "2000")]),
        price="0.50",
        own_remaining="0",
        baseline_front="8000",
    )
    assert zero_total["state"] == "unknown"
    assert zero_total["reason_codes"] == ["book_level_missing"]


def test_queue_position_own_fill_does_not_reduce_front() -> None:
    """T9: baseline=8000, C=9000, own=1000（自己成交 1000）→ front 8000."""
    result = _queue_estimate(
        _queue_book([("0.50", "9000")]),
        price="0.50",
        own_remaining="1000",
        baseline_front="8000",
    )
    assert result["state"] == "monitoring"
    assert result["front_estimate"] == Decimal("8000")
    assert result["level_total"] == Decimal("9000")
    assert result["ratio"] == Decimal("8000") / Decimal("9000")


def test_queue_position_bids_at_same_price_aggregate() -> None:
    """C 按同价多档聚合；聚合总量参与比例而非逐行最大值。"""
    result = _queue_estimate(
        _queue_book([("0.51", "500"), ("0.50", "3000"), ("0.50", "7000")]),
        price="0.50",
        own_remaining="2000",
        baseline_front="8000",
    )
    assert result["level_total"] == Decimal("10000")
    assert result["front_estimate"] == Decimal("8000")
    assert result["ratio"] == Decimal("0.80")


def test_queue_position_threshold_constant_attached() -> None:
    """附着常量 LP_QUEUE_PROTECTION_THRESHOLD = Decimal("0.5")."""
    assert polymarket_lp_risk.LP_QUEUE_PROTECTION_THRESHOLD == Decimal("0.5")
