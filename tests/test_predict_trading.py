from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from urllib.error import URLError

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


class FakeBuilder:
    last_order_input = None

    def __init__(self) -> None:
        self.price_per_share = 1000000
        self.max_collateral_debit = 1000000
        self.quote_calls = 0
        self.allowance_value: object = 1000000
        self.usdt_balance: object = 5000000
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

    def balance_of(self, asset: str) -> str:
        assert asset == "USDT"
        return self.usdt_balance

    def allowance(self, **kwargs: object) -> str:
        return "1000000"

    def get_approval_steps(self, scope) -> list[object]:
        self.last_scope = scope
        return list(self.approval_steps)

    def set_approval(self, step: object, *, approved: bool = True, amount: int = 0) -> object:
        self.set_approval_calls.append((step, approved, amount if approved else None))
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
    return FakeResponse({"id": "order-id", "hash": "order-hash"}, status=201)


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
    client._builder.price_per_share = 510000  # type: ignore[attr-defined]
    client._builder.max_collateral_debit = 510000  # type: ignore[attr-defined]
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
        "calculable_gas": Decimal("0"),
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
        "calculable_gas": Decimal("0"),
    }

    assert client.no_submit_cross_buy_preflight(order).accepted is True
    result = client.submit_cross_buy_once(order)

    assert result.accepted is True
    assert client._builder.quote_calls == 1  # type: ignore[attr-defined]
    assert sum(
        request.full_url.endswith("/v1/orders") and request.get_method() == "POST"
        for request in requests
    ) == 1


def test_cross_entry_rejects_quote_and_calculable_gas_over_approved_cost() -> None:
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
        "calculable_gas": Decimal("0.01"),
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

    failure_client, _ = make_client(lambda *args, **kwargs: (_ for _ in ()).throw(URLError("offline")))
    assert failure_client.submit_buy_once("896", "yes-token", 10**18).error_code == "ambiguous"


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
        if request.full_url.endswith("/v1/orders/matches"):
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
            return FakeResponse({"data": {"hash": "order-hash", "marketId": "896", "tokenId": "yes-token", "signer": DEPOSIT, "status": "FILLED", "amountFilled": "1"}})
        if request.full_url.endswith("/v1/orders/matches"):
            return FakeResponse({"data": {"matches": [{"orderHash": "order-hash", "transactionHash": "tx", "executedAmount": "1", "fee": "2"}]}})
        if request.full_url.endswith("/v1/account/activity"):
            return FakeResponse({"data": {"activities": [{"orderHash": "order-hash", "transactionHash": "tx", "executedAmount": "1", "fee": "2"}]}})
        if request.full_url.endswith("/v1/positions?marketId=896"):
            return FakeResponse({"data": {"positions": [{"tokenId": "yes-token", "amount": "1", "amountDelta": "1"}]}})
        raise AssertionError(request.full_url)

    client, _ = make_client(urlopen_fn)
    assert client.reconcile_buy("896", "yes-token", "order-hash") == {
        "verified": True,
        "conclusively_absent": False,
        "status": "verified",
        "filled_quantity": Decimal("1"),
        "position_quantity": Decimal("1"),
        "minimum_order_size": Decimal("1"),
    }


def test_book_parses_decimal_price_to_exact_wei() -> None:
    from open_trader.predict_trading import _book

    book = _book({"marketId": 896, "updateTimestampMs": 1, "asks": [["0.57", "1"]], "bids": []})
    assert book.asks[0][0] == 570000000000000000


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

    facts = client.approval_facts("896", exact_debit_wei=2_400_000)

    assert facts["predict_account"] == DEPOSIT
    assert facts["gas_signer"] == PRIVY_SIGNER
    assert facts["allowance"] == "0"
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
    assert client._builder.transfer_calls == 0  # type: ignore[attr-defined]
    assert client._builder.order_submit_calls == 0  # type: ignore[attr-defined]


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
        client.approval_facts("896", exact_debit_wei=2_400_000)


def test_account_snapshot_removes_allowance_ready_and_marks_residual_allowance_as_breaker() -> None:
    client, _ = make_client(response_for)
    client._builder.allowance_value = 0  # type: ignore[attr-defined]

    clean = client.account_snapshot()

    assert "allowance_ready" not in clean
    assert clean["allowance"] == "0"
    assert clean["allowance_breaker"] is False
    assert clean["scope_ready"] is True
    assert clean["gas_ready"] is True

    client._builder.allowance_value = 2_400_000  # type: ignore[attr-defined]
    residual = client.account_snapshot()

    assert residual["allowance_breaker"] is True
    assert residual["allowance"] == "2400000"


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
    client._builder.allowance_after_approve = 2_400_000  # type: ignore[attr-defined]

    result = client.set_exact_buy_allowance("896", 2_400_000)

    step, approved, amount = client._builder.set_approval_calls[0]  # type: ignore[attr-defined]
    assert (step.id, approved, amount) == ("ERC20_ALLOWANCE:CTF_EXCHANGE", True, 2_400_000)
    assert result["success"] is True
    assert result["allowance"] == "2400000"
    assert client._builder.order_submit_calls == 0  # type: ignore[attr-defined]
    assert client._builder.transfer_calls == 0  # type: ignore[attr-defined]


def test_clear_buy_allowance_uses_sdk_revoke_and_proves_zero_post_read() -> None:
    client, _ = make_client(response_for)
    client._builder.allowance_value = 2_400_000  # type: ignore[attr-defined]
    client._builder.allowance_after_clear = 0  # type: ignore[attr-defined]

    result = client.clear_buy_allowance("896")

    step, approved, amount = client._builder.set_approval_calls[0]  # type: ignore[attr-defined]
    assert (step.id, approved, amount) == ("ERC20_ALLOWANCE:CTF_EXCHANGE", False, None)
    assert result["success"] is True
    assert result["allowance"] == "0"
    assert client._builder.order_submit_calls == 0  # type: ignore[attr-defined]
    assert client._builder.transfer_calls == 0  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("method_name", "set_result", "post_allowance", "error_code"),
    [
        ("set_exact_buy_allowance", SimpleNamespace(success=False, receipt={"status": 0, "transactionHash": bytes.fromhex("34" * 32)}, cause=None), 0, "receipt_failed"),
        ("set_exact_buy_allowance", SimpleNamespace(success=True, receipt={"status": 1, "transactionHash": bytes.fromhex("35" * 32)}, cause=None), 7, "allowance_mismatch"),
        ("clear_buy_allowance", SimpleNamespace(success=False, receipt=None, cause=None), 2_400_000, "receipt_ambiguous"),
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
    client._builder.allowance_value = 2_400_000 if method_name == "clear_buy_allowance" else 0  # type: ignore[attr-defined]
    client._builder.allowance_after_approve = post_allowance  # type: ignore[attr-defined]
    client._builder.allowance_after_clear = post_allowance  # type: ignore[attr-defined]
    client._builder.next_set_approval_result = set_result  # type: ignore[attr-defined]

    result = getattr(client, method_name)("896", 2_400_000) if method_name == "set_exact_buy_allowance" else getattr(client, method_name)("896")

    assert result["success"] is False
    assert result["error_code"] == error_code
    assert "signature-sentinel" not in json.dumps(result, default=str)
    assert "api-key-sentinel" not in json.dumps(result, default=str)
    assert PRIVATE_KEY not in json.dumps(result, default=str)
