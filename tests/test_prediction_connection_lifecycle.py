from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
import websockets
from websockets.asyncio import client as ws_client
from websockets.exceptions import InvalidProxyMessage, InvalidProxyStatus
from websockets.uri import parse_proxy, parse_uri

from open_trader import prediction_arbitrage_store as store_module
from open_trader.polymarket_monitor import PolymarketMonitor
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from open_trader.prediction_websocket_compat import install_proxy_cleanup


async def _run_lifecycle_scenario(scenario):
    # Real-loop watchdog; business deadlines inside the scenario stay intact.
    return await asyncio.wait_for(scenario, timeout=5)


def test_busy_begin_preserves_sqlite_error_and_releases_connection(tmp_path, monkeypatch, caplog):
    store = PredictionArbitrageStore(tmp_path)
    monkeypatch.setattr(store_module, "_BUSY_TIMEOUT_MS", 30)
    connections = []
    original = store._connection

    def connection():
        result = original()
        connections.append(result)
        return result

    monkeypatch.setattr(store, "_connection", connection)

    def contender():
        with store._transaction():
            pytest.fail("a competing writer must not enter the transaction")

    blocker = sqlite3.connect(store.path, isolation_level=None)
    try:
        blocker.execute("BEGIN IMMEDIATE")
        with ThreadPoolExecutor(max_workers=1) as executor:
            with pytest.raises(sqlite3.OperationalError, match="database is locked") as caught:
                executor.submit(contender).result(timeout=2)
        assert caught.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
        assert "phase=begin" in caplog.text
    finally:
        blocker.close()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")
    with store._transaction() as connection:
        connection.execute("CREATE TABLE subsequent_write(value)")


@pytest.mark.parametrize("phase", ["body", "commit", "rollback"])
def test_transaction_cleanup_preserves_original_failure(tmp_path, monkeypatch, caplog, phase):
    store = PredictionArbitrageStore(tmp_path)
    original = ValueError("original operation failed")

    class Connection(sqlite3.Connection):
        def execute(self, sql, *args):
            if phase == "commit" and sql == "COMMIT":
                raise original
            if phase == "rollback" and sql == "ROLLBACK":
                raise sqlite3.OperationalError("injected rollback failure")
            return super().execute(sql, *args)

    connection = sqlite3.connect(store.path, isolation_level=None, factory=Connection)
    monkeypatch.setattr(store, "_connection", lambda: connection)
    with pytest.raises(ValueError) as caught:
        with store._transaction() as transaction:
            transaction.execute("CREATE TABLE uncommitted(value)")
            if phase != "commit":
                raise original
    assert caught.value is original
    if phase == "rollback":
        assert "injected rollback failure" in caplog.text
    with sqlite3.connect(store.path) as reader:
        assert reader.execute("SELECT name FROM sqlite_master WHERE name='uncommitted'").fetchone() is None
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")


def test_transaction_cancel_rolls_back_without_losing_cancellation(tmp_path):
    store = PredictionArbitrageStore(tmp_path)
    original = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError) as caught:
        with store._transaction() as connection:
            connection.execute("CREATE TABLE uncommitted(value)")
            raise original
    assert caught.value is original
    with store._read_connection() as reader:
        assert reader.execute("SELECT name FROM sqlite_master WHERE name='uncommitted'").fetchone() is None


def test_slow_transaction_reports_wait_separately_from_hold(tmp_path, monkeypatch, caplog):
    store = PredictionArbitrageStore(tmp_path)
    ticks = iter([10.0, 10.25, 11.5])
    monkeypatch.setattr(store_module, "monotonic", lambda: next(ticks))
    with store._transaction() as connection:
        connection.execute("CREATE TABLE committed(value)")
    assert "operation=test_slow_transaction_reports_wait_separately_from_hold" in caplog.text
    assert "phase=complete wait_seconds=0.250 hold_seconds=1.250" in caplog.text


class ProxyTransport:
    def __init__(self):
        self.closed = False
        self.writes = []

    def write(self, data):
        self.writes.append(data)

    def close(self):
        self.closed = True

    abort = close


class TestProxyHandshake:
    @pytest.fixture(autouse=True)
    def compatibility(self, monkeypatch):
        monkeypatch.setattr(ws_client, "connect_http_proxy", ws_client.connect_http_proxy)
        install_proxy_cleanup()

    @pytest.mark.parametrize("ending", ["cancel", "timeout", "eof", "connection_lost", "status", "malformed", "success"])
    def test_handshake_owns_transport_and_preserves_outcome(self, monkeypatch, ending):
        async def scenario():
            loop = asyncio.get_running_loop()
            ready = asyncio.Event()
            transport = ProxyTransport()
            protocol = None

            async def connect(factory, *args, **kwargs):
                nonlocal protocol, transport
                transport = ProxyTransport()
                protocol = factory()
                protocol.connection_made(transport)
                ready.set()
                return transport, protocol

            monkeypatch.setattr(loop, "create_connection", connect)
            proxy = parse_proxy("http://127.0.0.1:1")
            uri = parse_uri("wss://example.invalid/")

            async def handshake():
                if ending == "timeout":
                    async with asyncio.timeout(0.02):
                        return await ws_client.connect_http_proxy(proxy, uri)
                return await ws_client.connect_http_proxy(proxy, uri)

            task = asyncio.create_task(handshake())
            await ready.wait()
            assert transport.writes[0].startswith(b"CONNECT example.invalid:443 HTTP/1.1")
            if ending in {"cancel", "timeout"}:
                if ending == "cancel":
                    task.cancel()
                with pytest.raises(asyncio.CancelledError if ending == "cancel" else TimeoutError):
                    await task
                assert transport.closed
                # A queued response/loss callback must not complete a cancelled future.
                protocol.data_received(b"HTTP/1.1 200 Connection established\r\n\r\n")
                protocol.connection_lost(ConnectionResetError("late connection loss"))
                previous = transport
                ready.clear()
                retry = asyncio.create_task(ws_client.connect_http_proxy(proxy, uri))
                await ready.wait()
                protocol.data_received(b"HTTP/1.1 200 Connection established\r\n\r\n")
                assert await retry is transport
                assert transport is not previous and previous.closed
                assert not transport.closed
            elif ending in {"eof", "connection_lost"}:
                if ending == "eof":
                    protocol.eof_received()
                protocol.connection_lost(None)
                with pytest.raises(InvalidProxyMessage):
                    await asyncio.wait_for(task, timeout=0.1)
                assert transport.closed
            elif ending in {"status", "malformed"}:
                protocol.data_received(b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n" if ending == "status" else b"not HTTP\r\n\r\n")
                with pytest.raises(InvalidProxyStatus if ending == "status" else InvalidProxyMessage):
                    await task
                assert transport.closed
                protocol.eof_received()
                protocol.connection_lost(None)
            else:
                protocol.data_received(b"HTTP/1.1 200 Connection established\r\n\r\n")
                assert await task is transport
                assert not transport.closed

        asyncio.run(_run_lifecycle_scenario(scenario()))


def test_proxy_patch_is_version_scoped_and_idempotent(monkeypatch):
    original = object()
    monkeypatch.setattr(ws_client, "connect_http_proxy", original)
    monkeypatch.setattr(websockets, "__version__", "16.0")
    install_proxy_cleanup()
    assert ws_client.connect_http_proxy is original
    monkeypatch.setattr(websockets, "__version__", "15.0.1")
    install_proxy_cleanup()
    patched = ws_client.connect_http_proxy
    assert patched is not original
    install_proxy_cleanup()
    assert ws_client.connect_http_proxy is patched


def test_prediction_installs_compat_before_starting_loops(monkeypatch):
    from open_trader.prediction_runtime import PredictionRuntime

    original = object()
    monkeypatch.setattr(ws_client, "connect_http_proxy", original)
    runtime = PredictionRuntime.__new__(PredictionRuntime)
    runtime._state = "NEW"
    runtime._mode = "shadow"
    def start_shadow():
        assert ws_client.connect_http_proxy is not original
    runtime._start_shadow = start_shadow
    runtime.start()


@pytest.mark.parametrize("ending", ["stop", "crash", "cancel", "cleanup_error"])
def test_monitor_closes_owned_client_on_its_loop(tmp_path, monkeypatch, caplog, ending):
    owner = []
    closed = []
    drained = []
    original = RuntimeError("original monitor failure")

    class Client:
        def __init__(self):
            owner.append((asyncio.get_running_loop(), threading.get_ident()))

        async def close(self):
            if ending == "cleanup_error":
                assert drained == ["activity"]
            closed.append((asyncio.get_running_loop(), threading.get_ident()))

    monitor = PolymarketMonitor(store=PredictionArbitrageStore(tmp_path), trading=SimpleNamespace(), public_client_factory=Client)
    monitor._catalog_loaded = True

    async def scenario():
        entered = asyncio.Event()

        async def poll(_client):
            entered.set()
            if ending in {"crash", "cleanup_error"}:
                raise original
            await asyncio.Future()

        monkeypatch.setattr(monitor, "_poll_relation_validation", poll)
        if ending == "stop":
            monitor._stop_event.set()
        if ending == "cleanup_error":
            async def child():
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    raise ValueError("child cleanup failed")
            monitor._full_scan_task = asyncio.create_task(child())
            async def other_child():
                try:
                    await asyncio.Future()
                finally:
                    drained.append("activity")
            monitor._activity_scan_task = asyncio.create_task(other_child())
            await asyncio.sleep(0)
        task = asyncio.create_task(monitor._run_forever_once())
        if ending == "cancel":
            await entered.wait()
            task.cancel()
        if ending == "stop":
            await task
        else:
            error = asyncio.CancelledError if ending == "cancel" else RuntimeError
            with pytest.raises(error) as caught:
                await task
            if ending != "cancel":
                assert caught.value is original
        assert closed == owner
        assert monitor._client is None
        if ending == "cleanup_error":
            assert "prediction_monitor_task_cleanup_failed task=_full_scan_task" in caplog.text

    asyncio.run(_run_lifecycle_scenario(scenario()))


def test_monitor_close_failure_is_logged_and_propagated(tmp_path, caplog):
    original = OSError("client close failed")

    class Client:
        async def close(self):
            raise original

    monitor = PolymarketMonitor(store=PredictionArbitrageStore(tmp_path), trading=SimpleNamespace(), public_client_factory=Client)
    monitor._catalog_loaded = True
    monitor._stop_event.set()
    with caplog.at_level(logging.ERROR):
        with pytest.raises(OSError) as caught:
            asyncio.run(monitor._run_forever_once())
    assert caught.value is original
    assert "prediction_monitor_client_close_failed" in caplog.text


def test_monitor_close_failure_does_not_replace_active_failure(tmp_path, monkeypatch, caplog):
    original = ValueError("original monitor failure")
    class Client:
        async def close(self):
            raise OSError("client close failed")
    monitor = PolymarketMonitor(store=PredictionArbitrageStore(tmp_path), trading=SimpleNamespace(), public_client_factory=Client)
    monitor._catalog_loaded = True
    async def poll(_client):
        raise original
    monkeypatch.setattr(monitor, "_poll_relation_validation", poll)
    with pytest.raises(ValueError) as caught:
        asyncio.run(monitor._run_forever_once())
    assert caught.value is original
    assert "prediction_monitor_client_close_failed" in caplog.text


@pytest.mark.parametrize("pending", ["full", "catchup"])
@pytest.mark.parametrize("ending", ["crash", "cancel"])
def test_monitor_cleanup_does_not_spawn_replacement_scan(tmp_path, monkeypatch, pending, ending):
    clients = []
    closed = []

    class Client:
        def __init__(self):
            clients.append(self)

        async def close(self):
            closed.append(self)

    monitor = PolymarketMonitor(
        store=PredictionArbitrageStore(tmp_path), trading=SimpleNamespace(),
        public_client_factory=Client, relation_discovery=SimpleNamespace(),
    )
    monitor._catalog_loaded = True

    async def scenario():
        entered = asyncio.Event()

        async def scan(_client, **_kwargs):
            entered.set()
            await asyncio.Future()

        async def poll(client):
            monitor._activity_next_scan_at = None
            monitor._maybe_schedule_activity_scan(client)
            await entered.wait()
            monitor._full_scan_pending = pending == "full"
            monitor._activity_catchup_requested = pending == "catchup"
            if ending == "crash":
                raise RuntimeError("monitor crashed")
            asyncio.current_task().cancel()
            await asyncio.Future()

        monkeypatch.setattr(monitor, "_refresh_relation_activity", scan)
        monkeypatch.setattr(monitor, "_run_full_relation_scan", scan)
        monkeypatch.setattr(monitor, "_poll_relation_validation", poll)
        for attempt in range(2):
            entered.clear()
            with pytest.raises(RuntimeError if ending == "crash" else asyncio.CancelledError):
                await asyncio.create_task(monitor._run_forever_once())
            leaked = [task for task in (monitor._full_scan_task, monitor._activity_scan_task)
                      if task is not None and not task.done()]
            # Drain a failed reproduction too, so the test doesn't leak its own tasks.
            for task in leaked:
                task.cancel()
            await asyncio.gather(*leaked, return_exceptions=True)
            assert not leaked
            assert len(clients) == 2 * (attempt + 1)
            assert set(closed) == set(clients)
            assert not monitor._stop_event.is_set()

    asyncio.run(_run_lifecycle_scenario(scenario()))
