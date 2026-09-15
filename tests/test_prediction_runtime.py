from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
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
    _UnavailableCrossVenueMonitor,
    _RuntimeOwnershipLock,
)
from open_trader.llm_providers import PROVIDER_IDS, LlmCompletion
from open_trader.predict_cross_venue import (
    LlmCrossVenueEquivalenceValidator,
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


SHADOW_CROSS_USAGE: dict[str, int] = {
    "input_tokens": 1,
    "cached_input_tokens": 0,
    "output_tokens": 0,
    "reasoning_output_tokens": 0,
}


def _shadow_cross_completer(*, reason: str | None = None):
    calls: list[tuple[str, str]] = []

    def complete(system: str, user: str) -> LlmCompletion:
        calls.append((system, user))
        if reason is not None:
            return LlmCompletion(None, reason, dict(SHADOW_CROSS_USAGE))
        return LlmCompletion(
            json.dumps(_shadow_cross_result()), None, dict(SHADOW_CROSS_USAGE)
        )

    return complete, calls


def _shadow_completers(complete) -> dict[str, object]:
    return {provider: complete for provider in PROVIDER_IDS}


def test_cross_venue_llm_rejects_negative_budget(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="non-negative"):
        LlmCrossVenueEquivalenceValidator(
            PredictionArbitrageStore(tmp_path), max_llm_calls=-1
        )


def test_cross_venue_llm_budget_caps_only_uncached_calls(tmp_path: Path) -> None:
    complete, calls = _shadow_cross_completer()
    validator = LlmCrossVenueEquivalenceValidator(
        PredictionArbitrageStore(tmp_path),
        default_provider="codex",
        completers=_shadow_completers(complete),
        max_llm_calls=3,
    )

    results = [validator.validate(_shadow_cross_pair(index)) for index in range(4)]

    assert len(calls) == 3
    assert validator.llm_calls == 3
    assert validator.llm_successes == 3
    assert results[3].reason == "CODEX_BUDGET_EXHAUSTED"


def test_cross_venue_llm_cached_hit_does_not_consume_budget(tmp_path: Path) -> None:
    store = PredictionArbitrageStore(tmp_path)
    pair = _shadow_cross_pair(0)
    seed, _seed_calls = _shadow_cross_completer()
    assert LlmCrossVenueEquivalenceValidator(
        store,
        default_provider="codex",
        completers=_shadow_completers(seed),
    ).validate(pair).approved is False

    complete, calls = _shadow_cross_completer()
    validator = LlmCrossVenueEquivalenceValidator(
        store,
        default_provider="codex",
        completers=_shadow_completers(complete),
        max_llm_calls=0,
    )

    cached = validator.validate(pair)
    exhausted = validator.validate(_shadow_cross_pair(1))

    assert cached.reason == "LLM_REJECTED"
    assert exhausted.reason == "CODEX_BUDGET_EXHAUSTED"
    assert calls == []
    assert validator.llm_calls == validator.llm_successes == 0


def test_cross_venue_llm_timeout_is_strict_without_fallback(tmp_path: Path) -> None:
    complete, calls = _shadow_cross_completer(reason="CODEX_TIMEOUT")

    store = PredictionArbitrageStore(tmp_path)
    result = LlmCrossVenueEquivalenceValidator(
        store,
        default_provider="codex",
        completers=_shadow_completers(complete),
    ).validate(_shadow_cross_pair(0))

    assert result.approved is False
    assert result.reason == "CODEX_TIMEOUT"
    assert len(calls) == 1
    assert store.llm_usage_24h_by_provider().get("deepseek", {}) == {}
    assert store.llm_usage_24h_by_provider().get("zhipu", {}) == {}


def test_cross_venue_llm_failure_does_not_count_success(tmp_path: Path) -> None:
    complete, calls = _shadow_cross_completer(reason="CODEX_FAILED")

    validator = LlmCrossVenueEquivalenceValidator(
        PredictionArbitrageStore(tmp_path),
        default_provider="codex",
        completers=_shadow_completers(complete),
    )

    result = validator.validate(_shadow_cross_pair(0))

    assert result.reason == "CODEX_FAILED"
    assert len(calls) == 1
    assert validator.llm_calls == 1
    assert validator.llm_successes == 0


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


def test_lp_restart_owns_one_session_and_preserves_monitoring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restart resumes the durable session without starting a second loop."""

    import open_trader.prediction_runtime as runtime_module

    store = PredictionArbitrageStore(tmp_path)
    store.lp_create_session(
        "lp-session",
        "lp-key",
        state="entry_open",
        payload={
            "market_id": "market-1",
            "token_id": "token-1",
            "outcome": "YES",
            "quantity": Decimal("10"),
            "residual_quantity": Decimal("5"),
            "review_at": "2026-09-15T00:00:00Z",
            "scoring_status": "unknown",
        },
    )

    class FakeLP:
        def __init__(self) -> None:
            self.tick_calls = 0
            self.session_ids: list[str] = []
            self.tick_seen = threading.Event()

        def tick(self) -> dict[str, object]:
            active = store.lp_active_session()
            assert active is not None
            self.tick_calls += 1
            self.session_ids.append(str(active["session_id"]))
            self.tick_seen.set()
            return {"state": str(active["state"]), "session_id": active["session_id"]}

    class FakeExecution:
        def __init__(self, lp: FakeLP) -> None:
            self.lp = lp

        def lp_tick(self) -> dict[str, object]:
            return self.lp.tick()

    first = PredictionRuntime(
        data_dir=tmp_path,
        prediction_config_path=tmp_path / "prediction.json",
        dashboard_url="http://127.0.0.1:8766/",
    )
    second = PredictionRuntime(
        data_dir=tmp_path,
        prediction_config_path=tmp_path / "prediction.json",
        dashboard_url="http://127.0.0.1:8766/",
    )
    first_lp = FakeLP()
    second_lp = FakeLP()
    first.store = store
    first.lp = first_lp  # type: ignore[assignment]
    first.execution = FakeExecution(first_lp)  # type: ignore[assignment]
    second.store = store
    second.lp = second_lp  # type: ignore[assignment]
    second.execution = FakeExecution(second_lp)  # type: ignore[assignment]
    monkeypatch.setattr(runtime_module, "_LP_TICK_SECONDS", 0.01)

    try:
        first._owner.acquire()
        first._start_lp_monitor()
        first_thread = first._lp_thread
        assert first_thread is not None
        assert first_lp.tick_seen.wait(timeout=2)
        first._start_lp_monitor()
        assert first._lp_thread is first_thread
        assert first_lp.session_ids == ["lp-session"]
        assert store.lp_active_session()["session_id"] == "lp-session"  # type: ignore[index]

        with pytest.raises(PredictionRuntimeOwnershipError):
            second._owner.acquire()
        assert store.lp_active_session()["session_id"] == "lp-session"  # type: ignore[index]

        first._lp_stop_event.set()
        first_thread.join(timeout=2)
        assert not first_thread.is_alive()
        first._lp_thread = None
        first._owner.release()

        second._owner.acquire()
        second._start_lp_monitor()
        second_thread = second._lp_thread
        assert second_thread is not None
        assert second_lp.tick_seen.wait(timeout=2)
        assert second_lp.session_ids == ["lp-session"]
        assert store.lp_active_session()["session_id"] == "lp-session"  # type: ignore[index]
    finally:
        for runtime in (first, second):
            runtime._lp_stop_event.set()
            thread = runtime._lp_thread
            if thread is not None:
                thread.join(timeout=2)
                runtime._lp_thread = None
            runtime._owner.release()


def test_lp_report_waits_for_restart_reconciliation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A late report uses fills discovered by the restart's first LP tick."""
    import open_trader.polymarket_lp as lp_module
    import open_trader.prediction_arbitrage_execution as execution_module
    import open_trader.prediction_runtime as runtime_module

    clock_time = datetime(2026, 9, 15, 0, 3, tzinfo=UTC)
    cutoff = datetime(2026, 9, 15, 0, 0, tzinfo=UTC)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> FrozenDateTime:
            fixed = cls.fromtimestamp(clock_time.timestamp(), tz=tz or UTC)
            return fixed if tz is not None else fixed.replace(tzinfo=None)

    now = FrozenDateTime(2026, 9, 15, 0, 3, tzinfo=UTC)

    class Trading:
        def __init__(self) -> None:
            self.snapshot_started = threading.Event()
            self.release_snapshot = threading.Event()
            self.candidate_catalog_started = threading.Event()

        def lp_snapshot(self, _request: dict[str, object]) -> dict[str, object]:
            self.snapshot_started.set()
            assert self.release_snapshot.wait(timeout=5)
            return {
                "account": {
                    "authenticated": True,
                    "balance": Decimal("100"),
                    "allowance": Decimal("100"),
                    "positions": [{"token_id": "token-1", "size": Decimal("12")}],
                    "open_orders": [{"order_id": "exit-order", "token_id": "token-1", "side": "SELL"}],
                },
                "market": {
                    "market_id": "market-1",
                    "condition_id": "condition-1",
                    "token_id": "token-1",
                    "outcome": "YES",
                    "accepting_orders": True,
                    "minimum_order_size": Decimal("1"),
                    "tick_size": Decimal("0.01"),
                    "fee": Decimal("0"),
                    "fees_enabled": True,
                    "taker_fee_rate": Decimal("0"),
                },
                "book": {
                    "received_at": now,
                    "bids": [{"price": Decimal("0.48"), "size": Decimal("100")}],
                    "asks": [],
                },
                "trades": [
                    {
                        "trade_id": "buy-1",
                        "matched_at": FrozenDateTime(2026, 9, 14, 14, 0, tzinfo=UTC),
                        "status": "CONFIRMED",
                        "maker_orders": [{
                            "order_id": "entry-order", "token_id": "token-1",
                            "side": "BUY", "matched_amount": Decimal("20"),
                            "price": Decimal("0.50"), "fee": Decimal("0.02"),
                        }],
                    },
                    {
                        "trade_id": "sell-1",
                        "matched_at": FrozenDateTime(2026, 9, 14, 23, 45, tzinfo=UTC),
                        "status": "CONFIRMED",
                        "maker_orders": [{
                            "order_id": "exit-order", "token_id": "token-1",
                            "side": "SELL", "matched_amount": Decimal("8"),
                            "price": Decimal("0.55"), "fee": Decimal("0.01"),
                        }],
                    },
                ],
                "orders": [
                    {"order_id": "entry-order", "token_id": "token-1", "side": "BUY", "status": "FILLED"},
                    {"order_id": "exit-order", "token_id": "token-1", "side": "SELL", "status": "LIVE"},
                ],
            }

        def account_snapshot(self) -> dict[str, object]:
            return {
                "wallet_address": "0x2222222222222222222222222222222222222222",
                "p_usd_balance": Decimal("100"),
                "p_usd_allowance": Decimal("100"),
                "open_order_ids": [],
                "positions": [],
                "checked_at": now,
            }

        def readiness_snapshot(self) -> dict[str, object]:
            return {"relayer_ready": True, "merge_ready": True, "checked_at": now}

        def lp_reward_catalog(self, **_kwargs: object) -> dict[str, object]:
            self.candidate_catalog_started.set()
            return {"state": "known", "complete": True, "checked_at": now, "markets": []}

        def lp_reward_snapshot(self, reward_date: str, condition_id: str, **_kwargs: object) -> dict[str, object]:
            return {"state": "unknown", "reward_date": reward_date, "condition_id": condition_id}

        def get_order_scoring(self, _order_id: str) -> object:
            return None

        def cancel_order(self, _order_id: str) -> bool:
            return False

        def close(self) -> None:
            pass

    monkeypatch.setattr(lp_module, "datetime", FrozenDateTime)
    monkeypatch.setattr(execution_module, "datetime", FrozenDateTime)
    monkeypatch.setattr(runtime_module, "_LP_TICK_SECONDS", 0.01)
    seed_store = PredictionArbitrageStore(tmp_path)
    seed_store.lp_create_session(
        "restart-report-session",
        "restart-report-idempotency",
        state="entry_open",
        payload={
            "market_id": "market-1", "condition_id": "condition-1",
            "token_id": "token-1", "outcome": "YES",
            "price": Decimal("0.50"), "quantity": Decimal("20"),
            "review_at": cutoff, "entry_order_id": "entry-order",
            "passive_exit_order_id": "exit-order",
            "owned_order_ids": ["entry-order", "exit-order"],
            "order_history": {
                "entry-order": {"token_id": "token-1", "side": "BUY", "status": "LIVE"},
                "exit-order": {"token_id": "token-1", "side": "SELL", "status": "LIVE"},
            },
            "trade_events": [], "verified_paid_reward_events": [],
            "buy_filled_quantity": Decimal("0"), "buy_cost": Decimal("0"),
            "buy_fees": Decimal("0"), "sold_quantity": Decimal("0"),
            "sold_revenue": Decimal("0"), "sell_fees": Decimal("0"),
            "residual_quantity": Decimal("0"), "residual_exit_value": Decimal("0"),
            "account_checked_at": "2026-09-14T23:59:00Z",
            "book_checked_at": "2026-09-14T23:59:00Z",
        },
    )
    trading = Trading()
    config = SimpleNamespace(
        signer_address="0x1111111111111111111111111111111111111111",
        wallet_address="0x2222222222222222222222222222222222222222",
        predict=None,
    )

    class MacOSNotifier:
        pass

    class FeishuNotifier:
        pass

    monkeypatch.setattr(runtime_module, "load_trading_config", lambda _path: config)
    monkeypatch.setattr(
        runtime_module, "PolymarketTradingClient",
        SimpleNamespace(from_keychain=lambda _config: trading), raising=False,
    )
    monkeypatch.setattr(
        runtime_module, "PredictTradingClient",
        SimpleNamespace(from_keychain=lambda _config: None), raising=False,
    )
    runtime = PredictionRuntime(
        data_dir=tmp_path,
        prediction_config_path=tmp_path / "prediction.json",
        dashboard_url="http://127.0.0.1:8766/",
        notifier=SimpleNamespace(_notifiers=(MacOSNotifier(), FeishuNotifier())),
        cross_venue_monitor=_UnavailableCrossVenueMonitor("test-disabled"),
        solver_server_factory=lambda: object(),
        enable_n_leg_background=False,
    )
    runtime.start()
    try:
        assert runtime.state == "RUNNING"
        assert runtime.store is not None
        assert trading.snapshot_started.wait(timeout=2)
        assert trading.candidate_catalog_started.wait(timeout=2)
        assert runtime.store.lp_daily_report("2026-09-15") is None
        trading.release_snapshot.set()
        deadline = time.monotonic() + 3
        report = None
        pending_session = None
        while time.monotonic() < deadline:
            report = runtime.store.lp_daily_report("2026-09-15")
            pending_session = runtime.store.lp_active_session()
            if (
                report is not None
                and pending_session is not None
                and pending_session.get("state") == "needs_attention"
                and pending_session.get("review_status") == "awaiting_reconciliation"
            ):
                break
            time.sleep(0.01)
        assert pending_session is not None
        assert pending_session["state"] == "needs_attention"
        assert pending_session["review_status"] == "awaiting_reconciliation"
        assert str(pending_session["reconciliation"]).startswith("deadline_cancel_")
        assert report is not None
        session = report["sessions"][0]
        assert Decimal(str(session["realized_trade_pnl"])) == Decimal("0.382")
        assert session["residual_quantity_at_period_end"] == "12"
        assert report["generated_at"] == "2026-09-15T00:03:00Z"
        assert report["cutoff_market_data_status"] == "unknown"
    finally:
        trading.release_snapshot.set()
        runtime.stop()

    reopened = PredictionArbitrageStore(tmp_path)
    assert reopened.lp_daily_report("2026-09-15") == report


def test_lp_dashboard_refresh_cannot_block_risk_monitor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blocked server-side candidate refresh stays outside risk and UI lifecycles."""
    import open_trader.prediction_runtime as runtime_module

    class RewardProbe:
        def __init__(self) -> None:
            self.reward_started = threading.Event()
            self.release_reward = threading.Event()
            self.release_after_stop = threading.Event()
            self.stop_observed = threading.Event()
            self.reward_finished = threading.Event()
            self.catalog_started = threading.Event()
            self.release_catalog = threading.Event()
            self.catalog_finished = threading.Event()
            self.risk_snapshot_started = threading.Event()
            self.risk_reconciled = threading.Event()
            self.close_called = threading.Event()
            self.monitor_stopped = threading.Event()
            self.observation_stopped = threading.Event()
            self.calls = 0
            self.active = 0
            self.max_active = 0
            self.catalog_state = "unknown"
            self.catalog_calls = 0
            self.catalog_active = 0
            self.catalog_max_active = 0
            self._lock = threading.Lock()

    class FakeTrading:
        def __init__(self, probe: RewardProbe) -> None:
            self.probe = probe

        def lp_snapshot(self, _request: dict[str, object]) -> dict[str, object]:
            if (
                self.probe.catalog_started.is_set()
                or self.probe.reward_started.is_set()
            ):
                self.probe.risk_snapshot_started.set()
            return {
                "account": {
                    "authenticated": True,
                    "open_orders": [
                        {
                            "order_id": "entry-1",
                            "token_id": "token-1",
                            "market_id": "market-1",
                            "side": "BUY",
                            "status": "LIVE",
                        }
                    ],
                    "positions": [],
                },
                "book": {
                    "received_at": datetime.now(UTC),
                    "bids": [],
                },
                "trades": [],
                "orders": [
                    {
                        "order_id": "entry-1",
                        "token_id": "token-1",
                        "side": "BUY",
                        "status": "LIVE",
                    }
                ],
            }

        def account_snapshot(self) -> dict[str, object]:
            return {
                "wallet_address": "0x2222222222222222222222222222222222222222",
                "p_usd_balance": Decimal("100"),
                "p_usd_allowance": Decimal("100"),
                "open_order_ids": ["entry-1"],
                "positions": [],
                "checked_at": datetime.now(UTC),
            }

        def readiness_snapshot(self) -> dict[str, object]:
            return {
                "relayer_ready": True,
                "merge_ready": True,
                "checked_at": datetime.now(UTC),
            }

        def get_order_scoring(self, _order_id: str) -> object:
            return None

        def lp_reward_catalog(
            self, *, stop_event: threading.Event | None = None
        ) -> dict[str, object]:
            with self.probe._lock:
                self.probe.catalog_calls += 1
                self.probe.catalog_active += 1
                self.probe.catalog_max_active = max(
                    self.probe.catalog_max_active,
                    self.probe.catalog_active,
                )
            self.probe.catalog_started.set()
            try:
                if self.probe.catalog_state == "unknown":
                    assert self.probe.release_catalog.wait(timeout=5)
                    return {
                        "state": "unknown",
                        "complete": False,
                        "checked_at": datetime.now(UTC),
                        "daily_pool_usd": None,
                        "markets": (),
                    }
                return {
                    "state": "known",
                    "complete": True,
                    "checked_at": datetime.now(UTC),
                    "daily_pool_usd": Decimal("0"),
                    "markets": (),
                }
            finally:
                with self.probe._lock:
                    self.probe.catalog_active -= 1
                self.probe.catalog_finished.set()

        def lp_reward_snapshot(
            self,
            reward_date: str,
            condition_id: str,
            *,
            stop_event: threading.Event | None = None,
        ) -> dict[str, object]:
            assert reward_date == "2026-09-14"
            assert condition_id == "condition-1"
            with self.probe._lock:
                self.probe.calls += 1
                self.probe.active += 1
                self.probe.max_active = max(
                    self.probe.max_active, self.probe.active
                )
            self.probe.reward_started.set()
            try:
                if stop_event is None:
                    assert self.probe.release_reward.wait(timeout=5)
                else:
                    assert stop_event.wait(timeout=5)
                    self.probe.stop_observed.set()
                    assert self.probe.release_after_stop.wait(timeout=5)
                return {
                    "state": "known",
                    "reward_date": reward_date,
                    "condition_id": condition_id,
                    "account_amount": Decimal("0.80"),
                    "market_amount": Decimal("0.62"),
                }
            finally:
                with self.probe._lock:
                    self.probe.active -= 1
                self.probe.reward_finished.set()

        def close(self) -> None:
            assert self.probe.reward_finished.is_set()
            self.probe.close_called.set()

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
            pass

        def stop(self) -> None:
            probe_holder[0].monitor_stopped.set()

    class FakeObservationMonitor:
        def __init__(self, **_: object) -> None:
            pass

        def start(self) -> None:
            pass

        def stop(self) -> None:
            probe_holder[0].observation_stopped.set()

    class MacOSNotifier:
        pass

    class FeishuNotifier:
        pass

    class TestNotifier:
        def __init__(self) -> None:
            self._notifiers = (MacOSNotifier(), FeishuNotifier())

    seed_store = PredictionArbitrageStore(tmp_path)
    seed_store.lp_create_session(
        "runtime-reward-session",
        "runtime-reward-idempotency",
        state="entry_open",
        payload={
            "market_id": "market-1",
            "condition_id": "condition-1",
            "token_id": "token-1",
            "outcome": "YES",
            "price": Decimal("0.30"),
            "quantity": Decimal("1"),
            "review_at": "2099-09-15T00:00:00Z",
            "entry_order_id": "entry-1",
            "entry_expiration": 4102444800,
            "owned_order_ids": ["entry-1"],
            "order_history": {
                "entry-1": {
                    "order_id": "entry-1",
                    "token_id": "token-1",
                    "side": "BUY",
                    "status": "LIVE",
                }
            },
            "buy_filled_quantity": Decimal("0"),
            "buy_cost": Decimal("0"),
            "sold_quantity": Decimal("0"),
            "sold_revenue": Decimal("0"),
            "residual_quantity": Decimal("0"),
            "residual_exit_value": Decimal("0"),
            "fees": Decimal("0"),
            "fee_status": "known",
            "position_reconciled": False,
            "orders_terminal": False,
            "entry_cancel_requested": False,
            "stop_loss_latched": False,
            "scoring_status": "unknown",
            "scoring_checked_at": None,
            "reward_date": "2026-09-14",
            "trade_pnl": Decimal("0"),
            "paid_rewards": Decimal("0"),
        },
    )

    probe_holder = [RewardProbe()]
    monkeypatch.setattr(runtime_module, "_LP_TICK_SECONDS", 0.01)
    monkeypatch.setattr(runtime_module, "_LP_REWARD_SECONDS", 0.01, raising=False)
    monkeypatch.setattr(
        runtime_module,
        "load_trading_config",
        lambda _path: SimpleNamespace(
            signer_address="0x1111111111111111111111111111111111111111",
            wallet_address="0x2222222222222222222222222222222222222222",
            predict=None,
        ),
    )
    monkeypatch.setattr(
        runtime_module,
        "PolymarketTradingClient",
        SimpleNamespace(from_keychain=lambda _config: FakeTrading(probe_holder[0])),
    )
    monkeypatch.setattr(
        runtime_module,
        "PredictTradingClient",
        SimpleNamespace(from_keychain=lambda _config: None),
    )
    monkeypatch.setattr(runtime_module, "PolymarketMonitor", FakeMonitor)
    monkeypatch.setattr(runtime_module, "PredictionObservationMonitor", FakeObservationMonitor)
    monkeypatch.setattr(runtime_module, "RelationCatalog", lambda _path: object())
    monkeypatch.setattr(runtime_module, "LlmRelationValidator", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(runtime_module, "LlmTitleTranslator", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(runtime_module, "ensure_same_event_same_venue_scope", lambda _store: False)

    def new_runtime() -> PredictionRuntime:
        return PredictionRuntime(
            data_dir=tmp_path,
            prediction_config_path=tmp_path / "prediction.json",
            dashboard_url="http://127.0.0.1:8766/",
            cross_venue_monitor=_UnavailableCrossVenueMonitor("test-disabled"),
            solver_server_factory=lambda: object(),
            enable_n_leg_background=False,
            notifier=TestNotifier(),
        )

    first = new_runtime()
    first.start()
    try:
        assert first.state == "RUNNING"
        # No Dashboard request is made while this external catalog read is
        # blocked; the runtime-owned worker still starts the refresh.
        assert probe_holder[0].catalog_started.wait(timeout=2)
        assert probe_holder[0].catalog_calls == 1
        assert probe_holder[0].catalog_active == 1
        assert probe_holder[0].catalog_max_active == 1
        assert probe_holder[0].risk_snapshot_started.wait(timeout=2)
        assert not probe_holder[0].release_catalog.is_set()
        probe_holder[0].release_catalog.set()
        assert probe_holder[0].catalog_finished.wait(timeout=2)
        assert probe_holder[0].reward_started.wait(timeout=2)
        # This event is emitted only by the external account snapshot after
        # the reward reader has entered its blocked state.
        assert probe_holder[0].risk_snapshot_started.wait(timeout=2)
        session = first.store.lp_session("runtime-reward-session")  # type: ignore[union-attr]
        assert session is not None
        assert session["book_checked_at"] is not None
        assert not probe_holder[0].release_reward.is_set()
        assert probe_holder[0].active == 1
        assert probe_holder[0].max_active == 1
        with pytest.raises(RuntimeError, match="cannot start from RUNNING"):
            first.start()
        stop_finished = threading.Event()
        stop_errors: list[BaseException] = []

        def stop_runtime() -> None:
            try:
                first.stop()
            except BaseException as exc:
                stop_errors.append(exc)
            finally:
                stop_finished.set()

        stop_thread = threading.Thread(target=stop_runtime)
        stop_thread.start()
        assert probe_holder[0].stop_observed.wait(timeout=2)
        assert not probe_holder[0].close_called.is_set()
        assert first.lp is not None
        assert first._prediction_trading is not None
        assert first.store is not None
        assert not stop_finished.is_set()
        probe_holder[0].release_after_stop.set()
        assert stop_finished.wait(timeout=3)
        stop_thread.join(timeout=1)
        assert stop_errors == []
    finally:
        probe_holder[0].release_reward.set()
        probe_holder[0].release_catalog.set()
        probe_holder[0].release_after_stop.set()
        if first.state not in {"STOPPED", "NEW"}:
            first.stop()
    assert probe_holder[0].active == 0

    # Public stop/start recovery creates one fresh runtime task while the
    # completed session keeps its original UTC reward identity.
    probe_holder[0] = RewardProbe()
    probe_holder[0].catalog_state = "known"
    normal_reward_grace = runtime_module._LP_REWARD_STOP_GRACE_SECONDS
    monkeypatch.setattr(runtime_module, "_LP_REWARD_STOP_GRACE_SECONDS", 0.0)
    second = new_runtime()
    second.start()
    try:
        assert second.state == "RUNNING"
        assert probe_holder[0].catalog_started.wait(timeout=2)
        assert probe_holder[0].catalog_finished.wait(timeout=2)
        assert probe_holder[0].catalog_calls == 1
        assert probe_holder[0].catalog_max_active == 1
        assert probe_holder[0].reward_started.wait(timeout=2)
        assert probe_holder[0].risk_snapshot_started.wait(timeout=2)
        assert probe_holder[0].max_active == 1
        assert probe_holder[0].calls == 1
        refresh_thread = second._reward_thread
        assert refresh_thread is not None
        second._start_reward_monitor()
        assert second._reward_thread is refresh_thread
        with pytest.raises(RuntimeError, match="prediction runtime cleanup failed"):
            second.stop()
        assert second.state == "STOPPING"
        assert probe_holder[0].stop_observed.wait(timeout=2)
        assert probe_holder[0].active == 1
        assert probe_holder[0].monitor_stopped.is_set()
        assert probe_holder[0].observation_stopped.is_set()
        assert not probe_holder[0].close_called.is_set()
        assert second.lp is not None
        assert second.store is not None
        assert second._prediction_trading is not None
        assert second.store.lp_session("runtime-reward-session") is not None
        competing = new_runtime()
        with pytest.raises(PredictionRuntimeOwnershipError):
            competing.start()
        probe_holder[0].release_after_stop.set()
        assert probe_holder[0].reward_finished.wait(timeout=2)
        monkeypatch.setattr(
            runtime_module,
            "_LP_REWARD_STOP_GRACE_SECONDS",
            normal_reward_grace,
        )
        second.stop()
        assert second.state == "STOPPED"
        assert probe_holder[0].close_called.is_set()
        assert probe_holder[0].catalog_calls == 1
        assert probe_holder[0].calls == 1
    finally:
        probe_holder[0].release_reward.set()
        probe_holder[0].release_catalog.set()
        probe_holder[0].release_after_stop.set()
        probe_holder[0].reward_finished.wait(timeout=2)
        monkeypatch.setattr(
            runtime_module,
            "_LP_REWARD_STOP_GRACE_SECONDS",
            normal_reward_grace,
        )
        if second.state not in {"STOPPED", "NEW"}:
            second.stop()
    assert probe_holder[0].active == 0


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
        runtime_module, "LlmRelationValidator", lambda *_args, **_kwargs: object(), raising=False
    )
    monkeypatch.setattr(
        runtime_module, "LlmTitleTranslator", lambda *_args, **_kwargs: object(), raising=False
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


def test_disabled_n_leg_background_skips_resolver_and_driver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_trader.prediction_runtime as runtime_module

    class Fake:
        def __init__(self, *_: object, **__: object) -> None:
            pass

        def observation_snapshot(self) -> dict[str, object]:
            return {}

        def apply_safety_policy(
            self, _policy: object, *, git_sha: str
        ) -> dict[str, object]:
            return {"state": "baseline_enrolled"}

        def reconcile_startup(self) -> dict[str, object]:
            return {"status": "ready"}

        def notify_ready_opportunity(self, *_: object) -> None:
            pass

        def notify_observation(self, *_: object) -> None:
            pass

        def notify_monitor_failure(self, *_: object) -> None:
            pass

        def auto_eat_threshold(self, *_: object) -> None:
            pass

        def set_ready_observer(self, _observer: object) -> None:
            pass

        def set_observation_observer(self, _observer: object) -> None:
            pass

        def set_auto_eat_observer(self, _observer: object) -> None:
            pass

        def set_failure_observer(self, _observer: object) -> None:
            pass

        def set_cross_venue_monitor(self, _monitor: object) -> None:
            pass

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

        def close(self) -> None:
            pass

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
    monkeypatch.setattr(
        runtime_module,
        "PolymarketTradingClient",
        SimpleNamespace(from_keychain=lambda _config: Fake()),
        raising=False,
    )
    monkeypatch.setattr(
        runtime_module,
        "PredictTradingClient",
        SimpleNamespace(from_keychain=lambda _config: None),
        raising=False,
    )
    for name in (
        "PredictionArbitrageStore",
        "RelationCatalog",
        "PolymarketMonitor",
        "PredictionExecutionService",
        "LlmRelationValidator",
        "LlmTitleTranslator",
        "PredictionLiveResolver",
        "PredictionMonitorSelectionDriver",
    ):
        monkeypatch.setattr(runtime_module, name, Fake, raising=False)

    runtime = PredictionRuntime(
        data_dir=tmp_path,
        prediction_config_path=tmp_path / "prediction.json",
        dashboard_url="http://127.0.0.1:8766/",
        solver_server_factory=lambda: object(),
        cross_venue_monitor=_UnavailableCrossVenueMonitor("test-disabled"),
        enable_n_leg_background=False,
    )
    runtime.start()
    try:
        assert runtime.state == "RUNNING"
        assert runtime.live_resolver is None
        assert runtime.monitor_selection_driver is None
    finally:
        runtime.stop()

    assert runtime.state == "STOPPED"


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


def test_runtime_owns_one_shared_solver_server_for_its_start_stop_lifetime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_trader.prediction_runtime as runtime_module

    events: list[str] = []

    class FakeStore:
        def __init__(self, _path: Path) -> None:
            pass

        def apply_safety_policy(self, *_args: object, **_kwargs: object) -> None:
            pass

        def close(self) -> None:
            events.append("store.close")

    class FakeTrading:
        def close(self) -> None:
            events.append("trading.close")

    monitors: list[object] = []

    class FakeMonitor:
        def __init__(self, **_kwargs: object) -> None:
            self.shadow_observer = None
            monitors.append(self)

        def set_ready_observer(self, _observer: object) -> None:
            pass

        def set_observation_observer(self, _observer: object) -> None:
            pass

        def set_auto_eat_observer(self, _observer: object) -> None:
            pass

        def set_failure_observer(self, _observer: object) -> None:
            pass

        def set_shadow_observer(self, observer: object) -> None:
            self.shadow_observer = observer

        def start(self) -> None:
            events.append("monitor.start")

        def stop(self) -> None:
            events.append("monitor.stop")

    class FakeExecution:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def reconcile_startup(self) -> dict[str, object]:
            return {"state": "ready"}

        def notify_ready_opportunity(self, *_args: object) -> dict[str, object]:
            return {}

        def notify_observation(self, *_args: object) -> dict[str, object]:
            return {}

        def auto_eat_threshold(self, *_args: object) -> dict[str, object]:
            return {}

        def notify_monitor_failure(self, *_args: object) -> dict[str, object]:
            return {}

        def set_cross_venue_monitor(self, _monitor: object) -> None:
            pass

        def close(self) -> None:
            events.append("execution.close")

    class FakeSolverServer:
        def close(self) -> None:
            events.append("solver.close")

    monkeypatch.setattr(runtime_module, "PredictionArbitrageStore", FakeStore)
    monkeypatch.setattr(
        runtime_module,
        "RelationCatalog",
        lambda _path: SimpleNamespace(observation_snapshot=lambda: {}),
    )
    monkeypatch.setattr(runtime_module, "load_trading_config", lambda _path: object())
    monkeypatch.setattr(runtime_module, "PolymarketTradingClient", SimpleNamespace(from_keychain=lambda _config: FakeTrading()))
    monkeypatch.setattr(runtime_module, "PredictTradingClient", SimpleNamespace(from_keychain=lambda _config: None))
    monkeypatch.setattr(runtime_module, "LlmRelationValidator", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(runtime_module, "LlmTitleTranslator", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(runtime_module, "PolymarketMonitor", FakeMonitor)
    monkeypatch.setattr(runtime_module, "PredictionExecutionService", FakeExecution)
    monkeypatch.setattr(
        runtime_module,
        "PredictionLiveResolver",
        lambda **_kwargs: SimpleNamespace(
            start=lambda: events.append("resolver.start"),
            stop=lambda: events.append("resolver.stop"),
            is_idle=lambda: True,
            solutions=lambda: [],
        ),
    )
    monkeypatch.setattr(
        runtime_module,
        "PredictionMonitorSelectionDriver",
        lambda **_kwargs: SimpleNamespace(
            start=lambda: events.append("selection.start"),
            stop=lambda: events.append("selection.stop"),
            status=lambda: {
                "selection_pending": 0,
                "selection_failures_consecutive": 0,
                "selection_applied_generation": None,
            },
        ),
    )
    cross_observers: list[object] = []
    monkeypatch.setattr(
        runtime_module,
        "_build_cross_venue_monitor",
        lambda **kwargs: (
            cross_observers.append(kwargs["shadow_observer"])
            or _UnavailableCrossVenueMonitor("test")
        ),
    )
    servers: list[FakeSolverServer] = []

    runtime = PredictionRuntime(
        data_dir=tmp_path,
        prediction_config_path=tmp_path / "prediction.json",
        dashboard_url="http://127.0.0.1:8766/",
        solver_server_factory=lambda: servers.append(FakeSolverServer()) or servers[-1],
    )
    runtime.start()
    assert runtime.n_leg_shadow is not None
    assert callable(getattr(monitors[0], "shadow_observer", None))
    assert cross_observers == [monitors[0].shadow_observer]
    runtime.stop()

    assert len(servers) == 1
    assert events.index("selection.stop") < events.index("resolver.stop")
    assert events.index("resolver.stop") < events.index("solver.close")
    assert events.index("monitor.stop") < events.index("solver.close")
    assert events.index("solver.close") < events.index("execution.close")


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
    monkeypatch.setattr(runtime_module, "LlmRelationValidator", lambda *_a, **_k: object())
    monkeypatch.setattr(runtime_module, "LlmTitleTranslator", lambda *_a, **_k: object())
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
    monkeypatch.setattr(runtime_module, "LlmRelationValidator", lambda *_a, **_k: object())
    monkeypatch.setattr(runtime_module, "LlmTitleTranslator", lambda *_a, **_k: object())
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
    monkeypatch.setattr(runtime_module, "LlmRelationValidator", lambda *_a, **_k: object())
    monkeypatch.setattr(runtime_module, "LlmTitleTranslator", lambda *_a, **_k: object())
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
        "LlmRelationValidator",
        lambda *_a, **kwargs: (validator_kwargs.append(kwargs) or object()),
    )
    monkeypatch.setattr(runtime_module, "LlmTitleTranslator", lambda *_a, **_k: object())
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
    assert validator_kwargs[0]["max_llm_calls"] == 3
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
