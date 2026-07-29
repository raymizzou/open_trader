"""Deterministic same-event threshold relation discovery."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal

from .prediction_arbitrage import (
    MAX_EMERGENCY_LOSS,
    MAX_NORMAL_COST,
    BookLevel,
    ThresholdHedgeIntent,
    ThresholdHedgeLeg,
    ThresholdOrderBook,
    _book_segments,
    _protected_buy_candidates,
    _worst_price,
)


Relation = Literal["A_IMPLIES_B", "B_IMPLIES_A"]
Operator = Literal[">", ">=", "<", "<="]
Outcome = Literal["YES", "NO"]

_SPACE = re.compile(r"\s+")
_THRESHOLD = re.compile(
    r"(?P<currency>\$\s*)?"
    r"(?P<number>\d[\d,]*(?:\.\d+)?)"
    r"\s*(?P<suffix>trillion|billion|million|percent|[kmb%])?"
    r"(?=\s|[?.,;:!)]|$)",
    re.IGNORECASE,
)
_COMPARATORS: tuple[tuple[re.Pattern[str], Operator], ...] = (
    (
        re.compile(
            r"\b(?:greater\s+than\s+or\s+equal\s+to|at\s+least)\b",
            re.IGNORECASE,
        ),
        ">=",
    ),
    (
        re.compile(
            r"\b(?:less\s+than\s+or\s+equal\s+to|at\s+most)\b",
            re.IGNORECASE,
        ),
        "<=",
    ),
    (
        re.compile(r"\b(?:above|over|greater\s+than)\b", re.IGNORECASE),
        ">",
    ),
    (
        re.compile(r"\b(?:below|under|less\s+than)\b", re.IGNORECASE),
        "<",
    ),
)


@dataclass(frozen=True, slots=True)
class ThresholdMarket:
    event_id: str
    market_id: str
    condition_id: str
    question: str
    rules: str
    resolution_source: str
    end_date: str
    operator: Operator
    threshold: Decimal
    yes_token_id: str
    no_token_id: str
    group_item_threshold: str
    fees_enabled: bool | None
    fee_rate: Decimal | None
    minimum_order_size: Decimal
    tick_size: Decimal


@dataclass(frozen=True, slots=True)
class ThresholdBuyLeg:
    label: Literal["A", "B"]
    market_id: str
    condition_id: str
    outcome: Outcome
    token_id: str


@dataclass(frozen=True, slots=True)
class ThresholdRelation:
    relation_id: str
    event_id: str
    market_a: ThresholdMarket
    market_b: ThresholdMarket
    relation: Relation
    buy_leg_a: ThresholdBuyLeg
    buy_leg_b: ThresholdBuyLeg
    rules_hash_a: str
    rules_hash_b: str


@dataclass(frozen=True, slots=True)
class _ParsedQuestion:
    operator: Operator
    threshold: Decimal
    template: str


def _value(value: object, *names: str, default: object = None) -> object:
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
    for name in names:
        try:
            return getattr(value, name)
        except (AttributeError, TypeError):
            continue
    return default


def _nested(value: object, container: str, *names: str, default: object = None) -> object:
    direct = _value(value, *names, default=None)
    if direct is not None:
        return direct
    return _value(_value(value, container, default=None), *names, default=default)


def _items(value: object) -> tuple[object, ...]:
    if value is None:
        return ()
    if isinstance(value, Mapping):
        return tuple(value.values())
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return ()
        return _items(parsed)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return tuple(value)
    return ()


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _normalized(value: str) -> str:
    return _SPACE.sub(" ", value).strip().casefold()


def _threshold_value(match: re.Match[str]) -> Decimal | None:
    number = _decimal(match.group("number").replace(",", ""))
    if number is None:
        return None
    multiplier = {
        "k": Decimal("1000"),
        "m": Decimal("1000000"),
        "million": Decimal("1000000"),
        "b": Decimal("1000000000"),
        "billion": Decimal("1000000000"),
        "trillion": Decimal("1000000000000"),
    }.get((match.group("suffix") or "").casefold(), Decimal("1"))
    return number * multiplier


def _parse_question(question: str) -> _ParsedQuestion | None:
    comparator_match: re.Match[str] | None = None
    operator: Operator | None = None
    for pattern, candidate in _COMPARATORS:
        comparator_match = pattern.search(question)
        if comparator_match is not None:
            operator = candidate
            break
    if comparator_match is None or operator is None:
        return None
    threshold_match = _THRESHOLD.search(question, comparator_match.end())
    if threshold_match is None:
        return None
    threshold = _threshold_value(threshold_match)
    if threshold is None:
        return None
    template = _normalized(
        question[: threshold_match.start()]
        + "<threshold>"
        + question[threshold_match.end() :]
    )
    return _ParsedQuestion(operator, threshold, template)


def _normalize_rules(rules: str, threshold: Decimal) -> str:
    def replace(match: re.Match[str]) -> str:
        return "<threshold>" if _threshold_value(match) == threshold else match.group(0)

    return _normalized(_THRESHOLD.sub(replace, rules))


def _outcome_tokens(market: object) -> dict[str, str] | None:
    outcomes = _value(market, "outcomes", default=None)
    tokens: dict[str, str] = {}
    if isinstance(outcomes, Mapping):
        rows = outcomes.values()
        for row in rows:
            label = _text(_value(row, "label", "name", default="")).casefold()
            token = _text(
                _value(row, "token_id", "tokenId", "asset_id", "assetId", default="")
            )
            if label in {"yes", "no"} and token:
                tokens[label] = token
    else:
        labels = [_text(item).casefold() for item in _items(outcomes)]
        token_ids = [
            _text(item)
            for item in _items(
                _value(market, "clobTokenIds", "clob_token_ids", default=None)
            )
        ]
        if len(labels) != 2 or len(token_ids) != 2:
            return None
        tokens = dict(zip(labels, token_ids, strict=True))
    if set(tokens) != {"yes", "no"} or tokens["yes"] == tokens["no"]:
        return None
    return tokens


def _eligible_event(value: object) -> bool:
    return (
        _nested(value, "state", "active", default=True) is not False
        and _nested(value, "state", "closed", default=False) is not True
        and _nested(value, "state", "ended", default=False) is not True
        and _nested(value, "trading", "negRisk", "neg_risk", default=False)
        is not True
    )


def _market(
    event_id: str, value: object
) -> tuple[ThresholdMarket, str, str] | None:
    if (
        _nested(value, "state", "active", default=True) is False
        or _nested(value, "state", "closed", default=False) is True
        or _nested(
            value,
            "state",
            "acceptingOrders",
            "accepting_orders",
            default=True,
        )
        is False
        or _nested(
            value,
            "state",
            "enableOrderBook",
            "enable_order_book",
            default=True,
        )
        is False
        or _nested(value, "state", "negRisk", "neg_risk", default=False) is True
    ):
        return None
    market_id = _text(_value(value, "id", "market_id", "marketId", default=""))
    condition_id = _text(
        _value(value, "conditionId", "condition_id", default="")
    )
    question = _text(_value(value, "question", default=""))
    rules = _text(_value(value, "description", "rules", default=""))
    parsed = _parse_question(question)
    tokens = _outcome_tokens(value)
    if not market_id or not condition_id or not question or not rules or parsed is None or tokens is None:
        return None
    source = _text(
        _nested(
            value,
            "resolution",
            "resolutionSource",
            "resolution_source",
            "source",
            default="",
        )
    )
    end_date = _text(
        _nested(value, "state", "endDate", "end_date", default="")
    )
    if not end_date:
        return None
    minimum_order_size = _decimal(
        _nested(
            value,
            "trading",
            "orderMinSize",
            "minimumOrderSize",
            "minimum_order_size",
            default=None,
        )
    )
    tick_size = _decimal(
        _nested(
            value,
            "trading",
            "orderPriceMinTickSize",
            "minimumTickSize",
            "minimum_tick_size",
            default=None,
        )
    )
    if (
        minimum_order_size is None
        or minimum_order_size <= 0
        or tick_size is None
        or tick_size <= 0
    ):
        return None
    fees_enabled_value = _nested(
        value, "trading", "feesEnabled", "fees_enabled", default=None
    )
    fees_enabled = (
        fees_enabled_value if isinstance(fees_enabled_value, bool) else None
    )
    schedule = _nested(
        value, "trading", "feeSchedule", "fee_schedule", default=None
    )
    fee_rate = _decimal(_value(schedule, "rate", default=None))
    market = ThresholdMarket(
        event_id=event_id,
        market_id=market_id,
        condition_id=condition_id,
        question=question,
        rules=rules,
        resolution_source=source,
        end_date=end_date,
        operator=parsed.operator,
        threshold=parsed.threshold,
        yes_token_id=tokens["yes"],
        no_token_id=tokens["no"],
        group_item_threshold=_text(
            _value(
                value,
                "groupItemThreshold",
                "group_item_threshold",
                default="",
            )
        ),
        fees_enabled=fees_enabled,
        fee_rate=fee_rate,
        minimum_order_size=minimum_order_size,
        tick_size=tick_size,
    )
    return market, parsed.template, _normalize_rules(rules, parsed.threshold)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _relation_id(event_id: str, a: ThresholdMarket, b: ThresholdMarket) -> str:
    encoded = json.dumps(
        {
            "event_id": event_id,
            "condition_id_a": a.condition_id,
            "condition_id_b": b.condition_id,
            "rules_hash_a": _hash(_normalized(a.rules)),
            "rules_hash_b": _hash(_normalized(b.rules)),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"threshold:{_hash(encoded)}"


def _relation(event_id: str, a: ThresholdMarket, b: ThresholdMarket) -> ThresholdRelation:
    if a.operator in {">", ">="}:
        relation: Relation = "B_IMPLIES_A"
        outcomes: tuple[Outcome, Outcome] = ("YES", "NO")
    else:
        relation = "A_IMPLIES_B"
        outcomes = ("NO", "YES")
    token_a = a.yes_token_id if outcomes[0] == "YES" else a.no_token_id
    token_b = b.yes_token_id if outcomes[1] == "YES" else b.no_token_id
    return ThresholdRelation(
        relation_id=_relation_id(event_id, a, b),
        event_id=event_id,
        market_a=a,
        market_b=b,
        relation=relation,
        buy_leg_a=ThresholdBuyLeg(
            "A", a.market_id, a.condition_id, outcomes[0], token_a
        ),
        buy_leg_b=ThresholdBuyLeg(
            "B", b.market_id, b.condition_id, outcomes[1], token_b
        ),
        rules_hash_a=_hash(_normalized(a.rules)),
        rules_hash_b=_hash(_normalized(b.rules)),
    )


def discover_threshold_relations(
    events: Sequence[object],
) -> tuple[ThresholdRelation, ...]:
    relations: list[ThresholdRelation] = []
    for event in events:
        if not _eligible_event(event):
            continue
        event_id = _text(_value(event, "id", "event_id", "eventId", default=""))
        if not event_id:
            continue
        groups: dict[tuple[str, ...], list[ThresholdMarket]] = {}
        for raw_market in _items(_value(event, "markets", default=())):
            parsed = _market(event_id, raw_market)
            if parsed is None:
                continue
            market, question_template, rules_template = parsed
            key = (
                market.operator,
                question_template,
                rules_template,
                _normalized(market.resolution_source),
                market.end_date,
            )
            groups.setdefault(key, []).append(market)
        for markets in groups.values():
            ordered = sorted(
                markets,
                key=lambda item: (
                    item.threshold,
                    item.condition_id,
                    item.market_id,
                ),
            )
            for index, lower in enumerate(ordered):
                for higher in ordered[index + 1 :]:
                    if lower.threshold == higher.threshold:
                        continue
                    if (
                        lower.condition_id == higher.condition_id
                        or {
                            lower.yes_token_id,
                            lower.no_token_id,
                        }
                        & {higher.yes_token_id, higher.no_token_id}
                    ):
                        continue
                    relations.append(_relation(event_id, lower, higher))
    return tuple(
        sorted(
            relations,
            key=lambda item: (
                item.event_id,
                item.market_a.threshold,
                item.market_b.threshold,
                item.market_a.condition_id,
                item.market_b.condition_id,
            ),
        )
    )


def _fee_rate(market: ThresholdMarket) -> Decimal | None:
    if market.fees_enabled is False:
        return Decimal("0")
    if (
        market.fees_enabled is True
        and market.fee_rate is not None
        and market.fee_rate.is_finite()
        and market.fee_rate >= 0
    ):
        return market.fee_rate
    return None


def _book(
    books: Mapping[str, ThresholdOrderBook], token_id: str
) -> ThresholdOrderBook | None:
    value = books.get(token_id)
    if (
        not isinstance(value, ThresholdOrderBook)
        or value.token_id != token_id
        or not isinstance(value.confirmed_at, datetime)
        or not value.asks
        or not value.bids
    ):
        return None
    return value


def _fee(quantity: Decimal, rate: Decimal, price: Decimal) -> Decimal:
    return quantity * rate * price * (Decimal("1") - price)


def _sell_proceeds(
    bids: tuple[BookLevel, ...],
    *,
    quantity: Decimal,
    tick_size: Decimal,
    fee_rate: Decimal,
) -> Decimal | None:
    rows: list[tuple[Decimal, Decimal]] = []
    for level in bids:
        if (
            not isinstance(level, BookLevel)
            or not level.price.is_finite()
            or not level.size.is_finite()
            or level.price <= 0
            or level.price > 1
            or level.size <= 0
            or level.price % tick_size != 0
        ):
            return None
        rows.append((level.price, level.size))
    remaining = quantity
    net = Decimal("0")
    for price, size in sorted(rows, key=lambda item: item[0], reverse=True):
        filled = min(remaining, size)
        net += filled * price - _fee(filled, fee_rate, price)
        remaining -= filled
        if remaining <= 0:
            return net
    return None


def build_threshold_hedge_intent(
    relation: ThresholdRelation,
    books: Mapping[str, ThresholdOrderBook],
) -> ThresholdHedgeIntent | None:
    """Return the largest equal-share positive hedge with safe current unwinds."""

    if not isinstance(relation, ThresholdRelation):
        return None
    book_a = _book(books, relation.buy_leg_a.token_id)
    book_b = _book(books, relation.buy_leg_b.token_id)
    if book_a is None or book_b is None:
        return None
    rate_a = _fee_rate(relation.market_a)
    rate_b = _fee_rate(relation.market_b)
    if rate_a is None or rate_b is None:
        return None
    segments_a = _book_segments(book_a.asks, relation.market_a.tick_size)
    segments_b = _book_segments(book_b.asks, relation.market_b.tick_size)
    if segments_a is None or segments_b is None:
        return None
    candidates_a = _protected_buy_candidates(
        segments_a, relation.market_a.tick_size
    )
    candidates_b = _protected_buy_candidates(
        segments_b, relation.market_b.tick_size
    )
    minimum = max(
        relation.market_a.minimum_order_size,
        relation.market_b.minimum_order_size,
    )
    for quantity in sorted(candidates_a.keys() & candidates_b.keys(), reverse=True):
        if quantity < minimum:
            continue
        price_a = _worst_price(segments_a, quantity)
        price_b = _worst_price(segments_b, quantity)
        if price_a is None or price_b is None:
            continue
        cost_a = candidates_a[quantity]
        cost_b = candidates_b[quantity]
        fee_a = _fee(quantity, rate_a, price_a)
        fee_b = _fee(quantity, rate_b, price_b)
        maximum_fee = fee_a + fee_b
        total_cost = cost_a + cost_b + maximum_fee
        profit = quantity - total_cost
        if total_cost > MAX_NORMAL_COST or profit <= 0:
            continue
        unwind_a = _sell_proceeds(
            book_a.bids,
            quantity=quantity,
            tick_size=relation.market_a.tick_size,
            fee_rate=rate_a,
        )
        unwind_b = _sell_proceeds(
            book_b.bids,
            quantity=quantity,
            tick_size=relation.market_b.tick_size,
            fee_rate=rate_b,
        )
        if unwind_a is None or unwind_b is None:
            continue
        if (
            max(Decimal("0"), cost_a + fee_a - unwind_a) > MAX_EMERGENCY_LOSS
            or max(Decimal("0"), cost_b + fee_b - unwind_b)
            > MAX_EMERGENCY_LOSS
        ):
            continue
        leg_a = ThresholdHedgeLeg(
            label="A",
            condition_id=relation.buy_leg_a.condition_id,
            market_id=relation.buy_leg_a.market_id,
            outcome=relation.buy_leg_a.outcome,
            token_id=relation.buy_leg_a.token_id,
            quantity=quantity,
            max_price=price_a,
            max_cost=cost_a,
            tick_size=relation.market_a.tick_size,
        )
        leg_b = ThresholdHedgeLeg(
            label="B",
            condition_id=relation.buy_leg_b.condition_id,
            market_id=relation.buy_leg_b.market_id,
            outcome=relation.buy_leg_b.outcome,
            token_id=relation.buy_leg_b.token_id,
            quantity=quantity,
            max_price=price_b,
            max_cost=cost_b,
            tick_size=relation.market_b.tick_size,
        )
        return ThresholdHedgeIntent(
            relation_id=relation.relation_id,
            event_id=relation.event_id,
            relation=relation.relation,
            leg_a=leg_a,
            leg_b=leg_b,
            quantity=quantity,
            maximum_fee=maximum_fee,
            total_max_cost=total_cost,
            minimum_payout=quantity,
            minimum_profit=profit,
            net_edge=profit / quantity,
        )
    return None


def simple_annualized_yield(
    intent: ThresholdHedgeIntent,
    *,
    now: datetime,
    resolution_at: datetime,
) -> Decimal | None:
    if not isinstance(intent, ThresholdHedgeIntent):
        return None
    start = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    end = (
        resolution_at
        if resolution_at.tzinfo is not None
        else resolution_at.replace(tzinfo=UTC)
    )
    seconds = Decimal(str((end.astimezone(UTC) - start.astimezone(UTC)).total_seconds()))
    if seconds <= 0 or intent.total_max_cost <= 0:
        return None
    days = seconds / Decimal("86400")
    return (
        intent.minimum_profit
        / intent.total_max_cost
        * Decimal("365")
        / days
    )
