from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest
from eth_account import Account
from predict_sdk import ChainId

from open_trader.polymarket_trading import PredictConfig, TradingConfig
from open_trader.predict_trading import PredictTradingClient


DEPOSIT = "0xcE23B341C888A88C4C44D8B5Aa6D04A8615Ff435"
PRIVATE_KEY = "0x" + "11" * 32
PRIVY_SIGNER = Account.from_key(PRIVATE_KEY).address
USDT = "0x55d398326f99059fF775485246999027B3197955"
CTF_EXCHANGE = "0x8BC070BEdAB741406F4B1Eb65A72bee27894B689"


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


class RawResponse(FakeResponse):
    def read(self) -> bytes:
        return b"not-json"


class NumericLookingReceiptStatus:
    def __int__(self) -> int:
        return 1


class FakeBuilder:
    last_order_input = None

    def __init__(self) -> None:
        self.price_per_share = 1_000_000_000_000_000_000
        self.max_collateral_debit = 1_000_000_000_000_000_000
        self.quote_calls = 0
        self.balance_of_calls: list[tuple[str, str | None]] = []
        self.allowance_value: object = 1_000_000_000_000_000_000
        self.usdt_balance: object = 5_000_000_000_000_000_000
        self.gas_estimate = 1200000
        self.gas_price_wei = 1000000000
        self.bnb_balance_wei: object = 4000000000000000
        self.approval_steps = [
            SimpleNamespace(
                id="ERC20_ALLOWANCE:CTF_EXCHANGE",
                type="ERC20_ALLOWANCE",
                spender=CTF_EXCHANGE,
                token=USDT,
                label="Approve Exchange",
                description="Allows you to interact with the exchange.",
            )
        ]
        self.set_approval_calls: list[tuple[object, bool, int | None]] = []
        self.next_set_approval_result = SimpleNamespace(
            success=True,
            receipt={"status": 1, "transactionHash": bytes.fromhex("12" * 32)},
            cause=None,
        )
        self.set_approval_error: Exception | None = None
        self.allowance_after_approve = None
        self.allowance_after_clear = None
        self.approval_gas_calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
        self.kernel_contract_addresses: list[str] = []
        self.transfer_calls = 0
        self.order_submit_calls = 0
        self._execution_mode = 0
        self._predict_account = DEPOSIT
        self._addresses = SimpleNamespace(
            USDT=USDT,
            CTF_EXCHANGE=CTF_EXCHANGE,
            NEG_RISK_CTF_EXCHANGE="0x365fb81bd4A24D6303cd2F19c349dE6894D8d58A",
            YIELD_BEARING_CTF_EXCHANGE="0x6bEb5a40C032AFc305961162d8204CDA16DECFa5",
            YIELD_BEARING_NEG_RISK_CTF_EXCHANGE="0x8A289d458f5a134bA40015085A8F50Ffb681B41d",
            NEG_RISK_ADAPTER="0xc3Cf7c252f65E0d8D88537dF96569AE94a7F1A6E",
            YIELD_BEARING_NEG_RISK_ADAPTER="0x41dCe1A4B8FB5e6327701750aF6231B7CD0B2A40",
        )
        self.contracts = SimpleNamespace(
            usdt=SimpleNamespace(
                address=USDT,
                functions=SimpleNamespace(
                    allowance=self._allowance_method,
                    approve=self._approve_method,
                ),
                encode_abi=self._encode_usdt_abi,
            ),
            kernel=SimpleNamespace(abi=[]),
        )
        self._web3 = SimpleNamespace(eth=FakeEth(self))

    def sign_predict_account_message(self, message: str) -> str:
        assert message == "dynamic-message-sentinel"
        return "signature-sentinel"

    def get_market_order_amounts(self, input, book) -> SimpleNamespace:
        assert input.quantity_wei == 10**18
        self.quote_calls += 1
        return SimpleNamespace(
            maker_amount=self.max_collateral_debit,
            taker_amount=10**18,
            price_per_share=self.price_per_share,
        )

    def build_order(self, strategy: str, input) -> dict[str, object]:
        assert strategy == "MARKET"
        self.last_order_input = input
        return {"order": "sentinel"}

    def build_typed_data(self, order, **kwargs: object) -> dict[str, object]:
        return {"typed": "sentinel"}

    def sign_typed_data_order(self, typed) -> dict[str, object]:
        return {"signed": "order-sentinel"}

    def balance_of(self, asset: str, address: str | None = None) -> str:
        assert asset == "USDT"
        self.balance_of_calls.append((asset, address))
        return self.usdt_balance

    def allowance(self, **kwargs: object) -> str:
        return "1000000000000000000"

    def get_approval_steps(self, scope) -> list[object]:
        self.last_scope = scope
        return list(self.approval_steps)

    def set_approval(self, step: object, *, approved: bool = True, amount: int = 0) -> object:
        self.set_approval_calls.append((step, approved, amount if approved else None))
        if self.set_approval_error is not None:
            raise self.set_approval_error
        if approved and self.allowance_after_approve is not None:
            self.allowance_value = self.allowance_after_approve
        if not approved and self.allowance_after_clear is not None:
            self.allowance_value = self.allowance_after_clear
        return self.next_set_approval_result

    def _exchange_key(self, is_neg_risk: bool, is_yield_bearing: bool) -> str:
        if is_neg_risk:
            return "YIELD_BEARING_NEG_RISK_CTF_EXCHANGE" if is_yield_bearing else "NEG_RISK_CTF_EXCHANGE"
        return "YIELD_BEARING_CTF_EXCHANGE" if is_yield_bearing else "CTF_EXCHANGE"

    def _encode_execution_calldata(self, to: str, calldata: str, value: int = 0) -> bytes:
        return f"{to}:{calldata}:{value}".encode()

    def _allowance_method(self, owner: str, spender: str) -> object:
        builder = self
        return SimpleNamespace(call=lambda: _value_or_raise(builder.allowance_value))

    def _approve_method(self, spender: str, amount: int) -> object:
        return FakeGasMethod(self, "approve", (spender, amount))

    def _encode_usdt_abi(self, abi_element_identifier: str, args: list[object]) -> str:
        assert abi_element_identifier == "approve"
        return f"0xapprove-{args[0]}-{args[1]}"


class FakeGasMethod:
    def __init__(self, builder: FakeBuilder, name: str, args: tuple[object, ...]) -> None:
        self._builder = builder
        self._name = name
        self._args = args

    def estimate_gas(self, tx: dict[str, object]) -> int:
        self._builder.approval_gas_calls.append((self._name, self._args, tx))
        value = self._builder.gas_estimate
        return _value_or_raise(value)


class FakeKernelContract:
    def __init__(self, builder: FakeBuilder, address: str) -> None:
        self.functions = SimpleNamespace(
            execute=lambda mode, calldata: FakeGasMethod(builder, "execute", (mode, calldata))
        )
        builder.kernel_contract_addresses.append(address)


class FakeEth:
    def __init__(self, builder: FakeBuilder) -> None:
        self._builder = builder
        self.gas_price = builder.gas_price_wei

    def get_balance(self, address: str) -> int:
        assert address == PRIVY_SIGNER
        return _value_or_raise(self._builder.bnb_balance_wei)

    def contract(self, address: str, abi: object) -> FakeKernelContract:
        return FakeKernelContract(self._builder, address)


def _value_or_raise(value: object) -> object:
    if isinstance(value, BaseException):
        raise value
    return value


def make_client(urlopen_fn):
    builder_calls: list[tuple[object, str, object]] = []

    def builder_factory(chain_id: object, private_key: str, options: object) -> FakeBuilder:
        builder_calls.append((chain_id, private_key, options))
        return FakeBuilder()

    client = PredictTradingClient.from_keychain(
        TradingConfig("0x1111111111111111111111111111111111111111", "0x2222222222222222222222222222222222222222", PredictConfig(DEPOSIT)),
        sdk_builder=builder_factory,
        load_private_key=lambda: PRIVATE_KEY,
        load_api_key=lambda: "api-key-sentinel",
        urlopen_fn=urlopen_fn,
    )
    return client, builder_calls


def response_for(request, **kwargs):
    if request.full_url.endswith("/v1/auth/message"):
        return FakeResponse({"message": "dynamic-message-sentinel"})
    if request.full_url.endswith("/v1/auth"):
        return FakeResponse({"token": "jwt-sentinel"})
    if request.full_url.endswith("/v1/markets/896"):
        return FakeResponse({"data": {"feeRateBps": "200", "isNegRisk": False, "isYieldBearing": False}})
    if request.full_url.endswith("/v1/markets/896/orderbook"):
        return FakeResponse({"data": {"marketId": 896, "updateTimestampMs": 1, "asks": [["0.51", "3"]], "bids": [["0.50", "2"]]}})
    return FakeResponse(
        {
            "success": True,
            "data": {
                "code": "MATCHED",
                "orderId": "order-id",
                "orderHash": "order-hash",
            },
        },
        status=201,
    )


def test_auth_uses_dynamic_message_and_redacted_headers() -> None:
    requests = []

    def urlopen_fn(request, **kwargs):
        requests.append(request)
        return response_for(request)

    client, builder_calls = make_client(urlopen_fn)
    assert client.quote_market_buy("896", "yes-token", 10**18).minimum_redeemable_units == 10**18
    assert builder_calls[0][0] is ChainId.BNB_MAINNET
    assert builder_calls[0][1] == PRIVATE_KEY
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
    result = client.no_submit_buy_preflight("896", "yes-token", 10**18)
    assert result.accepted is True
    assert result.status == "preflight"
    assert not any("/v1/orders" in request.full_url for request in requests)


def test_cross_entry_rejects_a_moved_quote_above_approved_ceiling_without_post() -> None:
    requests = []
    now_ms = int(datetime.now(UTC).timestamp() * 1000)

    def urlopen_fn(request, **kwargs):
        requests.append(request)
        if request.full_url.endswith("/v1/markets/896/orderbook"):
            return FakeResponse(
                {"data": {"marketId": 896, "updateTimestampMs": now_ms, "asks": [["0.51", "3"]], "bids": [["0.50", "2"]]}}
            )
        return response_for(request)

    client, _ = make_client(urlopen_fn)
    client._builder.price_per_share = 510_000_000_000_000_000  # type: ignore[attr-defined]
    client._builder.max_collateral_debit = 510_000_000_000_000_000  # type: ignore[attr-defined]
    order = {
        "execution_id": "cross-entry-1",
        "idempotency_key": "cross-entry-1",
        "venue": "predict.fun",
        "market_id": "896",
        "condition_id": "predict-condition",
        "token_id": "yes-token",
        "outcome": "YES",
        "requested_quantity": Decimal("1"),
        "net_quantity": Decimal("1"),
        "max_price": Decimal("0.50"),
        "max_cost": Decimal("0.50"),
        "maximum_fee": Decimal("0.01"),
        "calculable_gas": Decimal("0.10"),
    }

    result = client.no_submit_cross_buy_preflight(order)

    assert result.accepted is False
    assert result.status == "rejected"
    assert not any(
        request.full_url.endswith("/v1/orders") and request.get_method() == "POST"
        for request in requests
    )


def test_cross_entry_posts_only_the_preflight_bound_order() -> None:
    requests = []
    now_ms = int(datetime.now(UTC).timestamp() * 1000)

    def urlopen_fn(request, **kwargs):
        requests.append(request)
        if request.full_url.endswith("/v1/markets/896/orderbook"):
            return FakeResponse(
                {"data": {"marketId": 896, "updateTimestampMs": now_ms, "asks": [["1.00", "3"]], "bids": [["0.99", "2"]]}}
            )
        return response_for(request)

    client, _ = make_client(urlopen_fn)
    order = {
        "execution_id": "cross-entry-2",
        "idempotency_key": "cross-entry-2",
        "venue": "predict.fun",
        "market_id": "896",
        "condition_id": "predict-condition",
        "token_id": "yes-token",
        "outcome": "YES",
        "requested_quantity": Decimal("1"),
        "net_quantity": Decimal("1"),
        "max_price": Decimal("1"),
        "max_cost": Decimal("1"),
        "maximum_fee": Decimal("0"),
        "calculable_gas": Decimal("0.10"),
    }

    assert client.no_submit_cross_buy_preflight(order).accepted is True
    result = client.submit_cross_buy_once(order)

    assert result.accepted is True
    assert client._builder.quote_calls == 1  # type: ignore[attr-defined]
    assert sum(
        request.full_url.endswith("/v1/orders") and request.get_method() == "POST"
        for request in requests
    ) == 1


def test_cross_entry_accepts_official_eighteen_decimal_sdk_quote() -> None:
    now_ms = int(datetime.now(UTC).timestamp() * 1000)

    def urlopen_fn(request, **kwargs):
        if request.full_url.endswith("/v1/markets/896/orderbook"):
            return FakeResponse(
                {"data": {"marketId": 896, "updateTimestampMs": now_ms, "asks": [["0.50", "3"]], "bids": [["0.49", "2"]]}}
            )
        return response_for(request)

    client, _ = make_client(urlopen_fn)
    client._builder.price_per_share = 500_000_000_000_000_000  # type: ignore[attr-defined]
    client._builder.max_collateral_debit = 500_000_000_000_000_000  # type: ignore[attr-defined]

    result = client.no_submit_cross_buy_preflight(
        {
            "execution_id": "official-eighteen-decimal",
            "idempotency_key": "official-eighteen-decimal",
            "venue": "predict.fun",
            "market_id": "896",
            "condition_id": "predict-condition",
            "token_id": "yes-token",
            "outcome": "YES",
            "requested_quantity": Decimal("1"),
            "net_quantity": Decimal("1"),
            "max_price": Decimal("0.5"),
            "max_cost": Decimal("0.5"),
            "maximum_fee": Decimal("0"),
            "calculable_gas": Decimal("0.10"),
        }
    )

    assert result.accepted is True


def test_cross_entry_rejects_unknown_zero_gas_without_post() -> None:
    requests = []
    now_ms = int(datetime.now(UTC).timestamp() * 1000)

    def urlopen_fn(request, **kwargs):
        requests.append(request)
        if request.full_url.endswith("/v1/markets/896/orderbook"):
            return FakeResponse(
                {"data": {"marketId": 896, "updateTimestampMs": now_ms, "asks": [["1.00", "3"]], "bids": [["0.99", "2"]]}}
            )
        return response_for(request)

    client, _ = make_client(urlopen_fn)
    order = {
        "execution_id": "cross-entry-3",
        "idempotency_key": "cross-entry-3",
        "venue": "predict.fun",
        "market_id": "896",
        "condition_id": "predict-condition",
        "token_id": "yes-token",
        "outcome": "YES",
        "requested_quantity": Decimal("1"),
        "net_quantity": Decimal("1"),
        "max_price": Decimal("1"),
        "max_cost": Decimal("1"),
        "maximum_fee": Decimal("0"),
        "calculable_gas": Decimal("0"),
    }

    assert client.no_submit_cross_buy_preflight(order).accepted is False
    assert not any(
        request.full_url.endswith("/v1/orders") and request.get_method() == "POST"
        for request in requests
    )


def test_submit_posts_once_and_transport_error_is_ambiguous() -> None:
    requests = []

    def urlopen_fn(request, **kwargs):
        requests.append(request)
        return response_for(request)

    client, _ = make_client(urlopen_fn)
    result = client.submit_buy_once("896", "yes-token", 10**18)
    assert (result.accepted, result.status, result.order_id) == (True, "accepted", "order-hash")
    assert sum(request.full_url.endswith("/v1/orders") for request in requests) == 1
    assert requests[-1].headers["Content-type"] == "application/json"

    def fail_order_only(request, **kwargs):
        if request.full_url.endswith("/v1/orders"):
            raise URLError("offline")
        return response_for(request)

    failure_client, _ = make_client(fail_order_only)
    assert failure_client.submit_buy_once("896", "yes-token", 10**18).error_code == "ambiguous"


@pytest.mark.parametrize(
    "order_response",
    (
        RawResponse(None),
        FakeResponse([]),
        FakeResponse({"data": {}}),
        FakeResponse({"success": True, "data": {"orderId": "order-id"}}),
        FakeResponse({"success": True, "data": {"orderHash": "order-hash"}}),
    ),
)
def test_predict_post_response_failures_are_ambiguous_after_single_attempt(
    order_response: FakeResponse,
) -> None:
    requests = []

    def urlopen_fn(request, **kwargs):
        requests.append(request)
        if request.full_url.endswith("/v1/orders"):
            return order_response
        return response_for(request)

    client, _ = make_client(urlopen_fn)

    result = client.submit_buy_once("896", "yes-token", 10**18)

    assert (result.accepted, result.status, result.order_id, result.error_code) == (
        False,
        "ambiguous",
        "",
        "ambiguous",
    )
    assert sum(
        request.full_url.endswith("/v1/orders") and request.get_method() == "POST"
        for request in requests
    ) == 1


def test_reconcile_returns_unknown_for_pending_order_with_preexisting_position() -> None:
    def urlopen_fn(request, **kwargs):
        if request.full_url.endswith("/v1/auth/message"):
            return FakeResponse({"message": "dynamic-message-sentinel"})
        if request.full_url.endswith("/v1/auth"):
            return FakeResponse({"token": "jwt-sentinel"})
        if request.full_url.endswith("/v1/markets/896"):
            return FakeResponse({"data": {"minimumOrderSize": "1"}})
        if request.full_url.endswith("/v1/orders/order-hash"):
            return FakeResponse({"data": {"hash": "order-hash", "marketId": "896", "tokenId": "yes-token", "signer": DEPOSIT, "status": "PENDING", "amountFilled": "1"}})
        if request.full_url.endswith("/v1/orders/matches?orderHashes=order-hash"):
            return FakeResponse({"data": {"matches": [{"orderHash": "order-hash", "transactionHash": "tx", "executedAmount": "1", "fee": "2"}]}})
        if request.full_url.endswith("/v1/account/activity"):
            return FakeResponse({"data": {"activities": [{"orderHash": "order-hash", "transactionHash": "tx", "executedAmount": "1", "fee": "2"}]}})
        if request.full_url.endswith("/v1/positions?marketId=896"):
            return FakeResponse({"data": {"positions": [{"tokenId": "yes-token", "amount": "9", "amountDelta": "0"}]}})
        raise AssertionError(request.full_url)

    client, _ = make_client(urlopen_fn)
    result = client.reconcile_buy("896", "yes-token", "order-hash")
    assert result == {"verified": False, "conclusively_absent": False, "status": "unknown"}


def test_reconcile_verifies_only_full_order_match_activity_and_position_agreement() -> None:
    def urlopen_fn(request, **kwargs):
        if request.full_url.endswith("/v1/auth/message"):
            return FakeResponse({"message": "dynamic-message-sentinel"})
        if request.full_url.endswith("/v1/auth"):
            return FakeResponse({"token": "jwt-sentinel"})
        if request.full_url.endswith("/v1/markets/896"):
            return FakeResponse({"data": {"minimumOrderSize": "1"}})
        if request.full_url.endswith("/v1/orders/order-hash"):
            return FakeResponse(
                {
                    "success": True,
                    "data": {
                        "order": {
                            "hash": "order-hash",
                            "tokenId": "yes-token",
                            "signer": DEPOSIT,
                        },
                        "id": "order-id",
                        "marketId": 896,
                        "status": "FILLED",
                        "amountFilled": "1",
                    },
                }
            )
        if request.full_url.endswith("/v1/orders/matches?orderHashes=order-hash"):
            return FakeResponse(
                {
                    "success": True,
                    "data": [
                        {
                            "market": {"id": 896},
                            "taker": {
                                "hash": "order-hash",
                                "signer": DEPOSIT,
                                "outcome": {"onChainId": "yes-token"},
                                "fee": {"amount": "20000000000000000", "type": "COLLATERAL"},
                            },
                            "makers": [],
                            "amountFilled": "1",
                            "transactionHash": "tx",
                        }
                    ],
                }
            )
        if request.full_url.endswith("/v1/account/activity"):
            return FakeResponse(
                {
                    "success": True,
                    "data": [
                        {
                            "name": "MATCH",
                            "transactionHash": "tx",
                            "amountFilled": "1",
                            "order": {
                                "hash": "order-hash",
                                "fee": {"amount": "20000000000000000", "type": "COLLATERAL"}
                            },
                            "market": {"id": 896},
                            "outcome": {"onChainId": "yes-token"},
                        }
                    ],
                }
            )
        if request.full_url.endswith("/v1/positions?marketId=896"):
            return FakeResponse(
                {
                    "success": True,
                    "data": [
                        {
                            "id": "position-id",
                            "market": {"id": 896},
                            "outcome": {"onChainId": "yes-token"},
                            "amount": "1000000000000000000",
                        }
                    ],
                }
            )
        raise AssertionError(request.full_url)

    client, _ = make_client(urlopen_fn)
    assert client.reconcile_buy("896", "yes-token", "order-hash") == {
        "verified": True,
        "conclusively_absent": False,
        "status": "verified",
        "filled_quantity": Decimal("1"),
        "position_quantity": Decimal("1"),
        "actual_fee": Decimal("0.02"),
        "execution_proof": {
            "verified": True,
            "venue": "predict.fun",
            "order_ids": ["order-hash"],
            "trade_ids": ["tx"],
            "fee": Decimal("0.02"),
        },
        "minimum_order_size": Decimal("0.01"),
    }


def test_reconcile_rejects_match_for_a_different_owned_order_hash() -> None:
    calls: list[str] = []

    def urlopen_fn(request, **kwargs):
        calls.append(request.full_url)
        if request.full_url.endswith("/v1/auth/message"):
            return FakeResponse({"message": "dynamic-message-sentinel"})
        if request.full_url.endswith("/v1/auth"):
            return FakeResponse({"token": "jwt-sentinel"})
        if request.full_url.endswith("/v1/orders/order-hash"):
            return FakeResponse({"data": {"order": {"hash": "order-hash", "tokenId": "yes-token", "signer": DEPOSIT}, "marketId": 896, "status": "FILLED", "amountFilled": "1"}})
        if "/v1/orders/matches" in request.full_url:
            return FakeResponse({"data": [{"market": {"id": 896}, "taker": {"hash": "different-order", "signer": DEPOSIT, "outcome": {"onChainId": "yes-token"}, "fee": {"amount": "0", "type": "COLLATERAL"}}, "makers": [], "amountFilled": "1", "transactionHash": "tx"}]})
        if request.full_url.endswith("/v1/account/activity"):
            return FakeResponse({"data": [{"transactionHash": "tx", "amountFilled": "1", "order": {"hash": "order-hash", "fee": {"amount": "0", "type": "COLLATERAL"}}, "market": {"id": 896}, "outcome": {"onChainId": "yes-token"}}]})
        if request.full_url.endswith("/v1/positions?marketId=896"):
            return FakeResponse({"data": [{"market": {"id": 896}, "outcome": {"onChainId": "yes-token"}, "amount": "1000000000000000000"}]})
        raise AssertionError(request.full_url)

    result = make_client(urlopen_fn)[0].reconcile_buy("896", "yes-token", "order-hash")
    assert result["verified"] is False
    assert any(url.endswith("/v1/orders/matches?orderHashes=order-hash") for url in calls)


def test_reconcile_404_requires_all_independent_reads_before_proving_absence() -> None:
    calls: list[str] = []

    def urlopen_fn(request, **kwargs):
        calls.append(request.full_url)
        if request.full_url.endswith("/v1/auth/message"):
            return FakeResponse({"message": "dynamic-message-sentinel"})
        if request.full_url.endswith("/v1/auth"):
            return FakeResponse({"token": "jwt-sentinel"})
        if request.full_url.endswith("/v1/orders/order-hash"):
            raise HTTPError(request.full_url, 404, "not found", {}, None)
        if request.full_url.endswith("/v1/orders") or request.full_url.endswith("/v1/orders/matches?orderHashes=order-hash") or request.full_url.endswith("/v1/account/activity") or request.full_url.endswith("/v1/positions?marketId=896"):
            return FakeResponse({"data": []})
        raise AssertionError(request.full_url)

    result = make_client(urlopen_fn)[0].reconcile_buy("896", "yes-token", "order-hash")
    assert result == {"verified": False, "conclusively_absent": True, "status": "absent"}
    assert any(url.endswith("/v1/orders") for url in calls)


@pytest.mark.parametrize(
    "malformed_path",
    (
        "/v1/orders",
        "/v1/orders/matches?orderHashes=order-hash",
        "/v1/account/activity",
        "/v1/positions?marketId=896",
    ),
)
def test_reconcile_404_keeps_unknown_when_an_independent_page_is_malformed(
    malformed_path: str,
) -> None:
    def urlopen_fn(request, **kwargs):
        if request.full_url.endswith("/v1/auth/message"):
            return FakeResponse({"message": "dynamic-message-sentinel"})
        if request.full_url.endswith("/v1/auth"):
            return FakeResponse({"token": "jwt-sentinel"})
        if request.full_url.endswith("/v1/orders/order-hash"):
            raise HTTPError(request.full_url, 404, "not found", {}, None)
        if request.full_url.endswith(malformed_path):
            return FakeResponse({"data": {}})
        if request.full_url.endswith("/v1/orders") or request.full_url.endswith("/v1/orders/matches?orderHashes=order-hash") or request.full_url.endswith("/v1/account/activity") or request.full_url.endswith("/v1/positions?marketId=896"):
            return FakeResponse({"data": []})
        raise AssertionError(request.full_url)

    assert make_client(urlopen_fn)[0].reconcile_buy("896", "yes-token", "order-hash") == {
        "verified": False,
        "conclusively_absent": False,
        "status": "unknown",
    }


def test_account_snapshot_reads_documented_direct_array_shapes() -> None:
    open_order = {"id": "order-id", "order": {"hash": "order-hash"}, "status": "OPEN"}
    position = {
        "id": "position-id",
        "market": {"id": 896},
        "outcome": {"onChainId": "yes-token"},
        "amount": "1",
    }

    def urlopen_fn(request, **kwargs):
        if request.full_url.endswith("/v1/auth/message"):
            return FakeResponse({"message": "dynamic-message-sentinel"})
        if request.full_url.endswith("/v1/auth"):
            return FakeResponse({"token": "jwt-sentinel"})
        if request.full_url.endswith("/v1/orders"):
            return FakeResponse({"success": True, "cursor": None, "data": [open_order]})
        if request.full_url.endswith("/v1/positions"):
            return FakeResponse({"success": True, "cursor": None, "data": [position]})
        raise AssertionError(request.full_url)

    client, _ = make_client(urlopen_fn)
    client._builder.allowance_value = 0  # type: ignore[attr-defined]

    snapshot = client.account_snapshot()

    assert snapshot["open_orders"] == (open_order,)
    assert snapshot["positions"] == (position,)


def test_book_parses_decimal_price_to_exact_wei() -> None:
    from open_trader.predict_trading import _book

    book = _book({"marketId": 896, "updateTimestampMs": 1, "asks": [["0.57", "1"]], "bids": []})
    assert book.asks[0][0] == 570000000000000000


@pytest.mark.parametrize("value", ("1.5", "-1", " 1", "١"))
def test_predict_raw_units_rejects_non_ascii_non_integer_strings(value: str) -> None:
    from open_trader.predict_trading import _raw_units

    assert _raw_units(value) is None


def test_submit_uses_fresh_server_fee_rate() -> None:
    def urlopen_fn(request, **kwargs):
        if request.full_url.endswith("/v1/markets/896"):
            return FakeResponse({"data": {"feeRateBps": "201", "isNegRisk": False, "isYieldBearing": False}})
        return response_for(request)

    client, _ = make_client(urlopen_fn)
    assert client.submit_buy_once("896", "yes-token", 10**18).accepted is True
    assert client._builder.last_order_input.fee_rate_bps == "201"


def test_cross_remediation_option_and_submit_bind_a_fresh_predict_buy_quote() -> None:
    requests = []

    def urlopen_fn(request, **kwargs):
        requests.append(request)
        if request.full_url.endswith("/v1/markets/896/orderbook"):
            return FakeResponse(
                {
                    "data": {
                        "marketId": 896,
                        "updateTimestampMs": int(datetime.now(UTC).timestamp() * 1000),
                        "asks": [["0.51", "3"]],
                        "bids": [["0.50", "2"]],
                    }
                }
            )
        return response_for(request)

    client, _ = make_client(urlopen_fn)
    quoted = client.cross_remediation_option(
        venue="predict.fun", market_id="896", condition_id="predict-condition",
        token_id="yes-token", outcome="YES", side="BUY", quantity=Decimal("1"),
        maximum_fee=Decimal("0.05"),
    )

    assert quoted["fresh"] is True
    option = quoted["option"]
    assert option["max_spend"] == Decimal("1")
    assert option["fee"] == Decimal("0")
    assert not any(request.full_url.endswith("/v1/orders") for request in requests)

    submitted = client.submit_cross_remediation_once(option)

    assert (submitted.accepted, submitted.status, submitted.order_id) == (
        True, "accepted", "order-hash",
    )
    assert sum(request.full_url.endswith("/v1/orders") for request in requests) == 1


def test_approval_facts_report_predict_account_owner_allowance_and_gas() -> None:
    client, _ = make_client(response_for)
    client._builder.allowance_value = 0  # type: ignore[attr-defined]

    facts = client.approval_facts("896", exact_debit_wei=2_400_000_000_000_000_000)

    assert facts["predict_account"] == DEPOSIT
    assert facts["gas_signer"] == PRIVY_SIGNER
    assert facts["allowance"] == "0"
    assert facts["allowance_breaker"] is False
    assert facts["approval_scope"] == {
        "operation": "TRADE",
        "side": "BUY",
        "is_neg_risk": False,
        "is_yield_bearing": False,
    }
    assert facts["scope_ready"] is True
    assert facts["bnb_balance"] == "0.004"
    assert facts["required_bnb"] == "0.003"
    assert facts["minimum_top_up_bnb"] == "0"
    assert "_approval_step" not in facts
    assert not any(str(key).startswith("_") for key in facts)
    assert client._builder.balance_of_calls == [("USDT", DEPOSIT)]  # type: ignore[attr-defined]
    assert client._builder.transfer_calls == 0  # type: ignore[attr-defined]
    assert client._builder.order_submit_calls == 0  # type: ignore[attr-defined]


def test_real_adapter_reports_human_usdt_and_raw_post_approval_units() -> None:
    client, _ = make_client(response_for)
    client._builder.allowance_value = 2_400_000_000_000_000_000  # type: ignore[attr-defined]

    facts = client.approval_facts("896", exact_debit_wei=2_400_000_000_000_000_000)

    assert facts["available_usdt"] == "5"
    assert facts["available_usdt_raw"] == "5000000000000000000"
    assert facts["allowance"] == "2.4"
    assert facts["allowance_raw"] == "2400000000000000000"
    assert facts["allowance_breaker"] is True
    assert facts["exact_debit_wei"] == 2_400_000_000_000_000_000

    client._builder.allowance_value = 0  # type: ignore[attr-defined]
    client._builder.allowance_after_approve = 2_400_000_000_000_000_000  # type: ignore[attr-defined]
    result = client.set_exact_buy_allowance("896", 2_400_000_000_000_000_000)

    assert result["allowance"] == "2.4"
    assert result["allowance_raw"] == "2400000000000000000"
    assert result["allowance_breaker"] is True
    assert result["exact_debit_wei"] == 2_400_000_000_000_000_000


def test_account_facts_preserve_integer_zeros_and_fractional_trailing_zeros() -> None:
    client, _ = make_client(response_for)
    client._builder.usdt_balance = 10_000_000_000_000_000_000  # type: ignore[attr-defined]
    client._builder.allowance_value = 20_000_000_000_000_000_000  # type: ignore[attr-defined]
    client._builder.bnb_balance_wei = 10_000_000_000_000_000_000  # type: ignore[attr-defined]

    facts = client.approval_facts(
        "896", exact_debit_wei=2_400_000_000_000_000_000
    )

    assert facts["available_usdt"] == "10"
    assert facts["allowance"] == "20"
    assert facts["bnb_balance"] == "10"


@pytest.mark.parametrize(
    ("steps", "market_payload", "allowance_value", "bnb_balance_wei", "error"),
    [
        ([], {"feeRateBps": "200", "isNegRisk": False, "isYieldBearing": False}, 0, 4_000_000_000_000_000, "approval"),
        ([SimpleNamespace(id="too-many-1", type="ERC20_ALLOWANCE", spender=CTF_EXCHANGE, token=USDT, label="", description=""), SimpleNamespace(id="too-many-2", type="ERC20_ALLOWANCE", spender=CTF_EXCHANGE, token=USDT, label="", description="")], {"feeRateBps": "200", "isNegRisk": False, "isYieldBearing": False}, 0, 4_000_000_000_000_000, "approval"),
        ([SimpleNamespace(id="erc1155", type="ERC1155_APPROVAL", spender=CTF_EXCHANGE, token=USDT, label="", description="")], {"feeRateBps": "200", "isNegRisk": False, "isYieldBearing": False}, 0, 4_000_000_000_000_000, "approval"),
        ([SimpleNamespace(id="wrong-token", type="ERC20_ALLOWANCE", spender="0x0000000000000000000000000000000000000001", token="0x0000000000000000000000000000000000000002", label="", description="")], {"feeRateBps": "200", "isNegRisk": False, "isYieldBearing": False}, 0, 4_000_000_000_000_000, "approval"),
        (None, {"feeRateBps": "200", "isNegRisk": "false", "isYieldBearing": False}, 0, 4_000_000_000_000_000, "market"),
        (None, {"feeRateBps": "200", "isNegRisk": False, "isYieldBearing": False}, -1, 4_000_000_000_000_000, "allowance"),
        (None, {"feeRateBps": "200", "isNegRisk": False, "isYieldBearing": False}, "not-a-number", 4_000_000_000_000_000, "allowance"),
        (None, {"feeRateBps": "200", "isNegRisk": False, "isYieldBearing": False}, 0, -1, "bnb"),
        (None, {"feeRateBps": "200", "isNegRisk": False, "isYieldBearing": False}, 0, "not-a-number", "bnb"),
    ],
)
def test_approval_facts_fail_closed_on_malformed_scope_or_balances(
    steps: list[object] | None,
    market_payload: dict[str, object],
    allowance_value: object,
    bnb_balance_wei: object,
    error: str,
) -> None:
    def urlopen_fn(request, **kwargs):
        if request.full_url.endswith("/v1/markets/896"):
            return FakeResponse({"data": market_payload})
        return response_for(request)

    client, _ = make_client(urlopen_fn)
    if steps is not None:
        client._builder.approval_steps = steps  # type: ignore[attr-defined]
    client._builder.allowance_value = allowance_value  # type: ignore[attr-defined]
    client._builder.bnb_balance_wei = bnb_balance_wei  # type: ignore[attr-defined]

    with pytest.raises(RuntimeError, match=error):
        client.approval_facts("896", exact_debit_wei=2_400_000_000_000_000_000)


def test_account_snapshot_removes_allowance_ready_and_marks_residual_allowance_as_breaker() -> None:
    client, _ = make_client(response_for)
    client._builder.allowance_value = 0  # type: ignore[attr-defined]

    clean = client.account_snapshot()

    assert "allowance_ready" not in clean
    assert clean["allowance"] == "0"
    assert clean["allowance_raw"] == "0"
    assert clean["allowance_breaker"] is False
    assert clean["scope_ready"] is True
    assert clean["gas_ready"] is True

    client._builder.allowance_value = 2_400_000_000_000_000_000  # type: ignore[attr-defined]
    residual = client.account_snapshot()

    assert residual["allowance_breaker"] is True
    assert residual["allowance"] == "2.4"
    assert residual["allowance_raw"] == "2400000000000000000"


def test_account_snapshot_marks_low_signer_bnb_read_only_without_breaker() -> None:
    client, _ = make_client(response_for)
    client._builder.allowance_value = 0  # type: ignore[attr-defined]
    client._builder.bnb_balance_wei = 2_000_000_000_000_000  # type: ignore[attr-defined]

    snapshot = client.account_snapshot()

    assert snapshot["allowance_breaker"] is False
    assert snapshot["gas_ready"] is False
    assert snapshot["minimum_top_up_bnb"] == "0.001"


def test_set_exact_buy_allowance_uses_sdk_set_approval_and_proves_exact_post_read() -> None:
    client, _ = make_client(response_for)
    client._builder.allowance_value = 0  # type: ignore[attr-defined]
    client._builder.allowance_after_approve = 2_400_000_000_000_000_000  # type: ignore[attr-defined]

    result = client.set_exact_buy_allowance("896", 2_400_000_000_000_000_000)

    step, approved, amount = client._builder.set_approval_calls[0]  # type: ignore[attr-defined]
    assert (step.id, approved, amount) == (
        "ERC20_ALLOWANCE:CTF_EXCHANGE",
        True,
        2_400_000_000_000_000_000,
    )
    assert result["success"] is True
    assert result["allowance"] == "2.4"
    assert result["allowance_raw"] == "2400000000000000000"
    assert result["allowance_breaker"] is True
    assert "_approval_step" not in result
    assert not any(str(key).startswith("_") for key in result)
    assert client._builder.order_submit_calls == 0  # type: ignore[attr-defined]
    assert client._builder.transfer_calls == 0  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "receipt_status",
    (
        1.0,
        0.0,
        1.5,
        Decimal("1"),
        Decimal("0"),
        Decimal("1.5"),
        "1",
        "0",
        "1.5",
        "0x1",
        "0x0",
        2,
        -1,
        True,
        False,
        NumericLookingReceiptStatus(),
    ),
)
def test_set_exact_buy_allowance_rejects_noncanonical_receipt_status_as_ambiguous(
    receipt_status: object,
) -> None:
    client, _ = make_client(response_for)
    client._builder.allowance_value = 0  # type: ignore[attr-defined]
    client._builder.allowance_after_approve = 2_400_000_000_000_000_000  # type: ignore[attr-defined]
    client._builder.next_set_approval_result = SimpleNamespace(  # type: ignore[attr-defined]
        success=True,
        receipt={"status": receipt_status, "transactionHash": "0xmalformed"},
        cause=None,
    )

    result = client.set_exact_buy_allowance("896", 2_400_000_000_000_000_000)

    assert len(client._builder.set_approval_calls) == 1  # type: ignore[attr-defined]
    assert result["success"] is False
    assert result["error_code"] == "receipt_ambiguous"
    assert result["possible_mutation"] is True
    assert "transaction_status" not in result
    assert client._builder.order_submit_calls == 0  # type: ignore[attr-defined]
    assert client._builder.transfer_calls == 0  # type: ignore[attr-defined]


def test_set_exact_buy_allowance_marks_builder_exception_as_possible_mutation() -> None:
    client, _ = make_client(response_for)
    client._builder.allowance_value = 0  # type: ignore[attr-defined]
    client._builder.set_approval_error = RuntimeError("transport-sentinel")  # type: ignore[attr-defined]

    result = client.set_exact_buy_allowance("896", 2_400_000_000_000_000_000)

    assert len(client._builder.set_approval_calls) == 1  # type: ignore[attr-defined]
    assert result["success"] is False
    assert result["error_code"] == "receipt_ambiguous"
    assert result["possible_mutation"] is True
    assert "transport-sentinel" not in json.dumps(result, default=str)


def test_set_exact_buy_allowance_marks_unverifiable_post_read_as_possible_mutation() -> None:
    client, _ = make_client(response_for)
    client._builder.allowance_value = 0  # type: ignore[attr-defined]
    client._builder.allowance_after_approve = ValueError("rpc-sentinel")  # type: ignore[attr-defined]

    result = client.set_exact_buy_allowance("896", 2_400_000_000_000_000_000)

    assert len(client._builder.set_approval_calls) == 1  # type: ignore[attr-defined]
    assert result["success"] is False
    assert result["error_code"] == "receipt_ambiguous"
    assert result["possible_mutation"] is True
    assert "rpc-sentinel" not in json.dumps(result, default=str)


@pytest.mark.parametrize(
    ("set_result", "post_allowance", "possible_mutation"),
    [
        (SimpleNamespace(success=False, receipt=None, cause=None), 0, True),
        (SimpleNamespace(success=False, receipt={"status": "unknown"}, cause=None), 0, True),
        (SimpleNamespace(success=False, receipt={"transactionHash": "0xambiguous"}, cause=None), 0, True),
        (SimpleNamespace(success=False, receipt={"status": 1, "transactionHash": "0xsubmitted"}, cause=None), 0, True),
        (SimpleNamespace(success=False, receipt={"status": 0, "transactionHash": "0xfailed"}, cause=None), 0, False),
        (SimpleNamespace(success=False, receipt={"status": 0, "transactionHash": "0xfailed"}, cause=None), 1, True),
    ],
)
def test_set_exact_buy_allowance_only_clears_possible_mutation_for_proven_zero_failed_receipt(
    set_result: object,
    post_allowance: int,
    possible_mutation: bool,
) -> None:
    client, _ = make_client(response_for)
    client._builder.allowance_value = 0  # type: ignore[attr-defined]
    client._builder.allowance_after_approve = post_allowance  # type: ignore[attr-defined]
    client._builder.next_set_approval_result = set_result  # type: ignore[attr-defined]

    result = client.set_exact_buy_allowance("896", 2_400_000_000_000_000_000)

    assert len(client._builder.set_approval_calls) == 1  # type: ignore[attr-defined]
    assert result["success"] is False
    assert result["possible_mutation"] is possible_mutation


def test_clear_buy_allowance_uses_sdk_revoke_and_proves_zero_post_read() -> None:
    client, _ = make_client(response_for)
    client._builder.allowance_value = 2_400_000_000_000_000_000  # type: ignore[attr-defined]
    client._builder.allowance_after_clear = 0  # type: ignore[attr-defined]

    result = client.clear_buy_allowance("896")

    step, approved, amount = client._builder.set_approval_calls[0]  # type: ignore[attr-defined]
    assert (step.id, approved, amount) == ("ERC20_ALLOWANCE:CTF_EXCHANGE", False, None)
    assert result["success"] is True
    assert result["allowance"] == "0"
    assert result["allowance_raw"] == "0"
    assert result["allowance_breaker"] is False
    assert "_approval_step" not in result
    assert not any(str(key).startswith("_") for key in result)
    assert client._builder.order_submit_calls == 0  # type: ignore[attr-defined]
    assert client._builder.transfer_calls == 0  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("method_name", "set_result", "post_allowance", "error_code"),
    [
        ("set_exact_buy_allowance", SimpleNamespace(success=False, receipt={"status": 0, "transactionHash": bytes.fromhex("34" * 32)}, cause=None), 0, "receipt_failed"),
        ("set_exact_buy_allowance", SimpleNamespace(success=True, receipt={"status": 1, "transactionHash": bytes.fromhex("35" * 32)}, cause=None), 7, "allowance_mismatch"),
        ("clear_buy_allowance", SimpleNamespace(success=False, receipt=None, cause=None), 2_400_000_000_000_000_000, "receipt_ambiguous"),
        ("clear_buy_allowance", SimpleNamespace(success=True, receipt={"status": 1, "transactionHash": bytes.fromhex("36" * 32)}, cause=None), 1, "allowance_mismatch"),
    ],
)
def test_allowance_mutations_return_redacted_failures_on_receipt_or_post_read_ambiguity(
    method_name: str,
    set_result: object,
    post_allowance: int,
    error_code: str,
) -> None:
    client, _ = make_client(response_for)
    client._builder.allowance_value = 2_400_000_000_000_000_000 if method_name == "clear_buy_allowance" else 0  # type: ignore[attr-defined]
    client._builder.allowance_after_approve = post_allowance  # type: ignore[attr-defined]
    client._builder.allowance_after_clear = post_allowance  # type: ignore[attr-defined]
    client._builder.next_set_approval_result = set_result  # type: ignore[attr-defined]

    result = getattr(client, method_name)("896", 2_400_000_000_000_000_000) if method_name == "set_exact_buy_allowance" else getattr(client, method_name)("896")

    assert result["success"] is False
    assert result["error_code"] == error_code
    assert "_approval_step" not in result
    assert not any(str(key).startswith("_") for key in result)
    assert "signature-sentinel" not in json.dumps(result, default=str)
    assert "api-key-sentinel" not in json.dumps(result, default=str)
    assert PRIVATE_KEY not in json.dumps(result, default=str)
