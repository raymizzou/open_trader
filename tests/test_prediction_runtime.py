from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import subprocess
import threading
from contextlib import ExitStack, contextmanager
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
    assert runtime.mode == "production"
    assert runtime.production_owner is False
    assert not (tmp_path / "prediction_arbitrage" / "runtime.lock").exists()
    assert runtime.store is None
    assert runtime.monitor is None
    assert runtime.cross_venue_monitor is None
    assert runtime.execution is None


@pytest.mark.parametrize("reader_generation", (True, False, 0, -1))
def test_reader_generation_must_be_a_positive_integer(
    tmp_path: Path, reader_generation: object
) -> None:
    with pytest.raises(ValueError):
        PredictionRuntime(
            data_dir=tmp_path,
            prediction_config_path=tmp_path / "prediction.json",
            dashboard_url="http://127.0.0.1:8766/",
            reader_generation=reader_generation,  # type: ignore[arg-type]
        )


def test_prediction_safety_policy_contains_only_semantic_public_inputs() -> None:
    import open_trader.prediction_runtime as runtime_module

    policy = runtime_module._prediction_safety_policy(
        SimpleNamespace(
            signer_address="0x1111111111111111111111111111111111111111",
            wallet_address="0x2222222222222222222222222222222222222222",
            predict=SimpleNamespace(
                wallet_address="0x3333333333333333333333333333333333333333",
                environment="mainnet",
            ),
        )
    )

    assert policy == {
        "policy_version": "prediction-controls-v1",
        "identity": {
            "signer_address": "0x1111111111111111111111111111111111111111",
            "wallet_address": "0x2222222222222222222222222222222222222222",
            "predict_wallet_address": "0x3333333333333333333333333333333333333333",
            "predict_environment": "mainnet",
        },
        "limits": {
            "book_freshness_seconds": "10",
            "cross_auto_daily_principal_cap": "100",
            "max_cross_unsettled_principal": "100",
            "max_emergency_loss": "2.00",
            "max_normal_cost": "20.00",
            "max_wallet_balance": "65.00",
            "min_estimated_profit": "1.00",
            "min_threshold_annualized_yield": "0.15",
        },
    }


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
    original_acquire = runtime_module._RuntimeOwnershipLock.acquire
    original_release = runtime_module._RuntimeOwnershipLock.release

    def acquire(lock: object) -> None:
        original_acquire(lock)  # type: ignore[arg-type]
        events.append("owner.acquire")

    def release(lock: object) -> None:
        original_release(lock)  # type: ignore[arg-type]
        events.append("owner.release")

    class FakeStore:
        def __init__(self, _data_dir: Path) -> None:
            events.append("store.construct")
            events.append("store.open")

        def apply_safety_policy(
            self, policy: object, *, git_sha: str
        ) -> dict[str, object]:
            events.append("policy.apply")
            assert isinstance(policy, dict)
            assert git_sha == "sha-1"
            return {"state": "baseline_enrolled"}

        def close(self) -> None:
            events.append("store.close")

    class FakeTrading:
        def close(self) -> None:
            events.append("trading.close")

    class FakeTradingClient:
        @classmethod
        def from_keychain(cls, _config: object) -> FakeTrading:
            events.append("client.construct")
            return FakeTrading()

    class FakeMonitor:
        def __init__(self, **_: object) -> None:
            events.append("monitor.construct")

        def set_ready_observer(self, _observer: object) -> None:
            pass

        def set_observation_observer(self, _observer: object) -> None:
            pass

        def set_auto_eat_observer(self, _observer: object) -> None:
            events.append("auto_eat.bind")

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

        def auto_eat_threshold(self, *_: object) -> dict[str, object]:
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
        lambda _path: SimpleNamespace(
            signer_address="0x1111111111111111111111111111111111111111",
            wallet_address="0x2222222222222222222222222222222222222222",
            predict=None,
        ),
        raising=False,
    )
    monkeypatch.setattr(runtime_module._RuntimeOwnershipLock, "acquire", acquire)
    monkeypatch.setattr(runtime_module._RuntimeOwnershipLock, "release", release)
    monkeypatch.setattr(
        runtime_module,
        "read_minimum_reader_generation",
        lambda _data_dir: events.append("generation.read") or 1,
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
        git_sha="sha-1",
        reader_generation=1,
    )
    runtime.start()
    try:
        assert runtime.state == "RUNNING"
        assert runtime.production_owner is True
        assert events.index("owner.acquire") < events.index("generation.read")
        assert events.count("generation.read") == 1
        assert events.index("generation.read") < events.index("store.construct")
        assert events.index("store.construct") < events.index("client.construct")
        assert events.index("policy.apply") < events.index("execution.construct")
        assert events.index("auto_eat.bind") < events.index("reconcile")
        assert events.index("reconcile") < events.index("polymarket.start")
        assert events.index("polymarket.start") < events.index("cross.start")
        with pytest.raises(RuntimeError, match="cannot start from RUNNING"):
            runtime.start()
    finally:
        runtime.stop()
        runtime.stop()

    assert runtime.production_owner is False
    assert events.index("cross.stop") < events.index("polymarket.stop")
    assert events.index("polymarket.stop") < events.index("execution.close")
    assert events.index("execution.close") < events.index("trading.close")
    assert events.index("trading.close") < events.index("store.close")
    assert events.index("store.close") < events.index("owner.release")


def test_incompatible_release_stops_before_writable_resources_and_releases_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_trader.prediction_runtime as runtime_module

    constructed: list[str] = []
    probes: list[Path] = []
    monkeypatch.setattr(
        runtime_module,
        "read_minimum_reader_generation",
        lambda path: probes.append(path) or 2,
        raising=False,
    )
    monkeypatch.setattr(
        runtime_module,
        "PredictionArbitrageStore",
        lambda _path: constructed.append("store") or object(),
    )
    monkeypatch.setattr(
        runtime_module.PolymarketTradingClient,
        "from_keychain",
        lambda _config: constructed.append("client") or object(),
    )
    runtime = PredictionRuntime(
        data_dir=tmp_path,
        prediction_config_path=tmp_path / "prediction.json",
        dashboard_url="http://127.0.0.1:8769",
        reader_generation=1,
    )

    with pytest.raises(
        runtime_module.PredictionRuntimeCompatibilityError,
        match="reader generation 1 is below required 2",
    ):
        runtime.start()

    assert constructed == []
    assert probes == [tmp_path]
    assert runtime.production_owner is False
    probe = runtime_module._RuntimeOwnershipLock(
        tmp_path / "prediction_arbitrage" / "runtime.lock"
    )
    probe.acquire()
    probe.release()


def test_legacy_runtime_without_release_generation_skips_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_trader.prediction_runtime as runtime_module

    class FakeStore:
        def __init__(self, _data_dir: Path) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        runtime_module,
        "read_minimum_reader_generation",
        lambda _path: (_ for _ in ()).throw(AssertionError("generation probed")),
        raising=False,
    )
    monkeypatch.setattr(runtime_module, "PredictionArbitrageStore", FakeStore)
    monkeypatch.setattr(
        runtime_module,
        "load_trading_config",
        lambda _path: (_ for _ in ()).throw(RuntimeError("config reached")),
    )
    runtime = PredictionRuntime(
        data_dir=tmp_path,
        prediction_config_path=tmp_path / "prediction.json",
        dashboard_url="http://127.0.0.1:8766",
    )

    with pytest.raises(RuntimeError, match="config reached"):
        runtime.start()
    runtime.stop()


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

        def set_auto_eat_observer(self, _observer: object) -> None:
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

        def auto_eat_threshold(self, *_: object) -> dict[str, object]:
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
    assert runtime.production_owner is True
    assert "monitor.start" not in events
    competing_owner = _RuntimeOwnershipLock(
        tmp_path / "prediction_arbitrage" / "runtime.lock"
    )
    with pytest.raises(PredictionRuntimeOwnershipError):
        competing_owner.acquire()
    runtime.stop()
    assert runtime.production_owner is False
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

        def set_auto_eat_observer(self, _observer: object) -> None:
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

        def auto_eat_threshold(self, *_: object) -> None:
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

        def set_auto_eat_observer(self, _observer: object) -> None:
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

        def auto_eat_threshold(self, *_: object) -> None:
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


def test_shadow_runtime_stops_on_first_guard_violation_from_owner_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_trader.prediction_runtime as runtime_module

    events: list[str] = []
    network_calls: list[str] = []
    validator_kwargs: list[dict[str, object]] = []
    cross_kwargs: list[dict[str, object]] = []

    class FakeStore:
        def __init__(self, _data_dir: Path) -> None:
            events.append("shadow_store.open")

        def apply_safety_policy(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("shadow must not enroll a production safety policy")

        def close(self) -> None:
            events.append("shadow_store.close")

    class FakePolymarketClient:
        def cancel_all(self) -> None:
            network_calls.append("cancel_all")

        def place_order(self) -> None:
            network_calls.append("place_order")

        def close(self) -> None:
            events.append("polymarket.close")

    class FakePolymarketTradingClient:
        @classmethod
        def from_keychain(cls, _config: object) -> FakePolymarketClient:
            events.append("clients.open")
            return FakePolymarketClient()

    class FakePredictClient:
        @classmethod
        def from_keychain(cls, _config: object) -> object:
            return object()

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
        def __init__(self, **kwargs: object) -> None:
            assert isinstance(kwargs["notifier"], runtime_module.NullNotifier)

        def reconcile_startup(self) -> None:
            raise AssertionError("shadow must not reconcile")

        def notify_ready_opportunity(self, *_: object) -> None:
            raise AssertionError("shadow must not notify")

        notify_observation = notify_ready_opportunity
        notify_monitor_failure = notify_ready_opportunity

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

    class Guard:
        def __init__(self, on_violation: object) -> None:
            self.on_violation = on_violation
            self.attempts: list[dict[str, object]] = []

        def violation(self, method: str) -> None:
            attempt = {
                "venue": "polymarket",
                "kind": "mutation",
                "method": method,
                "call_chain": [f"frame-{index}" for index in range(20)],
                "api_key": "must-not-leak",
            }
            self.attempts.append(attempt)
            self.on_violation(attempt)  # type: ignore[operator]
            raise RuntimeError("blocked")

    @contextmanager
    def fake_guard_polymarket(client: FakePolymarketClient, guard: Guard):
        events.append("guards.enter")
        original = client.cancel_all, client.place_order
        client.cancel_all = lambda: guard.violation("cancel_all")
        client.place_order = lambda: guard.violation("place_order")
        try:
            yield
        finally:
            client.cancel_all, client.place_order = original
            events.append("guards.exit")

    @contextmanager
    def fake_guard_predict(_client: object, _guard: Guard):
        events.append("predict_guard.enter")
        yield
        events.append("predict_guard.exit")

    class Owner:
        def acquire(self) -> None:
            events.append("shadow_owner.acquire")

        def release(self) -> None:
            events.append("shadow_owner.release")

    monkeypatch.setattr(runtime_module, "PredictionArbitrageStore", FakeStore)
    monkeypatch.setattr(runtime_module, "PolymarketTradingClient", FakePolymarketTradingClient)
    monkeypatch.setattr(runtime_module, "PredictTradingClient", FakePredictClient)
    monkeypatch.setattr(runtime_module, "load_trading_config", lambda _path: object())
    monkeypatch.setattr(runtime_module, "PolymarketMonitor", FakeMonitor)
    monkeypatch.setattr(runtime_module, "PredictionExecutionService", FakeExecution)
    monkeypatch.setattr(
        runtime_module,
        "CodexRelationValidator",
        lambda *_a, **kwargs: (validator_kwargs.append(kwargs) or object()),
    )
    monkeypatch.setattr(runtime_module, "CodexTitleTranslator", lambda *_a, **_k: object())
    monkeypatch.setattr(
        runtime_module,
        "_build_cross_venue_monitor",
        lambda **kwargs: (cross_kwargs.append(kwargs) or FakeCrossMonitor()),
    )
    monkeypatch.setattr(runtime_module, "PolymarketReadOnlyGuard", Guard)
    monkeypatch.setattr(runtime_module, "PredictReadOnlyGuard", Guard)
    monkeypatch.setattr(runtime_module, "guard_polymarket_client", fake_guard_polymarket)
    monkeypatch.setattr(runtime_module, "guard_predict_client", fake_guard_predict)

    runtime = PredictionRuntime(
        data_dir=tmp_path / "shadow",
        prediction_config_path=tmp_path / "prediction.json",
        dashboard_url="http://127.0.0.1:8766/",
        mode="shadow",
    )
    runtime._owner = Owner()  # type: ignore[assignment]
    runtime.start()
    assert runtime.mode == "shadow"
    assert runtime.production_owner is False

    with pytest.raises(RuntimeError, match="blocked"):
        runtime._prediction_trading.cancel_all()  # type: ignore[union-attr]
    with pytest.raises(RuntimeError, match="blocked"):
        runtime._prediction_trading.place_order()  # type: ignore[union-attr]
    assert network_calls == []
    callback_result: list[dict[str, object] | None] = []
    callback_thread = threading.Thread(
        target=lambda: callback_result.append(runtime.poll_shadow_failure())
    )
    callback_thread.start()
    callback_thread.join()
    assert callback_result == [None]
    assert runtime.state == "RUNNING"
    assert runtime.poll_shadow_failure() == {
        "venue": "polymarket",
        "kind": "mutation",
        "method": "cancel_all",
        "call_chain": [f"frame-{index}" for index in range(12)],
    }
    assert runtime.state == "STOPPED"
    assert runtime.shadow_evidence["guard_attempts"][0]["method"] == "cancel_all"
    assert runtime.shadow_evidence["guard_attempts"][1]["method"] == "place_order"
    assert validator_kwargs[0]["fallback_enabled"] is False
    assert validator_kwargs[0]["max_codex_calls"] == 3
    assert cross_kwargs[0]["holding_reconciler"] is None
    assert events == [
        "shadow_owner.acquire", "shadow_store.open", "clients.open", "guards.enter",
        "predict_guard.enter", "monitor.start", "cross.start", "cross.stop", "monitor.stop",
        "predict_guard.exit", "guards.exit",
        "execution.close", "polymarket.close", "shadow_store.close", "shadow_owner.release",
    ]


def test_shadow_cleanup_retains_lock_and_guards_when_monitor_thread_survives(
    tmp_path: Path,
) -> None:
    import open_trader.prediction_runtime as runtime_module

    release = threading.Event()
    exited: list[bool] = []

    class FakeMonitor:
        def __init__(self) -> None:
            self._thread = threading.Thread(target=release.wait, daemon=True)
            self._thread.start()

        def stop(self) -> None:
            pass

    class FakeResource:
        def close(self) -> None:
            pass

    runtime = PredictionRuntime(
        data_dir=tmp_path,
        prediction_config_path=tmp_path / "prediction.json",
        dashboard_url="http://127.0.0.1:8766/",
        mode="shadow",
    )
    runtime._owner.acquire()
    runtime._state = "RUNNING"
    monitor = FakeMonitor()
    runtime.monitor = monitor  # type: ignore[assignment]
    runtime.execution = FakeResource()  # type: ignore[assignment]
    runtime._prediction_trading = FakeResource()
    runtime.store = FakeResource()  # type: ignore[assignment]
    runtime._shadow_guards = ExitStack()

    @contextmanager
    def guard_scope():
        try:
            yield
        finally:
            exited.append(True)

    runtime._shadow_guards.enter_context(guard_scope())

    try:
        with pytest.raises(RuntimeError, match="cleanup failed"):
            runtime.stop()
        assert runtime.state == "STOPPING"
        assert exited == []
        competing_owner = runtime_module._RuntimeOwnershipLock(
            tmp_path / "prediction_arbitrage" / "runtime.lock"
        )
        with pytest.raises(PredictionRuntimeOwnershipError):
            competing_owner.acquire()
        release.set()
        runtime.monitor._thread.join(1)  # type: ignore[union-attr]
        runtime.stop()
        assert exited == [True]
    finally:
        release.set()
        monitor._thread.join(1)
        runtime._owner.release()
