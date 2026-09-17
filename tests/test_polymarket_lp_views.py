from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from open_trader import polymarket_lp_views
from open_trader.polymarket_lp_views import lp_candidate_rows, lp_shortlist


NOW = datetime(2026, 9, 15, 1, 0, tzinfo=UTC)


def test_lp_shortlist_uses_available_facts_and_caps_markets_before_risk() -> None:
    def direction(
        market_id: str,
        pool: int,
        *,
        amplitude: str = "0.005",
        history_state: str = "known",
        outcome: str = "YES",
    ) -> dict[str, object]:
        return {
            "market": {
                "market_id": market_id,
                "condition_id": f"condition-{market_id}",
                "outcome": outcome,
                "accepting_orders": True,
            },
            "daily_pool_usd": Decimal(pool),
            "reward_active": True,
            "history_summary": {
                "state": history_state,
                "amplitude": Decimal(amplitude),
                "checked_at": NOW,
                "window_start": NOW - timedelta(hours=24),
                "window_end": NOW,
            },
        }

    directions = [
        direction(f"M{index:02d}", 601 - index)
        for index in range(1, 61)
    ]
    directions.extend(
        (
            direction("V", 1000, amplitude="0.0101"),
            direction("U", 999, history_state="unknown"),
            direction("H", 998, history_state="insufficient_history"),
            direction("M01", 601, outcome="NO"),
        )
    )

    shortlisted = lp_shortlist(directions, now=NOW)
    assert [row["market_id"] for row in shortlisted] == [
        f"M{index:02d}" for index in range(1, 51)
    ]

    exact_boundary = direction("E", 2000, amplitude="0.0100")
    with_boundary = lp_shortlist([exact_boundary, *directions], now=NOW)
    assert [row["market_id"] for row in with_boundary[:2]] == ["E", "M01"]
    assert len(with_boundary) == 50
    assert all("risk" not in row for row in with_boundary)


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


def test_lp_screening_requires_a_complete_one_hour_midpoint_window() -> None:
    direction = _direction(
        "screen-market",
        "condition-screen",
        "YES",
        "screen-token",
        "100",
        [("0.50", "100")],
    )
    market = direction["market"]
    assert isinstance(market, dict)
    market["price_change_24h"] = Decimal("0.90")
    market["volume_24hr"] = Decimal("0")

    def history(
        *,
        interval_seconds: int = 5,
        omitted_elapsed: frozenset[int] = frozenset(),
        spike_elapsed: int | None = None,
    ) -> list[dict[str, object]]:
        rows = []
        for elapsed in range(0, 3601, interval_seconds):
            if elapsed in omitted_elapsed:
                continue
            midpoint = (
                Decimal("0.500")
                + Decimal("0.010") * Decimal(elapsed) / Decimal("3600")
            )
            if spike_elapsed is not None:
                midpoint = (
                    Decimal("0.530") if elapsed == spike_elapsed else Decimal("0.500")
                )
            rows.append(
                {
                    "condition_id": "condition-screen",
                    "token_id": "screen-token",
                    "received_at": NOW - timedelta(seconds=3600 - elapsed),
                    "best_bid_price": midpoint - Decimal("0.01"),
                    "best_bid_size": Decimal("100"),
                    "best_ask_price": midpoint + Decimal("0.01"),
                    "best_ask_size": Decimal("100"),
                }
            )
        return rows

    stable = polymarket_lp_views.screen_lp_direction(
        direction, history=history(), now=NOW
    )
    assert stable["state"] == "eligible"
    assert stable["stability_range"] == Decimal("0.010")

    missing_reference_direction = {
        **direction,
        "market": {
            key: value
            for key, value in market.items()
            if key not in {"price_change_24h", "price_change_24h_source"}
        },
    }
    missing_reference = polymarket_lp_views.screen_lp_direction(
        missing_reference_direction, history=history(), now=NOW
    )
    assert missing_reference["state"] == "eligible"
    assert missing_reference["price_change_24h"] is None
    assert missing_reference["price_change_24h_source"] is None

    volatile = polymarket_lp_views.screen_lp_direction(
        direction, history=history(spike_elapsed=1800), now=NOW
    )
    assert volatile["state"] == "rejected"
    assert volatile["stability_range"] == Decimal("0.030")

    ten_second_gap = polymarket_lp_views.screen_lp_direction(
        direction,
        history=history(interval_seconds=10),
        now=NOW,
    )
    assert ten_second_gap["state"] == "eligible"

    over_ten_second_gap = polymarket_lp_views.screen_lp_direction(
        direction,
        history=history(omitted_elapsed=frozenset({1800, 1805})),
        now=NOW,
    )
    assert over_ten_second_gap["state"] == "unknown"

    missing_window_start = polymarket_lp_views.screen_lp_direction(
        direction, history=history(omitted_elapsed=frozenset({0})), now=NOW
    )
    assert missing_window_start["state"] == "unknown"

    flat_trade_only = {
        **direction,
        "market": {**market, "last_trade_price": Decimal("0.50")},
    }
    without_bbo_history = polymarket_lp_views.screen_lp_direction(
        flat_trade_only, history=(), now=NOW
    )
    assert without_bbo_history["state"] == "unknown"


def test_lp_recommendations_rank_all_markets_and_keep_outcomes_alternative() -> None:
    directions: list[dict[str, object]] = []
    histories: dict[tuple[str, str], list[dict[str, object]]] = {}
    market_specs = (
        ("C", "condition-c", "200", "41", False, False),
        ("A", "condition-a", "100", "60", True, False),
        ("B", "condition-b", "100", "80", False, False),
        ("D", "condition-d", "100", "60", False, True),
        ("E", "condition-e", "300", "60", False, False),
    )
    for market_id, condition_id, pool, side_quantity, include_far_bid, omit_no in market_specs:
        for outcome in (("YES",) if omit_no else ("YES", "NO")):
            is_yes = outcome == "YES"
            top_bid = Decimal("0.51" if is_yes else "0.41")
            deep_bid = top_bid - Decimal("0.01")
            ask = top_bid + Decimal("0.01")
            ask_size = Decimal(side_quantity) - Decimal("21")
            raw_bids = [(str(top_bid), "1"), (str(deep_bid), "20")]
            book_bids = [
                {"price": top_bid, "size": Decimal("1")},
                {"price": deep_bid, "size": Decimal("20")},
            ]
            if include_far_bid and is_yes:
                raw_bids.append(("0.10", "10000"))
                book_bids.append(
                    {"price": Decimal("0.10"), "size": Decimal("10000")}
                )
            direction = _direction(
                market_id,
                condition_id,
                outcome,
                f"{market_id.lower()}-{outcome.lower()}",
                pool,
                raw_bids,
            )
            market = direction["market"]
            assert isinstance(market, dict)
            market["metadata_checked_at"] = NOW
            book = direction["book"]
            assert isinstance(book, dict)
            book.update(
                {
                    "condition_id": condition_id,
                    "token_id": f"{market_id.lower()}-{outcome.lower()}",
                    "bids": book_bids,
                    "asks": [{"price": ask, "size": ask_size}],
                }
            )
            directions.append(direction)

            midpoint = (top_bid + ask) / Decimal("2")
            is_volatile = market_id == "E"
            histories[(condition_id, f"{market_id.lower()}-{outcome.lower()}")] = [
                {
                    "condition_id": condition_id,
                    "token_id": f"{market_id.lower()}-{outcome.lower()}",
                    "received_at": NOW - timedelta(seconds=3600 - elapsed),
                    "best_bid_price": (
                        midpoint + Decimal("0.02")
                        if is_volatile and elapsed == 1800
                        else midpoint - Decimal("0.01")
                    ),
                    "best_bid_size": Decimal("100"),
                    "best_ask_price": (
                        midpoint + Decimal("0.04")
                        if is_volatile and elapsed == 1800
                        else midpoint + Decimal("0.01")
                    ),
                    "best_ask_size": Decimal("100"),
                }
                for elapsed in range(0, 3601, 5)
            ]

    rows = polymarket_lp_views.lp_recommendation_rows(
        directions, histories=histories, account=_account(), now=NOW
    )

    assert [row["market_id"] for row in rows] == ["C", "A", "B", "D"]
    assert all(row["state"] == "eligible" for row in rows)
    by_market = {str(row["market_id"]): row for row in rows}
    assert by_market["C"]["daily_pool_usd"] == Decimal("200")
    assert by_market["A"]["competition_state"] == "known"
    assert by_market["A"]["competition_quantity"] == Decimal("120")
    assert by_market["B"]["competition_state"] == "known"
    assert by_market["B"]["competition_quantity"] == Decimal("160")
    assert by_market["D"]["competition_state"] == "unknown"
    assert by_market["D"]["competition_quantity"] is None
    assert "E" not in by_market

    a_directions = by_market["A"]["directions"]
    assert isinstance(a_directions, dict)
    assert set(a_directions) == {"YES", "NO"}
    yes_guidance = a_directions["YES"]["guidance"]
    no_guidance = a_directions["NO"]["guidance"]
    assert yes_guidance["price"] == Decimal("0.51")
    assert yes_guidance["required_capital"] == Decimal("10.20")
    assert no_guidance["price"] == Decimal("0.41")
    assert no_guidance["required_capital"] == Decimal("8.20")
