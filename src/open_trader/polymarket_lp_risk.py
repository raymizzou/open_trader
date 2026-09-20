"""Pure screening-risk calculations shared by LP guidance and legacy sessions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import cast

BOOK_FRESHNESS_SECONDS = Decimal("10")
ACCOUNT_FRESHNESS_SECONDS = Decimal("120")
# Metadata caching may retain a positive market description for hours, while
# final entry risk must observe the market's dynamic state on a short bound.
_MARKET_METADATA_MAX_AGE_SECONDS = Decimal("60")
TERMINAL_ORDER_STATES = frozenset(
    {"FILLED", "MATCHED", "CANCELED", "CANCELLED", "REJECTED", "EXPIRED", "FAILED"}
)


def _field(value: object, name: str, default: object = None) -> object:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _items(value: object) -> tuple[object, ...]:
    if value is None or isinstance(value, (str, bytes, Mapping)):
        return () if value is None or isinstance(value, (str, bytes)) else (value,)
    try:
        return tuple(cast(Sequence[object], value))
    except TypeError:
        return (value,)


def _decimal(value: object, name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name}_invalid")
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name}_invalid") from exc
    if not parsed.is_finite():
        raise ValueError(f"{name}_invalid")
    return parsed


def _maybe_decimal(value: object) -> Decimal | None:
    try:
        result = _decimal(value, "value")
    except ValueError:
        return None
    return result


def _timestamp(value: object, *, name: str = "timestamp") -> datetime:
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            moment = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"{name}_invalid") from exc
    else:
        raise ValueError(f"{name}_invalid")
    if moment.tzinfo is None:
        raise ValueError(f"{name}_invalid")
    return moment.astimezone(UTC)


def _freshness(
    value: object,
    now: datetime,
    name: str,
    *,
    max_age: Decimal = BOOK_FRESHNESS_SECONDS,
) -> None:
    stamp = _timestamp(value, name=name)
    age = Decimal(str((now - stamp).total_seconds()))
    if age < 0 or age > max_age:
        raise ValueError(f"{name}_stale")


def _event_window_check(
    direction: Mapping[str, object], market: Mapping[str, object], now: datetime
) -> tuple[str | None, str | None, bool, datetime | None]:
    """Return a blocking event result, coverage state, and guidance deadline."""

    game_id = str(market.get("game_id") or "").strip()
    event_id = str(market.get("event_id") or "").strip()
    raw_starts = [
        market.get(field)
        for field in ("game_start_time", "event_start_time")
        if market.get(field) is not None
    ]
    starts: list[datetime] = []
    for value in raw_starts:
        try:
            starts.append(_timestamp(value, name="event_start_time"))
        except ValueError:
            return "unknown", "event_timing_unknown", False, None

    ended = market.get("event_ended")
    finished_at = market.get("event_finished_at")
    has_event_facts = bool(game_id or starts) or ended in (True, False) or finished_at is not None
    if not has_event_facts:
        # Gamma event ids can group markets without identifying a critical event.
        return None, None, True, None

    event_start = min(starts) if starts else None
    if ended is False:
        if event_start is None:
            return "unknown", "event_timing_unknown", False, None
        if now >= event_start:
            return "rejected", "event_in_progress", False, None
        if event_start - now <= timedelta(minutes=30):
            return "rejected", "event_starting_soon", False, None
        return None, None, False, event_start - timedelta(minutes=30)

    if ended is not True:
        return "unknown", "event_status_unknown", False, None

    end_value = finished_at
    if end_value is None:
        confirmation = direction.get("event_end_confirmation")
        if isinstance(confirmation, Mapping):
            confirmation_game_id = str(confirmation.get("game_id") or "").strip()
            confirmation_event_id = str(confirmation.get("event_id") or "").strip()
            game_matches = not game_id or confirmation_game_id == game_id
            event_matches = not event_id or confirmation_event_id == event_id
            if (game_id or event_id) and game_matches and event_matches:
                end_value = confirmation.get("confirmed_end_at")
    try:
        event_end = _timestamp(end_value, name="event_finished_at")
    except ValueError:
        return "unknown", "event_end_time_unknown", False, None
    if event_end > now:
        return "unknown", "event_end_time_in_future", False, None
    if now < event_end + timedelta(hours=1):
        return "rejected", "event_recovery_pending", False, None

    screening = direction.get("screening")
    if not isinstance(screening, Mapping):
        return "unknown", "post_event_screening_unknown", False, None
    screen_state = screening.get("state")
    if screen_state != "eligible":
        reasons = screening.get("reason_codes")
        reason = (
            str(reasons[0])
            if isinstance(reasons, Sequence)
            and not isinstance(reasons, (str, bytes))
            and reasons
            else "post_event_screening_unknown"
        )
        return ("rejected" if screen_state == "rejected" else "unknown"), reason, False, None
    return None, None, False, None


def _levels(value: object, name: str) -> list[tuple[Decimal, Decimal]]:
    rows: list[tuple[Decimal, Decimal]] = []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError("book_invalid")
    for row in value:
        price = _maybe_decimal(_field(row, "price"))
        size = _maybe_decimal(_field(row, "size", _field(row, "quantity")))
        if price is None or size is None or price <= 0 or price > 1 or size <= 0:
            continue
        rows.append((price, size))
    return rows


def _qualify_reward_quote(
    bids: Sequence[tuple[Decimal, Decimal]],
    asks: Sequence[tuple[Decimal, Decimal]],
    *,
    price: Decimal,
    reward_min_size: Decimal,
    reward_max_spread: Decimal,
    require_positive_score: bool = False,
    cumulative_depth: bool = False,
) -> tuple[tuple[Decimal, Decimal], tuple[Decimal, Decimal], Decimal]:
    if cumulative_depth:
        bid_depth = Decimal("0")
        bid = None
        for row in sorted(bids, key=lambda item: item[0], reverse=True):
            bid_depth += row[1]
            if bid_depth >= reward_min_size:
                bid = row
                break
        ask_depth = Decimal("0")
        ask = None
        for row in sorted(asks, key=lambda item: item[0]):
            ask_depth += row[1]
            if ask_depth >= reward_min_size:
                ask = row
                break
        if bid is None or ask is None:
            raise ValueError("midpoint_unknown")
    else:
        qualifying_asks = [row for row in asks if row[1] >= reward_min_size]
        qualifying_bids = [row for row in bids if row[1] >= reward_min_size]
        if not qualifying_asks or not qualifying_bids:
            raise ValueError("midpoint_unknown")
        ask = min(qualifying_asks, key=lambda row: row[0])
        bid = max(qualifying_bids, key=lambda row: row[0])
    midpoint = (ask[0] + bid[0]) / Decimal("2")
    if midpoint < Decimal("0.10") or midpoint > Decimal("0.90"):
        raise ValueError("midpoint_out_of_range")
    distance = abs(price - midpoint)
    if distance > reward_max_spread:
        raise ValueError("reward_distance_invalid")
    if require_positive_score and distance >= reward_max_spread:
        raise ValueError("reward_score_zero")
    return bid, ask, midpoint


def _projected_taker_fee(
    snapshot: Mapping[str, object], residual: Decimal
) -> Decimal | None:
    if residual <= 0:
        return Decimal("0")
    market = snapshot.get("market")
    if not isinstance(market, Mapping):
        return None
    if market.get("fees_enabled") is False:
        return Decimal("0")
    rate = _maybe_decimal(market.get("taker_fee_rate", market.get("fee_rate")))
    exponent = _maybe_decimal(market.get("fee_exponent", 1))
    book = snapshot.get("book")
    if (
        rate is None
        or exponent is None
        or rate < 0
        or exponent < 0
        or not isinstance(book, Mapping)
    ):
        return None
    try:
        bids = _levels(book.get("bids"), "bids")
    except ValueError:
        return None
    if not bids:
        return None
    remaining = residual
    total = Decimal("0")
    for price, size in sorted(bids, reverse=True):
        used = min(size, remaining)
        total += used * rate * (price * (Decimal("1") - price)) ** exponent
        remaining -= used
        if remaining <= 0:
            break
    if remaining > 0:
        return None
    return total.quantize(Decimal("0.00001"))


def _executable_bid_value(
    snapshot: Mapping[str, object], quantity: Decimal
) -> Decimal | None:
    book = snapshot.get("book")
    if not isinstance(book, Mapping) or quantity <= 0:
        return Decimal("0")
    try:
        rows = _levels(book.get("bids"), "bids")
    except ValueError:
        return None
    if not rows:
        return None
    remaining = quantity
    value = Decimal("0")
    for price, size in sorted(rows, reverse=True):
        used = min(size, remaining)
        value += used * price
        remaining -= used
        if remaining <= 0:
            break
    return value if remaining <= 0 else None


def _external_bid_sizes(
    bids: Sequence[tuple[Decimal, Decimal]],
    *,
    condition_id: str,
    token_id: str,
    own_orders: object,
) -> tuple[dict[Decimal, Decimal] | None, str | None]:
    own_size_by_price: dict[Decimal, Decimal] = {}
    for order in _items(own_orders):
        if not isinstance(order, Mapping):
            return None, "own_order_facts_unknown"
        if str(order.get("status") or "").upper() in TERMINAL_ORDER_STATES:
            continue
        side = str(order.get("side") or "").upper()
        if side not in {"BUY", "SELL"}:
            return None, "own_order_side_unknown"
        if side != "BUY":
            continue
        order_condition = order.get("condition_id", order.get("market"))
        order_token = order.get("token_id", order.get("asset_id"))
        if order_condition not in (None, "", condition_id):
            continue
        if order_token not in (None, "", token_id):
            continue
        if order_token in (None, ""):
            return None, "own_order_identity_unknown"
        order_price = _maybe_decimal(order.get("price"))
        remaining = _maybe_decimal(
            order.get(
                "remaining_size",
                order.get("remaining_quantity", order.get("size")),
            )
        )
        if remaining is None:
            original = _maybe_decimal(order.get("original_size"))
            matched = _maybe_decimal(order.get("size_matched", 0))
            if original is not None and matched is not None:
                remaining = max(Decimal("0"), original - matched)
        if order_price is None or remaining is None or order_price <= 0 or remaining < 0:
            return None, "own_order_depth_unknown"
        own_size_by_price[order_price] = (
            own_size_by_price.get(order_price, Decimal("0")) + remaining
        )

    bid_size_by_price: dict[Decimal, Decimal] = {}
    for level_price, size in bids:
        bid_size_by_price[level_price] = bid_size_by_price.get(
            level_price, Decimal("0")
        ) + size
    for level_price, own_size in own_size_by_price.items():
        if level_price in bid_size_by_price:
            bid_size_by_price[level_price] = max(
                Decimal("0"), bid_size_by_price[level_price] - own_size
            )
    return bid_size_by_price, None


def _lp_exit_values(
    market: Mapping[str, object],
    remaining_bids: Mapping[Decimal, Decimal],
    quantity: Decimal,
) -> tuple[Decimal | None, Decimal | None]:
    residual_book = {
        "bids": [
            {"price": price, "size": size}
            for price, size in remaining_bids.items()
        ]
    }
    gross_exit_value = _executable_bid_value({"book": residual_book}, quantity)
    exit_fee = _projected_taker_fee(
        {"market": market, "book": residual_book}, quantity
    )
    return gross_exit_value, exit_fee


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
        size = _maybe_decimal(_field(position, "size", _field(position, "quantity")))
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


def estimate_lp_stress_exit(
    book: object,
    *,
    market: Mapping[str, object],
    price: Decimal,
    quantity: Decimal,
    own_orders: object = (),
) -> dict[str, object]:
    """Estimate full exit value after removing the proposed best-bid level."""

    unknown = {
        "state": "unknown",
        "reason_codes": [],
        "fully_covered": None,
        "gross_exit_value": None,
        "exit_fee": None,
        "net_loss": None,
        "loss_ratio": None,
    }
    if not isinstance(book, Mapping):
        unknown["reason_codes"] = ["book_unknown"]
        return unknown
    condition_id = str(market.get("condition_id") or "").strip()
    token_id = str(market.get("token_id") or "").strip()
    if not condition_id or not token_id:
        unknown["reason_codes"] = ["market_identity_unknown"]
        return unknown
    if (
        book.get("condition_id", book.get("market")) != condition_id
        or book.get("token_id", book.get("asset_id")) != token_id
    ):
        unknown["reason_codes"] = ["book_identity_mismatch"]
        return unknown
    if not isinstance(price, Decimal) or not isinstance(quantity, Decimal):
        unknown["reason_codes"] = ["entry_terms_unknown"]
        return unknown
    if price <= 0 or price > 1 or quantity <= 0:
        unknown["reason_codes"] = ["entry_terms_invalid"]
        return unknown
    try:
        bids = _levels(book.get("bids"), "bids")
    except ValueError:
        unknown["reason_codes"] = ["book_unknown"]
        return unknown
    if not bids:
        unknown["reason_codes"] = ["book_unknown"]
        return unknown
    best_bid = max(level_price for level_price, _ in bids)
    if best_bid != price:
        unknown["reason_codes"] = ["best_bid_changed"]
        return unknown

    bid_size_by_price, order_error = _external_bid_sizes(
        bids,
        condition_id=condition_id,
        token_id=token_id,
        own_orders=own_orders,
    )
    if bid_size_by_price is None:
        unknown["reason_codes"] = [order_error or "own_order_facts_unknown"]
        return unknown

    remaining_bids = {
        level_price: size
        for level_price, size in bid_size_by_price.items()
        if level_price != price
    }
    gross_exit_value, exit_fee = _lp_exit_values(market, remaining_bids, quantity)
    if gross_exit_value is None:
        return {
            **unknown,
            "state": "rejected",
            "reason_codes": ["exit_liquidity_insufficient"],
            "fully_covered": False,
        }
    if exit_fee is None:
        unknown["reason_codes"] = ["exit_fee_unknown"]
        return unknown

    capital = price * quantity
    net_loss = max(Decimal("0"), capital - gross_exit_value + exit_fee)
    loss_ratio = net_loss / capital
    eligible = loss_ratio <= Decimal("0.10")
    return {
        "state": "eligible" if eligible else "rejected",
        "reason_codes": [] if eligible else ["stress_loss_exceeded"],
        "fully_covered": True,
        "gross_exit_value": gross_exit_value,
        "exit_fee": exit_fee,
        "net_loss": net_loss,
        "loss_ratio": loss_ratio,
        "capital": capital,
    }


def _own_remaining_by_side(
    own_orders: object,
    *,
    condition_id: str,
    token_id: str,
) -> tuple[dict[str, Decimal] | None, str | None]:
    """Sum this account's non-terminal remaining size per side for one token."""

    totals: dict[str, Decimal] = {"BUY": Decimal("0"), "SELL": Decimal("0")}
    for order in _items(own_orders):
        if not isinstance(order, Mapping):
            return None, "own_order_facts_unknown"
        if str(order.get("status") or "").upper() in TERMINAL_ORDER_STATES:
            continue
        side = str(order.get("side") or "").upper()
        if side not in {"BUY", "SELL"}:
            return None, "own_order_side_unknown"
        order_condition = order.get("condition_id", order.get("market"))
        order_token = order.get("token_id", order.get("asset_id"))
        if order_condition not in (None, "", condition_id):
            continue
        if order_token not in (None, "", token_id):
            continue
        if order_token in (None, ""):
            return None, "own_order_identity_unknown"
        remaining = _maybe_decimal(
            order.get(
                "remaining_size",
                order.get("remaining_quantity", order.get("size")),
            )
        )
        if remaining is None:
            original = _maybe_decimal(order.get("original_size"))
            matched = _maybe_decimal(order.get("size_matched", 0))
            if original is not None and matched is not None:
                remaining = max(Decimal("0"), original - matched)
        if remaining is None or remaining < 0:
            return None, "own_order_depth_unknown"
        totals[side] += remaining
    return totals, None


def evaluate_lp_book_share(
    book: object,
    *,
    condition_id: str,
    token_id: str,
    own_orders: object,
) -> dict[str, object]:
    """Share of each book side taken by this account's resting orders.

    Issue #145: per side, own non-terminal open-order remaining quantity
    divided by the total quantity of that book side (the public snapshot
    already includes own resting orders).  Any unknown input keeps the
    fields None instead of faking zero.
    """

    unknown_side = {
        "own_side_quantity": None,
        "side_total_quantity": None,
        "book_share_pct": None,
    }

    def result(
        state: str, reason_codes: list[str], sides: dict[str, dict[str, object]]
    ) -> dict[str, object]:
        return {
            "state": state,
            "reason_codes": reason_codes,
            "BUY": sides["BUY"],
            "SELL": sides["SELL"],
        }

    sides: dict[str, dict[str, object]] = {
        "BUY": dict(unknown_side),
        "SELL": dict(unknown_side),
    }
    if not isinstance(book, Mapping):
        return result("unknown", ["book_unknown"], sides)
    book_condition = str(book.get("condition_id", book.get("market")) or "").strip()
    book_token = str(book.get("token_id", book.get("asset_id")) or "").strip()
    if not book_condition or not book_token:
        return result("unknown", ["book_unknown"], sides)
    if book_condition != condition_id or book_token != token_id:
        return result("unknown", ["book_identity_mismatch"], sides)
    try:
        bids = _levels(book.get("bids"), "bids")
        asks = _levels(book.get("asks"), "asks")
    except ValueError:
        return result("unknown", ["book_unknown"], sides)
    own_sizes, error = _own_remaining_by_side(
        own_orders, condition_id=condition_id, token_id=token_id
    )
    if own_sizes is None:
        return result("unknown", [error or "own_order_facts_unknown"], sides)
    side_totals = {
        "BUY": sum((size for _price, size in bids), Decimal("0")),
        "SELL": sum((size for _price, size in asks), Decimal("0")),
    }
    known = True
    for side in ("BUY", "SELL"):
        total = side_totals[side]
        own = own_sizes[side]
        share: Decimal | None
        if total <= 0:
            share = None
            known = False
        else:
            share = (own / total * Decimal("100")).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        sides[side] = {
            "own_side_quantity": own,
            "side_total_quantity": total,
            "book_share_pct": share,
        }
    return result("known" if known else "unknown", [] if known else ["book_side_empty"], sides)


def evaluate_lp_exposure(
    book: object,
    *,
    market: Mapping[str, object],
    account: Mapping[str, object],
    now: datetime,
) -> dict[str, object]:
    """Estimate current position and resting-buy risk against stress depth."""

    risk_quantity: Decimal | None = None
    risk_principal: Decimal | None = None
    checked_at = now.astimezone(UTC) if isinstance(now, datetime) and now.tzinfo else None

    def unknown(
        reason: str,
        *,
        quantity: Decimal | None = risk_quantity,
        principal: Decimal | None = risk_principal,
    ) -> dict[str, object]:
        return {
            "state": "unknown",
            "reason_codes": [reason],
            "risk_quantity": quantity,
            "risk_principal": principal,
            "gross_exit_value": None,
            "exit_fee": None,
            "stress_loss": None,
            "loss_ratio": None,
            "warning": None,
            "threshold": Decimal("0.10"),
            "currency": "USD",
            "checked_at": checked_at,
        }

    if checked_at is None:
        return unknown("exposure_time_unknown")
    if not isinstance(book, Mapping):
        return unknown("book_unknown")
    condition_id = str(market.get("condition_id") or "").strip()
    token_id = str(market.get("token_id") or "").strip()
    if not condition_id or not token_id:
        return unknown("market_identity_unknown")
    if (
        book.get("condition_id", book.get("market")) != condition_id
        or book.get("token_id", book.get("asset_id")) != token_id
    ):
        return unknown("book_identity_mismatch")
    if not isinstance(account, Mapping) or account.get("authenticated") is not True:
        return unknown("account_auth_unknown")
    try:
        _freshness(book.get("received_at"), checked_at, "book_freshness")
        _freshness(
            account.get("checked_at"),
            checked_at,
            "account_freshness",
            max_age=ACCOUNT_FRESHNESS_SECONDS,
        )
    except ValueError as exc:
        return unknown(str(exc))
    if (
        account.get("open_orders_complete") is not True
        or account.get("positions_complete") is not True
        or not isinstance(account.get("open_orders"), Sequence)
        or isinstance(account.get("open_orders"), (str, bytes))
        or not isinstance(account.get("positions"), Sequence)
        or isinstance(account.get("positions"), (str, bytes))
    ):
        return unknown("account_facts_unknown")
    if market.get("fees_enabled") is not True and market.get("fees_enabled") is not False:
        return unknown("exit_fee_unknown")

    risk_quantity = Decimal("0")
    risk_principal = Decimal("0")
    position_quantity = Decimal("0")
    open_buy_quantity = Decimal("0")
    positions = cast(Sequence[object], account["positions"])
    for position in positions:
        if not isinstance(position, Mapping):
            return unknown("position_facts_unknown")
        position_token = position.get("token_id", position.get("asset_id"))
        position_condition = position.get(
            "condition_id", position.get("market")
        )
        if position_token in (None, ""):
            if position_condition == condition_id:
                return unknown("position_identity_unknown")
            continue
        if str(position_token) != token_id:
            continue
        if position_condition != condition_id:
            return unknown("position_identity_mismatch")
        size = _maybe_decimal(position.get("size", position.get("quantity")))
        if size is None or size < 0:
            return unknown("position_size_unknown")
        if size == 0:
            continue
        price = _maybe_decimal(
            position.get(
                "average_price",
                position.get("avg_price", position.get("average_cost")),
            )
        )
        if price is None or price <= 0 or price > 1:
            return unknown("position_cost_unknown")
        position_quantity += size
        risk_quantity += size
        risk_principal += size * price

    open_orders = cast(Sequence[object], account["open_orders"])
    for order in open_orders:
        if not isinstance(order, Mapping):
            return unknown("own_order_facts_unknown")
        status = str(order.get("status") or "").upper()
        if status in TERMINAL_ORDER_STATES:
            continue
        side = str(order.get("side") or "").upper()
        if side not in {"BUY", "SELL"}:
            return unknown("own_order_side_unknown")
        order_token = order.get("token_id", order.get("asset_id"))
        order_condition = order.get("condition_id", order.get("market"))
        if order_token in (None, ""):
            if order_condition == condition_id and side == "BUY":
                return unknown("own_order_identity_unknown")
            continue
        if str(order_token) != token_id:
            continue
        if order_condition != condition_id:
            return unknown("own_order_identity_mismatch")
        if side != "BUY":
            continue
        if not status:
            return unknown("own_order_status_unknown")
        price = _maybe_decimal(order.get("price"))
        remaining = _maybe_decimal(
            order.get(
                "remaining_quantity",
                order.get("remaining_size", order.get("size")),
            )
        )
        if remaining is None:
            original = _maybe_decimal(order.get("original_size", order.get("quantity")))
            matched = _maybe_decimal(
                order.get("size_matched", order.get("filled_quantity", 0))
            )
            if original is not None and matched is not None:
                remaining = max(Decimal("0"), original - matched)
        if price is None or price <= 0 or price > 1 or remaining is None or remaining < 0:
            return unknown("own_order_cost_unknown")
        open_buy_quantity += remaining
        risk_quantity += remaining
        risk_principal += remaining * price

    if risk_quantity == 0:
        return {
            "state": "known",
            "reason_codes": [],
            "risk_quantity": Decimal("0"),
            "position_quantity": position_quantity,
            "open_buy_quantity": open_buy_quantity,
            "risk_principal": Decimal("0"),
            "gross_exit_value": Decimal("0"),
            "exit_fee": Decimal("0"),
            "stress_loss": Decimal("0"),
            "loss_ratio": None,
            "warning": False,
            "threshold": Decimal("0.10"),
            "currency": "USD",
            "checked_at": checked_at,
        }
    if risk_principal <= 0:
        return unknown("risk_principal_unknown")

    try:
        bids = _levels(book.get("bids"), "bids")
    except ValueError:
        return unknown("book_unknown")
    if not bids:
        return unknown("book_unknown")
    bid_sizes, order_error = _external_bid_sizes(
        bids,
        condition_id=condition_id,
        token_id=token_id,
        own_orders=open_orders,
    )
    if bid_sizes is None:
        return unknown(order_error or "own_order_facts_unknown")
    external_bids = {
        price: size for price, size in bid_sizes.items() if size > 0
    }
    if not external_bids:
        return unknown("exit_liquidity_insufficient")
    best_external_bid = max(external_bids)
    remaining_bids = {
        price: size
        for price, size in external_bids.items()
        if price != best_external_bid
    }
    gross_exit_value, exit_fee = _lp_exit_values(
        market, remaining_bids, risk_quantity
    )
    if gross_exit_value is None:
        return unknown("exit_liquidity_insufficient")
    if exit_fee is None:
        return unknown("exit_fee_unknown")
    stress_loss = max(
        Decimal("0"), risk_principal - gross_exit_value + exit_fee
    )
    loss_ratio = stress_loss / risk_principal
    warning = loss_ratio >= Decimal("0.10")
    return {
        "state": "known",
        "reason_codes": ["stress_loss_threshold"] if warning else [],
        "risk_quantity": risk_quantity,
        "position_quantity": position_quantity,
        "open_buy_quantity": open_buy_quantity,
        "risk_principal": risk_principal,
        "gross_exit_value": gross_exit_value,
        "exit_fee": exit_fee,
        "stress_loss": stress_loss,
        "loss_ratio": loss_ratio,
        "warning": warning,
        "threshold": Decimal("0.10"),
        "currency": "USD",
        "checked_at": checked_at,
    }


def evaluate_lp_entry(
    direction: object,
    *,
    account: Mapping[str, object],
    now: datetime,
    reservations: object = (),
    candidate: bool = False,
) -> dict[str, object]:
    """Return a manual LP entry guide when current input facts pass risk checks."""

    if not isinstance(now, datetime) or now.tzinfo is None:
        return {"state": "unknown", "reason_codes": ["screen_time_unknown"], "guidance": None}
    checked_at = now.astimezone(UTC)
    if not isinstance(direction, Mapping):
        return {"state": "unknown", "reason_codes": ["market_facts_unknown"], "guidance": None}
    market = direction.get("market")
    book = direction.get("book")
    if not isinstance(market, Mapping) or not isinstance(book, Mapping):
        return {"state": "unknown", "reason_codes": ["market_facts_unknown"], "guidance": None}

    condition_id = str(market.get("condition_id") or "").strip()
    token_id = str(market.get("token_id") or "").strip()
    if not condition_id or not token_id:
        return {"state": "unknown", "reason_codes": ["market_identity_unknown"], "guidance": None}
    freshness_checks = [
        (
            market.get("metadata_checked_at"),
            _MARKET_METADATA_MAX_AGE_SECONDS,
            "market_metadata_time_unknown",
            "market_metadata_stale",
        ),
        (direction.get("reward_checked_at"), 60, "reward_time_unknown", "reward_data_stale"),
    ]
    if candidate:
        freshness_checks.append(
            (
                market.get("fees_checked_at"),
                _MARKET_METADATA_MAX_AGE_SECONDS,
                "market_fees_time_unknown",
                "market_fees_stale",
            )
        )
    for field, max_age, unknown_code, stale_code in freshness_checks:
        try:
            age = (checked_at - _timestamp(field, name="metadata_checked_at")).total_seconds()
        except ValueError:
            return {"state": "unknown", "reason_codes": [unknown_code], "guidance": None}
        if age < 0 or age > max_age:
            return {"state": "unknown", "reason_codes": [stale_code], "guidance": None}

    reward_guidance_deadline: datetime | None = None
    if direction.get("reward_guidance_deadline") is not None:
        try:
            reward_guidance_deadline = _timestamp(
                direction.get("reward_guidance_deadline"),
                name="reward_guidance_deadline",
            )
        except ValueError:
            return {
                "state": "unknown",
                "reason_codes": ["reward_deadline_unknown"],
                "guidance": None,
            }
        if reward_guidance_deadline <= checked_at:
            return {
                "state": "rejected",
                "reason_codes": ["reward_expired"],
                "guidance": None,
            }

    (
        event_state,
        event_reason,
        event_coverage_incomplete,
        event_guidance_deadline,
    ) = _event_window_check(direction, market, checked_at)
    if event_state is not None:
        return {
            "state": event_state,
            "reason_codes": [event_reason] if event_reason else [],
            "guidance": None,
        }

    if direction.get("reward_active") is False:
        return {"state": "rejected", "reason_codes": ["reward_inactive"], "guidance": None}
    if direction.get("reward_active") is not True:
        return {"state": "unknown", "reason_codes": ["reward_status_unknown"], "guidance": None}
    pool = _maybe_decimal(direction.get("daily_pool_usd"))
    if pool is None:
        return {"state": "unknown", "reason_codes": ["reward_pool_unknown"], "guidance": None}
    if pool <= 0:
        return {"state": "rejected", "reason_codes": ["reward_pool_empty"], "guidance": None}
    if market.get("accepting_orders") is False:
        return {"state": "rejected", "reason_codes": ["market_not_accepting_orders"], "guidance": None}
    if market.get("accepting_orders") is not True:
        return {"state": "unknown", "reason_codes": ["market_status_unknown"], "guidance": None}
    if (
        book.get("condition_id", book.get("market")) != condition_id
        or book.get("token_id", book.get("asset_id")) != token_id
    ):
        return {"state": "unknown", "reason_codes": ["book_identity_mismatch"], "guidance": None}
    candidate_max_age = Decimal("60") if candidate else None
    try:
        _freshness(
            book.get("received_at"),
            checked_at,
            "book_freshness",
            max_age=(candidate_max_age or BOOK_FRESHNESS_SECONDS),
        )
        _freshness(
            account.get("checked_at"),
            checked_at,
            "account_freshness",
            max_age=(candidate_max_age or BOOK_FRESHNESS_SECONDS),
        )
        bids = _levels(book.get("bids"), "bids")
        asks = _levels(book.get("asks"), "asks")
    except ValueError as exc:
        return {"state": "unknown", "reason_codes": [str(exc)], "guidance": None}
    if any(
        not isinstance(account.get(field), Sequence)
        or isinstance(account.get(field), (str, bytes))
        for field in ("open_orders", "positions")
    ):
        return {"state": "unknown", "reason_codes": ["account_facts_unknown"], "guidance": None}
    if (
        account.get("open_orders_complete") is not True
        if candidate
        else account.get("open_orders_complete") is False
    ) or (
        account.get("positions_complete") is not True
        if candidate
        else account.get("positions_complete") is False
    ):
        return {"state": "unknown", "reason_codes": ["account_facts_unknown"], "guidance": None}
    if not bids or not asks:
        return {"state": "unknown", "reason_codes": ["book_invalid"], "guidance": None}
    price = max(level_price for level_price, _ in bids)
    best_ask = min(level_price for level_price, _ in asks)
    if price >= best_ask:
        return {"state": "unknown", "reason_codes": ["book_crossed"], "guidance": None}

    tick = _maybe_decimal(market.get("tick_size"))
    minimum = _maybe_decimal(market.get("minimum_order_size"))
    reward_minimum = _maybe_decimal(market.get("reward_min_size"))
    reward_spread = _maybe_decimal(market.get("reward_max_spread"))
    if any(value is None for value in (tick, minimum, reward_minimum, reward_spread)):
        return {"state": "unknown", "reason_codes": ["market_rules_unknown"], "guidance": None}
    if tick <= 0 or minimum <= 0 or reward_minimum <= 0 or reward_spread <= 0:
        return {"state": "unknown", "reason_codes": ["market_rules_invalid"], "guidance": None}
    quantity = max(minimum, reward_minimum)
    quantity = (quantity / Decimal("0.01")).to_integral_value(rounding=ROUND_CEILING) * Decimal("0.01")
    if price % tick != 0:
        return {"state": "rejected", "reason_codes": ["price_off_tick"], "guidance": None}
    try:
        _qualify_reward_quote(
            bids,
            asks,
            price=price,
            reward_min_size=reward_minimum,
            reward_max_spread=reward_spread,
            require_positive_score=True,
            cumulative_depth=candidate,
        )
    except ValueError as exc:
        reason = str(exc)
        state = "unknown" if reason == "midpoint_unknown" else "rejected"
        return {"state": state, "reason_codes": [reason], "guidance": None}
    if account.get("authenticated") is not True:
        return {"state": "unknown", "reason_codes": ["account_auth_unknown"], "guidance": None}
    expected_identity = market.get(
        "account_wallet_address",
        direction.get("account_wallet_address", direction.get("account_id")),
    )
    observed_identity = account.get("wallet_address", account.get("account_id"))
    if expected_identity is not None:
        if observed_identity in (None, ""):
            return {"state": "unknown", "reason_codes": ["account_identity_unknown"], "guidance": None}
        if str(observed_identity).casefold() != str(expected_identity).casefold():
            return {"state": "unknown", "reason_codes": ["account_identity_mismatch"], "guidance": None}
    if _has_market_order(account, market):
        return {"state": "rejected", "reason_codes": ["market_already_participating"], "guidance": None}
    adjusted_account = _account_after_reservations(account, reservations)
    if adjusted_account is None:
        return {"state": "unknown", "reason_codes": ["account_facts_unknown"], "guidance": None}
    balance = _maybe_decimal(adjusted_account.get("balance"))
    allowance = _maybe_decimal(adjusted_account.get("allowance"))
    capital = price * quantity
    if balance is None or allowance is None:
        return {"state": "unknown", "reason_codes": ["account_funds_unknown"], "guidance": None}
    if balance < capital or allowance < capital:
        return {"state": "rejected", "reason_codes": ["balance_insufficient"], "guidance": None}

    stress = estimate_lp_stress_exit(
        book,
        market=market,
        price=price,
        quantity=quantity,
    )
    if stress["state"] != "eligible":
        return {"state": stress["state"], "reason_codes": list(stress["reason_codes"]), "guidance": None}
    guidance: dict[str, object] = {
        key: market.get(key)
        for key in ("market_id", "condition_id", "token_id", "outcome")
    }
    expires_at = checked_at + timedelta(seconds=60)
    if event_guidance_deadline is not None:
        expires_at = min(expires_at, event_guidance_deadline)
    if reward_guidance_deadline is not None:
        expires_at = min(expires_at, reward_guidance_deadline)
    guidance.update(
        {
            "price": price,
            "quantity": quantity,
            "required_capital": capital,
            "minimum_order_size": minimum,
            "reward_min_size": reward_minimum,
            "estimated_exit_loss": stress["net_loss"],
            "estimated_exit_loss_ratio": stress["loss_ratio"],
            "checked_at": checked_at.isoformat(timespec="microseconds").replace("+00:00", "Z"),
            "expires_at": expires_at.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        }
    )
    return {
        "state": "eligible",
        "reason_codes": ["event_coverage_incomplete"]
        if event_coverage_incomplete
        else [],
        "guidance": guidance,
    }


_TARGET_SHARE_FRACTION = Decimal("0.05")
_TARGET_SHARE_QUANTUM = Decimal("0.000000000001")


def estimate_lp_target_share_yield(
    book: object,
    *,
    price: Decimal,
    reward_min_size: Decimal,
    reward_max_spread: Decimal,
    daily_pool_usd: Decimal,
    now: datetime,
) -> dict[str, object]:
    """Estimate the hourly yield of holding a 5% official reward share.

    Issue #138 round 2: the trial candidate ordering needs a comparable
    capital-yield figure, not the old whole-pool optimistic upper bound.
    The model asks how many shares one side of this book must carry to own
    5% of the market's official reward pool, weights every resting level by
    its quadratic reward distance ``w(p) = (1 - |p - m|/v)^2``, bounds the
    competition with ``C = min(A, B) + |A - B|/3`` (no maker grouping is
    published), and solves ``q*w/3 / (C + q*w/3) = 0.05`` for the target
    quantity ``q = 3C / (19w)``.  Pure arithmetic: no I/O, no orders.

    All unknown inputs keep the numeric fields None — never zero, and never
    a fallback to the whole-pool upper bound.
    """

    unknown: dict[str, object] = {
        "state": "unknown",
        "reason_codes": [],
        "yield_pct_per_hour": None,
        "yield_pct_per_hour_display": None,
        "target_quantity": None,
        "target_capital_usd": None,
        "hourly_reward_usd": None,
        "midpoint": None,
        "competition_upper_bound": None,
        "checked_at": None,
    }

    def unknown_with(codes: list[str]) -> dict[str, object]:
        return {**unknown, "reason_codes": codes}

    if not isinstance(now, datetime) or now.tzinfo is None:
        return unknown_with(["estimate_time_unknown"])
    checked_at = now.astimezone(UTC)
    if not isinstance(book, Mapping):
        return unknown_with(["book_unknown"])
    if any(
        not isinstance(value, Decimal) or value <= 0
        for value in (reward_min_size, reward_max_spread)
    ):
        return unknown_with(["market_rules_invalid"])
    pool = _maybe_decimal(daily_pool_usd)
    if pool is None or pool < 0:
        return unknown_with(["reward_pool_unknown"])
    quote_price = _maybe_decimal(price)
    if quote_price is None or quote_price <= 0 or quote_price > 1:
        return unknown_with(["entry_terms_invalid"])
    try:
        bids = _levels(book.get("bids"), "bids")
        asks = _levels(book.get("asks"), "asks")
    except ValueError:
        return unknown_with(["book_unknown"])

    try:
        # Same midpoint convention as the eligibility gate: cumulative-depth
        # qualifying quotes, the [0.10, 0.90] midpoint band, and a strictly
        # positive reward distance for our own quote (w > 0).
        _bid, _ask, midpoint = _qualify_reward_quote(
            bids,
            asks,
            price=quote_price,
            reward_min_size=reward_min_size,
            reward_max_spread=reward_max_spread,
            require_positive_score=True,
            cumulative_depth=True,
        )
    except ValueError as exc:
        return unknown_with([str(exc)])

    def unit_weight(level_price: Decimal) -> Decimal | None:
        distance = abs(level_price - midpoint)
        if distance >= reward_max_spread:
            return None
        weight = Decimal("1") - distance / reward_max_spread
        return weight * weight

    side_weights: dict[str, Decimal] = {"bid": Decimal("0"), "ask": Decimal("0")}
    for side, levels in (("bid", bids), ("ask", asks)):
        total = Decimal("0")
        for level_price, size in levels:
            weight = unit_weight(level_price)
            if weight is not None:
                total += size * weight
        side_weights[side] = total

    bid_weight = side_weights["bid"]
    ask_weight = side_weights["ask"]
    competition = min(bid_weight, ask_weight) + abs(bid_weight - ask_weight) / Decimal(
        "3"
    )
    if competition <= 0:
        # Structurally unreachable for a qualified direction (our own price
        # level already gives A > 0); kept as a defensive honest unknown.
        return unknown_with(["competition_upper_bound_nonpositive"])

    quote_weight = unit_weight(quote_price)
    if quote_weight is None or quote_weight <= 0:
        return unknown_with(["reward_score_zero"])

    target_quantity = (
        Decimal("3") * competition / (Decimal("19") * quote_weight)
    ).quantize(_TARGET_SHARE_QUANTUM, rounding=ROUND_HALF_UP)
    target_quantity = max(target_quantity, reward_min_size)
    target_quantity = (
        target_quantity / Decimal("0.01")
    ).to_integral_value(rounding=ROUND_CEILING) * Decimal("0.01")
    target_capital = target_quantity * quote_price
    hourly_reward = pool * _TARGET_SHARE_FRACTION / Decimal("24")
    yield_pct_per_hour = hourly_reward / target_capital * Decimal("100")
    return {
        "state": "known",
        "reason_codes": [],
        "yield_pct_per_hour": yield_pct_per_hour,
        "yield_pct_per_hour_display": yield_pct_per_hour.quantize(
            Decimal("0.000001"), rounding=ROUND_HALF_UP
        ),
        "target_quantity": target_quantity,
        "target_capital_usd": target_capital,
        "hourly_reward_usd": hourly_reward.quantize(
            Decimal("0.000001"), rounding=ROUND_HALF_UP
        ),
        "midpoint": midpoint,
        "competition_upper_bound": competition,
        "checked_at": checked_at,
    }
