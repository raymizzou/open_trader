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


#: In-process registry of conditionId -> YES clobTokenId, injected by the
#: orchestrator from the derived group's venue metadata before it calls the
#: harness.  Real mechanical relations key their actions by conditionId
#: (0x-prefixed) while the CLOB book endpoint is keyed by numeric
#: clobTokenId, so the contract-keyed wrapper translates through this table.
_CONTRACT_TOKEN_MAP: dict[str, str] = {}


def set_contract_token_map(mapping: Mapping[str, str]) -> None:
    """Replace the in-process conditionId -> YES clobTokenId registry."""

    _CONTRACT_TOKEN_MAP.clear()
    _CONTRACT_TOKEN_MAP.update(
        (str(contract), str(token)) for contract, token in mapping.items()
    )


def contract_token_map() -> Mapping[str, str]:
    """Read-only snapshot of the current conditionId -> YES token registry."""

    return dict(_CONTRACT_TOKEN_MAP)


def contract_keyed_live_books(
    token_ids: Sequence[str],
    *,
    client_factory: Callable[[], object] | None = None,
) -> Mapping[str, ThresholdOrderBook]:
    """Live books for conditionId-keyed requests (read-only, monitor-shaped).

    Each requested conditionId is translated to its registered YES
    clobTokenId, fetched through the same ``live_books`` read-only seam
    (zero-write contract inherited verbatim), and returned keyed by the
    requested conditionId.  Unmapped conditionIds are skipped — they never
    reach the client and never raise; unknown/unfetched tokens stay absent.
    ``client_factory`` is forwarded to ``live_books`` only when explicitly
    given, so a replaced module-level ``live_books`` (test seam) keeps its
    own signature; production uses its default read-only client.
    """

    requested = tuple(str(token) for token in token_ids)
    translated = tuple(
        dict.fromkeys(
            _CONTRACT_TOKEN_MAP[contract]
            for contract in requested
            if contract in _CONTRACT_TOKEN_MAP
        )
    )
    if not translated:
        return {}
    if client_factory is None:
        fetched = live_books(translated)
    else:
        fetched = live_books(translated, client_factory=client_factory)
    keyed: dict[str, ThresholdOrderBook] = {}
    for contract in requested:
        book = fetched.get(_CONTRACT_TOKEN_MAP.get(contract, ""))
        if book is not None:
            keyed[contract] = book
    return keyed
