"""Pure Decimal rules for a protected two-leg prediction-market order."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_CEILING
from typing import Literal, Mapping


MIN_NET_EDGE = Decimal("0.01")
MIN_ESTIMATED_PROFIT = Decimal("1.00")
MAX_NORMAL_COST = Decimal("20.00")
MAX_WALLET_BALANCE = Decimal("65.00")
MAX_EMERGENCY_LOSS = Decimal("2.00")
COLLATERAL_SPEND_QUANTUM = Decimal("0.01")
PROTECTED_BUY_SHARE_PRECISION = {
    Decimal("0.1"): 3,
    Decimal("0.01"): 4,
    Decimal("0.005"): 5,
    Decimal("0.0025"): 6,
    Decimal("0.001"): 5,
    Decimal("0.0001"): 6,
}


@dataclass(frozen=True, slots=True)
class BookLevel:
    price: Decimal
    size: Decimal


@dataclass(frozen=True, slots=True)
class ConfirmedBooks:
    yes_token_id: str
    no_token_id: str
    yes_asks: tuple[BookLevel, ...]
    no_asks: tuple[BookLevel, ...]
    confirmed_at: datetime


@dataclass(frozen=True, slots=True)
class MarketFacts:
    event_id: str
    market_id: str
    condition_id: str
    slug: str
    question: str
    volume_24h: Decimal
    minimum_order_size: Decimal
    tick_size: Decimal
    fee_verified_zero: bool
    neg_risk: bool


@dataclass(frozen=True, slots=True)
class PairIntent:
    event_id: str
    market_id: str
    condition_id: str
    yes_token_id: str
    no_token_id: str
    quantity: Decimal
    yes_max_price: Decimal
    no_max_price: Decimal
    yes_max_cost: Decimal
    no_max_cost: Decimal
    total_max_cost: Decimal
    minimum_profit: Decimal
    net_edge: Decimal


@dataclass(frozen=True, slots=True)
class ThresholdOrderBook:
    token_id: str
    asks: tuple[BookLevel, ...]
    bids: tuple[BookLevel, ...]
    confirmed_at: datetime


@dataclass(frozen=True, slots=True)
class ThresholdHedgeLeg:
    label: Literal["A", "B"]
    condition_id: str
    market_id: str
    outcome: Literal["YES", "NO"]
    token_id: str
    quantity: Decimal
    max_price: Decimal
    max_cost: Decimal
    tick_size: Decimal


@dataclass(frozen=True, slots=True)
class ThresholdHedgeIntent:
    relation_id: str
    event_id: str
    relation: Literal["A_IMPLIES_B", "B_IMPLIES_A"]
    leg_a: ThresholdHedgeLeg
    leg_b: ThresholdHedgeLeg
    quantity: Decimal
    maximum_fee: Decimal
    total_max_cost: Decimal
    minimum_payout: Decimal
    minimum_profit: Decimal
    net_edge: Decimal


def build_pair_intent(
    facts: MarketFacts,
    books: ConfirmedBooks,
    *,
    balance: Decimal,
    allowance: Decimal,
) -> PairIntent | None:
    """Return the largest conservative equal-share FOK pair, if eligible."""

    precision = _validated_facts(facts)
    if precision is None or not _validated_books(books):
        return None
    available_balance = _nonnegative_decimal(balance)
    available_allowance = _nonnegative_decimal(allowance)
    if (
        available_balance is None
        or available_allowance is None
        or available_balance > MAX_WALLET_BALANCE
    ):
        return None

    yes_segments = _book_segments(books.yes_asks, facts.tick_size)
    no_segments = _book_segments(books.no_asks, facts.tick_size)
    if yes_segments is None or no_segments is None:
        return None

    yes_candidates = _protected_buy_candidates(yes_segments, facts.tick_size)
    no_candidates = _protected_buy_candidates(no_segments, facts.tick_size)
    for quantity in sorted(yes_candidates.keys() & no_candidates.keys(), reverse=True):
        if quantity < facts.minimum_order_size or quantity <= 0:
            continue
        yes_cost = yes_candidates[quantity]
        no_cost = no_candidates[quantity]
        total_cost = yes_cost + no_cost
        if total_cost > MAX_NORMAL_COST:
            continue
        if total_cost > available_balance or total_cost > available_allowance:
            continue

        yes_price = _worst_price(yes_segments, quantity)
        no_price = _worst_price(no_segments, quantity)
        if yes_price is None or no_price is None:
            continue
        minimum_profit = quantity - total_cost
        if minimum_profit < MIN_ESTIMATED_PROFIT:
            continue
        net_edge = minimum_profit / quantity
        if net_edge < MIN_NET_EDGE:
            continue
        return PairIntent(
            event_id=facts.event_id,
            market_id=facts.market_id,
            condition_id=facts.condition_id,
            yes_token_id=books.yes_token_id,
            no_token_id=books.no_token_id,
            quantity=quantity,
            yes_max_price=yes_price,
            no_max_price=no_price,
            yes_max_cost=yes_cost,
            no_max_cost=no_cost,
            total_max_cost=total_cost,
            minimum_profit=minimum_profit,
            net_edge=net_edge,
        )
    return None


def estimated_unwind_loss(
    *,
    filled_cost: Decimal,
    sell_price: Decimal,
    quantity: Decimal,
) -> Decimal:
    """Estimate the non-negative loss from selling a filled leg."""

    cost = _nonnegative_decimal(filled_cost)
    price = _nonnegative_decimal(sell_price)
    shares = _positive_decimal(quantity)
    if cost is None or price is None or shares is None or price > Decimal("1"):
        return Decimal("Infinity")
    return max(Decimal("0"), cost - price * shares)


def monitored_event_sort_key(event: Mapping[str, object]) -> tuple[object, ...]:
    """Sort actionable events first, then finite profit, volume, and ID."""

    actionable_value = event.get("actionable", event.get("is_actionable", False))
    if not isinstance(actionable_value, bool):
        actionable_value = str(event.get("eligibility", "")).strip().lower() in {
            "actionable",
            "eligible",
            "可参与",
        }
    actionable_group = 0 if actionable_value else 1

    profit = _first_event_decimal(
        event,
        "profit",
        "estimated_profit",
        "minimum_profit",
        "gross_upper_bound",
        "profit_upper_bound",
    )
    volume = _first_event_decimal(event, "volume_24h", "volume", "volume24hr")
    if volume is None or volume < 0:
        volume = Decimal("0")
    event_id = event.get("event_id", event.get("id", ""))
    if not isinstance(event_id, str):
        event_id = str(event_id)
    return (
        actionable_group,
        1 if profit is None else 0,
        Decimal("0") if profit is None else -profit,
        -volume,
        event_id,
    )


def _validated_facts(facts: MarketFacts) -> int | None:
    if not isinstance(facts, MarketFacts):
        return None
    if not all(
        isinstance(value, str) and value.strip()
        for value in (
            facts.event_id,
            facts.market_id,
            facts.condition_id,
            facts.slug,
            facts.question,
        )
    ):
        return None
    volume = _nonnegative_decimal(facts.volume_24h)
    minimum = _positive_decimal(facts.minimum_order_size)
    if volume is None or minimum is None:
        return None
    if not isinstance(facts.tick_size, Decimal):
        return None
    if not facts.tick_size.is_finite() or facts.tick_size <= 0:
        return None
    precision = PROTECTED_BUY_SHARE_PRECISION.get(facts.tick_size)
    if precision is None:
        return None
    if facts.fee_verified_zero is not True or facts.neg_risk is not False:
        return None
    return precision


def _validated_books(books: ConfirmedBooks) -> bool:
    return (
        isinstance(books, ConfirmedBooks)
        and isinstance(books.confirmed_at, datetime)
        and isinstance(books.yes_token_id, str)
        and bool(books.yes_token_id.strip())
        and isinstance(books.no_token_id, str)
        and bool(books.no_token_id.strip())
        and books.yes_token_id != books.no_token_id
        and isinstance(books.yes_asks, tuple)
        and isinstance(books.no_asks, tuple)
        and bool(books.yes_asks)
        and bool(books.no_asks)
    )


def _book_segments(
    asks: tuple[BookLevel, ...],
    tick_size: Decimal,
) -> list[tuple[Decimal, Decimal, Decimal]] | None:
    levels: list[tuple[Decimal, Decimal]] = []
    for level in asks:
        if not isinstance(level, BookLevel):
            return None
        price = _positive_decimal(level.price)
        size = _positive_decimal(level.size)
        if (
            price is None
            or size is None
            or price > Decimal("1")
            or price % tick_size != 0
        ):
            return None
        levels.append((price, size))
    levels.sort(key=lambda item: item[0])
    segments: list[tuple[Decimal, Decimal, Decimal]] = []
    previous_depth = Decimal("0")
    for price, size in levels:
        previous_depth += size
        segments.append((price, previous_depth - size, previous_depth))
    return segments


def _protected_buy_candidates(
    segments: list[tuple[Decimal, Decimal, Decimal]], tick_size: Decimal
) -> dict[Decimal, Decimal]:
    candidates: dict[Decimal, Decimal] = {}
    for cents in range(1, 2001):
        spend = COLLATERAL_SPEND_QUANTUM * cents
        for price, previous_depth, total_depth in segments:
            quantity = protected_buy_quantity(
                spend=spend,
                price=price,
                tick_size=tick_size,
            )
            if quantity is None:
                break
            if previous_depth < quantity <= total_depth:
                old_spend = candidates.get(quantity)
                if old_spend is None or spend < old_spend:
                    candidates[quantity] = spend
                break
    return candidates


def protected_buy_quantity(
    *, spend: Decimal, price: Decimal, tick_size: Decimal
) -> Decimal | None:
    """Return the protected-BUY share amount for a supported tick size."""

    if not all(
        isinstance(value, Decimal) and value.is_finite() and value > 0
        for value in (spend, price, tick_size)
    ):
        return None
    precision = PROTECTED_BUY_SHARE_PRECISION.get(
        tick_size,
        max(5, -tick_size.as_tuple().exponent + 1),
    )
    if price > Decimal("1") or price % tick_size != 0:
        return None
    quantum = Decimal(1).scaleb(-precision)
    return (spend / price).quantize(quantum, rounding=ROUND_CEILING)


def _worst_price(
    segments: list[tuple[Decimal, Decimal, Decimal]], quantity: Decimal
) -> Decimal | None:
    for price, _, total_depth in segments:
        if quantity <= total_depth:
            return price
    return None


def _nonnegative_decimal(value: object) -> Decimal | None:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        return None
    return value


def _positive_decimal(value: object) -> Decimal | None:
    value = _nonnegative_decimal(value)
    return value if value is not None and value > 0 else None


def _first_event_decimal(event: Mapping[str, object], *names: str) -> Decimal | None:
    for name in names:
        value = event.get(name)
        if isinstance(value, Decimal) and value.is_finite():
            return value
        if isinstance(value, (int, str)) and not isinstance(value, bool):
            try:
                parsed = Decimal(str(value))
            except Exception:
                continue
            if parsed.is_finite():
                return parsed
    return None
