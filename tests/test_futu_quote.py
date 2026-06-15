from __future__ import annotations

from decimal import Decimal

import pytest

from open_trader.futu_quote import FutuQuoteClient, FutuQuoteError
from open_trader.futu_watch import QuoteSnapshot


class FakeDataFrame:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def to_dict(self, orient: str) -> list[dict[str, object]]:
        assert orient == "records"
        return self._rows


class FakeOpenQuoteContext:
    def __init__(self, *, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.closed = False
        self.requested_symbols: list[str] = []

    def get_market_snapshot(self, symbols: list[str]) -> tuple[int, object]:
        self.requested_symbols = symbols
        return (
            0,
            FakeDataFrame(
                [
                    {"code": "US.VIXY", "last_price": 94.5},
                    {"code": "US.QQQ", "last_price": "510.25"},
                ]
            ),
        )

    def close(self) -> None:
        self.closed = True


class FakeFailingContext(FakeOpenQuoteContext):
    def get_market_snapshot(self, symbols: list[str]) -> tuple[int, object]:
        return -1, "OpenD connection failed"


def test_futu_quote_client_returns_normalized_snapshots() -> None:
    client = FutuQuoteClient(
        host="127.0.0.1",
        port=11111,
        context_factory=FakeOpenQuoteContext,
    )

    snapshots = client.get_snapshots(["US.VIXY", "US.QQQ"])

    assert snapshots == {
        "US.VIXY": QuoteSnapshot("US.VIXY", Decimal("94.5")),
        "US.QQQ": QuoteSnapshot("US.QQQ", Decimal("510.25")),
    }
    assert client.context.requested_symbols == ["US.VIXY", "US.QQQ"]


def test_futu_quote_client_raises_clear_error_on_sdk_failure() -> None:
    client = FutuQuoteClient(
        host="127.0.0.1",
        port=11111,
        context_factory=FakeFailingContext,
    )

    with pytest.raises(FutuQuoteError, match="OpenD connection failed"):
        client.get_snapshots(["US.VIXY"])


def test_futu_quote_client_close_closes_context() -> None:
    client = FutuQuoteClient(
        host="127.0.0.1",
        port=11111,
        context_factory=FakeOpenQuoteContext,
    )

    client.close()

    assert client.context.closed is True
