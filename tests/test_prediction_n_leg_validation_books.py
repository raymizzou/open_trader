"""Issue #71 finishing: live book-source adapter tests (shape + zero-write).

The adapter ``open_trader.prediction_n_leg_validation_books.live_books`` is
the harness book seam backed by the same read-only Polymarket CLOB channel
the monitor uses.  The tests inject a fake CLOB client that records every
endpoint access, so any trading mutation (order/cancel/merge/redeem/
allowance) shows up in the call log and fails the zero-write assertions.
"""

from __future__ import annotations

import json
from copy import deepcopy
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from open_trader.prediction_arbitrage import BookLevel, ThresholdOrderBook
from open_trader.prediction_n_leg_validation import run_paper_three_way
from open_trader.prediction_n_leg_validation_books import (
    PaperBook,
    live_books,
    paper_live_books,
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

PAPER_BOOKS = {
    f"paper-token-{contract}": {
        "token_id": f"paper-token-{contract}",
        "asks": [{"price": price, "size": "10"}],
        "bids": [],
        "min_order_size": "5",
        "tick_size": "0.01",
        "minimum_order_notional": "2",
    }
    for contract, price in (("a", "0.30"), ("b", "0.32"), ("c", "0.33"))
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


def paper_rows() -> dict[str, dict[str, object]]:
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "prediction_n_leg_validation_frozen_n3.json")
        .read_text(encoding="utf-8")
    )
    problem = deepcopy(fixture["problem"])
    for state in problem["terminal_state_sets"]:
        for atom in state["atoms"]:
            atom["capital_release_at"] = None
    contracts = ("a", "b", "c")
    tokens = {contract: f"paper-token-{contract}" for contract in contracts}
    endpoints = [
        {
            "venue": "polymarket",
            "contract_id": contract,
            "yes_token_id": tokens[contract],
            "fees_enabled": False,
            "settlement_rules": "known supported football rule",
        }
        for contract in contracts
    ]
    return {
        "paper-three": {
            "version_id": "paper-version",
            "status": "PENDING",
            "activation": "PENDING",
            "endpoints": endpoints,
            "model": {
                "template": "FOOTBALL_REGULAR_TIME_3WAY_V1",
                "group_id": "paper-group",
                "member_count": 3,
                "directions": {"a": "HOME_WIN", "b": "DRAW", "c": "AWAY_WIN"},
                "rules": {
                    contract: "known supported football rule" for contract in contracts
                },
                "tokens": {
                    contract: {"YES": tokens[contract], "NO": f"paper-no-{contract}"}
                    for contract in contracts
                },
                "terminal_states": ["NORMAL_YES", "NORMAL_NO"],
                "payouts": {
                    contract: {"NORMAL_YES": 1, "NORMAL_NO": 0}
                    for contract in contracts
                },
                "capital_release": None,
                "incomplete_reasons": ["MISSING_CAPITAL_RELEASE_AT"],
                "problem": problem,
            },
        }
    }


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


def test_paper_live_books_preserves_order_facts(tmp_path: Path) -> None:
    missing_facts = deepcopy(PAPER_BOOKS)
    missing_facts["paper-token-b"].pop("min_order_size")
    missing_facts["paper-token-b"].pop("tick_size")
    before = datetime.now(UTC)
    facts_fake = RecordingFakeClobClient(missing_facts)
    facts = paper_live_books(
        ("paper-token-a", "paper-token-b"),
        client_factory=lambda: facts_fake,
    )
    after = datetime.now(UTC)

    assert isinstance(facts["paper-token-a"], PaperBook)
    known = facts["paper-token-a"]
    assert known.token_id == "paper-token-a"
    assert known.minimum_order_size == Decimal("5")
    assert known.tick_size == Decimal("0.01")
    assert known.minimum_order_notional == Decimal("2")
    assert known.book_convention == "outcome-token"
    assert before <= known.confirmed_at <= after
    missing = facts["paper-token-b"]
    assert missing.minimum_order_size is None
    assert missing.tick_size is None
    assert [call[0] for call in facts_fake.calls] == [
        "get_order_books",
        "close",
    ]
    assert facts_fake.calls[0][1] == ("paper-token-a", "paper-token-b")

    source_fake = RecordingFakeClobClient(deepcopy(PAPER_BOOKS))
    report = run_paper_three_way(
        paper_rows(),
        book_source=lambda token_ids: paper_live_books(
            token_ids,
            client_factory=lambda: source_fake,
        ),
        data_dir=tmp_path / "paper",
    )
    assert report["status"] == "PASS"
    assert report["reason"] is None
    assert report["economics"]["guaranteed_profit_units"] > 0
    assert report["order_ready"] is False
    assert [call[0] for call in source_fake.calls] == [
        "get_order_books",
        "close",
    ]


# ---------------------------------------------------------------------------
# Issue #114: the conditionId-keyed wrapper (contract_keyed_live_books) is
# retired.  The harness resolves each action's read to its direction's
# clobTokenId itself, so the default book source is the token-keyed
# live_books seam below — BUY_NO legs now read the NO token's book.
# ---------------------------------------------------------------------------
