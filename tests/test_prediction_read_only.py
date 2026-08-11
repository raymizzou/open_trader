from __future__ import annotations

from types import SimpleNamespace

import pytest

from open_trader.prediction_read_only import (
    PredictReadOnlyGuard,
    PolymarketReadOnlyGuard,
    ReadOnlyViolation,
    guard_polymarket_client,
    guard_predict_client,
)


class FakePolymarketClient:
    def __init__(self, transport_calls: list[str]) -> None:
        self._client = SimpleNamespace()
        self.notifier = SimpleNamespace(send_notification=lambda: transport_calls.append("notification"))
        for method in ("place_limit_order", "cancel_all", "merge_positions", "redeem_positions"):
            setattr(self, method, lambda _method=method: transport_calls.append(_method))


@pytest.mark.parametrize(
    ("method", "kind"),
    (
        ("place_limit_order", "mutation"),
        ("cancel_all", "mutation"),
        ("merge_positions", "mutation"),
        ("redeem_positions", "mutation"),
    ),
)
def test_polymarket_guard_records_and_blocks_mutations_before_transport(
    method: str, kind: str
) -> None:
    transport_calls: list[str] = []
    violations: list[dict[str, object]] = []
    client = FakePolymarketClient(transport_calls)
    guard = PolymarketReadOnlyGuard(on_violation=violations.append)

    with guard_polymarket_client(client, guard), pytest.raises(ReadOnlyViolation) as exc_info:
        getattr(client, method)()

    assert transport_calls == []
    assert exc_info.value.attempt == violations[0]
    assert violations[0]["venue"] == "polymarket"
    assert violations[0]["kind"] == kind
    assert violations[0]["method"] == method
    assert violations[0]["call_chain"]
    assert set(violations[0]) == {"venue", "kind", "method", "call_chain"}


def test_polymarket_guard_blocks_notification_and_restores_client() -> None:
    transport_calls: list[str] = []
    client = FakePolymarketClient(transport_calls)
    original_cancel_all = client.cancel_all
    guard = PolymarketReadOnlyGuard()

    with guard_polymarket_client(client, guard), pytest.raises(ReadOnlyViolation):
        client.notifier.send_notification()

    assert transport_calls == []
    assert guard.live_notifications == 1
    assert guard.attempts[0]["kind"] == "notification"
    assert client.cancel_all is original_cancel_all
    client.cancel_all()
    assert transport_calls == ["cancel_all"]


class FakePredictClient:
    def __init__(self, transport_calls: list[str]) -> None:
        self._builder = SimpleNamespace(
            approval=lambda: transport_calls.append("approval"),
            transfer=lambda: transport_calls.append("transfer"),
            _web3=SimpleNamespace(
                eth=SimpleNamespace(
                    send_raw_transaction=lambda: transport_calls.append("raw_transaction")
                )
            ),
        )
        self.submit_order = lambda: transport_calls.append("order")


@pytest.mark.parametrize(
    ("target", "method"),
    (
        ("client", "submit_order"),
        ("builder", "approval"),
        ("builder", "transfer"),
        ("eth", "send_raw_transaction"),
    ),
)
def test_predict_guard_records_and_blocks_mutations_before_transport(
    target: str, method: str
) -> None:
    transport_calls: list[str] = []
    client = FakePredictClient(transport_calls)
    guard = PredictReadOnlyGuard()

    with guard_predict_client(client, guard), pytest.raises(ReadOnlyViolation):
        value = client
        if target == "builder":
            value = client._builder
        elif target == "eth":
            value = client._builder._web3.eth
        getattr(value, method)()

    assert transport_calls == []
    assert guard.mutation_calls == 1
    assert guard.attempts[0]["venue"] == "predict"
    assert guard.attempts[0]["kind"] == "mutation"
    assert guard.attempts[0]["method"] == method
    assert guard.attempts[0]["call_chain"]


def test_predict_guard_restores_client_after_context_exit() -> None:
    transport_calls: list[str] = []
    client = FakePredictClient(transport_calls)
    original = client._builder.approval

    with guard_predict_client(client, PredictReadOnlyGuard()):
        pass

    assert client._builder.approval is original
    client._builder.approval()
    assert transport_calls == ["approval"]


def test_predict_guard_preserves_builder_reads_and_signed_no_submit_construction() -> None:
    calls: list[str] = []

    class SigningBuilder:
        def balance_of(self, asset: str) -> int:
            calls.append("balance_of")
            assert asset == "USDT"
            return 10

        def build_order(self) -> dict[str, str]:
            calls.append("build_order")
            return {"signature": "local-only"}

    class SigningClient:
        def __init__(self) -> None:
            self._builder = SigningBuilder()

        def approval_facts(self) -> dict[str, object]:
            assert self._builder.balance_of("USDT") == 10
            return {"approval": "read-only"}

        def no_submit_buy_preflight(self) -> SimpleNamespace:
            assert self._builder.build_order()["signature"] == "local-only"
            return SimpleNamespace(accepted=True, status="preflight")

    client = SigningClient()
    guard = PredictReadOnlyGuard()

    with guard_predict_client(client, guard):
        assert client.approval_facts() == {"approval": "read-only"}
        assert client.no_submit_buy_preflight().status == "preflight"

    assert calls == ["balance_of", "build_order"]
    assert guard.attempts == []


def test_predict_guard_preserves_stateful_sdk_signing() -> None:
    class Signer:
        signature = "local-signature"

        def sign(self) -> str:
            return object.__getattribute__(self, "signature")

    class StatefulSigningBuilder:
        def __init__(self) -> None:
            self._signer = Signer()

        def sign_predict_account_message(self, message: str) -> str:
            assert message == "predict-message"
            return self._signer.sign()

    class StatefulSigningClient:
        def __init__(self) -> None:
            self._builder = StatefulSigningBuilder()

        def _authenticate(self) -> str:
            assert self._builder.sign_predict_account_message("predict-message") == "local-signature"
            return "jwt-fixture"

    client = StatefulSigningClient()
    guard = PredictReadOnlyGuard()

    with guard_predict_client(client, guard):
        assert client._authenticate() == "jwt-fixture"

    assert guard.attempts == []


@pytest.mark.parametrize(
    ("action", "kind"),
    (
        ("set_approval", "mutation"),
        ("cleanup_allowance", "mutation"),
        ("post_order", "mutation"),
        ("transfer_usdt", "mutation"),
        ("redeem_positions", "mutation"),
        ("send_notification", "notification"),
    ),
)
def test_polymarket_guard_blocks_nested_sdk_proxy_mutation(
    action: str, kind: str
) -> None:
    calls: list[str] = []
    nested = SimpleNamespace(
        _ctx_inner=SimpleNamespace(**{action: lambda: calls.append(action)})
    )
    client = SimpleNamespace(_client=nested)
    guard = PolymarketReadOnlyGuard()

    with guard_polymarket_client(client, guard), pytest.raises(ReadOnlyViolation):
        getattr(client._client._ctx_inner, action)()

    assert calls == []
    assert guard.attempts[0]["method"] == action
    assert guard.attempts[0]["kind"] == kind
    assert guard.mutation_calls == (0 if kind == "notification" else 1)
    assert guard.live_notifications == (1 if kind == "notification" else 0)


def test_polymarket_guard_blocks_raw_nested_transport_before_delivery() -> None:
    posted: list[object] = []

    class Transport:
        def post_json(self, payload: object) -> object:
            posted.append(payload)
            return {"posted": True}

    class Context:
        def __init__(self) -> None:
            self.transport = Transport()

    class Adapter:
        def __init__(self) -> None:
            self._ctx = Context()

        def read_account(self) -> object:
            return self._ctx.transport.post_json({"probe": "secret"})

    client = SimpleNamespace(_client=Adapter())
    guard = PolymarketReadOnlyGuard()

    with guard_polymarket_client(client, guard), pytest.raises(ReadOnlyViolation):
        client._client.read_account()

    assert posted == []
    assert guard.mutation_calls == 1
    assert guard.attempts[0]["method"] == "post_json"
    assert "secret" not in str(guard.attempts[0])


def test_polymarket_guard_preserves_nested_network_send() -> None:
    class Transport:
        def __init__(self) -> None:
            self.send_calls = 0

        def send(self) -> object:
            self.send_calls += 1
            return {"ok": True}

    transport = Transport()
    client = SimpleNamespace(_client=SimpleNamespace(transport=transport))
    guard = PolymarketReadOnlyGuard()

    with guard_polymarket_client(client, guard):
        assert client._client.transport.send()["ok"] is True

    assert transport.send_calls == 1
    assert guard.attempts == []


def test_polymarket_guard_preserves_nested_send_internal_iterator() -> None:
    class Transport:
        def __init__(self) -> None:
            self.responses = iter(({"ok": True},))

        def send(self) -> object:
            return next(self.responses)

    transport = Transport()
    client = SimpleNamespace(_client=SimpleNamespace(transport=transport))
    guard = PolymarketReadOnlyGuard()

    with guard_polymarket_client(client, guard):
        assert client._client.transport.send()["ok"] is True

    assert guard.attempts == []


def test_polymarket_guard_preserves_nested_local_signing() -> None:
    class Adapter:
        def __init__(self) -> None:
            self.orders = iter(({"order_type": "FOK"},))

        def create_market_order(self, **_: object) -> object:
            return next(self.orders)

    client = SimpleNamespace(_client=Adapter())
    guard = PolymarketReadOnlyGuard()

    with guard_polymarket_client(client, guard):
        assert client._client.create_market_order(side="BUY")["order_type"] == "FOK"

    assert guard.attempts == []


def test_polymarket_guard_blocks_notifier_send_but_allows_network_send() -> None:
    class Notifier:
        def send(self) -> None:
            raise AssertionError("notifier target called")

    class Transport:
        def send(self) -> object:
            return {"ok": True}

    client = SimpleNamespace(
        _client=SimpleNamespace(transport=Transport()),
        notifier=Notifier(),
    )
    guard = PolymarketReadOnlyGuard()

    with guard_polymarket_client(client, guard), pytest.raises(ReadOnlyViolation):
        client.notifier.send()
    assert guard.live_notifications == 1
    assert guard.attempts[0]["method"] == "send"

    with guard_polymarket_client(client, guard):
        assert client._client.transport.send()["ok"] is True
    assert guard.live_notifications == 1


def test_predict_guard_blocks_nested_raw_transaction_before_delivery() -> None:
    sent: list[bytes] = []

    class Builder:
        def __init__(self) -> None:
            self._web3 = SimpleNamespace(
                eth=SimpleNamespace(
                    send_raw_transaction=lambda raw: sent.append(raw)
                )
            )

        def build_order(self) -> object:
            return self._web3.eth.send_raw_transaction(b"secret-signed-transaction")

    client = SimpleNamespace(_builder=Builder())
    guard = PredictReadOnlyGuard()

    with guard_predict_client(client, guard), pytest.raises(ReadOnlyViolation):
        client._builder.build_order()

    assert sent == []
    assert guard.mutation_calls == 1
    assert guard.attempts[0]["method"] == "send_raw_transaction"
    assert "secret-signed-transaction" not in str(guard.attempts[0])
