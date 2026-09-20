from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from open_trader import polymarket_lp_views
from open_trader.polymarket_lp_views import (
    lp_candidate_rows,
    lp_shortlist,
    lp_trial_candidates,
)


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


def _trial_direction(
    market_id: str,
    *,
    outcome: str = "YES",
    minimum_order_size: str = "10",
    reward_min_size: str = "20",
    latest_midpoint: str | None = "0.33",
    pool: str = "120",
    event_start: datetime | None = None,
) -> dict[str, object]:
    market: dict[str, object] = {
        "market_id": market_id,
        "condition_id": f"condition-{market_id}",
        "token_id": f"{market_id.lower()}-{outcome.lower()}",
        "outcome": outcome,
        "market_title": f"Market {market_id}",
        "market_url": f"https://polymarket.com/event/{market_id}",
        "accepting_orders": True,
        "minimum_order_size": Decimal(minimum_order_size),
        "reward_min_size": Decimal(reward_min_size),
    }
    if event_start is not None:
        market["event_ended"] = False
        market["event_start_time"] = event_start
    summary: dict[str, object] = {
        "state": "known",
        "amplitude": Decimal("0.005"),
        "checked_at": NOW,
        "window_start": NOW - timedelta(hours=24),
        "window_end": NOW,
        "sample_count": 2,
        "valid_until": NOW + timedelta(hours=24),
    }
    if latest_midpoint is not None:
        summary["latest_midpoint"] = Decimal(latest_midpoint)
    return {
        "market": market,
        "daily_pool_usd": Decimal(pool),
        "reward_active": True,
        "history_summary": summary,
    }


def _trial(
    directions: object,
    *,
    competition: object,
    available: str | None = "480",
) -> dict[str, object]:
    budget: dict[str, object] = (
        {} if available is None else {"available_capital": Decimal(available)}
    )
    return lp_trial_candidates(
        directions,
        competition=competition,
        account_budget_facts=budget,
        now=NOW,
    )


def test_trial_candidates_order_backup_queue_by_pool_then_market_id() -> None:
    # Backup rows carry no usable reference price, so the assumed upper bound
    # is undefined; they queue by daily pool descending, then market_id asc.
    def stale(market_id: str, pool: str) -> dict[str, object]:
        direction = _trial_direction(market_id, pool=pool, latest_midpoint="0.30")
        direction["history_summary"]["checked_at"] = NOW - timedelta(hours=2)
        return direction

    result = _trial(
        [stale("C", "200"), stale("A", "500"), stale("B", "100")],
        competition={},
    )

    # Pools 200/500/100 → order 500, 200, 100 (ids deliberately differ from
    # pool order so market_id sorting cannot fake a pool sort).
    assert [row["market_id"] for row in result["rows"]] == ["A", "C", "B"]
    assert all(row["queue"] == "backup" for row in result["rows"])

    tied = _trial(
        [stale("Z", "120"), stale("M", "120")],
        competition={},
    )
    assert [row["market_id"] for row in tied["rows"]] == ["M", "Z"]


def test_trial_candidates_report_query_rate_upper_bound_and_copy() -> None:
    # Independent arithmetic: capital = 20 × 0.45 = 9;
    # 5 / (24 × 9) × 100 = 2.3148148… → ROUND_HALF_UP 6dp → 2.314815.
    # Issue #138 round 2: the optimistic figure stays an internal queue key
    # and the row copy no longer presents it as an assumed hourly yield.
    direction = _trial_direction("A", pool="5", latest_midpoint="0.45")

    result = _trial([direction], competition={})

    row = result["rows"][0]
    assert row["query_rate_upper_bound"] == Decimal("2.314815")
    reason = " ".join(str(item) for item in row["reason"])
    assert "假设" not in reason
    assert "查询顺序" in reason
    assert "仅决定查询顺序" in reason
    assert "非预计收益" in reason


def test_trial_candidates_derive_min_quantity_and_reference_capital() -> None:
    direction = _trial_direction(
        "A", minimum_order_size="10", reward_min_size="20", latest_midpoint="0.33"
    )

    result = _trial([direction], competition={})

    row = result["rows"][0]
    assert row["market_id"] == "A"
    assert row["condition_id"] == "condition-A"
    assert row["min_quantity"] == Decimal("20")
    assert row["reference_capital"] == Decimal("6.60")
    assert row["daily_pool_usd"] == Decimal("120")


def test_trial_candidates_exclude_markets_over_available_capital() -> None:
    directions = [
        _trial_direction("A", reward_min_size="1000", latest_midpoint="0.20"),
        _trial_direction("B", reward_min_size="1000", latest_midpoint="0.20"),
        _trial_direction("C", reward_min_size="1000", latest_midpoint="0.20"),
        _trial_direction("D", reward_min_size="1000", latest_midpoint="0.52"),
    ]

    result = _trial(directions, competition={}, available="480")

    assert [row["market_id"] for row in result["rows"]] == ["A", "B", "C"]
    assert result["funnel"]["excluded"]["over_available"] == 1
    assert result["funnel"]["trial"] == 3

    tighter = _trial(directions, competition={}, available="100")
    assert tighter["rows"] == []
    assert tighter["funnel"]["excluded"]["over_available"] == 4
    assert tighter["funnel"]["trial"] == 0


def test_trial_candidates_cap_ten_report_gap_and_zero_competition() -> None:
    twelve = [_trial_direction(f"M{index:02d}") for index in range(1, 13)]
    competition = {
        f"condition-M{index:02d}": (Decimal(index) + Decimal("1"), NOW)
        for index in range(1, 13)
    }

    capped = _trial(twelve, competition=competition)
    assert len(capped["rows"]) == 10
    assert capped["rows"][0]["market_id"] == "M01"
    assert capped["funnel"]["trial"] == 10

    short = _trial(twelve[:7], competition=dict(list(competition.items())[:7]))
    assert len(short["rows"]) == 7
    assert short["funnel"]["gap_reason"]

    zero = _trial(
        [_trial_direction("Z")],
        competition={"condition-Z": (Decimal("0"), NOW)},
    )
    assert zero["rows"] == []
    assert zero["funnel"]["excluded"]["competition_empty"] == 1
    assert zero["funnel"]["sort"] == 0

    unread = _trial(
        [_trial_direction("U")],
        competition={"condition-U": (None, NOW)},
    )
    assert len(unread["rows"]) == 1
    assert unread["rows"][0]["competition"]["state"] == "unknown"
    assert unread["rows"][0]["competition"]["raw_value"] is None
    assert unread["funnel"]["excluded"]["competition_empty"] == 0


def test_trial_candidates_order_by_query_rate_upper_bound_first() -> None:
    # Independent arithmetic: X 1/(24×10)×100 = 0.416667; Y 5/(24×9)×100 = 2.314815.
    # Y's competition value (50) must not outrank X's (1): the assumed hourly
    # upper bound is the primary sort key.
    directions = [
        _trial_direction("X", pool="1", latest_midpoint="0.50"),
        _trial_direction("Y", pool="5", latest_midpoint="0.45"),
    ]
    competition = {
        "condition-X": (Decimal("1"), NOW),
        "condition-Y": (Decimal("50"), NOW),
    }

    result = _trial(directions, competition=competition)

    assert [row["market_id"] for row in result["rows"]] == ["Y", "X"]


def test_trial_candidates_order_by_competition_pool_yield_and_identity() -> None:
    # Independent arithmetic: capital = 20 × midpoint; assumed hourly upper
    # bound = pool ÷ (24 × capital) × 100.
    # A: 200/10 → 83.333333, C/U: 120/8 → 62.5, B/D/E: 100/10 → 41.666667.
    # C and U tie → C has known competition 12.5, ranks before unknown U.
    # B/D/E tie → competition D(5), E(5) before B(12.5); D/E share competition
    # and capital → market_id D < E.
    directions = [
        _trial_direction("B", pool="100", latest_midpoint="0.50"),
        _trial_direction("A", pool="200", latest_midpoint="0.50"),
        _trial_direction("C", pool="120", latest_midpoint="0.40"),
        _trial_direction("E", pool="100", latest_midpoint="0.50"),
        _trial_direction("D", pool="100", latest_midpoint="0.50"),
        _trial_direction("U", pool="120", latest_midpoint="0.40"),
    ]
    competition = {
        "condition-A": (Decimal("28.4"), NOW),
        "condition-B": (Decimal("12.5"), NOW),
        "condition-C": (Decimal("12.5"), NOW),
        "condition-D": (Decimal("5"), NOW),
        "condition-E": (Decimal("5"), NOW),
    }

    result = _trial(directions, competition=competition)

    assert [row["market_id"] for row in result["rows"]] == [
        "A", "C", "U", "D", "E", "B",
    ]
    unknown_row = result["rows"][2]
    assert unknown_row["competition"]["state"] == "unknown"
    assert unknown_row["competition"]["value"] is None


def test_trial_candidates_assemble_batch_from_normal_and_backup_queues() -> None:
    # Independent arithmetic: pool 200-i ÷ (24 × 20 × 0.55) × 100 with
    # capital 11.00 gives each normal market a distinct assumed upper bound,
    # descending with pool. Backup markets carry a stale price.
    def normal(market_id: str, pool: str) -> dict[str, object]:
        return _trial_direction(market_id, pool=pool, latest_midpoint="0.55")

    def stale(market_id: str, pool: str) -> dict[str, object]:
        direction = normal(market_id, pool)
        direction["history_summary"]["checked_at"] = NOW - timedelta(hours=2)
        return direction

    twelve = [normal(f"M{index:02d}", str(200 - index)) for index in range(1, 13)]
    backups = [stale("B200", "200"), stale("B300", "300")]

    batched = _trial([*twelve, *backups], competition={})

    rows = batched["rows"]
    assert [row["market_id"] for row in rows] == [
        "M01", "M02", "M03", "M04", "M05", "M06",
        "M07", "M08", "M09", "B300",
    ]
    assert [row["queue"] for row in rows] == ["normal"] * 9 + ["backup"]
    assert all(row["verification"] == "pending" for row in rows)
    condition_ids = [row["condition_id"] for row in rows]
    assert len(condition_ids) == len(set(condition_ids))
    assert batched["funnel"]["normal_queue_count"] == 12
    assert batched["funnel"]["backup_queue_count"] == 2
    assert batched["funnel"]["reference_price_unknown"] == 2

    without_backup = _trial(twelve, competition={})
    assert [row["queue"] for row in without_backup["rows"]] == ["normal"] * 10

    four = [normal(f"N{index:02d}", str(200 - index)) for index in range(1, 5)]
    ten_backups = [
        stale(f"S{index:02d}", str(300 - index)) for index in range(1, 11)
    ]
    mixed = _trial([*four, *ten_backups], competition={})

    assert [row["queue"] for row in mixed["rows"]] == ["normal"] * 4 + ["backup"] * 6
    assert [row["market_id"] for row in mixed["rows"]][4:] == [
        "S01", "S02", "S03", "S04", "S05", "S06",
    ]
    assert mixed["funnel"]["normal_queue_count"] == 4
    assert mixed["funnel"]["backup_queue_count"] == 10
    assert mixed["funnel"]["reference_price_unknown"] == 10


def test_lp_trial_candidates_exposes_full_consumption_queues() -> None:
    """S1: the service consumes the full ordered queues, not just the display batch."""

    def normal(market_id: str, pool: str) -> dict[str, object]:
        return _trial_direction(market_id, pool=pool, latest_midpoint="0.55")

    def stale(market_id: str, pool: str) -> dict[str, object]:
        direction = normal(market_id, pool)
        direction["history_summary"]["checked_at"] = NOW - timedelta(hours=2)
        return direction

    fourteen = [normal(f"M{index:02d}", str(200 - index)) for index in range(1, 15)]
    backups = [stale("B200", "200"), stale("B300", "300")]

    result = _trial([*fourteen, *backups], competition={})

    # Independent order source: pool 200-i with equal capital means the
    # assumed upper bound descends with pool, so M01..M14; backups sort by
    # pool descending: B300 before B200.
    assert [row["market_id"] for row in result["queue_normal"]] == [
        f"M{index:02d}" for index in range(1, 15)
    ]
    assert [row["market_id"] for row in result["queue_backup"]] == ["B300", "B200"]
    assert all(row["queue"] == "normal" for row in result["queue_normal"])
    assert all(row["queue"] == "backup" for row in result["queue_backup"])
    condition_ids = [row["condition_id"] for row in result["queue_normal"]]
    assert len(condition_ids) == len(set(condition_ids)) == 14

    # Funnel counts and the ten-slot display batch keep their current meaning.
    assert result["funnel"]["normal_queue_count"] == 14
    assert result["funnel"]["backup_queue_count"] == 2
    assert result["funnel"]["trial"] == 10
    assert [row["market_id"] for row in result["rows"]] == [
        "M01", "M02", "M03", "M04", "M05", "M06",
        "M07", "M08", "M09", "B300",
    ]
    assert all(row["verification"] == "pending" for row in result["rows"])


def test_trial_candidates_exclude_known_capital_over_available_hard() -> None:
    # Independent arithmetic: quantity = max(1000, 1000) = 1000;
    # capital = 1000 × 0.50 = 500.
    direction = _trial_direction(
        "A",
        minimum_order_size="1000",
        reward_min_size="1000",
        latest_midpoint="0.50",
    )

    excluded = _trial([direction], competition={}, available="480")
    assert excluded["rows"] == []
    assert excluded["funnel"]["excluded"]["over_available"] == 1
    trial_codes = [
        row["code"] for row in excluded["funnel"]["reasons"]["trial"]
    ]
    assert trial_codes == ["capital_over_available"]

    affordable = _trial([dict(direction)], competition={}, available="600")
    assert [row["market_id"] for row in affordable["rows"]] == ["A"]
    assert affordable["rows"][0]["queue"] == "normal"

    unknown_available = _trial([dict(direction)], competition={}, available=None)
    assert [row["market_id"] for row in unknown_available["rows"]] == ["A"]
    assert unknown_available["rows"][0]["queue"] == "normal"
    assert unknown_available["funnel"]["excluded"]["over_available"] == 0


def test_trial_candidates_pick_fresh_lowest_capital_representative() -> None:
    # YES: price 0.30 fresh → capital 20 × 0.30 = 6.00. NO: price 0.20 checked
    # 2h ago → stale, so its would-be 4.00 capital is unusable and YES (the
    # only direction with a known price) represents the market.
    yes = _trial_direction("A", outcome="YES", latest_midpoint="0.30")
    no = _trial_direction("A", outcome="NO", latest_midpoint="0.20")
    no["history_summary"]["checked_at"] = NOW - timedelta(hours=2)

    result = _trial([yes, no], competition={})

    row = result["rows"][0]
    assert row["outcome"] == "YES"
    assert row["reference_capital"] == Decimal("6.00")
    assert row["queue"] == "normal"

    # Both directions stale → the market falls to the backup queue with no
    # reference capital; the row identity is the first direction by outcome
    # label then token_id ("NO" < "YES").
    stale_yes = _trial_direction("B", outcome="YES", latest_midpoint="0.30")
    stale_yes["history_summary"]["checked_at"] = NOW - timedelta(hours=2)
    stale_no = _trial_direction("B", outcome="NO", latest_midpoint="0.20")
    stale_no["history_summary"]["checked_at"] = NOW - timedelta(hours=2)

    backup = _trial([stale_yes, stale_no], competition={})

    backup_row = backup["rows"][0]
    assert backup_row["queue"] == "backup"
    assert backup_row["reference_capital"] is None
    assert backup_row["outcome"] == "NO"


def test_trial_candidates_gate_reference_price_by_one_hour_freshness() -> None:
    # The 24h amplitude summary is 2h old, so the market still passes the
    # base screen; but the reference price is older than 1h → stale: no
    # reference price/capital, backup queue only, amplitude evidence kept.
    direction = _trial_direction("S", latest_midpoint="0.30")
    direction["history_summary"]["checked_at"] = NOW - timedelta(hours=2)
    direction["history_summary"]["valid_until"] = NOW + timedelta(hours=22)

    result = _trial([direction], competition={})

    row = result["rows"][0]
    assert row["queue"] == "backup"
    assert row["reference_price"] is None
    assert row["reference_capital"] is None
    assert row["reference_price_state"] == "stale"
    assert row["summary"]["amplitude"] == Decimal("0.005")


def test_trial_candidates_tie_breaks_by_competition_capital_market_id() -> None:
    # Independent arithmetic: pool 120 ÷ (24 × 12) × 100 = 41.666667 for each
    # of JIA/YI/BING, so the tie-break chain decides. YI carries competition 8
    # fetched 50 min ago (still known; fetch time is not compared within 1h),
    # JIA 12.5 fetched 5 min ago, BING has no entry → unknown sorts after known.
    tied = [
        _trial_direction("JIA", pool="120", latest_midpoint="0.60"),
        _trial_direction("YI", pool="120", latest_midpoint="0.60"),
        _trial_direction("BING", pool="120", latest_midpoint="0.60"),
    ]
    competition = {
        "condition-JIA": (Decimal("12.5"), NOW - timedelta(minutes=5)),
        "condition-YI": (Decimal("8"), NOW - timedelta(minutes=50)),
    }

    result = _trial(tied, competition=competition)

    assert [row["market_id"] for row in result["rows"]] == ["YI", "JIA", "BING"]
    assert result["rows"][0]["competition"]["state"] == "known"

    # Same assumed upper bound and competition → lower reference capital
    # first: D2 60/(24×6)×100 = 41.666667 (capital 6) vs D1 120/(24×12)×100
    # = 41.666667 (capital 12).
    capitals = [
        _trial_direction("D1", pool="120", latest_midpoint="0.60"),
        _trial_direction("D2", pool="60", latest_midpoint="0.30"),
    ]
    same_competition = {
        "condition-D1": (Decimal("5"), NOW),
        "condition-D2": (Decimal("5"), NOW),
    }
    capital_order = _trial(capitals, competition=same_competition)
    assert [row["market_id"] for row in capital_order["rows"]] == ["D2", "D1"]

    # Same assumed upper bound, competition, and capital → market_id ascending.
    identical = [
        _trial_direction("Z9", pool="120", latest_midpoint="0.60"),
        _trial_direction("A1", pool="120", latest_midpoint="0.60"),
    ]
    market_order = _trial(identical, competition=same_competition)
    assert [row["market_id"] for row in market_order["rows"]] == ["A1", "Z9"]


def test_trial_candidates_treat_stale_competition_as_unusable() -> None:
    stale = _trial(
        [_trial_direction("S")],
        competition={
            "condition-S": (Decimal("12.5"), NOW - timedelta(hours=2)),
        },
    )
    row = stale["rows"][0]
    assert row["competition"]["state"] == "unknown"
    assert row["competition"]["stale"] is True
    assert row["competition"]["raw_value"] == Decimal("12.5")
    assert row["competition"]["value"] is None

    # With equal assumed upper bounds, fresh competition ranks ahead of a
    # stale value; a higher assumed upper bound always outranks competition.
    mixed = _trial(
        [_trial_direction("S"), _trial_direction("T")],
        competition={
            "condition-S": (Decimal("12.5"), NOW - timedelta(hours=2)),
            "condition-T": (Decimal("99"), NOW),
        },
    )
    assert [row["market_id"] for row in mixed["rows"]] == ["T", "S"]


def test_trial_candidates_report_expired_history_base_rejection() -> None:
    """Expired summaries vanish in the shortlist; the base stage must say why."""
    expired_by_age = _trial_direction("X")
    expired_by_age["history_summary"]["checked_at"] = NOW - timedelta(hours=25)
    expired_by_validity = _trial_direction("Y")
    expired_by_validity["history_summary"]["valid_until"] = NOW

    result = _trial([expired_by_age, expired_by_validity], competition={})

    assert result["rows"] == []
    base_codes = [row["code"] for row in result["funnel"]["reasons"]["base"]]
    assert base_codes.count("history_summary_expired") == 2


def test_trial_candidates_report_event_window_base_rejections() -> None:
    """In-progress and starting-soon markets are base-stage rejections."""
    in_progress = _trial_direction("G", event_start=NOW)
    starting_soon = _trial_direction("H", event_start=NOW + timedelta(minutes=30))

    result = _trial([in_progress, starting_soon], competition={})

    assert result["rows"] == []
    assert result["funnel"]["base"] == 0
    base_codes = {row["code"] for row in result["funnel"]["reasons"]["base"]}
    assert "event_in_progress" in base_codes
    assert "event_starting_soon" in base_codes


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
