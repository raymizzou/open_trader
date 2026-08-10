from __future__ import annotations

import asyncio
import multiprocessing
import os
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from open_trader.prediction_runtime import (
    PredictionRuntime,
    PredictionRuntimeOwnershipError,
    _CrossVenueRuntime,
    _RuntimeOwnershipLock,
)


def _hold_owner_lock(path: str, ready: object, release: object) -> None:
    lock = _RuntimeOwnershipLock(Path(path))
    lock.acquire()
    ready.set()  # type: ignore[attr-defined]
    release.wait(10)  # type: ignore[attr-defined]
    lock.release()


def _hold_owner_lock_then_exit(path: str, marker_path: str) -> None:
    lock = _RuntimeOwnershipLock(Path(path))
    lock.acquire()
    Path(marker_path).write_text("ready", encoding="utf-8")
    os._exit(0)


def _try_owner_lock(path: str, result: object) -> None:
    lock = _RuntimeOwnershipLock(Path(path))
    try:
        lock.acquire()
    except PredictionRuntimeOwnershipError:
        result.put("blocked")  # type: ignore[attr-defined]
        return
    result.put("acquired")  # type: ignore[attr-defined]
    lock.release()


def test_runtime_constructor_is_side_effect_free(tmp_path: Path) -> None:
    runtime = PredictionRuntime(
        data_dir=tmp_path,
        prediction_config_path=tmp_path / "prediction.json",
        dashboard_url="http://127.0.0.1:8766/",
    )

    assert runtime.state == "NEW"
    assert not (tmp_path / "prediction_arbitrage" / "runtime.lock").exists()
    assert runtime.store is None
    assert runtime.monitor is None
    assert runtime.cross_venue_monitor is None
    assert runtime.execution is None


def test_runtime_owner_lock_rejects_a_second_owner_until_release(
    tmp_path: Path,
) -> None:
    path = tmp_path / "prediction_arbitrage" / "runtime.lock"
    first = _RuntimeOwnershipLock(path)
    second = _RuntimeOwnershipLock(path)

    first.acquire()
    try:
        with pytest.raises(PredictionRuntimeOwnershipError):
            second.acquire()
    finally:
        first.release()

    second.acquire()
    second.release()


def test_runtime_owner_lock_excludes_a_real_second_process(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    path = tmp_path / "prediction_arbitrage" / "runtime.lock"
    ready = context.Event()
    release = context.Event()
    result = context.Queue()
    first = context.Process(
        target=_hold_owner_lock,
        args=(str(path), ready, release),
    )
    first.start()
    try:
        assert ready.wait(5)
        second = context.Process(target=_try_owner_lock, args=(str(path), result))
        second.start()
        second.join(5)
        assert second.exitcode == 0
        assert result.get(timeout=1) == "blocked"

        release.set()
        first.join(5)
        assert first.exitcode == 0

        third = context.Process(target=_try_owner_lock, args=(str(path), result))
        third.start()
        third.join(5)
        assert third.exitcode == 0
        assert result.get(timeout=1) == "acquired"
    finally:
        release.set()
        first.join(5)


def test_runtime_owner_lock_releases_after_owner_process_exit(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    path = tmp_path / "prediction_arbitrage" / "runtime.lock"
    marker = tmp_path / "owner-ready"
    first = context.Process(
        target=_hold_owner_lock_then_exit,
        args=(str(path), str(marker)),
    )
    first.start()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not marker.exists():
            time.sleep(0.05)
        assert marker.exists()
        first.join(5)
        assert first.exitcode == 0

        successor = _RuntimeOwnershipLock(path)
        successor.acquire()
        successor.release()
    finally:
        if first.is_alive():
            first.kill()
        first.join(5)


def test_runtime_starts_and_stops_prediction_resources_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_trader.prediction_runtime as runtime_module

    events: list[str] = []

    class FakeStore:
        def __init__(self, _data_dir: Path) -> None:
            events.append("store.open")

        def close(self) -> None:
            events.append("store.close")

    class FakeTrading:
        def close(self) -> None:
            events.append("trading.close")

    class FakeTradingClient:
        @classmethod
        def from_keychain(cls, _config: object) -> FakeTrading:
            return FakeTrading()

    class FakeMonitor:
        def __init__(self, **_: object) -> None:
            events.append("monitor.construct")

        def set_ready_observer(self, _observer: object) -> None:
            pass

        def set_observation_observer(self, _observer: object) -> None:
            pass

        def set_failure_observer(self, _observer: object) -> None:
            pass

        def start(self) -> None:
            events.append("polymarket.start")

        def stop(self) -> None:
            events.append("polymarket.stop")

    class FakeExecution:
        def __init__(self, **_: object) -> None:
            events.append("execution.construct")

        def reconcile_startup(self) -> dict[str, object]:
            events.append("reconcile")
            return {"status": "ready"}

        def notify_ready_opportunity(self, *_: object) -> dict[str, object]:
            return {"status": "ignored"}

        def notify_observation(self, *_: object) -> dict[str, object]:
            return {"status": "ignored"}

        def notify_monitor_failure(self, *_: object) -> dict[str, object]:
            return {"status": "ignored"}

        def set_cross_venue_monitor(self, _monitor: object) -> None:
            pass

        def close(self) -> None:
            events.append("execution.close")

    class FakeCrossMonitor:
        async def start(self) -> None:
            events.append("cross.start")

        async def stop(self) -> None:
            events.append("cross.stop")

        def snapshot(self) -> dict[str, object]:
            return {"status": "ready"}

    monkeypatch.setattr(
        runtime_module,
        "load_trading_config",
        lambda _path: SimpleNamespace(predict=None),
        raising=False,
    )
    monkeypatch.setattr(
        runtime_module,
        "PolymarketTradingClient",
        FakeTradingClient,
        raising=False,
    )
    monkeypatch.setattr(
        runtime_module,
        "PredictTradingClient",
        SimpleNamespace(from_keychain=lambda _config: None),
        raising=False,
    )
    monkeypatch.setattr(runtime_module, "PredictionArbitrageStore", FakeStore, raising=False)
    monkeypatch.setattr(runtime_module, "PolymarketMonitor", FakeMonitor, raising=False)
    monkeypatch.setattr(
        runtime_module, "PredictionExecutionService", FakeExecution, raising=False
    )
    monkeypatch.setattr(
        runtime_module, "CodexRelationValidator", lambda *_args, **_kwargs: object(), raising=False
    )
    monkeypatch.setattr(
        runtime_module, "CodexTitleTranslator", lambda *_args, **_kwargs: object(), raising=False
    )

    runtime = PredictionRuntime(
        data_dir=tmp_path,
        prediction_config_path=tmp_path / "prediction.json",
        dashboard_url="http://127.0.0.1:8766/",
        cross_venue_monitor=FakeCrossMonitor(),
    )
    runtime.start()
    try:
        assert runtime.state == "RUNNING"
        assert events.index("reconcile") < events.index("polymarket.start")
        assert events.index("polymarket.start") < events.index("cross.start")
        with pytest.raises(RuntimeError, match="cannot start from RUNNING"):
            runtime.start()
    finally:
        runtime.stop()
        runtime.stop()

    assert events.index("cross.stop") < events.index("polymarket.stop")
    assert events.index("polymarket.stop") < events.index("execution.close")
    assert events.index("execution.close") < events.index("trading.close")
    assert events.index("trading.close") < events.index("store.close")


def test_failed_runtime_is_terminal_and_stop_does_not_repeat_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_trader.prediction_runtime as runtime_module

    closed: list[str] = []

    class FakeStore:
        def __init__(self, _data_dir: Path) -> None:
            pass

        def close(self) -> None:
            closed.append("store")

    monkeypatch.setattr(runtime_module, "PredictionArbitrageStore", FakeStore)
    monkeypatch.setattr(
        runtime_module,
        "load_trading_config",
        lambda _path: (_ for _ in ()).throw(RuntimeError("bad config")),
    )
    runtime = PredictionRuntime(
        data_dir=tmp_path,
        prediction_config_path=tmp_path / "prediction.json",
        dashboard_url="http://127.0.0.1:8766/",
    )

    with pytest.raises(RuntimeError, match="bad config"):
        runtime.start()
    with pytest.raises(RuntimeError, match="cannot start from FAILED"):
        runtime.start()

    runtime.stop()
    runtime.stop()
    assert closed == ["store"]


def test_reconcile_failure_keeps_runtime_locked_and_does_not_start_monitors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_trader.prediction_runtime as runtime_module

    events: list[str] = []

    class FakeStore:
        def __init__(self, _data_dir: Path) -> None:
            pass

        def close(self) -> None:
            events.append("store.close")

    class FakeTrading:
        def close(self) -> None:
            events.append("trading.close")

    class FakeTradingClient:
        @classmethod
        def from_keychain(cls, _config: object) -> FakeTrading:
            return FakeTrading()

    class FakeMonitor:
        def __init__(self, **_: object) -> None:
            pass

        def set_ready_observer(self, _observer: object) -> None:
            pass

        def set_observation_observer(self, _observer: object) -> None:
            pass

        def set_failure_observer(self, _observer: object) -> None:
            pass

        def start(self) -> None:
            events.append("monitor.start")

        def stop(self) -> None:
            events.append("monitor.stop")

    class FakeExecution:
        def __init__(self, **_: object) -> None:
            pass

        def reconcile_startup(self) -> dict[str, object]:
            raise RuntimeError("reconcile failed")

        def notify_ready_opportunity(self, *_: object) -> dict[str, object]:
            return {"state": "ignored"}

        def notify_observation(self, *_: object) -> dict[str, object]:
            return {"state": "ignored"}

        def notify_monitor_failure(self, *_: object) -> dict[str, object]:
            return {"state": "ignored"}

        def set_cross_venue_monitor(self, _monitor: object) -> None:
            pass

    monkeypatch.setattr(runtime_module, "PredictionArbitrageStore", FakeStore)
    monkeypatch.setattr(runtime_module, "PolymarketTradingClient", FakeTradingClient)
    monkeypatch.setattr(
        runtime_module,
        "PredictTradingClient",
        SimpleNamespace(from_keychain=lambda _config: None),
    )
    monkeypatch.setattr(runtime_module, "load_trading_config", lambda _path: object())
    monkeypatch.setattr(runtime_module, "CodexRelationValidator", lambda *_a, **_k: object())
    monkeypatch.setattr(runtime_module, "CodexTitleTranslator", lambda *_a, **_k: object())
    monkeypatch.setattr(runtime_module, "PolymarketMonitor", FakeMonitor)
    monkeypatch.setattr(runtime_module, "PredictionExecutionService", FakeExecution)

    runtime = PredictionRuntime(
        data_dir=tmp_path,
        prediction_config_path=tmp_path / "prediction.json",
        dashboard_url="http://127.0.0.1:8766/",
    )
    runtime.start()

    assert runtime.state == "NOT_READY"
    assert "monitor.start" not in events
    competing_owner = _RuntimeOwnershipLock(
        tmp_path / "prediction_arbitrage" / "runtime.lock"
    )
    with pytest.raises(PredictionRuntimeOwnershipError):
        competing_owner.acquire()
    runtime.stop()
    competing_owner.acquire()
    competing_owner.release()


def test_locked_reconcile_result_keeps_runtime_not_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_trader.prediction_runtime as runtime_module

    events: list[str] = []

    class FakeTrading:
        def close(self) -> None:
            pass

    class FakeTradingClient:
        @classmethod
        def from_keychain(cls, _config: object) -> FakeTrading:
            return FakeTrading()

    class FakeMonitor:
        def __init__(self, **_: object) -> None:
            pass

        def set_ready_observer(self, _observer: object) -> None:
            pass

        def set_observation_observer(self, _observer: object) -> None:
            pass

        def set_failure_observer(self, _observer: object) -> None:
            pass

        def start(self) -> None:
            events.append("monitor.start")

        def stop(self) -> None:
            events.append("monitor.stop")

    class FakeExecution:
        def __init__(self, **_: object) -> None:
            pass

        def reconcile_startup(self) -> dict[str, object]:
            return {"state": "locked", "reason": "account_unavailable"}

        def notify_ready_opportunity(self, *_: object) -> None:
            pass

        def notify_observation(self, *_: object) -> None:
            pass

        def notify_monitor_failure(self, *_: object) -> None:
            pass

        def set_cross_venue_monitor(self, _monitor: object) -> None:
            pass

    monkeypatch.setattr(runtime_module, "PredictionArbitrageStore", lambda _dir: object())
    monkeypatch.setattr(runtime_module, "PolymarketTradingClient", FakeTradingClient)
    monkeypatch.setattr(
        runtime_module,
        "PredictTradingClient",
        SimpleNamespace(from_keychain=lambda _config: None),
    )
    monkeypatch.setattr(runtime_module, "load_trading_config", lambda _path: object())
    monkeypatch.setattr(runtime_module, "CodexRelationValidator", lambda *_a, **_k: object())
    monkeypatch.setattr(runtime_module, "CodexTitleTranslator", lambda *_a, **_k: object())
    monkeypatch.setattr(runtime_module, "PolymarketMonitor", FakeMonitor)
    monkeypatch.setattr(runtime_module, "PredictionExecutionService", FakeExecution)

    runtime = PredictionRuntime(
        data_dir=tmp_path,
        prediction_config_path=tmp_path / "prediction.json",
        dashboard_url="http://127.0.0.1:8766/",
    )
    runtime.start()
    try:
        assert runtime.state == "NOT_READY"
        assert events == []
    finally:
        runtime.stop()


def test_core_initialization_failure_releases_resources_and_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_trader.prediction_runtime as runtime_module

    closed: list[str] = []

    class FakeStore:
        def __init__(self, _data_dir: Path) -> None:
            pass

        def close(self) -> None:
            closed.append("store")

    monkeypatch.setattr(runtime_module, "PredictionArbitrageStore", FakeStore)
    monkeypatch.setattr(
        runtime_module,
        "load_trading_config",
        lambda _path: (_ for _ in ()).throw(RuntimeError("bad config")),
    )
    runtime = PredictionRuntime(
        data_dir=tmp_path,
        prediction_config_path=tmp_path / "prediction.json",
        dashboard_url="http://127.0.0.1:8766/",
    )

    with pytest.raises(RuntimeError, match="bad config"):
        runtime.start()

    assert runtime.state == "FAILED"
    assert closed == ["store"]
    owner = _RuntimeOwnershipLock(
        tmp_path / "prediction_arbitrage" / "runtime.lock"
    )
    owner.acquire()
    owner.release()


def test_cross_start_failure_degrades_only_cross_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_trader.prediction_runtime as runtime_module

    events: list[str] = []

    class FakeStore:
        def __init__(self, _data_dir: Path) -> None:
            pass

    class FakeTrading:
        def close(self) -> None:
            pass

    class FakeTradingClient:
        @classmethod
        def from_keychain(cls, _config: object) -> FakeTrading:
            return FakeTrading()

    class FakeMonitor:
        def __init__(self, **_: object) -> None:
            pass

        def set_ready_observer(self, _observer: object) -> None:
            pass

        def set_observation_observer(self, _observer: object) -> None:
            pass

        def set_failure_observer(self, _observer: object) -> None:
            pass

        def start(self) -> None:
            events.append("polymarket.start")

        def stop(self) -> None:
            events.append("polymarket.stop")

    class FakeExecution:
        def __init__(self, **_: object) -> None:
            pass

        def reconcile_startup(self) -> dict[str, object]:
            return {"state": "ready"}

        def notify_ready_opportunity(self, *_: object) -> None:
            pass

        def notify_observation(self, *_: object) -> None:
            pass

        def notify_monitor_failure(self, *_: object) -> None:
            pass

        def set_cross_venue_monitor(self, _monitor: object) -> None:
            pass

    class FailingCrossMonitor:
        async def start(self) -> None:
            raise RuntimeError("cross start failed")

        async def stop(self) -> None:
            pass

        def snapshot(self) -> dict[str, object]:
            return {"status": "degraded"}

    monkeypatch.setattr(runtime_module, "PredictionArbitrageStore", FakeStore)
    monkeypatch.setattr(runtime_module, "PolymarketTradingClient", FakeTradingClient)
    monkeypatch.setattr(
        runtime_module,
        "PredictTradingClient",
        SimpleNamespace(from_keychain=lambda _config: None),
    )
    monkeypatch.setattr(runtime_module, "load_trading_config", lambda _path: object())
    monkeypatch.setattr(runtime_module, "CodexRelationValidator", lambda *_a, **_k: object())
    monkeypatch.setattr(runtime_module, "CodexTitleTranslator", lambda *_a, **_k: object())
    monkeypatch.setattr(runtime_module, "PolymarketMonitor", FakeMonitor)
    monkeypatch.setattr(runtime_module, "PredictionExecutionService", FakeExecution)

    runtime = PredictionRuntime(
        data_dir=tmp_path,
        prediction_config_path=tmp_path / "prediction.json",
        dashboard_url="http://127.0.0.1:8766/",
        cross_venue_monitor=FailingCrossMonitor(),
    )
    runtime.start()
    try:
        assert runtime.state == "RUNNING"
        assert events == ["polymarket.start"]
        assert isinstance(
            runtime.cross_venue_monitor,
            runtime_module._UnavailableCrossVenueMonitor,
        )
    finally:
        runtime.stop()


def test_cross_runtime_start_timeout_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import open_trader.prediction_runtime as runtime_module

    class SlowCrossMonitor:
        async def start(self) -> None:
            await asyncio.sleep(0.05)

        async def stop(self) -> None:
            pass

    monkeypatch.setattr(runtime_module, "_CROSS_VENUE_START_TIMEOUT", 0.001)
    runtime = _CrossVenueRuntime(SlowCrossMonitor())

    with pytest.raises(RuntimeError, match="did not start"):
        runtime.start()
    assert not runtime.thread_alive


def test_cross_runtime_stop_failure_is_reported_and_keeps_owner_locked(
    tmp_path: Path,
) -> None:
    events: list[str] = []

    class FailingCrossMonitor:
        async def start(self) -> None:
            pass

        async def stop(self) -> None:
            events.append("cross.stop")
            raise RuntimeError("cross stop failed")

    class FakeResource:
        def __init__(self, name: str) -> None:
            self.name = name

        def close(self) -> None:
            events.append(f"{self.name}.close")

    class FakeMonitor:
        def stop(self) -> None:
            events.append("polymarket.stop")

    runtime = PredictionRuntime(
        data_dir=tmp_path,
        prediction_config_path=tmp_path / "prediction.json",
        dashboard_url="http://127.0.0.1:8766/",
    )
    runtime._owner.acquire()
    runtime._state = "RUNNING"
    runtime._cross_runtime = _CrossVenueRuntime(FailingCrossMonitor())
    runtime._cross_runtime.start()
    runtime.monitor = FakeMonitor()  # type: ignore[assignment]
    runtime.execution = FakeResource("execution")  # type: ignore[assignment]
    runtime._prediction_trading = FakeResource("trading")
    runtime._predict_trading = FakeResource("predict")
    runtime.store = FakeResource("store")  # type: ignore[assignment]

    try:
        with pytest.raises(RuntimeError, match="cleanup failed"):
            runtime.stop()
        assert runtime.state == "STOPPING"
        assert events == [
            "cross.stop",
            "polymarket.stop",
            "execution.close",
            "trading.close",
            "predict.close",
            "store.close",
        ]
        competing_owner = _RuntimeOwnershipLock(
            tmp_path / "prediction_arbitrage" / "runtime.lock"
        )
        with pytest.raises(PredictionRuntimeOwnershipError):
            competing_owner.acquire()
    finally:
        runtime._owner.release()
