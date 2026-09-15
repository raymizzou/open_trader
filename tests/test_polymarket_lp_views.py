from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from open_trader import polymarket_lp_views
from open_trader.polymarket_lp_views import lp_candidate_rows


NOW = datetime(2026, 9, 15, 1, 0, tzinfo=UTC)


def _direction(
    market_id: str,
    condition_id: str,
    outcome: str,
    token_id: str,
    pool: str,
    bids: list[tuple[str, str]],
) -> dict[str, object]:
    return {
        "market": {
            "market_id": market_id,
            "condition_id": condition_id,
            "token_id": token_id,
            "outcome": outcome,
            "market_title": f"Market {market_id}",
            "market_url": f"https://polymarket.com/event/{market_id}",
            "accepting_orders": True,
            "exchange_type": "CLOB",
            "tick_size": Decimal("0.01"),
            "minimum_order_size": Decimal("1"),
            "reward_min_size": Decimal("20"),
            "reward_max_spread": Decimal("0.10"),
            "fees_enabled": False,
            "fee": Decimal("0"),
            "taker_fee_rate": Decimal("0"),
            "fee_exponent": Decimal("1"),
        },
        "book": {
            "received_at": NOW,
            "bids": [
                {"price": Decimal(price), "size": Decimal(size)}
                for price, size in bids
            ],
            "asks": [
                {"price": Decimal("0.52"), "size": Decimal("100")}
            ],
        },
        "daily_pool_usd": Decimal(pool),
        "reward_active": True,
        "reward_checked_at": NOW,
    }


def _account(
    *,
    balance: str = "1000",
    allowance: str = "1000",
    open_orders: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "authenticated": True,
        "balance": Decimal(balance),
        "allowance": Decimal(allowance),
        "open_orders": open_orders or [],
        "positions": [],
        "checked_at": NOW,
    }


def test_candidates_use_best_bid_minimum_size_and_reward_pool_order() -> None:
    account = {
        "authenticated": True,
        "balance": Decimal("1000"),
        "allowance": Decimal("1000"),
        "open_orders": [],
        "positions": [],
        "checked_at": NOW,
    }
    directions = [
        _direction(
            "A",
            "condition-a",
            "YES",
            "a-yes",
            "100",
            [("0.51", "1"), ("0.50", "100")],
        ),
        _direction(
            "A",
            "condition-a",
            "NO",
            "a-no",
            "100",
            [("0.42", "100")],
        ),
        _direction(
            "B",
            "condition-b",
            "YES",
            "b-yes",
            "200",
            [("0.45", "100")],
        ),
    ]

    rows = lp_candidate_rows(directions, account=account, now=NOW)

    assert len(rows) == 3
    assert rows[0]["market_id"] == "B"
    a_yes = next(row for row in rows if row["token_id"] == "a-yes")
    assert a_yes["price"] == Decimal("0.51")
    assert a_yes["quantity"] == Decimal("20")
    assert a_yes["required_capital"] == Decimal("10.20")
    assert {
        (row["outcome"], row["token_id"])
        for row in rows
        if row["market_id"] == "A"
    } == {("YES", "a-yes"), ("NO", "a-no")}


def test_candidates_hide_ineligible_or_unaffordable_orders() -> None:
    direction = _direction(
        "A", "condition-a", "YES", "a-yes", "100", [("0.51", "100")]
    )

    assert lp_candidate_rows(
        [direction], account=_account(balance="10.19"), now=NOW
    ) == []

    expired_reward = _direction(
        "A", "condition-a", "YES", "a-yes", "100", [("0.51", "100")]
    )
    expired_reward["reward_active"] = False
    assert lp_candidate_rows([expired_reward], account=_account(), now=NOW) == []

    zero_score = _direction(
        "A", "condition-a", "YES", "a-yes", "100", [("0.50", "100")]
    )
    zero_score["market"]["reward_max_spread"] = Decimal("0.01")  # type: ignore[index]
    assert lp_candidate_rows([zero_score], account=_account(), now=NOW) == []

    unknown_fee = _direction(
        "A", "condition-a", "YES", "a-yes", "100", [("0.51", "100")]
    )
    unknown_fee["market"]["fees_enabled"] = True  # type: ignore[index]
    unknown_fee["market"]["fee"] = Decimal("0")  # type: ignore[index]
    unknown_fee["market"]["taker_fee_rate"] = None  # type: ignore[index]
    assert lp_candidate_rows([unknown_fee], account=_account(), now=NOW) == []

    stale_book = _direction(
        "A", "condition-a", "YES", "a-yes", "100", [("0.51", "100")]
    )
    stale_book["book"]["received_at"] = NOW - timedelta(seconds=11)  # type: ignore[index]
    assert lp_candidate_rows([stale_book], account=_account(), now=NOW) == []

    insufficient_depth = _direction(
        "A", "condition-a", "YES", "a-yes", "100", [("0.51", "10"), ("0.50", "9")]
    )
    insufficient_depth["market"]["minimum_order_size"] = Decimal("20")  # type: ignore[index]
    insufficient_depth["market"]["reward_min_size"] = Decimal("1")  # type: ignore[index]
    assert lp_candidate_rows([insufficient_depth], account=_account(), now=NOW) == []

    stop_loss = _direction(
        "A", "condition-a", "YES", "a-yes", "100", [("0.50", "100")]
    )
    stop_loss["book"]["asks"] = [  # type: ignore[index]
        {"price": Decimal("0.50"), "size": Decimal("100")}
    ]
    stop_loss["market"]["fees_enabled"] = True  # type: ignore[index]
    stop_loss["market"]["fee"] = Decimal("0")  # type: ignore[index]
    stop_loss["market"]["taker_fee_rate"] = Decimal("1")  # type: ignore[index]
    assert lp_candidate_rows([stop_loss], account=_account(), now=NOW) == []

    other_open_buy = {
        "order_id": "other-buy",
        "market_id": "other-market",
        "token_id": "other-token",
        "side": "BUY",
        "price": Decimal("0.50"),
        "remaining_size": Decimal("20"),
        "status": "LIVE",
    }
    committed = _account(balance="20", allowance="20", open_orders=[other_open_buy])
    assert lp_candidate_rows([direction], account=committed, now=NOW) == []
    assert len(lp_candidate_rows([direction], account=_account(balance="20", allowance="20"), now=NOW)) == 1

    break_even_direction = _direction(
        "A", "condition-a", "YES", "a-yes", "100", [("0.50", "100")]
    )
    reserved_buy = {**other_open_buy, "order_id": "reserved-buy"}
    available_after_one_deduction = _account(
        balance="20", allowance="20", open_orders=[reserved_buy]
    )
    assert len(
        lp_candidate_rows(
            [break_even_direction],
            account=available_after_one_deduction,
            now=NOW,
            reservations=[{"order_id": "reserved-buy", "amount": Decimal("10")}],
        )
    ) == 1

    target_open_buy = {
        **other_open_buy,
        "market_id": "A",
        "token_id": "a-yes",
    }
    assert lp_candidate_rows(
        [direction],
        account=_account(open_orders=[target_open_buy]),
        now=NOW,
    ) == []


def test_report_separates_partial_exit_realized_pnl_and_open_inventory() -> None:
    opening = {
        "session_id": "session-a",
        "buy_filled_quantity": Decimal("20"),
        "buy_cost": Decimal("10"),
        "buy_fees": Decimal("0.02"),
        "sold_quantity": Decimal("8"),
        "sold_revenue": Decimal("4.40"),
        "sell_fees": Decimal("0.01"),
        "residual_quantity": Decimal("12"),
        "residual_exit_value": Decimal("5.76"),
        "projected_exit_fee": Decimal("0.02"),
        "cumulative_unpaid_rewards": Decimal("1.50"),
    }

    totals = polymarket_lp_views.lp_report_totals(
        opening, paid_rewards=Decimal("0.20")
    )

    assert totals == {
        "realized_trade_pnl": Decimal("0.382"),
        "residual_cost": Decimal("6.012"),
        "residual_exit_net_value": Decimal("5.74"),
        "residual_pnl": Decimal("-0.272"),
        "paid_rewards": Decimal("0.20"),
        "realized_net_pnl": Decimal("0.582"),
    }

    without_exit_fee = {**opening, "projected_exit_fee": None}
    unknown_exit = polymarket_lp_views.lp_report_totals(
        without_exit_fee, paid_rewards=Decimal("0.20")
    )
    assert unknown_exit["realized_trade_pnl"] == Decimal("0.382")
    assert unknown_exit["residual_exit_net_value"] is None
    assert unknown_exit["residual_pnl"] is None
    assert unknown_exit["realized_net_pnl"] == Decimal("0.582")

    unknown_paid = polymarket_lp_views.lp_report_totals(opening, paid_rewards=None)
    assert unknown_paid["paid_rewards"] is None
    assert unknown_paid["realized_net_pnl"] is None

    unknown_buy_fees = polymarket_lp_views.lp_report_totals(
        {**opening, "buy_fees": None}, paid_rewards=Decimal("0.20")
    )
    assert unknown_buy_fees["realized_trade_pnl"] is None
    assert unknown_buy_fees["residual_cost"] is None
    assert unknown_buy_fees["realized_net_pnl"] is None

    unknown_sell_fees = polymarket_lp_views.lp_report_totals(
        {**opening, "sell_fees": None}, paid_rewards=Decimal("0.20")
    )
    assert unknown_sell_fees["realized_trade_pnl"] is None
    assert unknown_sell_fees["residual_cost"] == Decimal("6.012")
    assert unknown_sell_fees["realized_net_pnl"] is None
