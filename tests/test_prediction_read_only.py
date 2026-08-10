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
