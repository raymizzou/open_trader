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

from polymarket import AsyncPublicClient

from open_trader.prediction_arbitrage import ThresholdOrderBook
from open_trader.polymarket_monitor import (
    _asks,
    _items,
    _value,
)

#: Same client construction as the monitor's default public client factory.
DEFAULT_CLIENT_FACTORY: Callable[[], object] = AsyncPublicClient


async def _fetch_books(
    token_ids: tuple[str, ...],
    *,
    client_factory: Callable[[], object],
) -> dict[str, ThresholdOrderBook]:
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
        books: dict[str, ThresholdOrderBook] = {}
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
    return asyncio.run(_fetch_books(requested, client_factory=client_factory))


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
