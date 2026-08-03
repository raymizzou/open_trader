from __future__ import annotations

import json
from types import SimpleNamespace
from urllib.error import URLError

from predict_sdk import ChainId

from open_trader.polymarket_trading import PredictConfig, TradingConfig
from open_trader.predict_trading import PredictTradingClient


DEPOSIT = "0xcE23B341C888A88C4C44D8B5Aa6D04A8615Ff435"


class FakeResponse:
    def __init__(self, payload: object, status: int = 200) -> None:
        self.payload = payload
        self.status = status

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None


class FakeBuilder:
    def sign_predict_account_message(self, message: str) -> str:
        assert message == "dynamic-message-sentinel"
        return "signature-sentinel"

    def get_market_order_amounts(self, input, book) -> SimpleNamespace:
        assert input.quantity_wei == 10**18
        return SimpleNamespace(maker_amount=1000000, taker_amount=10**18, price_per_share=1000000)

    def build_order(self, strategy: str, input) -> dict[str, object]:
        assert strategy == "MARKET"
        return {"order": "sentinel"}

    def build_typed_data(self, order, **kwargs: object) -> dict[str, object]:
        return {"typed": "sentinel"}

    def sign_typed_data_order(self, typed) -> dict[str, object]:
        return {"signed": "order-sentinel"}

    def balance_of(self, asset: str) -> str:
        assert asset == "USDT"
        return "5000000"

    def allowance(self, **kwargs: object) -> str:
        return "1000000"


def make_client(urlopen_fn):
    builder_calls: list[tuple[object, str, object]] = []

    def builder_factory(chain_id: object, private_key: str, options: object) -> FakeBuilder:
        builder_calls.append((chain_id, private_key, options))
        return FakeBuilder()

    client = PredictTradingClient.from_keychain(
        TradingConfig("0x1111111111111111111111111111111111111111", "0x2222222222222222222222222222222222222222", PredictConfig(DEPOSIT)),
        sdk_builder=builder_factory,
        load_private_key=lambda: "private-sentinel",
        load_api_key=lambda: "api-key-sentinel",
        urlopen_fn=urlopen_fn,
    )
    return client, builder_calls


def response_for(request):
    if request.full_url.endswith("/v1/auth/message"):
        return FakeResponse({"message": "dynamic-message-sentinel"})
    if request.full_url.endswith("/v1/auth"):
        return FakeResponse({"token": "jwt-sentinel"})
    if request.full_url.endswith("/v1/markets/896"):
        return FakeResponse({"data": {"feeRateBps": "200", "isNegRisk": False, "isYieldBearing": False}})
    if request.full_url.endswith("/v1/markets/896/orderbook"):
        return FakeResponse({"data": {"marketId": 896, "updateTimestampMs": 1, "asks": [["0.51", "3"]], "bids": [["0.50", "2"]]}})
    return FakeResponse({"id": "order-id", "hash": "order-hash"}, status=201)


def test_auth_uses_dynamic_message_and_redacted_headers() -> None:
    requests = []

    def urlopen_fn(request, **kwargs):
        requests.append(request)
        return response_for(request)

    client, builder_calls = make_client(urlopen_fn)
    assert client.quote_market_buy("896", "yes-token", 10**18).minimum_redeemable_units == 10**18
    assert builder_calls[0][0] is ChainId.BNB_MAINNET
    assert builder_calls[0][1] == "private-sentinel"
    assert builder_calls[0][2].predict_account == DEPOSIT
    auth = json.loads(requests[1].data)
    assert auth == {"signer": DEPOSIT, "signature": "signature-sentinel", "message": "dynamic-message-sentinel"}
    assert requests[0].headers["X-api-key"] == "api-key-sentinel"
    assert requests[2].headers["Authorization"] == "Bearer jwt-sentinel"
    assert requests[1].headers["User-agent"] == "open-trader/0.1"
    assert "private-sentinel" not in repr(client)
    assert "api-key-sentinel" not in repr(client)
    assert "jwt-sentinel" not in repr(client)


def test_preflight_signs_market_fok_without_order_request() -> None:
    requests = []

    def urlopen_fn(request, **kwargs):
        requests.append(request)
        return response_for(request)

    client, _ = make_client(urlopen_fn)
    result = client.no_submit_buy_preflight("896", "yes-token", 10**18, fee_rate_bps=200)
    assert result.accepted is True
    assert result.status == "preflight"
    assert not any("/v1/orders" in request.full_url for request in requests)


def test_submit_posts_once_and_transport_error_is_ambiguous() -> None:
    requests = []

    def urlopen_fn(request, **kwargs):
        requests.append(request)
        return response_for(request)

    client, _ = make_client(urlopen_fn)
    result = client.submit_buy_once("896", "yes-token", 10**18, fee_rate_bps=200)
    assert (result.accepted, result.status, result.order_id) == (True, "accepted", "order-hash")
    assert sum(request.full_url.endswith("/v1/orders") for request in requests) == 1

    failure_client, _ = make_client(lambda *args, **kwargs: (_ for _ in ()).throw(URLError("offline")))
    assert failure_client.submit_buy_once("896", "yes-token", 10**18, fee_rate_bps=200).error_code == "ambiguous"


def test_reconcile_requires_order_match_activity_and_position() -> None:
    def urlopen_fn(request, **kwargs):
        if request.full_url.endswith("/v1/auth/message"):
            return FakeResponse({"message": "dynamic-message-sentinel"})
        if request.full_url.endswith("/v1/auth"):
            return FakeResponse({"token": "jwt-sentinel"})
        if request.full_url.endswith("/v1/orders/order-hash"):
            return FakeResponse({"data": {"hash": "order-hash", "marketId": "896", "tokenId": "yes-token"}})
        if request.full_url.endswith("/v1/orders/matches"):
            return FakeResponse({"data": {"matches": [{"orderHash": "order-hash", "transactionHash": "tx"}]}})
        if request.full_url.endswith("/v1/account/activity"):
            return FakeResponse({"data": {"activities": [{"orderHash": "order-hash"}]}})
        if request.full_url.endswith("/v1/positions?marketId=896"):
            return FakeResponse({"data": {"positions": [{"tokenId": "yes-token", "amount": "1"}]}})
        raise AssertionError(request.full_url)

    client, _ = make_client(urlopen_fn)
    result = client.reconcile_buy("896", "yes-token", "order-hash")
    assert result == {"verified": True, "conclusively_absent": False, "status": "verified"}
