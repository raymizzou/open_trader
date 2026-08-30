"""Issue #71 finishing: live book-source adapter tests (shape + zero-write).

The adapter ``open_trader.prediction_n_leg_validation_books.live_books`` is
the harness book seam backed by the same read-only Polymarket CLOB channel
the monitor uses.  The tests inject a fake CLOB client that records every
endpoint access, so any trading mutation (order/cancel/merge/redeem/
allowance) shows up in the call log and fails the zero-write assertions.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal

import pytest

from open_trader.prediction_arbitrage import BookLevel, ThresholdOrderBook
from open_trader.prediction_n_leg_validation_books import (
    contract_keyed_live_books,
    contract_token_map,
    live_books,
    set_contract_token_map,
)


# Raw SDK-shaped CLOB books: aliases the monitor accepts (asset_id), price
# and size as strings, epoch-millisecond and ISO exchange timestamps.
RAW_BOOKS = {
    "token-a": {
        "asset_id": "token-a",
        "asks": [{"price": "0.33", "size": "10"}],
        "bids": [],
        "timestamp": 1755295200000,
    },
    "token-b": {
        "token_id": "token-b",
        "asks": [
            {"price": "0.32", "size": "20"},
            {"price": "0.31", "size": "5"},
        ],
        "bids": [{"price": "0.30", "size": "7"}],
        "timestamp": "2026-08-16T02:00:00Z",
    },
}

# Any client attribute outside this allowlist that the adapter touches is a
# potential write path and fails the test via the recording fake.
ALLOWED_CALLS = frozenset({"get_order_books", "close"})


class RecordingFakeClobClient:
    """Fake public CLOB client that records every endpoint access."""

    def __init__(self, raw_books: dict[str, dict]) -> None:
        self.raw_books = raw_books
        self.calls: list[tuple] = []

    async def get_order_books(self, *, token_ids: list[str]) -> tuple[dict, ...]:
        self.calls.append(("get_order_books", tuple(token_ids)))
        # Mirror the real read: unknown tokens are simply absent from the
        # response (they must not raise).
        return tuple(self.raw_books[token] for token in token_ids if token in self.raw_books)

    async def close(self) -> None:
        self.calls.append(("close", ()))

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)

        def _record(*args: object, **kwargs: object) -> object:
            self.calls.append((name, args, kwargs))
            raise AssertionError(
                f"adapter touched a non-allowlisted client endpoint: {name}"
            )

        return _record


def make_fake() -> RecordingFakeClobClient:
    return RecordingFakeClobClient(dict(RAW_BOOKS))


def test_live_books_returns_monitor_shaped_books_per_token() -> None:
    fake = make_fake()

    books = live_books(
        ("token-a", "token-b"),
        client_factory=lambda: fake,
    )

    assert isinstance(books, Mapping)
    assert set(books) == {"token-a", "token-b"}
    book = books["token-a"]
    assert isinstance(book, ThresholdOrderBook)
    assert book.token_id == "token-a"
    # Same shape as PolymarketMonitor.cross_venue_books: asks/bids tuples of
    # BookLevel(price, size) plus a confirmed_at timestamp.
    assert book.asks == (BookLevel(Decimal("0.33"), Decimal("10")),)
    assert book.bids == ()
    assert isinstance(book.confirmed_at, datetime)
    deep = books["token-b"]
    assert deep.asks == (
        BookLevel(Decimal("0.32"), Decimal("20")),
        BookLevel(Decimal("0.31"), Decimal("5")),
    )
    assert deep.bids == (BookLevel(Decimal("0.30"), Decimal("7")),)
    assert isinstance(deep.confirmed_at, datetime)


def test_live_books_only_calls_read_endpoints() -> None:
    fake = make_fake()

    live_books(("token-a", "token-b"), client_factory=lambda: fake)

    called_names = [call[0] for call in fake.calls]
    assert "get_order_books" in called_names
    assert set(called_names) <= ALLOWED_CALLS
    book_calls = [call for call in fake.calls if call[0] == "get_order_books"]
    assert len(book_calls) == 1
    assert book_calls[0][1] == ("token-a", "token-b")


def test_live_books_omits_unknown_tokens_without_raising() -> None:
    fake = make_fake()

    books = live_books(
        ("token-a", "unknown-token"),
        client_factory=lambda: fake,
    )

    assert set(books) == {"token-a"}
    assert "unknown-token" not in books


# ---------------------------------------------------------------------------
# C1: conditionId-keyed wrapper over the same live book seam.
#
# Real mechanical relations key their actions by conditionId (0x-prefixed)
# while the CLOB get_order_books endpoint is keyed by numeric clobTokenId;
# the wrapper translates through an in-process registry the orchestrator
# injects from the derived group's venue metadata.
# ---------------------------------------------------------------------------

CONTRACT_RAW_BOOKS = {
    "yes-a": {
        "asset_id": "yes-a",
        "asks": [{"price": "0.33", "size": "10"}],
        "bids": [],
        "timestamp": 1755295200000,
    },
    "yes-b": {
        "token_id": "yes-b",
        "asks": [
            {"price": "0.32", "size": "20"},
            {"price": "0.31", "size": "5"},
        ],
        "bids": [{"price": "0.30", "size": "7"}],
        "timestamp": "2026-08-16T02:00:00Z",
    },
}


def make_contract_fake() -> RecordingFakeClobClient:
    return RecordingFakeClobClient(dict(CONTRACT_RAW_BOOKS))


@pytest.fixture()
def clean_contract_registry():
    """Leave the module-level registry empty before and after each test."""

    set_contract_token_map({})
    yield
    set_contract_token_map({})


def test_contract_keyed_live_books_returns_books_keyed_by_requested_condition_id(
    clean_contract_registry,
) -> None:
    fake = make_contract_fake()
    set_contract_token_map({"cond-a": "yes-a", "cond-b": "yes-b"})

    books = contract_keyed_live_books(
        ("cond-a", "cond-b", "cond-x"),
        client_factory=lambda: fake,
    )

    assert isinstance(books, Mapping)
    # Returned keys are the requested conditionIds; the unmapped cond-x is
    # simply absent and never raises.
    assert set(books) == {"cond-a", "cond-b"}
    assert "cond-x" not in books
    # Book shape identical to the V1 live_books assertions.
    book = books["cond-a"]
    assert isinstance(book, ThresholdOrderBook)
    assert book.asks == (BookLevel(Decimal("0.33"), Decimal("10")),)
    assert book.bids == ()
    assert isinstance(book.confirmed_at, datetime)
    deep = books["cond-b"]
    assert deep.asks == (
        BookLevel(Decimal("0.32"), Decimal("20")),
        BookLevel(Decimal("0.31"), Decimal("5")),
    )
    assert deep.bids == (BookLevel(Decimal("0.30"), Decimal("7")),)
    assert isinstance(deep.confirmed_at, datetime)


def test_contract_keyed_live_books_fetches_only_mapped_yes_tokens_zero_write(
    clean_contract_registry,
) -> None:
    fake = make_contract_fake()
    set_contract_token_map({"cond-a": "yes-a", "cond-b": "yes-b"})

    contract_keyed_live_books(
        ("cond-a", "cond-b", "cond-x"),
        client_factory=lambda: fake,
    )

    # The CLOB client only ever sees the translated YES token ids.
    book_calls = [call for call in fake.calls if call[0] == "get_order_books"]
    assert len(book_calls) == 1
    assert book_calls[0][1] == ("yes-a", "yes-b")
    # Zero-write contract inherited from live_books: read endpoints only.
    assert {call[0] for call in fake.calls} <= ALLOWED_CALLS


def test_contract_keyed_live_books_empty_registry_returns_empty_mapping(
    clean_contract_registry,
) -> None:
    fake = make_contract_fake()

    books = contract_keyed_live_books(
        ("cond-a", "cond-b"),
        client_factory=lambda: fake,
    )

    assert books == {}
    assert fake.calls == []
