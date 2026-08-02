from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from urllib.error import HTTPError

from open_trader.polymarket_trading import KeychainError, PredictConfig
from open_trader.predict_source import PredictSource


PREDICT_WALLET = "0xcE23B341C888A88C4C44D8B5Aa6D04A8615Ff435"


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


def market(**changes: object) -> dict[str, object]:
    result: dict[str, object] = {
        "id": 123,
        "conditionId": "predict-condition",
        "question": "Will this test pass?",
        "description": "Resolves from the named source.",
        "resolutionSource": "Predict Oracle",
        "closesAt": "2026-12-31T00:00:00Z",
        "settlementAt": "2027-01-01T00:00:00Z",
        "outcomes": [
            {"name": "YES", "onChainId": "predict-yes"},
            {"name": "NO", "onChainId": "predict-no"},
        ],
        "collateralToken": {"symbol": "USDT"},
        "minimumOrderSize": "5",
        "decimalPrecision": 2,
        "feeRateBps": "200",
        "polymarketConditionIds": ["poly-condition"],
        "tradingStatus": "OPEN",
        "status": "REGISTERED",
        "marketType": "BINARY",
        "isNegRisk": False,
        "isYieldBearing": False,
    }
    result.update(changes)
    return result


def source_with_responses(
    responses: list[object],
    *,
    connector: object | None = None,
    key_loader: object = lambda: "predict-key-sentinel",
) -> tuple[PredictSource, list[object]]:
    requests: list[object] = []

    def opener(request: object, **kwargs: object) -> FakeResponse:
        requests.append(request)
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return FakeResponse(response)

    return (
        PredictSource(
            PredictConfig(PREDICT_WALLET),
            key_loader=key_loader,
            urlopen_fn=opener,
            websocket_connect=connector,
            now_fn=lambda: datetime(2026, 8, 2, tzinfo=UTC),
            sleep_fn=lambda seconds: None,
        ),
        requests,
    )


def test_list_open_markets_uses_mainnet_api_key_and_keeps_only_standard_binary_markets() -> None:
    source, requests = source_with_responses(
        [
            {"success": True, "cursor": "next", "data": [market(), market(isNegRisk=True)]},
            {"success": True, "cursor": None, "data": [market(id=124, outcomes=[{"name": "YES"}])]},
        ]
    )

    markets = asyncio.run(source.list_open_markets())

    assert [item.market_id for item in markets] == ["123"]
    assert markets[0].condition_id == "predict-condition"
    assert markets[0].polymarket_condition_ids == ("poly-condition",)
    assert markets[0].tick_size == Decimal("0.01")
    assert markets[0].fee_rate_bps == Decimal("200")
    assert all(request.full_url.startswith("https://api.predict.fun/") for request in requests)
    assert all(request.headers["X-api-key"] == "predict-key-sentinel" for request in requests)


def test_complete_yes_book_derives_no_asks_at_the_market_tick() -> None:
    source, _ = source_with_responses(
        [
            {"success": True, "data": market()},
            {
                "success": True,
                "data": {
                    "marketId": 123,
                    "version": 1,
                    "updateTimestampMs": 1788048000000,
                    "asks": [["0.51", "3"]],
                    "bids": [["0.50", "2"], ["0.45", "4"]],
                },
            },
        ]
    )

    book = asyncio.run(source.get_order_book("123"))

    assert book is not None
    assert book.yes_asks[0].price == Decimal("0.51")
    assert book.no_asks == (
        type(book.yes_asks[0])(price=Decimal("0.55"), size=Decimal("4")),
        type(book.yes_asks[0])(price=Decimal("0.50"), size=Decimal("2")),
    )
    assert book.source_timestamp == datetime.fromtimestamp(1788048000, UTC)


def test_out_of_order_book_is_dropped_and_marks_source_stale() -> None:
    source, _ = source_with_responses(
        [
            {"success": True, "data": market()},
            {
                "success": True,
                "data": {
                    "marketId": 123,
                    "version": 2,
                    "updateTimestampMs": 1788048000000,
                    "asks": [["0.51", "3"]],
                    "bids": [["0.50", "2"]],
                },
            },
            {
                "success": True,
                "data": {
                    "marketId": 123,
                    "version": 1,
                    "updateTimestampMs": 1788048000000,
                    "asks": [["0.51", "3"]],
                    "bids": [["0.50", "2"]],
                },
            },
        ]
    )

    assert asyncio.run(source.get_order_book("123")) is not None
    assert asyncio.run(source.get_order_book("123")) is None
    assert source.snapshot()["rest"] == "stale"


def test_missing_or_rejected_key_is_safe_and_never_retried() -> None:
    pending, pending_requests = source_with_responses(
        [], key_loader=lambda: (_ for _ in ()).throw(KeychainError("keychain_empty"))
    )
    blocked, blocked_requests = source_with_responses(
        [HTTPError("https://api.predict.fun/v1/markets", 401, "no", {}, None)]
    )

    assert asyncio.run(pending.list_open_markets()) == ()
    assert pending.snapshot()["rest"] == "pending"
    assert pending_requests == []
    assert asyncio.run(blocked.list_open_markets()) == ()
    assert blocked.snapshot()["rest"] == "auth_blocked"
    assert len(blocked_requests) == 1


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.messages = [
            json.dumps({"type": "M", "topic": "heartbeat", "data": 1736696400000}),
            json.dumps(
                {
                    "type": "M",
                    "topic": "predictOrderbook/123",
                    "data": {
                        "marketId": 123,
                        "version": 1,
                        "updateTimestampMs": 1788048000000,
                        "asks": [["0.51", "3"]],
                        "bids": [["0.50", "2"]],
                    },
                }
            ),
        ]

    async def __aenter__(self) -> "FakeWebSocket":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def recv(self) -> str:
        if not self.messages:
            raise StopAsyncIteration
        return self.messages.pop(0)


def test_stream_subscribes_and_echoes_heartbeat_without_exposing_api_key() -> None:
    websocket = FakeWebSocket()
    source, _ = source_with_responses(
        [{"success": True, "data": market()}], connector=lambda *args, **kwargs: websocket
    )

    async def collect_one() -> object:
        stream = source.stream_books(("123",))
        try:
            return await anext(stream)
        finally:
            await stream.aclose()

    book = asyncio.run(collect_one())

    assert book.market_id == "123"
    assert [json.loads(message) for message in websocket.sent] == [
        {"method": "subscribe", "requestId": 1, "params": ["predictOrderbook/123"]},
        {"method": "heartbeat", "data": 1736696400000},
    ]
    snapshot = source.snapshot()
    assert snapshot["wallet"] == "0xcE23…f435"
    assert snapshot["rest"] == "ready"
    assert snapshot["ws"] == "ready"
    assert "predict-key-sentinel" not in repr(snapshot)
