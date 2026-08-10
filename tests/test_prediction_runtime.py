from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import subprocess
from datetime import UTC, datetime
from decimal import Decimal
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
from open_trader.predict_cross_venue import (
    CodexCrossVenueEquivalenceValidator,
    ExplicitMarketPair,
    VenueMarket,
)
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore


def _shadow_cross_pair(index: int) -> ExplicitMarketPair:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    finish = datetime(2027, 1, 1, tzinfo=UTC)
    return ExplicitMarketPair(
        pair_id=f"shadow-pair-{index}",
        predict=VenueMarket(
            exchange="predict.fun",
            market_id="predict-market",
            condition_id="predict-condition",
            question=f"Test market {index}",
            rules=f"Predict rules {index}",
            event_start_at=now,
            event_end_at=finish,
            yes_token_id="predict-yes",
            no_token_id="predict-no",
            settlement_asset="USDT",
            minimum_order_size=Decimal("1"),
            tick_size=Decimal("0.01"),
            fee_rate_bps=Decimal("0"),
            rules_fingerprint="predict-fingerprint",
            category_slug="test",
            resolution_provider="test oracle",
        ),
        polymarket=VenueMarket(
            exchange="polymarket",
            market_id="poly-market",
            condition_id="poly-condition",
            question=f"Test market {index}",
            rules=f"Polymarket rules {index}",
            close_at=finish,
            settlement_at=finish,
            yes_token_id="poly-yes",
            no_token_id="poly-no",
            settlement_asset="USDC",
            minimum_order_size=Decimal("1"),
            tick_size=Decimal("0.01"),
            fee_rate_bps=Decimal("0"),
            rules_fingerprint="poly-fingerprint",
        ),
    )


def _shadow_cross_result() -> dict[str, object]:
    return {
        "schema_version": 2,
        "decision": "REJECT",
        "summary": "Not approved.",
        "predict": {
            "exchange": "predict.fun",
            "market_id": "predict-market",
            "condition_id": "predict-condition",
            "rules_fingerprint": "predict-fingerprint",
        },
        "polymarket": {
            "exchange": "polymarket",
            "market_id": "poly-market",
            "condition_id": "poly-condition",
            "rules_fingerprint": "poly-fingerprint",
        },
        "direct_outcome_mapping": {
            "predict_yes": "YES",
            "predict_no": "NO",
            "polymarket_yes": "YES",
            "polymarket_no": "NO",
        },
        "canonical_cutoff": "2027-01-01T00:00:00Z",
        "contract_shape": "BINARY",
        "divergent_states": {
            "PREDICT_YES_POLYMARKET_NO": {"possible": False, "reason": "same"},
            "POLYMARKET_YES_PREDICT_NO": {"possible": False, "reason": "same"},
        },
        "evidence": [],
        "uncertainties": ["ambiguous"],
    }


def _shadow_cross_jsonl() -> str:
    return "\n".join(
        (
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(_shadow_cross_result())}}),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1}}),
        )
    )


class _ShadowCrossRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(
        self, command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(command)
        return subprocess.CompletedProcess(
            command, 0, stdout=_shadow_cross_jsonl(), stderr=""
        )


def test_cross_venue_codex_rejects_negative_budget(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="non-negative"):
        CodexCrossVenueEquivalenceValidator(
            PredictionArbitrageStore(tmp_path), model="gpt-test", max_codex_calls=-1
        )


def test_cross_venue_codex_budget_caps_only_uncached_calls(tmp_path: Path) -> None:
    runner = _ShadowCrossRunner()
    fallback_calls: list[str] = []
    validator = CodexCrossVenueEquivalenceValidator(
        PredictionArbitrageStore(tmp_path),
        model="gpt-test",
        runner=runner,
        fallback_enabled=False,
        max_codex_calls=3,
        fallback=lambda *_: (fallback_calls.append("called") or None, "disabled"),
    )

    results = [validator.validate(_shadow_cross_pair(index)) for index in range(4)]

    assert len(runner.calls) == 3
    assert validator.codex_calls == 3
    assert validator.codex_successes == 3
    assert results[3].reason == "CODEX_BUDGET_EXHAUSTED"
    assert fallback_calls == []


def test_cross_venue_codex_cached_hit_does_not_consume_budget(tmp_path: Path) -> None:
    store = PredictionArbitrageStore(tmp_path)
    pair = _shadow_cross_pair(0)
    assert CodexCrossVenueEquivalenceValidator(store, model="gpt-test", runner=_ShadowCrossRunner()).validate(pair).approved is False
    runner = _ShadowCrossRunner()
    validator = CodexCrossVenueEquivalenceValidator(
        store, model="gpt-test", runner=runner, fallback_enabled=False, max_codex_calls=0
    )

    cached = validator.validate(pair)
    exhausted = validator.validate(_shadow_cross_pair(1))

    assert cached.reason == "LLM_REJECTED"
    assert exhausted.reason == "CODEX_BUDGET_EXHAUSTED"
    assert runner.calls == []
    assert validator.codex_calls == validator.codex_successes == 0


def test_cross_venue_codex_timeout_without_fallback_records_no_deepseek_usage(tmp_path: Path) -> None:
    fallback_calls: list[str] = []

    def timeout(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(command, 1)

    store = PredictionArbitrageStore(tmp_path)
    result = CodexCrossVenueEquivalenceValidator(
        store,
        model="gpt-test",
        runner=timeout,
        fallback_enabled=False,
        fallback=lambda *_: (fallback_calls.append("called") or None, "disabled"),
    ).validate(_shadow_cross_pair(0))

    assert result.reason == "CODEX_TIMEOUT"
    assert fallback_calls == []
    assert store.llm_usage_24h_by_provider().get("deepseek", {}) == {}


def test_cross_venue_codex_default_fallback_is_preserved(tmp_path: Path) -> None:
    fallback_calls: list[str] = []
    result = CodexCrossVenueEquivalenceValidator(
        PredictionArbitrageStore(tmp_path),
        model="gpt-test",
        runner=lambda command, **_kwargs: subprocess.CompletedProcess(command, 1, stdout="", stderr="failed"),
        fallback=lambda *_: (fallback_calls.append("called") or json.dumps(_shadow_cross_result()), None),
    ).validate(_shadow_cross_pair(0))

    assert result.reason == "LLM_REJECTED"
    assert fallback_calls == ["called"]


def test_cross_venue_codex_nonzero_exit_with_fallback_does_not_count_success(
    tmp_path: Path,
) -> None:
    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command, 1, stdout=_shadow_cross_jsonl(), stderr="failed"
        )

    validator = CodexCrossVenueEquivalenceValidator(
        PredictionArbitrageStore(tmp_path),
        model="gpt-test",
        runner=runner,
        fallback_enabled=False,
    )

    result = validator.validate(_shadow_cross_pair(0))

    assert result.reason == "CODEX_FAILED"
    assert validator.codex_calls == 1
    assert validator.codex_successes == 0


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
