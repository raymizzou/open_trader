"""Read-only projections for the Polymarket LP panel."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from .polymarket_lp import (
    LP_CANDIDATE_REFRESH_SECONDS,
    STOP_LOSS,
    TERMINAL_ORDER_STATES,
    PolymarketLPService,
    _decimal,
    _field,
    _freshness,
    _items,
    _iso,
    _maybe_decimal,
    _timestamp,
)

_BEIJING = ZoneInfo("Asia/Shanghai")


def _next_review_at(now: datetime) -> datetime:
    local_now = now.astimezone(_BEIJING)
    review_at = datetime.combine(local_now.date(), time(8), tzinfo=_BEIJING)
    if local_now >= review_at:
        review_at += timedelta(days=1)
    return review_at.astimezone(UTC)


def _account_after_reservations(
    account: Mapping[str, object], reservations: object
) -> dict[str, object] | None:
    balance = _maybe_decimal(account.get("balance"))
    allowance = _maybe_decimal(account.get("allowance"))
    if balance is None or allowance is None or balance < 0 or allowance < 0:
        return None

    reservation_by_id: dict[str, Decimal] = {}
    for reservation in _items(reservations):
        if not isinstance(reservation, Mapping):
            return None
        order_id = str(reservation.get("order_id") or "").strip()
        amount = _maybe_decimal(reservation.get("amount"))
        if not order_id or amount is None or amount < 0:
            return None
        previous = reservation_by_id.get(order_id)
        if previous is not None and previous != amount:
            return None
        reservation_by_id[order_id] = amount

    open_buy_amount = Decimal("0")
    counted_open_ids: set[str] = set()
    seen_open_ids: set[str] = set()
    for order in _items(account.get("open_orders")):
        order_id = str(_field(order, "order_id", _field(order, "id", "")) or "").strip()
        side = str(_field(order, "side", "")).upper()
        status = str(_field(order, "status", "")).upper()
        if status in TERMINAL_ORDER_STATES:
            continue
        if side not in {"BUY", "SELL"}:
            return None
        if side != "BUY":
            continue
        if order_id and order_id in seen_open_ids:
            continue
        if order_id:
            seen_open_ids.add(order_id)
        price = _maybe_decimal(_field(order, "price"))
        remaining = _maybe_decimal(
            _field(order, "remaining_size", _field(order, "size"))
        )
        if remaining is None:
            original = _maybe_decimal(_field(order, "original_size"))
            matched = _maybe_decimal(_field(order, "size_matched", 0))
            if original is not None and matched is not None:
                remaining = max(Decimal("0"), original - matched)
        amount = (
            price * remaining
            if price is not None and price > 0 and remaining is not None and remaining >= 0
            else reservation_by_id.get(order_id)
            if order_id
            else None
        )
        if amount is None:
            return None
        open_buy_amount += amount
        if order_id:
            counted_open_ids.add(order_id)

    reserved_amount = sum(
        (
            amount
            for order_id, amount in reservation_by_id.items()
            if order_id not in counted_open_ids
        ),
        Decimal("0"),
    )
    available = dict(account)
    available["balance"] = balance - open_buy_amount - reserved_amount
    available["allowance"] = allowance - open_buy_amount - reserved_amount
    return available


def _has_market_order(account: Mapping[str, object], market: Mapping[str, object]) -> bool:
    market_id = str(market.get("market_id") or "")
    condition_id = str(market.get("condition_id") or "")
    token_id = str(market.get("token_id") or "")
    for order in _items(account.get("open_orders")):
        status = str(_field(order, "status", "")).upper()
        if status in TERMINAL_ORDER_STATES:
            continue
        token = str(_field(order, "token_id", _field(order, "asset_id", "")) or "")
        order_market = str(
            _field(order, "market_id", _field(order, "market", "")) or ""
        )
        order_condition = str(_field(order, "condition_id", "") or "")
        if token == token_id or order_market in {market_id, condition_id} or order_condition == condition_id:
            return True
    for position in _items(account.get("positions")):
        size = _maybe_decimal(
            _field(position, "size", _field(position, "quantity"))
        )
        if size is None:
            return True
        if size <= 0:
            continue
        token = str(_field(position, "token_id", _field(position, "asset_id", "")) or "")
        position_market = str(
            _field(position, "market_id", _field(position, "market", "")) or ""
        )
        position_condition = str(_field(position, "condition_id", "") or "")
        if (
            token == token_id
            or position_market in {market_id, condition_id}
            or position_condition == condition_id
        ):
            return True
    return False


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
