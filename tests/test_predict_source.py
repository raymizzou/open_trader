from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from urllib.error import HTTPError

import pytest

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
        "title": "Will this test pass?",
        "conditionId": "predict-condition",
        "question": "Will this test pass?",
        "description": "Resolves from the named source.",
        "categorySlug": "btc-year-end",
        "outcomes": [
            {"name": "YES", "onChainId": "predict-yes"},
            {"name": "NO", "onChainId": "predict-no"},
        ],
        "decimalPrecision": 2,
        "feeRateBps": "200",
        "polymarketConditionIds": ["poly-condition"],
        "tradingStatus": "OPEN",
        "status": "REGISTERED",
        "isVisible": True,
        "isNegRisk": False,
        "isYieldBearing": False,
        "oracleQuestionId": "oracle-question",
        "resolverAddress": "0x1111111111111111111111111111111111111111",
        "spreadThreshold": "0.01",
        "shareThreshold": "0.01",
        "isBoosted": False,
        "createdAt": "2026-01-01T00:00:00Z",
        "marketVariant": "DEFAULT",
        "rewards": [],
    }
    result.update(changes)
    return result


def category(**changes: object) -> dict[str, object]:
    result: dict[str, object] = {
        "slug": "btc-year-end",
        "startsAt": "2026-01-01T00:00:00Z",
        "endsAt": "2026-12-31T23:59:00Z",
        "resolutionProvider": "PREDICT_DOT_FUN",
    }
    result.update(changes)
    return result


def source_with_responses(
    responses: list[object],
    *,
    categories: dict[str, object] | None = None,
    connector: object | None = None,
    key_loader: object = lambda: "predict-key-sentinel",
    sleep_fn: object = lambda seconds: None,
    now_fn: object = lambda: datetime(2026, 8, 2, tzinfo=UTC),
) -> tuple[PredictSource, list[object]]:
    requests: list[object] = []

    def opener(request: object, **kwargs: object) -> FakeResponse:
        requests.append(request)
        path = request.full_url.removeprefix("https://api.predict.fun")
        if path.startswith("/v1/categories/"):
            slug = path.removeprefix("/v1/categories/")
            payload = (categories or {}).get(slug, category(slug=slug))
            return FakeResponse({"success": True, "data": payload})
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


def test_get_market_joins_category_timing_and_resolution_provider() -> None:
    source, requests = source_with_responses(
        [{"success": True, "data": market(id=896, slug="btc-year-end-896")}]
    )

    result = asyncio.run(source.get_market("896"))

    assert result is not None
    assert result.market_slug == "btc-year-end-896"
    assert result.category_slug == "btc-year-end"
    assert result.event_start_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert result.event_end_at == datetime(2026, 12, 31, 23, 59, tzinfo=UTC)
    assert result.resolution_provider == "PREDICT_DOT_FUN"
    assert [request.full_url.removeprefix("https://api.predict.fun") for request in requests] == [
        "/v1/markets/896",
        "/v1/categories/btc-year-end",
    ]


def test_category_request_is_reused_for_sibling_markets() -> None:
    source, requests = source_with_responses(
        [{"success": True, "data": [market(id=896), market(id=897)]}]
    )

    result = asyncio.run(source.list_open_markets())

    assert [item.market_id for item in result] == ["896", "897"]
    assert [request.full_url.removeprefix("https://api.predict.fun") for request in requests] == [
        "/v1/markets?first=100&status=OPEN",
        "/v1/categories/btc-year-end",
    ]


def test_missing_or_unparseable_category_timing_excludes_market() -> None:
    missing, _ = source_with_responses(
        [{"success": True, "data": market(id=896)}],
        categories={"btc-year-end": category(endsAt=None)},
    )
    unparseable, _ = source_with_responses(
        [{"success": True, "data": market(id=896)}],
        categories={"btc-year-end": category(startsAt="not-a-date")},
    )

    assert asyncio.run(missing.get_market("896")) is None
    assert asyncio.run(unparseable.get_market("896")) is None


def test_non_increasing_category_window_excludes_market() -> None:
    equal, _ = source_with_responses(
        [{"success": True, "data": market(id=896)}],
        categories={"btc-year-end": category(endsAt="2026-01-01T00:00:00Z")},
    )
    reversed_window, _ = source_with_responses(
        [{"success": True, "data": market(id=896)}],
        categories={"btc-year-end": category(endsAt="2025-12-31T23:59:00Z")},
    )

    assert asyncio.run(equal.get_market("896")) is None
    assert asyncio.run(reversed_window.get_market("896")) is None


@pytest.mark.parametrize("field", ("oracleQuestionId", "resolverAddress"))
def test_missing_official_market_identity_excludes_market(field: str) -> None:
    source, _ = source_with_responses(
        [{"success": True, "data": market(id=896, **{field: None})}]
    )

    assert asyncio.run(source.get_market("896")) is None


def test_official_default_variant_is_accepted() -> None:
    source, _ = source_with_responses(
        [{"success": True, "data": market(id=896, marketVariant="DEFAULT")}]
    )

    assert asyncio.run(source.get_market("896")) is not None


def test_rules_fingerprint_changes_with_each_matching_input() -> None:
    def fingerprint(*, changes: dict[str, object] | None = None, category_changes: dict[str, object] | None = None) -> str:
        source, _ = source_with_responses(
            [{"success": True, "data": market(id=896, **(changes or {}))}],
            categories={"btc-year-end": category(**(category_changes or {}))},
        )
        result = asyncio.run(source.get_market("896"))
        assert result is not None
        return result.rules_fingerprint

    baseline = fingerprint()
    for changes, category_changes in (
        ({"question": "A different question"}, None),
        ({"description": "A different rule"}, None),
        (None, {"resolutionProvider": "OTHER"}),
        (None, {"startsAt": "2026-01-02T00:00:00Z"}),
        (None, {"endsAt": "2026-12-30T23:59:00Z"}),
        ({"outcomes": [{"name": "YES", "onChainId": "other-yes"}, {"name": "NO", "onChainId": "predict-no"}]}, None),
        ({"polymarketConditionIds": ["other-condition"]}, None),
    ):
        assert fingerprint(changes=changes, category_changes=category_changes) != baseline


def test_empty_polymarket_ids_remain_empty_without_catalog_scan() -> None:
    source, requests = source_with_responses(
        [{"success": True, "data": market(id=896, polymarketConditionIds=[])}]
    )

    result = asyncio.run(source.get_market("896"))

    assert result is not None
    assert result.polymarket_condition_ids == ()
    assert len(requests) == 2


def test_list_open_markets_uses_mainnet_api_key_and_keeps_only_standard_binary_markets() -> None:
    source, requests = source_with_responses(
        [
            {"success": True, "cursor": "next", "data": [market(), market(isNegRisk=True)]},
            {"success": True, "cursor": None, "data": [market(id=124, tradingStatus="CLOSED")]},
        ]
    )

    markets = asyncio.run(source.list_open_markets())

    assert [item.market_id for item in markets] == ["123"]
    assert markets[0].condition_id == "predict-condition"
    assert markets[0].polymarket_condition_ids == ("poly-condition",)
    assert markets[0].tick_size == Decimal("0.01")
    assert markets[0].minimum_order_size == Decimal("0.01")
    assert markets[0].settlement_asset == "USDT"
    assert markets[0].fee_rate_bps == Decimal("200")
    assert all(request.full_url.startswith("https://api.predict.fun/") for request in requests)
    assert all(request.headers["X-api-key"] == "predict-key-sentinel" for request in requests)
    assert all(request.headers["User-agent"] == "open-trader/0.1" for request in requests)
    market_urls = [request.full_url for request in requests if "/v1/markets?" in request.full_url]
    assert market_urls == [
        "https://api.predict.fun/v1/markets?first=100&status=OPEN",
        "https://api.predict.fun/v1/markets?first=100&status=OPEN&after=next",
    ]


def test_list_open_markets_limit_stops_after_first_eligible_market() -> None:
    source, requests = source_with_responses(
        [{"success": True, "cursor": "next", "data": [market()]}]
    )

    markets = asyncio.run(source.list_open_markets(limit=1))

    assert [item.market_id for item in markets] == ["123"]
    assert not any("after=" in request.full_url for request in requests)


@pytest.mark.parametrize(
    "changes",
    (
        {"isNegRisk": True},
        {"isYieldBearing": True},
        {"tradingStatus": "CLOSED"},
        {"isVisible": False},
        {"marketVariant": "SPORTS"},
    ),
)
def test_explicitly_out_of_scope_markets_are_a_healthy_empty_scan(
    changes: dict[str, object],
) -> None:
    source, requests = source_with_responses(
        [{"success": True, "cursor": None, "data": [market(**changes)]}]
    )

    assert asyncio.run(source.list_open_markets()) == ()
    assert source.snapshot()["rest"] == "ready"
    assert len(requests) == 1


def test_non_yes_no_market_is_a_healthy_empty_scan() -> None:
    source, _ = source_with_responses(
        [
            {
                "success": True,
                "cursor": None,
                "data": [
                    market(
                        outcomes=[
                            {"name": "Doosan Bears", "onChainId": "predict-home"},
                            {"name": "KT Wiz", "onChainId": "predict-away"},
                        ]
                    )
                ],
            }
        ]
    )

    assert asyncio.run(source.list_open_markets()) == ()
    assert source.snapshot()["rest"] == "ready"


@pytest.mark.parametrize(
    ("changes", "category_changes"),
    (
        ({"conditionId": None}, {}),
        ({"feeRateBps": "bad"}, {}),
        (
            {
                "outcomes": [
                    {"name": "YES", "onChainId": "predict-yes"},
                    {"name": None, "onChainId": "predict-no"},
                ]
            },
            {},
        ),
        ({}, {"endsAt": "bad"}),
    ),
)
def test_v1_claim_with_malformed_required_evidence_marks_source_stale(
    changes: dict[str, object], category_changes: dict[str, object]
) -> None:
    source, _ = source_with_responses(
        [{"success": True, "cursor": None, "data": [market(**changes)]}],
        categories={"btc-year-end": category(**category_changes)},
    )

    assert asyncio.run(source.list_open_markets()) == ()
    assert source.snapshot()["rest"] == "stale"
    assert source.snapshot()["rest_reason"] == "rest_stale"


def test_list_open_markets_rejects_repeated_cursor_without_returning_partial_markets() -> None:
    source, requests = source_with_responses(
        [
            {"success": True, "cursor": "repeat", "data": [market(id=123)]},
            {"success": True, "cursor": "repeat", "data": [market(id=124)]},
        ]
    )

    assert asyncio.run(source.list_open_markets()) == ()
    assert len([request for request in requests if "/v1/markets?" in request.full_url]) == 2
    assert source.snapshot()["rest"] == "stale"
    assert source.snapshot()["rest_reason"] == "rest_stale"


@pytest.mark.parametrize("cursor", ("", 0, False, [], {}))
def test_list_open_markets_rejects_invalid_cursor(cursor: object) -> None:
    source, _ = source_with_responses(
        [{"success": True, "cursor": cursor, "data": [market(id=123)]}]
    )

    assert asyncio.run(source.list_open_markets()) == ()
    assert source.snapshot()["rest"] == "stale"
    assert source.snapshot()["rest_reason"] == "rest_stale"


def test_complete_yes_book_derives_no_asks_at_the_market_tick() -> None:
    source, _ = source_with_responses(
        [
            {"success": True, "data": market()},
            {
                "success": True,
                "data": {
                    "marketId": 123,
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


def test_older_rest_book_timestamp_is_dropped_and_marks_source_stale() -> None:
    source, _ = source_with_responses(
        [
            {"success": True, "data": market()},
            {
                "success": True,
                "data": {
                    "marketId": 123,
                    "updateTimestampMs": 1788048001000,
                    "asks": [["0.51", "3"]],
                    "bids": [["0.50", "2"]],
                },
            },
            {
                "success": True,
                "data": {
                    "marketId": 123,
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


def test_repeated_rest_snapshot_timestamp_refreshes_the_confirmed_book() -> None:
    received = [datetime(2026, 8, 2, tzinfo=UTC)]
    snapshot = {
        "marketId": 123,
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
    setup_reasons: list[object] = []
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

    def connect(*args: object, **kwargs: object) -> FakeWebSocket:
        del args, kwargs
        setup_reasons.append(source.snapshot()["ws_reason"])
        return next(connections)

    source, _ = source_with_responses(
        [{"success": True, "data": market()}],
        connector=connect,
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
    assert setup_reasons == ["ws_connecting", "ws_reconnecting"]
    assert [{
        "venue": "predict.fun",
        "wallet": "0xcE23…f435",
        "rest": "ready",
        "ws": "stale",
        "ws_generation": 1,
    }.items() <= snapshot.items() for snapshot in reconnect_snapshots] == [True]
    assert source.snapshot()["ws"] == "ready"
    assert source.snapshot()["ws_generation"] == 1


def test_websocket_drops_duplicate_and_rollback_versions() -> None:
    source, _ = source_with_responses(
        [{"success": True, "data": market()}],
        connector=lambda *args, **kwargs: FakeWebSocket(
            [
                book_message(2, 1788048000000),
                book_message(2, 1788048001000),
                book_message(1, 1788048002000),
                book_message(3, 1788048003000),
            ]
        ),
    )

    async def collect_two() -> tuple[object, object]:
        stream = source.stream_books(("123",))
        try:
            return await anext(stream), await anext(stream)
        finally:
            await stream.aclose()

    first, second = asyncio.run(collect_two())

    assert first.source_timestamp == datetime.fromtimestamp(1788048000, UTC)
    assert second.source_timestamp == datetime.fromtimestamp(1788048003, UTC)
    assert source.snapshot()["ws"] == "ready"


def test_websocket_duplicate_and_rollback_keep_accepted_book_healthy() -> None:
    class BlockingWebSocket(FakeWebSocket):
        async def recv(self) -> str:
            if self.messages:
                return await super().recv()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    source, _ = source_with_responses(
        [{"success": True, "data": market()}],
        connector=lambda *args, **kwargs: BlockingWebSocket(
            [
                book_message(2, 1788048000000),
                book_message(2, 1788048001000),
                book_message(1, 1788048002000),
            ]
        ),
    )

    async def consume_duplicates() -> None:
        stream = source.stream_books(("123",))
        pending = None
        try:
            await anext(stream)
            pending = asyncio.create_task(anext(stream))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert source.snapshot()["ws"] == "ready"
        finally:
            if pending is not None:
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
            await stream.aclose()

    asyncio.run(consume_duplicates())


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
