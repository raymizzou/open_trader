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
    sleep_fn: object = lambda seconds: None,
    now_fn: object = lambda: datetime(2026, 8, 2, tzinfo=UTC),
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
            now_fn=now_fn,
            sleep_fn=sleep_fn,
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


def test_repeated_rest_snapshot_version_refreshes_the_confirmed_book() -> None:
    received = [datetime(2026, 8, 2, tzinfo=UTC)]
    snapshot = {
        "marketId": 123,
        "version": 2,
        "updateTimestampMs": 1788048000000,
        "asks": [["0.51", "3"]],
        "bids": [["0.50", "2"]],
    }
    source, _ = source_with_responses(
        [
            {"success": True, "data": market()},
            {"success": True, "data": snapshot},
            {"success": True, "data": snapshot},
        ],
        now_fn=lambda: received[0],
    )

    first = asyncio.run(source.get_order_book("123"))
    received[0] = datetime(2026, 8, 2, 0, 0, 1, tzinfo=UTC)
    second = asyncio.run(source.get_order_book("123"))

    assert first is not None and second is not None
    assert second.received_at > first.received_at
    assert source.snapshot()["rest"] == "ready"


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


def test_snapshot_reports_predict_health_without_api_key() -> None:
    pending, _ = source_with_responses(
        [], key_loader=lambda: (_ for _ in ()).throw(KeychainError("keychain_empty"))
    )
    blocked, _ = source_with_responses(
        [HTTPError("https://api.predict.fun/v1/markets", 401, "no", {}, None)]
    )
    ready, _ = source_with_responses([{"success": True, "data": []}])
    failed, _ = source_with_responses([{"success": False, "data": []}])

    assert asyncio.run(pending.list_open_markets()) == ()
    assert asyncio.run(blocked.list_open_markets()) == ()
    assert asyncio.run(ready.list_open_markets()) == ()
    assert asyncio.run(failed.list_open_markets()) == ()

    pending_snapshot = pending.snapshot()
    assert pending_snapshot["rest"] == "pending"
    assert pending_snapshot["reason"] == "api_key_pending"
    assert pending_snapshot["balance"] is None
    assert pending_snapshot["balance_state"] == "unavailable"
    assert pending_snapshot["settlement_asset"] == "USDT"
    assert blocked.snapshot()["reason"] == "auth_blocked"
    assert ready.snapshot()["last_success"] == "2026-08-02T00:00:00+00:00"
    assert ready.snapshot()["reason"] is None
    assert failed.snapshot()["reason"] == "rest_stale"
    assert "predict-key-sentinel" not in repr(ready.snapshot())


class FakeWebSocket:
    def __init__(self, messages: list[object] | None = None) -> None:
        self.sent: list[str] = []
        self.messages = messages or [
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
        message = self.messages.pop(0)
        if isinstance(message, Exception):
            raise message
        return str(message)


def book_message(version: int, timestamp: int, *, asks: object = None) -> str:
    return json.dumps(
        {
            "type": "M",
            "topic": "predictOrderbook/123",
            "data": {
                "marketId": 123,
                "version": version,
                "updateTimestampMs": timestamp,
                "asks": asks if asks is not None else [["0.51", "3"]],
                "bids": [["0.50", "2"]],
            },
        }
    )


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


def test_stream_reconnect_accepts_a_fresh_lower_version_snapshot() -> None:
    reconnect_snapshots: list[dict[str, object]] = []
    source: PredictSource

    async def reconnect_sleep(seconds: float) -> None:
        del seconds
        reconnect_snapshots.append(source.snapshot())

    connections = iter(
        (
            FakeWebSocket(
                [
                    book_message(5, 1788048000000),
                    ConnectionError("connection dropped"),
                ]
            ),
            FakeWebSocket(
                [
                    book_message(1, 1788048001000),
                    book_message(6, 1788048002000),
                ]
            ),
        )
    )
    source, _ = source_with_responses(
        [{"success": True, "data": market()}],
        connector=lambda *args, **kwargs: next(connections),
        sleep_fn=reconnect_sleep,
    )

    async def collect_two() -> tuple[object, object]:
        stream = source.stream_books(("123",))
        try:
            return await anext(stream), await anext(stream)
        finally:
            await stream.aclose()

    _, reconnected = asyncio.run(collect_two())

    assert reconnected.source_timestamp == datetime.fromtimestamp(1788048001, UTC)
    assert [{
        "venue": "predict.fun",
        "wallet": "0xcE23…f435",
        "rest": "ready",
        "ws": "stale",
        "ws_generation": 1,
    }.items() <= snapshot.items() for snapshot in reconnect_snapshots] == [True]
    assert source.snapshot()["ws"] == "ready"
    assert source.snapshot()["ws_generation"] == 1


def test_non_finite_book_prices_are_dropped_without_raising() -> None:
    source, _ = source_with_responses(
        [
            {"success": True, "data": market()},
            {
                "success": True,
                "data": {
                    "marketId": 123,
                    "version": 1,
                    "updateTimestampMs": 1788048000000,
                    "asks": [["NaN", "3"]],
                    "bids": [["0.50", "2"]],
                },
            },
        ]
    )

    assert asyncio.run(source.get_order_book("123")) is None
    assert source.snapshot()["rest"] == "stale"


def test_rest_and_websocket_failures_keep_each_other_health() -> None:
    websocket_source, _ = source_with_responses(
        [{"success": True, "data": market()}],
        connector=lambda *args, **kwargs: FakeWebSocket(
            [book_message(1, 1788048000000, asks=[]), book_message(2, 1788048001000)]
        ),
    )

    async def stream_one(source: PredictSource) -> object:
        stream = source.stream_books(("123",))
        try:
            return await anext(stream)
        finally:
            await stream.aclose()

    assert asyncio.run(stream_one(websocket_source)).market_id == "123"
    assert websocket_source.snapshot()["rest"] == "ready"
    assert websocket_source.snapshot()["ws"] == "ready"

    rest_source, _ = source_with_responses(
        [
            {"success": True, "data": market()},
            {
                "success": True,
                "data": {
                    "marketId": 123,
                    "version": 2,
                    "updateTimestampMs": 1788048001000,
                    "asks": [],
                    "bids": [["0.50", "2"]],
                },
            },
        ],
        connector=lambda *args, **kwargs: FakeWebSocket([book_message(1, 1788048000000)]),
    )

    assert asyncio.run(stream_one(rest_source)).market_id == "123"
    assert asyncio.run(rest_source.get_order_book("123")) is None
    assert rest_source.snapshot()["rest"] == "stale"
    assert rest_source.snapshot()["ws"] == "ready"


def test_equal_rest_and_websocket_versions_have_independent_freshness() -> None:
    payload = {
        "success": True,
        "data": {
            "marketId": 123,
            "version": 5,
            "updateTimestampMs": 1788048000000,
            "asks": [["0.51", "3"]],
            "bids": [["0.50", "2"]],
        },
    }
    websocket_first, _ = source_with_responses(
        [{"success": True, "data": market()}, payload],
        connector=lambda *args, **kwargs: FakeWebSocket(
            [book_message(5, 1788048000000)]
        ),
    )

    async def ws_then_rest() -> tuple[object, object]:
        stream = websocket_first.stream_books(("123",))
        try:
            return await anext(stream), await websocket_first.get_order_book("123")
        finally:
            await stream.aclose()

    ws_book, rest_book = asyncio.run(ws_then_rest())
    assert ws_book is not None and rest_book is not None
    assert websocket_first.snapshot()["ws"] == "ready"
    assert websocket_first.snapshot()["rest"] == "ready"

    rest_first, _ = source_with_responses(
        [{"success": True, "data": market()}, payload],
        connector=lambda *args, **kwargs: FakeWebSocket(
            [book_message(5, 1788048000000)]
        ),
    )

    async def rest_then_ws() -> tuple[object, object]:
        rest = await rest_first.get_order_book("123")
        stream = rest_first.stream_books(("123",))
        try:
            return rest, await anext(stream)
        finally:
            await stream.aclose()

    rest_book, ws_book = asyncio.run(rest_then_ws())
    assert rest_book is not None and ws_book is not None
    assert rest_first.snapshot()["rest"] == "ready"
    assert rest_first.snapshot()["ws"] == "ready"
