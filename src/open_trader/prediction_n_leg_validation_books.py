"""Issue #71 finishing: read-only live book source for the N-leg harness.

``live_books`` is the ``--book-source`` seam of
``open_trader.prediction_n_leg_validation``: given the token ids of one
compiled N>=3 relation it returns the current CLOB order books keyed by
token id, with exactly the ``PolymarketMonitor.cross_venue_books`` shape
(``ThresholdOrderBook`` per token).

Zero-write contract: the only client construction is the same official
read-only ``AsyncPublicClient`` the monitor uses, the only endpoints touched
are ``get_order_books`` and ``close``, and the book parsing reuses the
monitor's own helpers so both paths can never drift.  No authenticated
client, no order/cancel/merge/redeem/allowance call exists on this path.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from dataclasses import dataclass
from decimal import Decimal
from typing import cast

from polymarket import AsyncPublicClient

from open_trader.prediction_arbitrage import BookLevel, ThresholdOrderBook
from open_trader.polymarket_monitor import (
    _decimal,
    _asks,
    _items,
    _value,
)
from open_trader.prediction_snapshot_scheduler import OUTCOME_TOKEN_BOOK_CONVENTION

#: Same client construction as the monitor's default public client factory.
DEFAULT_CLIENT_FACTORY: Callable[[], object] = AsyncPublicClient


@dataclass(frozen=True, slots=True)
class PaperBook:
    """Token-keyed paper book with the order facts needed for sizing.

    ``ThresholdOrderBook`` intentionally carries only executable levels and
    receipt time for the live resolver. Paper analysis also needs the venue's
    minimum size and price tick, so this small adapter preserves those facts
    without changing the live book contract.
    """

    token_id: str
    asks: tuple[BookLevel, ...]
    bids: tuple[BookLevel, ...]
    confirmed_at: datetime
    minimum_order_size: Decimal | None
    tick_size: Decimal | None
    minimum_order_notional: Decimal | None
    taker_fee_bps: Decimal | None
    available: bool = True
    book_convention: str = OUTCOME_TOKEN_BOOK_CONVENTION


def _strict_timestamp(value: object) -> datetime | None:
    if isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    elif isinstance(value, datetime):
        parsed = value
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        return None
    return parsed.astimezone(UTC)


def paper_book_from_payload(
    value: object,
    *,
    token_id: str,
    taker_fee_bps: Decimal | None,
) -> PaperBook:
    """Decode one token-keyed paper book without inventing missing facts."""

    actual_token = _value(value, "token_id", "asset_id", "assetId", default=None)
    if actual_token is not None and str(actual_token) != token_id:
        raise ValueError("TOKEN_ID_MISMATCH")
    asks = _asks(_value(value, "asks", default=()))
    bids = _asks(_value(value, "bids", default=())) or ()
    confirmed_at = _strict_timestamp(
        _value(value, "confirmed_at", default=None)
    )
    if asks is None or confirmed_at is None:
        raise ValueError("MISSING_BOOKS")
    # Public CLOB captures are not guaranteed to be ordered. The cost builder
    # consumes asks best-first, so paper mode normalizes the adapter boundary
    # once and keeps the live monitor's parser unchanged.
    asks = tuple(sorted(asks, key=lambda level: level.price))
    bids = tuple(sorted(bids, key=lambda level: level.price, reverse=True))
    minimum_order_size = _decimal(
        _value(value, "minimum_order_size", "min_order_size", default=None)
    )
    tick_size = _decimal(
        _value(value, "tick_size", "minimum_tick_size", default=None)
    )
    minimum_order_notional = _decimal(
        _value(
            value,
            "minimum_order_notional",
            "min_order_notional",
            default=None,
        )
    )
    available = _value(value, "available", default=True)
    if type(available) is not bool:
        raise ValueError("MISSING_BOOKS")
    return PaperBook(
        token_id,
        asks,
        bids,
        confirmed_at,
        minimum_order_size,
        tick_size,
        minimum_order_notional,
        taker_fee_bps,
        available,
        OUTCOME_TOKEN_BOOK_CONVENTION,
    )


async def _fetch_books(
    token_ids: tuple[str, ...],
    *,
    client_factory: Callable[[], object],
    include_order_facts: bool = False,
) -> dict[str, ThresholdOrderBook | PaperBook]:
    """One read-only CLOB book fetch, parsed exactly like the monitor."""

    client = client_factory()
    try:
        get_books = getattr(client, "get_order_books", None)
        if not callable(get_books):
            raise RuntimeError("public client has no order-book read")
        raw_books = await _maybe_await(
            get_books(token_ids=[str(token) for token in token_ids])
        )
        received_at = datetime.now(UTC)
        books: dict[str, ThresholdOrderBook | PaperBook] = {}
        for raw_book in _items(raw_books):
            token = _value(
                raw_book,
                "token_id",
                "asset_id",
                "assetId",
                default=None,
            )
            if not isinstance(token, str) or token not in token_ids:
                continue
            # Monitor semantics: parse both sides with the same helpers and
            # skip books without a usable ask side (nothing to lift).
            asks = _asks(_value(raw_book, "asks", default=()))
            bids = _asks(_value(raw_book, "bids", default=()))
            if asks is None:
                continue
            # Monitor semantics: confirmed_at is the receive time; the raw
            # exchange timestamp belongs to the monitor's separate timing map,
            # which is not part of the cross_venue_books return shape.
            if include_order_facts:
                books[token] = PaperBook(
                    token_id=token,
                    asks=tuple(sorted(asks, key=lambda level: level.price)),
                    bids=tuple(sorted(bids or (), key=lambda level: level.price, reverse=True)),
                    confirmed_at=received_at,
                    minimum_order_size=_decimal(
                        _value(
                            raw_book,
                            "minimum_order_size",
                            "min_order_size",
                            "orderMinSize",
                            default=None,
                        )
                    ),
                    tick_size=_decimal(
                        _value(
                            raw_book,
                            "tick_size",
                            "minimum_tick_size",
                            "orderPriceMinTickSize",
                            default=None,
                        )
                    ),
                    minimum_order_notional=_decimal(
                        _value(
                            raw_book,
                            "minimum_order_notional",
                            "min_order_notional",
                            default=None,
                        )
                    ),
                    taker_fee_bps=None,
                    book_convention=OUTCOME_TOKEN_BOOK_CONVENTION,
                )
            else:
                books[token] = ThresholdOrderBook(
                    token_id=token,
                    asks=asks,
                    bids=bids or (),
                    confirmed_at=received_at,
                )
        return books
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            with contextlib.suppress(Exception):
                await _maybe_await(close())


async def _maybe_await(value: object) -> object:
    if inspect.isawaitable(value):
        return await value
    return value


def live_books(
    token_ids: Sequence[str],
    *,
    client_factory: Callable[[], object] = DEFAULT_CLIENT_FACTORY,
) -> Mapping[str, ThresholdOrderBook]:
    """Current CLOB books for ``token_ids`` (read-only, monitor-shaped).

    Unknown token ids are simply absent from the returned mapping; they never
    raise.  ``client_factory`` is an injection seam for tests; production uses
    the same read-only public client the monitor constructs.
    """

    requested = tuple(str(token) for token in token_ids)
    if not requested:
        return {}
    books = asyncio.run(
        _fetch_books(requested, client_factory=client_factory)
    )
    return cast(Mapping[str, ThresholdOrderBook], books)


def paper_live_books(
    token_ids: Sequence[str],
    *,
    client_factory: Callable[[], object] = DEFAULT_CLIENT_FACTORY,
) -> Mapping[str, PaperBook]:
    """Current public CLOB books with the order facts paper sizing needs.

    This uses the same one-batch, public-client lifecycle as ``live_books``;
    it only preserves minimum size, tick and optional minimum-notional facts.
    Missing facts remain ``None`` for the caller to reject explicitly.
    """

    requested = tuple(str(token) for token in token_ids)
    if not requested:
        return {}
    books = asyncio.run(
        _fetch_books(
            requested,
            client_factory=client_factory,
            include_order_facts=True,
        )
    )
    return cast(Mapping[str, PaperBook], books)


#: In-process registry of conditionId -> {"yes_token_id", "no_token_id"},
#: injected by the orchestrator from the derived group's venue metadata.
#: Issue #114 retired the contract-keyed book wrapper: the harness itself
#: resolves each action's read to its direction's clobTokenId (the tokens
#: persist on the activated replica endpoints via the leg token map), so the
#: registry is orchestrator-side run state and a diagnostics surface only.
_CONTRACT_TOKEN_MAP: dict[str, dict[str, str]] = {}


def set_contract_token_map(mapping: Mapping[str, Mapping[str, str]]) -> None:
    """Replace the in-process conditionId -> YES/NO clobTokenId registry."""

    _CONTRACT_TOKEN_MAP.clear()
    _CONTRACT_TOKEN_MAP.update(
        (str(contract), dict(tokens)) for contract, tokens in mapping.items()
    )


def contract_token_map() -> Mapping[str, Mapping[str, str]]:
    """Read-only snapshot of the current conditionId -> token pair registry."""

    return {contract: dict(tokens) for contract, tokens in _CONTRACT_TOKEN_MAP.items()}
