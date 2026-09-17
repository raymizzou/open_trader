"""Read-only projections for the Polymarket LP panel."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from .polymarket_lp_risk import (
    _account_after_reservations,
    _has_market_order,
    evaluate_lp_entry,
)
from .polymarket_lp import (
    LP_CANDIDATE_REFRESH_SECONDS,
    STOP_LOSS,
    PolymarketLPService,
    _decimal,
    _freshness,
    _iso,
    _maybe_decimal,
    _timestamp,
)

_BEIJING = ZoneInfo("Asia/Shanghai")
LP_DAILY_AMPLITUDE_LIMIT = Decimal("0.01")
LP_SHORTLIST_LIMIT = 50


def _lp_history_summary(direction: Mapping[str, object]) -> Mapping[str, object] | None:
    value = direction.get("history_summary")
    if isinstance(value, Mapping):
        return value
    market = direction.get("market")
    if isinstance(market, Mapping):
        value = market.get("history_summary")
        if isinstance(value, Mapping):
            return value
    return None


def _lp_summary_amplitude(summary: Mapping[str, object]) -> Decimal | None:
    return _maybe_decimal(summary.get("amplitude"))


def _lp_shortlist_rows(
    direction_facts: object,
    *,
    now: datetime | None = None,
) -> list[dict[str, object]]:
    """Build every market passing the light rules before the fixed cap."""

    if not isinstance(direction_facts, (list, tuple)):
        return []
    checked_at = now.astimezone(UTC) if isinstance(now, datetime) and now.tzinfo else None
    markets: dict[str, dict[str, object]] = {}
    for direction in direction_facts:
        if not isinstance(direction, Mapping):
            continue
        market = direction.get("market")
        if not isinstance(market, Mapping):
            market = direction
        condition_id = str(market.get("condition_id") or "").strip()
        market_id = str(market.get("market_id") or condition_id).strip()
        if not condition_id or not market_id:
            continue
        if direction.get("reward_active") is not True:
            continue
        pool = _maybe_decimal(direction.get("daily_pool_usd", market.get("daily_pool_usd")))
        if pool is None or pool <= 0 or market.get("accepting_orders") is not True:
            continue
        if any(direction.get(key) is True or market.get(key) is True for key in ("participating", "already_participating", "known_participation")):
            continue
        summary = _lp_history_summary(direction)
        if summary is None or str(summary.get("state") or "").lower() not in {"known", "ready", "eligible"}:
            continue
        amplitude = _lp_summary_amplitude(summary)
        if amplitude is None or amplitude < 0 or amplitude > LP_DAILY_AMPLITUDE_LIMIT:
            continue
        if checked_at is not None:
            checked_value = summary.get("checked_at", summary.get("updated_at"))
            try:
                checked_summary_at = _timestamp(checked_value, name="history_checked_at")
            except ValueError:
                continue
            age = (checked_at - checked_summary_at).total_seconds()
            if age < 0 or age >= 2 * 60 * 60:
                continue
            valid_until = summary.get("valid_until")
            if valid_until is not None:
                try:
                    if checked_at >= _timestamp(valid_until, name="history_valid_until"):
                        continue
                except ValueError:
                    continue
        row = markets.setdefault(
            condition_id,
            {
                "market_id": market_id,
                "condition_id": condition_id,
                "market_title": market.get("market_title"),
                "market_url": market.get("market_url"),
                "daily_pool_usd": pool,
                "state": "eligible",
                "directions": [],
            },
        )
        directions = row["directions"]
        if isinstance(directions, list):
            directions.append(
                {
                    "outcome": str(market.get("outcome") or direction.get("outcome") or "").upper(),
                    "token_id": market.get("token_id", direction.get("token_id")),
                    "history_summary": dict(summary),
                }
            )
        if pool > _maybe_decimal(row.get("daily_pool_usd")):
            row["daily_pool_usd"] = pool
    rows = list(markets.values())
    rows.sort(key=lambda row: (-_maybe_decimal(row.get("daily_pool_usd")) or Decimal("0"), str(row.get("market_id") or row.get("condition_id") or "")))
    return rows


def lp_shortlist(
    direction_facts: object,
    *,
    now: datetime | None = None,
    limit: int = LP_SHORTLIST_LIMIT,
) -> list[dict[str, object]]:
    """Select reward markets from prepared facts before any risk reads."""

    if type(limit) is not int or limit < 1:
        return []
    return _lp_shortlist_rows(direction_facts, now=now)[:LP_SHORTLIST_LIMIT]


def _next_review_at(now: datetime) -> datetime:
    local_now = now.astimezone(_BEIJING)
    review_at = datetime.combine(local_now.date(), time(8), tzinfo=_BEIJING)
    if local_now >= review_at:
        review_at += timedelta(days=1)
    return review_at.astimezone(UTC)


def screen_lp_direction(
    direction: object,
    *,
    history: object,
    now: datetime,
) -> dict[str, object]:
    """Screen one reward direction using only market facts and BBO history."""

    result: dict[str, object] = {
        "state": "unknown",
        "reason_codes": [],
        "stability_range": None,
        "stability_min_midpoint": None,
        "stability_max_midpoint": None,
        "stability_sample_count": 0,
        "competition_state": "unknown",
        "competition_quantity": None,
        "price_change_24h": None,
        "price_change_24h_source": None,
    }

    def unknown(reason: str) -> dict[str, object]:
        result["reason_codes"] = [reason]
        return result

    if not isinstance(now, datetime) or now.tzinfo is None:
        return unknown("screen_time_unknown")
    checked_at = now.astimezone(UTC)
    if not isinstance(direction, Mapping):
        return unknown("market_facts_unknown")
    market = direction.get("market")
    book = direction.get("book")
    if not isinstance(market, Mapping):
        return unknown("market_facts_unknown")
    price_change_24h = _maybe_decimal(market.get("price_change_24h"))
    price_change_24h_source = market.get("price_change_24h_source")
    if price_change_24h is not None:
        result["price_change_24h"] = price_change_24h
        result["price_change_24h_source"] = (
            price_change_24h_source
            if isinstance(price_change_24h_source, str)
            and price_change_24h_source.strip()
            else None
        )

    condition_id = str(market.get("condition_id") or "").strip()
    token_id = str(market.get("token_id") or "").strip()
    if not condition_id or not token_id:
        return unknown("market_identity_unknown")
    if direction.get("reward_active") is False:
        result["state"] = "rejected"
        result["reason_codes"] = ["reward_inactive"]
        return result
    if direction.get("reward_active") is not True:
        return unknown("reward_status_unknown")
    pool = _maybe_decimal(direction.get("daily_pool_usd"))
    if pool is None:
        return unknown("reward_pool_unknown")
    if pool <= 0:
        result["state"] = "rejected"
        result["reason_codes"] = ["reward_pool_empty"]
        return result
    if market.get("accepting_orders") is False:
        result["state"] = "rejected"
        result["reason_codes"] = ["market_not_accepting_orders"]
        return result
    if market.get("accepting_orders") is not True:
        return unknown("market_status_unknown")

    reward_stamp = direction.get("reward_checked_at")
    try:
        reward_age = (checked_at - _timestamp(reward_stamp, name="reward_checked_at")).total_seconds()
    except ValueError:
        return unknown("reward_freshness_unknown")
    if reward_age < 0 or reward_age > 60:
        return unknown("reward_data_stale")

    history_rows = history if isinstance(history, (list, tuple)) else None
    if history_rows is None:
        return unknown("stability_history_unknown")

    def levels_for(value: Mapping[str, object]) -> tuple[
        list[tuple[Decimal, Decimal]], list[tuple[Decimal, Decimal]]
    ] | None:
        compact_names = (
            "best_bid_price",
            "best_bid_size",
            "best_ask_price",
            "best_ask_size",
        )
        if any(name in value for name in compact_names):
            bid_price, bid_size, ask_price, ask_size = (
                _maybe_decimal(value.get(name)) for name in compact_names
            )
            if (
                bid_price is None
                or bid_size is None
                or ask_price is None
                or ask_size is None
                or not (0 < bid_price <= 1)
                or not (0 < ask_price <= 1)
                or bid_size <= 0
                or ask_size <= 0
            ):
                return None
            return ([(bid_price, bid_size)], [(ask_price, ask_size)])
        try:
            return (
                PolymarketLPService._levels(value.get("bids"), "bids"),
                PolymarketLPService._levels(value.get("asks"), "asks"),
            )
        except (TypeError, ValueError):
            return None

    window_start = checked_at - timedelta(hours=1)
    earliest_anchor = window_start - timedelta(seconds=10)
    samples: list[tuple[datetime, Decimal]] = []
    for row in history_rows:
        if not isinstance(row, Mapping):
            continue
        if (
            row.get("condition_id") != condition_id
            or row.get("token_id") != token_id
        ):
            continue
        try:
            received_at = _timestamp(row.get("received_at"), name="received_at")
        except ValueError:
            continue
        if received_at < earliest_anchor or received_at > checked_at:
            continue
        levels = levels_for(row)
        if levels is None:
            continue
        bids, asks = levels
        if not bids or not asks:
            continue
        best_bid = max(price for price, _ in bids)
        best_ask = min(price for price, _ in asks)
        if best_bid >= best_ask:
            continue
        samples.append((received_at, (best_bid + best_ask) / Decimal("2")))

    samples.sort(key=lambda sample: sample[0])
    anchors = [sample for sample in samples if sample[0] < window_start]
    window_samples = [sample for sample in samples if sample[0] >= window_start]
    covered = bool(window_samples) and (
        window_samples[0][0] == window_start or bool(anchors)
    )
    if anchors:
        latest_anchor = anchors[-1]
        covered = covered and (window_start - latest_anchor[0]).total_seconds() <= 10
        coverage_samples = [latest_anchor, *window_samples]
    else:
        coverage_samples = window_samples
    covered = covered and (
        (checked_at - coverage_samples[-1][0]).total_seconds() <= 10
    ) if coverage_samples else False
    covered = covered and all(
        (current[0] - previous[0]).total_seconds() <= 10
        for previous, current in zip(coverage_samples, coverage_samples[1:])
    )
    if not covered:
        return unknown("stability_history_incomplete")

    midpoints = [sample[1] for sample in coverage_samples]
    minimum_midpoint = min(midpoints)
    maximum_midpoint = max(midpoints)
    stability_range = maximum_midpoint - minimum_midpoint
    result.update(
        {
            "stability_range": stability_range,
            "stability_min_midpoint": minimum_midpoint,
            "stability_max_midpoint": maximum_midpoint,
            "stability_sample_count": len(coverage_samples),
        }
    )

    competition_state = "unknown"
    competition_quantity: Decimal | None = None
    spread = _maybe_decimal(market.get("reward_max_spread"))
    if isinstance(book, Mapping) and spread is not None and spread > 0:
        book_condition = book.get("condition_id", book.get("market"))
        book_token = book.get("token_id", book.get("asset_id"))
        try:
            book_age = (
                checked_at - _timestamp(book.get("received_at"), name="received_at")
            ).total_seconds()
            book_levels = levels_for(book)
        except ValueError:
            book_age = -1
            book_levels = None
        if (
            (book_condition is None or book_condition == condition_id)
            and (book_token is None or book_token == token_id)
            and 0 <= book_age <= 10
            and book_levels is not None
            and book_levels[0]
            and book_levels[1]
        ):
            bids, asks = book_levels
            best_bid = max(price for price, _ in bids)
            best_ask = min(price for price, _ in asks)
            if best_bid < best_ask:
                midpoint = (best_bid + best_ask) / Decimal("2")
                competition_quantity = sum(
                    (
                        size
                        for price, size in (*bids, *asks)
                        if abs(price - midpoint) < spread
                    ),
                    Decimal("0"),
                )
                competition_state = "known"

    result["competition_state"] = competition_state
    result["competition_quantity"] = competition_quantity
    if stability_range > Decimal("0.01"):
        result["state"] = "rejected"
        result["reason_codes"] = ["stability_range_exceeded"]
    else:
        result["state"] = "eligible"
    return result


def lp_recommendation_rows(
    direction_facts: object,
    *,
    histories: Mapping[tuple[str, str], object],
    account: Mapping[str, object],
    now: datetime,
    reservations: object = (),
) -> list[dict[str, object]]:
    """Return actionable market rows with independently screened outcomes."""

    if (
        not isinstance(now, datetime)
        or now.tzinfo is None
        or not isinstance(direction_facts, (list, tuple))
        or not isinstance(histories, Mapping)
        or not isinstance(account, Mapping)
    ):
        return []
    checked_at = now.astimezone(UTC)
    market_competition: dict[str, dict[str, Decimal | None]] = {}
    recommendations: dict[str, dict[str, object]] = {}

    for direction in direction_facts:
        if not isinstance(direction, Mapping):
            continue
        market = direction.get("market")
        if not isinstance(market, Mapping):
            continue
        condition_id = str(market.get("condition_id") or "").strip()
        token_id = str(market.get("token_id") or "").strip()
        outcome = str(market.get("outcome") or "").strip().upper()
        if not condition_id or not token_id or outcome not in {"YES", "NO"}:
            continue

        screening = screen_lp_direction(
            direction,
            history=histories.get((condition_id, token_id)),
            now=checked_at,
        )
        competition = market_competition.setdefault(condition_id, {})
        quantity = _maybe_decimal(screening.get("competition_quantity"))
        competition[outcome] = (
            quantity if screening.get("competition_state") == "known" else None
        )
        if screening.get("state") != "eligible":
            continue

        entry = evaluate_lp_entry(
            {**dict(direction), "screening": screening},
            account=account,
            now=checked_at,
            reservations=reservations,
        )
        guidance = entry.get("guidance")
        pool = _maybe_decimal(direction.get("daily_pool_usd"))
        if entry.get("state") != "eligible" or not isinstance(guidance, Mapping) or pool is None:
            continue

        row = recommendations.get(condition_id)
        if row is None:
            row = {
                "market_id": market.get("market_id"),
                "condition_id": condition_id,
                "market_title": market.get("market_title"),
                "market_url": market.get("market_url"),
                "daily_pool_usd": pool,
                "state": "eligible",
                "competition_state": "unknown",
                "competition_quantity": None,
                "directions": {},
            }
            recommendations[condition_id] = row
        directions = row["directions"]
        if isinstance(directions, dict):
            directions[outcome] = {
                "token_id": token_id,
                "state": entry.get("state"),
                "reason_codes": list(entry.get("reason_codes", ())),
                "screening": screening,
                "guidance": dict(guidance),
            }

    for condition_id, row in recommendations.items():
        outcomes = market_competition.get(condition_id, {})
        yes_quantity = outcomes.get("YES")
        no_quantity = outcomes.get("NO")
        if yes_quantity is not None and no_quantity is not None:
            total = yes_quantity + no_quantity
            row["competition_state"] = "known"
            row["competition_quantity"] = total
        directions = row["directions"]
        if isinstance(directions, dict):
            for recommendation in directions.values():
                if not isinstance(recommendation, dict):
                    continue
                screening = recommendation.get("screening")
                if isinstance(screening, Mapping):
                    recommendation["screening"] = {
                        **dict(screening),
                        "competition_state": row["competition_state"],
                        "competition_quantity": row["competition_quantity"],
                    }

    rows = list(recommendations.values())
    rows.sort(
        key=lambda row: (
            -_decimal(row["daily_pool_usd"], "daily_pool_usd"),
            row.get("competition_state") != "known",
            _maybe_decimal(row.get("competition_quantity")) or Decimal("0"),
            str(row.get("condition_id") or ""),
        )
    )
    return rows


def lp_candidate_rows(
    direction_facts: object,
    *,
    account: Mapping[str, object],
    now: datetime,
    reservations: object = (),
) -> list[dict[str, object]]:
    """Return currently qualifying minimum-size BUY opportunities."""

    if not isinstance(now, datetime) or now.tzinfo is None:
        return []
    checked_at = now.astimezone(UTC)
    if not isinstance(account, Mapping) or not isinstance(direction_facts, (list, tuple)):
        return []
    try:
        _freshness(account.get("checked_at"), checked_at, "account_freshness")
    except ValueError:
        return []
    available_account = _account_after_reservations(account, reservations)
    if available_account is None:
        return []

    review_at = _next_review_at(checked_at)
    rows: list[dict[str, object]] = []
    for direction in direction_facts:
        if not isinstance(direction, Mapping):
            continue
        market = direction.get("market")
        book = direction.get("book")
        if not isinstance(market, Mapping) or not isinstance(book, Mapping):
            continue
        if direction.get("reward_active") is not True:
            continue
        pool = _maybe_decimal(direction.get("daily_pool_usd"))
        if pool is None or pool <= 0:
            continue
        reward_checked_at = direction.get("reward_checked_at")
        if reward_checked_at is None:
            continue
        try:
            reward_stamp = _timestamp(reward_checked_at, name="reward_checked_at")
            age = Decimal(str((checked_at - reward_stamp).total_seconds()))
            if age < 0 or age > LP_CANDIDATE_REFRESH_SECONDS:
                continue
            stamp = book.get("received_at")
            if stamp is None:
                continue
            _freshness(stamp, checked_at, "book_freshness")

            bids = PolymarketLPService._levels(book.get("bids"), "bids")
            if not bids:
                continue
            price = max(level_price for level_price, _ in bids)
            minimum = _decimal(market.get("minimum_order_size"), "minimum_order_size")
            reward_minimum = _decimal(market.get("reward_min_size"), "reward_min_size")
            quantity = max(minimum, reward_minimum)
            identity = {
                key: market.get(key)
                for key in ("market_id", "condition_id", "token_id", "outcome")
            }
            request = {
                **identity,
                "price": price,
                "quantity": quantity,
                "review_at": review_at,
            }
            if _has_market_order(account, market):
                continue
            snapshot = {"market": market, "book": book, "account": available_account}
            facts = PolymarketLPService._validate_snapshot(
                request, snapshot, now=checked_at
            )
            if abs(price - facts["midpoint"]) >= facts["reward_max_spread"]:
                continue
            maker_fee = _maybe_decimal(market.get("fee"))
            if market.get("fees_enabled") is False:
                maker_fee = Decimal("0")
            if maker_fee is None or maker_fee != 0:
                continue
            required_capital = price * quantity
            exit_value = PolymarketLPService._executable_bid_value(snapshot, quantity)
            exit_fee = PolymarketLPService._projected_taker_fee(snapshot, quantity)
            if exit_value is None or exit_fee is None:
                continue
            estimated_exit_loss = max(
                Decimal("0"), required_capital - exit_value + exit_fee
            )
            if estimated_exit_loss >= STOP_LOSS:
                continue
            rows.append(
                {
                    **identity,
                    "market_title": market.get("market_title"),
                    "market_url": market.get("market_url"),
                    "daily_pool_usd": pool,
                    "price": price,
                    "quantity": quantity,
                    "required_capital": required_capital,
                    "estimated_exit_loss": estimated_exit_loss,
                    "checked_at": _iso(checked_at),
                    "review_at": _iso(review_at),
                    "preflight": facts,
                }
            )
        except (ValueError, TypeError, ArithmeticError):
            continue

    rows.sort(
        key=lambda row: (
            -_decimal(row["daily_pool_usd"], "daily_pool_usd"),
            str(row.get("market_id") or ""),
            str(row.get("outcome") or ""),
        )
    )
    return rows


def lp_report_totals(
    opening: Mapping[str, object], *, paid_rewards: Decimal | None = None
) -> dict[str, object]:
    """Project realized trade P&L separately from marked open inventory."""

    buy_quantity = _maybe_decimal(opening.get("buy_filled_quantity"))
    buy_cost = _maybe_decimal(opening.get("buy_cost"))
    buy_fees = _maybe_decimal(opening.get("buy_fees"))
    sold_quantity = _maybe_decimal(opening.get("sold_quantity"))
    sold_revenue = _maybe_decimal(opening.get("sold_revenue"))
    sell_fees = _maybe_decimal(opening.get("sell_fees"))
    residual_quantity = _maybe_decimal(opening.get("residual_quantity"))
    residual_exit_value = _maybe_decimal(opening.get("residual_exit_value"))
    projected_exit_fee = _maybe_decimal(opening.get("projected_exit_fee"))
    verified_paid_rewards = _maybe_decimal(paid_rewards)

    def nonnegative(value: Decimal | None) -> Decimal | None:
        return value if value is not None and value >= 0 else None

    buy_quantity = nonnegative(buy_quantity)
    buy_cost = nonnegative(buy_cost)
    buy_fees = nonnegative(buy_fees)
    sold_quantity = nonnegative(sold_quantity)
    sold_revenue = nonnegative(sold_revenue)
    sell_fees = nonnegative(sell_fees)
    residual_quantity = nonnegative(residual_quantity)
    residual_exit_value = nonnegative(residual_exit_value)
    projected_exit_fee = nonnegative(projected_exit_fee)
    verified_paid_rewards = nonnegative(verified_paid_rewards)

    realized_trade_pnl: Decimal | None = None
    if (
        sold_quantity == 0
        and sold_revenue == 0
        and sell_fees == 0
    ):
        realized_trade_pnl = Decimal("0")
    elif (
        buy_quantity is not None
        and buy_quantity > 0
        and buy_cost is not None
        and buy_fees is not None
        and sold_quantity is not None
        and sold_quantity <= buy_quantity
        and sold_revenue is not None
        and sell_fees is not None
    ):
        sold_fraction = sold_quantity / buy_quantity
        allocated_buy_cost = buy_cost * sold_fraction
        allocated_buy_fees = buy_fees * sold_fraction
        realized_trade_pnl = (
            sold_revenue - allocated_buy_cost - allocated_buy_fees - sell_fees
        )

    residual_cost: Decimal | None = None
    if (
        buy_quantity is not None
        and buy_cost is not None
        and buy_fees is not None
        and residual_quantity is not None
        and (buy_quantity > 0 or residual_quantity == 0)
    ):
        residual_cost = (
            Decimal("0")
            if buy_quantity == 0
            else (buy_cost + buy_fees) * residual_quantity / buy_quantity
        )

    residual_exit_net_value = (
        residual_exit_value - projected_exit_fee
        if residual_exit_value is not None and projected_exit_fee is not None
        else None
    )
    residual_pnl = (
        residual_exit_net_value - residual_cost
        if residual_exit_net_value is not None and residual_cost is not None
        else None
    )
    realized_net_pnl = (
        realized_trade_pnl + verified_paid_rewards
        if realized_trade_pnl is not None and verified_paid_rewards is not None
        else None
    )
    return {
        "realized_trade_pnl": realized_trade_pnl,
        "residual_cost": residual_cost,
        "residual_exit_net_value": residual_exit_net_value,
        "residual_pnl": residual_pnl,
        "paid_rewards": verified_paid_rewards,
        "realized_net_pnl": realized_net_pnl,
    }
