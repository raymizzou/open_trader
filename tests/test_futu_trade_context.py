from __future__ import annotations

from dataclasses import dataclass
import sys
import types
from typing import Any

import pytest

from open_trader.futu_account import FutuAccountClient, FutuAccountError
from open_trader.kelly_order_execution import (
    FutuOrderExecutionError,
    FutuSimulateOrderExecutionClient,
)
from open_trader.kelly_paper_order_sync import (
    FutuPaperOrderSyncError,
    FutuSimulatePaperOrderClient,
)


class _FakeTable:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def to_dict(self, orient: str) -> list[dict[str, object]]:
        assert orient == "records"
        return [dict(row) for row in self.rows]


@dataclass
class _FakeContextState:
    attempts: int = 0
    query_calls: int = 0
    order_calls: int = 0
    closed: bool = False


class _FakeOpenSecTradeContext:
    fail_initial = True
    raise_initial = False
    instances: list[_FakeOpenSecTradeContext] = []

    def __init__(
        self,
        *,
        host: str,
        port: int,
        filter_trdmarket: str = "HK",
    ) -> None:
        self.host = host
        self.port = port
        self.filter_trdmarket = filter_trdmarket
        self._auto_reconnect = True
        self._ready = False
        self._sync_query_connect_timeout: float | None = None
        self._reconnect_results = [-1, 0]
        self.state = _FakeContextState()
        type(self).instances.append(self)
        while True:
            ret = self._init_connect_sync()
            if ret == 0 or not self._auto_reconnect:
                return

    def _init_connect_sync(self) -> int:
        if not self._ready:
            self.state.attempts += 1
            if type(self).raise_initial:
                raise RuntimeError("initial handshake failed")
            if type(self).fail_initial:
                type(self).fail_initial = False
                return -1
            self._ready = True
            return 0
        return self._reconnect_results.pop(0) if self._reconnect_results else 0

    def set_sync_query_connect_timeout(self, timeout: float) -> None:
        self._sync_query_connect_timeout = timeout

    def simulate_disconnect_and_reconnect(self) -> list[int]:
        return [self._init_connect_sync(), self._init_connect_sync()]

    def close(self) -> None:
        self.state.closed = True

    def get_acc_list(self) -> tuple[int, _FakeTable]:
        self.state.query_calls += 1
        return 0, _FakeTable([
            {"acc_id": 1, "acc_index": 0, "trd_env": "REAL", "acc_status": "ACTIVE"},
            {"acc_id": 2, "acc_index": 0, "trd_env": "SIMULATE", "acc_status": "ACTIVE"},
        ])

    def accinfo_query(self, **_: object) -> tuple[int, _FakeTable]:
        self.state.query_calls += 1
        return 0, _FakeTable([{"cash": "100", "total_assets": "100"}])

    def position_list_query(self, **_: object) -> tuple[int, _FakeTable]:
        self.state.query_calls += 1
        return 0, _FakeTable([])

    def order_list_query(self, **_: object) -> tuple[int, _FakeTable]:
        self.state.order_calls += 1
        return 0, _FakeTable([])

    def history_order_list_query(self, **_: object) -> tuple[int, _FakeTable]:
        self.state.order_calls += 1
        return 0, _FakeTable([])

    def place_order(self, **_: object) -> tuple[int, _FakeTable]:
        self.state.order_calls += 1
        return 0, _FakeTable([])


def _install_fake_futu(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    _FakeOpenSecTradeContext.instances.clear()
    _FakeOpenSecTradeContext.fail_initial = True
    _FakeOpenSecTradeContext.raise_initial = False
    module = types.ModuleType("futu")
    module.OpenSecTradeContext = _FakeOpenSecTradeContext  # type: ignore[attr-defined]
    module.RET_OK = 0
    module.RET_ERROR = -1
    module.TrdMarket = types.SimpleNamespace(HK="HK", US="US", CN="CN")
    module.TrdSide = types.SimpleNamespace(BUY="BUY", SELL="SELL")
    monkeypatch.setitem(sys.modules, "futu", module)
    return module


def test_public_account_client_stops_failed_initial_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_futu(monkeypatch)

    with pytest.raises(FutuAccountError) as caught:
        FutuAccountClient(
            host="127.0.0.1",
            port=11111,
            connectivity_checker=lambda _host, _port: True,
        )

    assert caught.value.error_type == "trade_context_failed"
    context = _FakeOpenSecTradeContext.instances[-1]
    assert context.state.attempts == 1
    assert context.state.closed is True
    assert context.state.query_calls == 0


@pytest.mark.parametrize(
    ("client_type", "error_type"),
    [
        (FutuSimulateOrderExecutionClient, FutuOrderExecutionError),
        (FutuSimulatePaperOrderClient, FutuPaperOrderSyncError),
    ],
)
def test_public_simulate_clients_stop_failed_initial_connection(
    monkeypatch: pytest.MonkeyPatch,
    client_type: type[object],
    error_type: type[Exception],
) -> None:
    _install_fake_futu(monkeypatch)
    kwargs: dict[str, Any] = {
        "host": "127.0.0.1",
        "port": 11111,
        "trd_market": "CN",
        "connectivity_checker": lambda _host, _port: True,
    }
    if client_type is FutuSimulatePaperOrderClient:
        kwargs["experiment_symbol_index"] = {}

    with pytest.raises(error_type) as caught:
        client_type(**kwargs)  # type: ignore[arg-type]

    assert caught.value.error_type == "trade_context_failed"  # type: ignore[attr-defined]
    context = _FakeOpenSecTradeContext.instances[-1]
    assert context.state.attempts == 1
    assert context.state.closed is True
    assert context.state.query_calls == 0
    assert context.state.order_calls == 0


def test_public_clients_recover_on_new_context_and_preserve_sdk_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _install_fake_futu(monkeypatch)
    _FakeOpenSecTradeContext.fail_initial = False

    account = FutuAccountClient(
        host="127.0.0.1",
        port=11111,
        connectivity_checker=lambda _host, _port: True,
    )
    try:
        account_snapshot = account.fetch_snapshot()
        assert account_snapshot.accounts[0].trd_env == "REAL"
        account_context = account.context
        assert account_context.host == "127.0.0.1"
        assert account_context.port == 11111
        assert account_context._sync_query_connect_timeout == 10.0
        assert account_context.simulate_disconnect_and_reconnect() == [-1, 0]
        assert account_context.state.closed is False
        assert account_context._auto_reconnect is True
    finally:
        account.close()

    simulate = FutuSimulateOrderExecutionClient(
        host="127.0.0.1",
        port=11111,
        trd_market="CN",
        connectivity_checker=lambda _host, _port: True,
    )
    paper = FutuSimulatePaperOrderClient(
        host="127.0.0.1",
        port=11111,
        trd_market="CN",
        experiment_symbol_index={},
        connectivity_checker=lambda _host, _port: True,
    )
    try:
        assert simulate.environment == "SIMULATE"
        assert simulate.trd_market == "CN"
        assert simulate.context.filter_trdmarket == "CN"
        assert simulate.context._sync_query_connect_timeout == 10.0
        assert simulate.context.simulate_disconnect_and_reconnect() == [-1, 0]
        assert simulate.context._auto_reconnect is True
        assert paper.environment == "SIMULATE"
        assert paper.trd_market == "CN"
        assert paper.context.filter_trdmarket == "CN"
        assert paper.context._sync_query_connect_timeout == 10.0
        assert paper.context.simulate_disconnect_and_reconnect() == [-1, 0]
        assert paper.context._auto_reconnect is True
        assert all(context.state.order_calls == 0 for context in _FakeOpenSecTradeContext.instances)
    finally:
        simulate.close()
        paper.close()

    _FakeOpenSecTradeContext.raise_initial = True
    with pytest.raises(FutuAccountError) as initial_error:
        FutuAccountClient(
            host="127.0.0.1",
            port=11111,
            connectivity_checker=lambda _host, _port: True,
        )
    assert initial_error.value.error_type == "trade_context_failed"
    assert _FakeOpenSecTradeContext.instances[-1].state.closed is True

    class UnsupportedContext:
        constructed = False

        def __init__(self, **_: object) -> None:
            type(self).constructed = True

    unsupported = types.ModuleType("futu")
    unsupported.OpenSecTradeContext = UnsupportedContext  # type: ignore[attr-defined]
    unsupported.RET_OK = 0
    monkeypatch.setitem(sys.modules, "futu", unsupported)
    with pytest.raises(FutuAccountError) as unsupported_error:
        FutuAccountClient(
            host="127.0.0.1",
            port=11111,
            connectivity_checker=lambda _host, _port: True,
        )
    assert unsupported_error.value.error_type == "trade_context_failed"
    assert UnsupportedContext.constructed is False
