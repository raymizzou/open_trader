from __future__ import annotations

import asyncio
import hashlib
import json
import multiprocessing
import os
import threading
from collections.abc import Mapping
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime, timedelta
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
from open_trader.notifications import FeishuWebhookNotifier, NullNotifier
from open_trader.predict_cross_venue import (
    LlmCrossVenueEquivalenceValidator,
    ExplicitMarketPair,
    VenueMarket,
)
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from open_trader.prediction_arbitrage_execution import PredictionExecutionService


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


def test_n_leg_pause_keeps_lp_running_without_n_leg_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_trader.prediction_runtime as runtime_module

    calls: list[tuple[str, object]] = []
    lp_events = {
        name: threading.Event()
        for name in ("account", "catalog", "metadata", "history", "books", "reward")
    }

    class ExternalTrading:
        config = SimpleNamespace(wallet_address="0xwallet")

        def __init__(self) -> None:
            self.order = {
                "order_id": "manual-order",
                "market_id": "manual-market",
                "condition_id": "manual-condition",
                "token_id": "manual-token",
                "market_title": "Manual order retained",
                "outcome": "NO",
                "side": "BUY",
                "status": "LIVE",
                "price": Decimal("0.36"),
                "original_size": Decimal("100"),
                "size_matched": Decimal("0"),
                "remaining_size": Decimal("100"),
            }
            self.lp_order = {
                "order_id": "lp-order",
                "market_id": "lp-risk-market",
                "condition_id": "lp-risk-condition",
                "token_id": "lp-risk-yes",
                "market_title": "LP risk market",
                "outcome": "YES",
                "side": "BUY",
                "status": "LIVE",
                "price": Decimal("0.40"),
                "original_size": Decimal("20"),
                "size_matched": Decimal("0"),
                "remaining_size": Decimal("20"),
                "expiration": (datetime.now(UTC) + timedelta(hours=1)),
            }

        def readiness_snapshot(self) -> dict[str, object]:
            return {
                "checked_at": datetime.now(UTC),
                "relayer_ready": True,
                "merge_ready": True,
            }

        def account_snapshot(self) -> dict[str, object]:
            return {
                "checked_at": datetime.now(UTC),
                "open_order_ids": ("manual-order", "lp-order"),
                "positions": (),
            }

        def lp_account_snapshot(self) -> dict[str, object]:
            lp_events["account"].set()
            return {
                "authenticated": True,
                "balance": Decimal("100"),
                "allowance": Decimal("100"),
                "open_orders": (dict(self.order), dict(self.lp_order)),
                "positions": (),
                "checked_at": datetime.now(UTC),
                "open_orders_complete": True,
                "positions_complete": True,
            }

        def get_order_scoring(self, order_id: str) -> bool:
            return order_id == "lp-order"

        def lp_snapshot(self, _request: object) -> dict[str, object]:
            now = datetime.now(UTC)
            return {
                "account": {
                    "authenticated": True,
                    "balance": Decimal("100"),
                    "allowance": Decimal("100"),
                    "open_orders": (dict(self.order), dict(self.lp_order)),
                    "positions": (),
                    "checked_at": now,
                },
                "orders": (dict(self.lp_order),),
                "book": {
                    "token_id": "lp-risk-yes",
                    "condition_id": "lp-risk-condition",
                    "received_at": now,
                    "source_timestamp": now,
                    "bids": [
                        {"price": Decimal("0.40"), "size": Decimal("100")},
                        {"price": Decimal("0.39"), "size": Decimal("100")},
                    ],
                    "asks": [{"price": Decimal("0.42"), "size": Decimal("100")}],
                },
                "trades": (),
                "market": {
                    "market_id": "lp-risk-market",
                    "condition_id": "lp-risk-condition",
                    "token_id": "lp-risk-yes",
                    "outcome": "YES",
                    "fees_enabled": False,
                    "fee": Decimal("0"),
                    "fee_exponent": Decimal("1"),
                    "taker_fee_rate": Decimal("0"),
                },
            }

        def lp_reward_catalog(
            self,
            *,
            condition_ids: object = None,
            stop_event: threading.Event | None = None,
        ) -> dict[str, object]:
            del condition_ids, stop_event
            lp_events["catalog"].set()
            return {
                "state": "known",
                "complete": True,
                "checked_at": datetime.now(UTC),
                "daily_pool_usd": Decimal("100"),
                "markets": ({
                    "condition_id": "candidate-condition",
                    "reward_active": True,
                    "rewards_max_spread": Decimal("0.03"),
                    "rewards_min_size": Decimal("10"),
                    "daily_pool_usd": Decimal("100"),
                    "native_reward_configs": (),
                    "sponsored_reward_configs": (),
                },),
            }

        def lp_market_metadata(self, condition_ids: object, **_kwargs: object) -> dict[str, dict[str, object]]:
            del condition_ids
            lp_events["metadata"].set()
            checked_at = datetime.now(UTC)
            return {
                "candidate-condition": {
                    "market_id": "candidate-market",
                    "condition_id": "candidate-condition",
                    "market_title": "Candidate market",
                    "market_url": "https://polymarket.com/event/candidate",
                    "exchange_type": "CLOB",
                    "metadata_checked_at": checked_at,
                    "fees_checked_at": checked_at,
                    "event_ended": False,
                    "event_start_time": datetime.now(UTC) + timedelta(hours=2),
                    "accepting_orders": True,
                    "minimum_order_size": Decimal("1"),
                    "tick_size": Decimal("0.01"),
                    "fees_enabled": False,
                    "fee": Decimal("0"),
                    "fee_exponent": Decimal("1"),
                    "taker_fee_rate": Decimal("0"),
                    "reward_min_size": Decimal("10"),
                    "reward_max_spread": Decimal("0.03"),
                    "outcomes": {
                        "yes": {"label": "YES", "token_id": "candidate-yes"},
                        "no": {"label": "NO", "token_id": "candidate-no"},
                    },
                }
            }

        def lp_price_history(
            self,
            token_ids: object,
            *,
            start_ts: int,
            end_ts: int,
            fidelity: int,
            stop_event: object = None,
        ) -> dict[str, object]:
            del fidelity, stop_event
            lp_events["history"].set()
            sample_timestamps = list(range(start_ts, end_ts + 1, 60))
            if sample_timestamps[-1] != end_ts:
                sample_timestamps.append(end_ts)
            return {
                "state": "known",
                "history": {
                    token_id: [
                        {
                            "t": timestamp,
                            "p": Decimal("0.505") if timestamp == end_ts else Decimal("0.50"),
                        }
                        for timestamp in sample_timestamps
                    ]
                    for token_id in tuple(token_ids)  # type: ignore[arg-type]
                },
            }

        def lp_order_books(self, token_ids: object, *, stop_event: threading.Event | None = None) -> dict[str, dict[str, object]]:
            del stop_event
            lp_events["books"].set()
            now = datetime.now(UTC)
            return {
                token: {
                    "token_id": token,
                    "condition_id": "candidate-condition",
                    "received_at": now,
                    "source_timestamp": now,
                    "bids": [
                        {"price": Decimal("0.40"), "size": Decimal("100")},
                        {"price": Decimal("0.39"), "size": Decimal("100")},
                    ],
                    "asks": [{"price": Decimal("0.42"), "size": Decimal("100")}],
                }
                for token in token_ids
            }

        def lp_reward_snapshot(self, reward_date: str, condition_id: str, **_kwargs: object) -> dict[str, object]:
            lp_events["reward"].set()
            return {
                "state": "known",
                "reward_date": reward_date,
                "condition_id": condition_id,
                "market_amount": Decimal("0.25"),
                "account_amount": Decimal("0.25"),
                "market_asset": "USDC.e",
                "account_asset": "USDC.e",
            }

        def close(self) -> None:
            calls.append(("trading-close", None))

        def __getattr__(self, name: str) -> object:
            if name in {"cancel_orders", "lp_post_order", "post_order", "submit_protected_sell"}:
                def forbidden(*_args: object, **_kwargs: object) -> None:
                    calls.append(("order-mutation", name))
                    raise AssertionError(f"paused runtime attempted {name}")
                return forbidden
            raise AttributeError(name)

    class ExternalNotifier:
        def __init__(self, channel: str) -> None:
            self.channel = channel

        def send(self, *args: object, **kwargs: object) -> bool:
            calls.append(("notification", (self.channel, args, kwargs)))
            return True

    trading = ExternalTrading()
    config = SimpleNamespace(
        signer_address="0xsigner",
        wallet_address="0xwallet",
        predict=None,
    )
    store = PredictionArbitrageStore(tmp_path)
    store.n_leg_safety_config_write(
        1,
        {
            "max_per_trade_cost_units": 10_000_000,
            "max_total_unsettled_capital_units": 20_000_000,
            "max_partial_fill_loss_units": 10_000_000,
            "max_auto_repair_loss_units": 10_000_000,
        },
    )
    store.n_leg_mode_control_write(
        mode="MANUAL", contract_generation=7, qualification_policy_version=3,
        safety_config_version=1, enabled_execution_scope_version=[],
    )
    store.n_leg_create_batch({
        "execution_batch_id": "nleg-accounting-batch",
        "opportunity_episode_id": "nleg-accounting-episode",
        "episode_lineage_id": "nleg-accounting-lineage",
        "mode": "MANUAL",
        "state": "INCIDENT",
        "entry_fingerprint": "nleg-accounting-entry",
        "total_unsettled_capital_units": 1_000_000,
        "reservation_units": 1_000_000,
        "reservations": [{"remaining_units": 0, "holding_units": 1_000_000}],
        "legs": [{"receipt": {"state": "REJECTED"}}],
    })
    store.n_leg_acknowledge_incident(
        "nleg-accounting-batch",
        acknowledgement={"actor": "test", "reconciliation": "fresh_clean"},
    )
    store.lp_create_session(
        "lp-session", "lp-idempotency", state="complete",
        payload={
            "market_id": "manual-market", "condition_id": "manual-condition",
            "token_id": "manual-token", "outcome": "NO", "price": "0.36",
            "quantity": "100", "review_at": (datetime.now(UTC).replace(microsecond=0)).isoformat(),
        },
    )
    risk_stale_at = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    risk_expiry = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    store.lp_create_session(
        "lp-risk-session", "lp-risk-idempotency", state="entry_open",
        payload={
            "market_id": "lp-risk-market",
            "condition_id": "lp-risk-condition",
            "token_id": "lp-risk-yes",
            "outcome": "YES",
            "price": Decimal("0.40"),
            "quantity": Decimal("20"),
            "review_at": risk_expiry,
            "entry_order_id": "lp-order",
            "entry_expiration": risk_expiry,
            "owned_order_ids": ["lp-order"],
            "order_history": {
                "lp-order": {
                    "order_id": "lp-order",
                    "token_id": "lp-risk-yes",
                    "side": "BUY",
                    "status": "LIVE",
                    "price": Decimal("0.40"),
                    "original_size": Decimal("20"),
                    "size_matched": Decimal("0"),
                    "remaining_size": Decimal("20"),
                    "expiration": risk_expiry,
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
            "position_reconciled": True,
            "orders_terminal": False,
            "entry_cancel_requested": False,
            "stop_loss_latched": False,
            "scoring_status": "unknown",
            "scoring_checked_at": risk_stale_at,
            "scoring_order_id": "lp-order",
            "scoring_order_role": "entry",
            "account_checked_at": risk_stale_at,
            "book_checked_at": risk_stale_at,
            "reward_date": datetime.now(UTC).date().isoformat(),
            "trade_pnl": Decimal("0"),
            "paid_rewards": Decimal("0"),
        },
    )
    accounting_before = store.n_leg_control()
    session_before = store.lp_session("lp-session")
    risk_before = store.lp_session("lp-risk-session")
    assert risk_before is not None
    assert risk_before["state"] == "entry_open"
    assert risk_before["account_checked_at"] == risk_stale_at
    assert risk_before["book_checked_at"] == risk_stale_at
    solver_calls: list[str] = []
    predict_calls: list[str] = []

    def make_runtime() -> PredictionRuntime:
        return PredictionRuntime(
            data_dir=tmp_path,
            prediction_config_path=tmp_path / "prediction.json",
            dashboard_url="http://127.0.0.1:8766/",
            notifier=SimpleNamespace(_notifiers=(ExternalNotifier("macos"), ExternalNotifier("feishu"))),
            solver_server_factory=lambda: (_ for _ in ()).throw(
                (solver_calls.append("started") or AssertionError("solver must not start while N_LEG is paused"))
            ),
        )

    monkeypatch.setenv("OPEN_TRADER_NLEG_PAUSED", "1")
    monkeypatch.setattr(runtime_module, "load_trading_config", lambda _path: config)
    monkeypatch.setattr(
        runtime_module,
        "PolymarketTradingClient",
        SimpleNamespace(from_keychain=lambda _config: trading),
    )
    monkeypatch.setattr(
        runtime_module,
        "PredictTradingClient",
        SimpleNamespace(from_keychain=lambda _config: (predict_calls.append("started") or (_ for _ in ()).throw(
            AssertionError("Predict account client must not start while N_LEG is paused")
        ))),
    )
    monkeypatch.setattr(runtime_module, "_LP_TICK_SECONDS", 0.001)
    monkeypatch.setattr(runtime_module, "_LP_REWARD_SECONDS", 0.01)
    monkeypatch.setattr(runtime_module, "_LP_BOOK_SAMPLE_SECONDS", 0.01)

    runtime = make_runtime()
    try:
        runtime.start()
        assert runtime.state == "RUNNING"
        assert all(event.wait(timeout=3) for event in lp_events.values())
        candidate = runtime.lp.candidate_snapshot()  # type: ignore[union-attr]
        deadline = time.monotonic() + 2
        while candidate.get("state") != "ready" and time.monotonic() < deadline:
            time.sleep(0.01)
            candidate = runtime.lp.candidate_snapshot()  # type: ignore[union-attr]
        reward_deadline = time.monotonic() + 2
        risk_session = store.lp_session("lp-risk-session")
        while (
            isinstance(risk_session, dict)
            and not isinstance(risk_session.get("reward_observation"), dict)
            and time.monotonic() < reward_deadline
        ):
            time.sleep(0.01)
            risk_session = store.lp_session("lp-risk-session")
        dashboard = runtime.execution.lp_dashboard()  # type: ignore[union-attr]
        assert runtime.solver_server is None
        assert runtime.relation_catalog is None
        assert runtime.live_resolver is None
        assert runtime.observation_monitor is None
        assert runtime.predict_snapshot_refresher is None
        assert dashboard["orders"][0]["order_id"] == "manual-order"
        assert dashboard["orders"][0]["management"] == "manual_read_only"
        assert candidate["state"] == "ready"
        assert candidate["complete"] is True
        assert candidate["catalog_complete"] is True
        assert candidate["missing_metadata_condition_ids"] == []
        assert candidate.get("missing_book_token_ids", []) == []
        candidate_rows = [
            row
            for row in candidate["candidates"]
            if isinstance(row, dict)
            and row.get("market_id") == "candidate-market"
            and row.get("condition_id") == "candidate-condition"
        ]
        # Issue 141 起每轮仅队首 1 行读取实时盘口并置 verified，方向代表按占资
        # 最低（同占资按 outcome 升序取代表），因此不再断言特定方向；
        # 队首行实时买价为 0.40。
        assert len(candidate_rows) == 1, repr(candidate_rows)
        head_row = candidate_rows[0]
        assert head_row["verification"] == "verified"
        assert Decimal(str(head_row["realtime_price"])) == Decimal("0.40")
        risk_session = store.lp_session("lp-risk-session")
        assert risk_session is not None
        assert risk_session["state"] == "entry_open"
        assert risk_session["account_checked_at"] != risk_stale_at
        assert risk_session["book_checked_at"] != risk_stale_at
        assert risk_session["scoring_status"] == "true"
        assert risk_session["scoring_checked_at"] != risk_stale_at
        assert dashboard["lp_session"]["state"] == "entry_open"
        reward_observation = dashboard["lp_session"]["reward_observation"]
        assert reward_observation["status"] == "below"
        assert Decimal(str(reward_observation["market_amount"])) == Decimal("0.25")
        assert Decimal(str(reward_observation["account_amount"])) == Decimal("0.25")
        assert Decimal(str(risk_session["paid_rewards"])) == Decimal("0")
        assert Decimal(str(risk_session["trade_pnl"])) == Decimal("0")
    finally:
        runtime.stop()

    accounting_after = store.n_leg_control()
    session_after = store.lp_session("lp-session")
    risk_after = store.lp_session("lp-risk-session")
    assert accounting_after["total_unsettled_capital_units"] == accounting_before["total_unsettled_capital_units"] == 1_000_000
    assert accounting_after["active_batch_id"] is None
    assert session_after["state"] == session_before["state"] == "complete"  # type: ignore[index]
    assert session_after["condition_id"] == session_before["condition_id"] == "manual-condition"  # type: ignore[index]
    assert risk_after is not None
    assert risk_after["state"] == "entry_open"
    assert risk_after["owned_order_ids"] == ["lp-order"]
    assert Decimal(str(risk_after["paid_rewards"])) == Decimal("0")
    assert Decimal(str(risk_after["trade_pnl"])) == Decimal("0")
    assert not solver_calls
    assert not predict_calls
    assert not [call for call in calls if call[0] == "order-mutation"]
    assert not [
        call
        for call in calls
        if call[0] == "notification" and "N_LEG" in repr(call[1])
    ]

    preparation_before_restart = runtime.lp.preparation_snapshot()  # type: ignore[union-attr]
    last_attempt_before_restart = preparation_before_restart["last_attempt_at"]
    summary_before_restart = store.lp_price_history_summary(
        "candidate-condition", "candidate-yes", now=datetime.now(UTC)
    )
    assert summary_before_restart is not None
    summary_timestamps_before_restart = {
        key: summary_before_restart[key]
        for key in ("checked_at", "window_start", "window_end")
    }
    for event in lp_events.values():
        event.clear()
    warm_restart_started_at = datetime.now(UTC)
    restarted = make_runtime()
    try:
        restarted.start()
        assert restarted.state == "RUNNING"
        assert all(
            event.wait(timeout=3)
            for name, event in lp_events.items()
            if name != "history"
        )
        assert not lp_events["history"].is_set()
        restarted_preparation = restarted.lp.preparation_snapshot()  # type: ignore[union-attr]
        deadline = time.monotonic() + 2
        while (
            restarted_preparation.get("state") != "ready"
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
            restarted_preparation = restarted.lp.preparation_snapshot()  # type: ignore[union-attr]
        assert restarted_preparation["state"] == "ready"
        assert restarted_preparation["last_attempt_at"] != last_attempt_before_restart
        restarted_attempt_at = datetime.fromisoformat(
            str(restarted_preparation["last_attempt_at"]).replace("Z", "+00:00")
        )
        assert warm_restart_started_at <= restarted_attempt_at <= (
            warm_restart_started_at + timedelta(seconds=2)
        )
        restarted_dashboard = restarted.execution.lp_dashboard()  # type: ignore[union-attr]
        assert restarted.solver_server is None
        assert restarted.relation_catalog is None
        assert restarted.live_resolver is None
        assert restarted.observation_monitor is None
        assert restarted.predict_snapshot_refresher is None
        assert restarted_dashboard["orders"][0]["order_id"] == "manual-order"
        assert restarted_dashboard["orders"][0]["management"] == "manual_read_only"
        restarted_risk = store.lp_session("lp-risk-session")
        assert restarted_risk is not None
        assert restarted_risk["state"] == "entry_open"
        assert restarted_risk["owned_order_ids"] == ["lp-order"]
        assert Decimal(str(restarted_risk["paid_rewards"])) == Decimal("0")
        assert Decimal(str(restarted_risk["trade_pnl"])) == Decimal("0")
        assert restarted_dashboard["lp_session"]["reward_observation"]["status"] == "below"
        summary_after_restart = store.lp_price_history_summary(
            "candidate-condition", "candidate-yes", now=datetime.now(UTC)
        )
        assert summary_after_restart is not None
        assert {
            key: summary_after_restart[key]
            for key in ("checked_at", "window_start", "window_end")
        } == summary_timestamps_before_restart
    finally:
        restarted.stop()

    assert store.n_leg_control()["total_unsettled_capital_units"] == 1_000_000
    assert not solver_calls
    assert not predict_calls
    assert not [call for call in calls if call[0] == "order-mutation"]
    assert not [
        call
        for call in calls
        if call[0] == "notification" and "N_LEG" in repr(call[1])
    ]


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


def test_restart_preserves_manual_orders_and_resumes_monitoring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_trader.polymarket_monitor as monitor_module
    import open_trader.prediction_runtime as runtime_module

    account_orders = [
        {
            "id": "manual-no-order",
            "market": "condition-no",
            "asset_id": "no-token",
            "outcome": "NO",
            "side": "BUY",
            "status": "LIVE",
            "price": Decimal("0.36"),
            "original_size": Decimal("100"),
            "size_matched": Decimal("0"),
        },
        {
            "id": "manual-yes-order",
            "market": "condition-yes",
            "asset_id": "yes-token",
            "outcome": "YES",
            "side": "BUY",
            "status": "LIVE",
            "price": Decimal("0.27"),
            "original_size": Decimal("100"),
            "size_matched": Decimal("0"),
        },
    ]
    mutations: list[tuple[str, object]] = []
    config = SimpleNamespace(
        signer_address="0x1111111111111111111111111111111111111111",
        wallet_address="0x2222222222222222222222222222222222222222",
        predict=None,
    )

    class FakeTrading:
        def account_snapshot(self) -> dict[str, object]:
            return {
                "wallet_address": config.wallet_address,
                "p_usd_balance": Decimal("100"),
                "p_usd_allowance": Decimal("100"),
                "open_order_ids": tuple(str(row["id"]) for row in account_orders),
                "positions": (),
                "checked_at": datetime.now(UTC),
            }

        def readiness_snapshot(self) -> dict[str, object]:
            return {
                "relayer_ready": "ready",
                "merge_ready": "ready",
                "geoblock": "allowed",
                "checked_at": datetime.now(UTC),
            }

        def lp_account_snapshot(self) -> dict[str, object]:
            return {
                "authenticated": True,
                "balance": Decimal("100"),
                "allowance": Decimal("100"),
                "open_orders": tuple(dict(row) for row in account_orders),
                "positions": (),
                "checked_at": datetime.now(UTC),
            }

        def get_order_scoring(self, _order_id: str) -> bool:
            return False

        def lp_reward_snapshot(
            self, reward_date: str, condition_id: str
        ) -> dict[str, object]:
            return {
                "state": "unknown",
                "reward_date": reward_date,
                "condition_id": condition_id,
            }

        def lp_reward_catalog(self, *, stop_event: threading.Event | None = None) -> dict[str, object]:
            del stop_event
            return {
                "state": "known",
                "complete": True,
                "markets": [],
                "checked_at": datetime.now(UTC),
            }

        def cancel_orders(self, order_ids: tuple[str, ...]) -> tuple[str, ...]:
            mutations.append(("cancel", order_ids))
            account_orders[:] = [
                row for row in account_orders if str(row["id"]) not in order_ids
            ]
            return order_ids

        def submit_pair_once(self, *_args: object, **_kwargs: object) -> None:
            mutations.append(("submit_pair", None))

        def submit_threshold_hedge_once(
            self, *_args: object, **_kwargs: object
        ) -> None:
            mutations.append(("submit_threshold", None))

        def create_limit_order(self, **_kwargs: object) -> None:
            mutations.append(("create_limit_order", None))

        def post_order(self, _signed: object) -> None:
            mutations.append(("post_order", None))

        def submit_protected_sell(self, **_kwargs: object) -> None:
            mutations.append(("submit_protected_sell", None))

        def close(self) -> None:
            pass

    class FakeStream:
        async def __anext__(self) -> object:
            await asyncio.sleep(10)
            raise StopAsyncIteration

        async def close(self) -> None:
            pass

    class FakePublicClient:
        async def list_events(self, **_kwargs: object) -> list[object]:
            return [
                {
                    "id": "test-event",
                    "title": "Test event",
                    "slug": "test-event",
                    "state": {"active": True, "closed": False, "ended": False},
                    "metrics": {"volume_24hr": Decimal("1000")},
                    "markets": [
                        {
                            "id": "test-market",
                            "condition_id": "test-condition",
                            "question": "Test question",
                            "slug": "test-market",
                            "state": {
                                "active": True,
                                "closed": False,
                                "accepting_orders": True,
                                "enable_order_book": True,
                                "neg_risk": False,
                            },
                            "outcomes": [
                                {"label": "YES", "token_id": "book-yes"},
                                {"label": "NO", "token_id": "book-no"},
                            ],
                            "trading": {
                                "minimum_order_size": Decimal("1"),
                                "minimum_tick_size": Decimal("0.01"),
                                "fees_enabled": False,
                                "neg_risk": False,
                            },
                        }
                    ],
                }
            ]

        async def get_order_books(self, **_kwargs: object) -> list[object]:
            return []

        async def subscribe(self, *_args: object, **_kwargs: object) -> FakeStream:
            return FakeStream()

        async def close(self) -> None:
            pass

    monkeypatch.setattr(runtime_module, "load_trading_config", lambda _path: config)
    monkeypatch.setattr(
        runtime_module,
        "PolymarketTradingClient",
        SimpleNamespace(from_keychain=lambda _config: FakeTrading()),
    )
    monkeypatch.setattr(
        runtime_module,
        "PredictTradingClient",
        SimpleNamespace(from_keychain=lambda _config: None),
    )
    monkeypatch.setattr(monitor_module, "AsyncPublicClient", FakePublicClient)

    expected_orders = [
        ("manual-no-order", "BUY", "0.36", "100", "manual_read_only", True),
        ("manual-yes-order", "BUY", "0.27", "100", "manual_read_only", True),
    ]
    observations: list[dict[str, object]] = []
    for _ in range(2):
        runtime = PredictionRuntime(
            data_dir=tmp_path,
            prediction_config_path=tmp_path / "prediction.json",
            dashboard_url="http://127.0.0.1:8766/",
            notifier=SimpleNamespace(
                _notifiers=(SimpleNamespace(channel="macos"), SimpleNamespace(channel="feishu"))
            ),
            cross_venue_monitor=_UnavailableCrossVenueMonitor("test-disabled"),
            solver_server_factory=lambda: object(),
            enable_n_leg_background=False,
        )
        try:
            runtime.start()
            assert runtime.store is not None
            assert runtime.execution is not None
            # Issue #146: poll like the page does until the background
            # snapshot is published instead of forcing a pipeline run.
            dashboard = runtime.execution.lp_dashboard()
            dashboard_deadline = time.monotonic() + 5
            while dashboard.get("state") != "ready" and time.monotonic() < dashboard_deadline:
                runtime.execution.refresh_lp_dashboard_snapshot()
                dashboard = runtime.execution.lp_dashboard()
                time.sleep(0.05)
            observations.append(
                {
                    "state": runtime.state,
                    "dashboard_state": dashboard["state"],
                    "stale": dashboard["stale"],
                    "orders": [
                        (
                            str(row["order_id"]),
                            str(row["side"]),
                            str(row["price"]),
                            str(row["quantity"]),
                            str(row["management"]),
                            row["read_only"],
                        )
                        for row in dashboard["orders"]
                    ],
                    "incidents": len(runtime.store.histories("incidents")),
                }
            )
        finally:
            if runtime.state not in {"NEW", "STOPPED"}:
                runtime.stop()

    expected = {
        "state": "RUNNING",
        "dashboard_state": "ready",
        "stale": False,
        "orders": expected_orders,
        "incidents": 0,
    }
    assert observations == [expected, expected]
    assert mutations == []


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
                        "state": "known",
                        "complete": True,
                        "checked_at": datetime.now(UTC),
                        "daily_pool_usd": Decimal("0"),
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

        def lp_market_metadata(
            self, _condition_ids: tuple[str, ...], *, stop_event: object = None
        ) -> dict[str, object]:
            del stop_event
            return {}

        def lp_price_history(
            self,
            _token_ids: tuple[str, ...],
            *,
            start_ts: int,
            end_ts: int,
            fidelity: int,
            stop_event: object = None,
        ) -> dict[str, object]:
            del start_ts, end_ts, fidelity, stop_event
            return {"state": "known", "history": {}}

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


def test_candidate_monitors_run_independently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #146 A8: scan and maintenance run on independent threads, the
    scheduler wait never spins below one second, a manual refresh forces a
    scan round, and the dashboard snapshot refreshes in the background."""
    import open_trader.prediction_runtime as runtime_module

    scan_entered = threading.Event()
    scan_release = threading.Event()
    maintenance_calls: list[float] = []
    dashboard_calls: list[float] = []

    class StubLP:
        def __init__(self, _store, _exchange, owner_lock=None) -> None:
            del owner_lock
            self.scan_forces: list[bool] = []

        def refresh_candidates(
            self, *, stop_event=None, force: bool = False
        ) -> dict[str, object]:
            del stop_event
            self.scan_forces.append(force)
            scan_entered.set()
            assert scan_release.wait(timeout=10)
            return {"state": "unknown", "scanning": False}

        def refresh_candidate_recommendations(
            self, *, stop_event=None
        ) -> dict[str, object]:
            del stop_event
            maintenance_calls.append(time.monotonic())
            return {"state": "ready"}

        def candidate_maintenance_wait_seconds(self) -> float:
            return 0.0

        def refresh_rewards(self, *, stop_event=None) -> dict[str, object]:
            del stop_event
            return {"state": "none"}

    class FakeTrading:
        config = SimpleNamespace(
            signer_address="0x" + "1" * 40,
            wallet_address="0x" + "2" * 40,
        )

    class FakeMonitor:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def stop(self) -> None:
            return None

    class FakeExecution:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def reconcile_startup(self) -> dict[str, object]:
            return {"state": "ready"}

        def lp_tick(self) -> dict[str, object]:
            return {"state": "none"}

        def set_cross_venue_monitor(self, _monitor: object) -> None:
            return None

        def refresh_lp_dashboard_snapshot(self) -> dict[str, object]:
            dashboard_calls.append(time.monotonic())
            return {"state": "ready"}

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        runtime_module,
        "PolymarketTradingClient",
        SimpleNamespace(from_keychain=lambda _config: FakeTrading()),
    )
    monkeypatch.setattr(runtime_module, "PolymarketMonitor", FakeMonitor)
    monkeypatch.setattr(runtime_module, "PredictionExecutionService", FakeExecution)
    monkeypatch.setattr(runtime_module, "load_trading_config", lambda _path: FakeTrading.config)
    monkeypatch.setattr(runtime_module, "PolymarketLPService", StubLP)
    monkeypatch.setattr(runtime_module, "_LP_DASHBOARD_SNAPSHOT_SECONDS", 0.05)

    runtime = PredictionRuntime(
        data_dir=tmp_path,
        prediction_config_path=tmp_path / "prediction.json",
        dashboard_url="http://127.0.0.1:8766/",
        mode="production",
        n_leg_paused=True,
        enable_n_leg_background=False,
    )
    runtime.start()
    try:
        assert runtime.lp is not None
        # The scan parks inside refresh_candidates; the maintenance thread
        # still runs on its own cadence.
        assert scan_entered.wait(timeout=5)
        deadline = time.monotonic() + 6
        while len(maintenance_calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(maintenance_calls) >= 2
        assert len(maintenance_calls) >= 2 and (
            maintenance_calls[-1] - maintenance_calls[0] >= 0.9
        )
        # The dashboard snapshot thread ran on its own cadence.
        assert len(dashboard_calls) >= 1

        # Releasing the scan lets it finish; the failed (non-ready) round
        # waits 60 seconds, so the next scan round comes from the manual
        # page refresh and runs with force=True.
        scan_release.set()
        deadline = time.monotonic() + 5
        while len(runtime.lp.scan_forces) < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert runtime.queue_lp_candidate_refresh() is True
        deadline = time.monotonic() + 5
        while len(runtime.lp.scan_forces) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(runtime.lp.scan_forces) == 2
        assert runtime.lp.scan_forces[0] is True
        assert runtime.lp.scan_forces[1] is True
    finally:
        scan_release.set()
        runtime.stop()
    assert runtime.state == "STOPPED"


def test_candidate_monitor_scan_cadence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S6: the candidate loop forces only the first round and manual wakes.

    Ordinary 60-second wakes call refresh_candidates(force=False); the
    service-level 300-second scan window turns those into snapshot reads.
    Only queue_lp_candidate_refresh() (a manual page refresh) requests a
    forced new round.
    """
    import open_trader.prediction_runtime as runtime_module

    class StubLP:
        def __init__(self, _store, _exchange, owner_lock=None) -> None:
            del owner_lock
            self.calls: list[bool] = []

        def refresh_candidates(
            self, *, stop_event=None, force: bool = False
        ) -> dict[str, object]:
            del stop_event
            self.calls.append(force)
            return {"state": "ready", "scanning": False}

        def refresh_candidate_recommendations(
            self, *, stop_event=None
        ) -> dict[str, object]:
            del stop_event
            return {"state": "ready"}

        def candidate_maintenance_wait_seconds(self) -> float:
            return 0.01

        def refresh_rewards(self, *, stop_event=None) -> dict[str, object]:
            del stop_event
            return {"state": "none"}

    class FakeTrading:
        config = SimpleNamespace(
            signer_address="0x" + "1" * 40,
            wallet_address="0x" + "2" * 40,
        )

    class FakeMonitor:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def stop(self) -> None:
            return None

    class FakeExecution:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def reconcile_startup(self) -> dict[str, object]:
            return {"state": "ready"}

        def lp_tick(self) -> dict[str, object]:
            return {"state": "none"}

        def set_cross_venue_monitor(self, _monitor: object) -> None:
            return None

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        runtime_module,
        "PolymarketTradingClient",
        SimpleNamespace(from_keychain=lambda _config: FakeTrading()),
    )
    monkeypatch.setattr(runtime_module, "PolymarketMonitor", FakeMonitor)
    monkeypatch.setattr(runtime_module, "PredictionExecutionService", FakeExecution)
    monkeypatch.setattr(runtime_module, "load_trading_config", lambda _path: FakeTrading.config)
    monkeypatch.setattr(runtime_module, "PolymarketLPService", StubLP)

    runtime = PredictionRuntime(
        data_dir=tmp_path,
        prediction_config_path=tmp_path / "prediction.json",
        dashboard_url="http://127.0.0.1:8766/",
        mode="production",
        n_leg_paused=True,
        enable_n_leg_background=False,
    )
    runtime.start()
    try:
        assert runtime.lp is not None
        # Issue #146 D6: the first round is forced; a ready round then waits
        # out the 300-second scan window instead of polling every minute.
        deadline = time.monotonic() + 5
        while len(runtime.lp.calls) < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert runtime.lp.calls[0] is True
        time.sleep(2.5)
        assert len(runtime.lp.calls) == 1

        # A manual page refresh interrupts the window and forces the round.
        assert runtime.queue_lp_candidate_refresh() is True
        deadline = time.monotonic() + 5
        while len(runtime.lp.calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert runtime.lp.calls[1] is True
        # The window restarts after the forced round: no ordinary wake.
        time.sleep(2.5)
        assert len(runtime.lp.calls) == 2
    finally:
        runtime.stop()
    assert runtime.state == "STOPPED"


def test_lp_trial_maintenance_runs_without_page_and_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_trader.prediction_runtime as runtime_module

    initial_now = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
    clock = {"now": initial_now}
    clock_reads = 0

    def read_clock() -> datetime:
        nonlocal clock_reads
        clock_reads += 1
        return clock["now"]

    condition_ids = ("runtime-condition-a", "runtime-condition-b")
    markets = {
        condition_id: {
            "market_id": f"market-{condition_id}",
            "yes_token": f"{condition_id}-yes",
            "no_token": f"{condition_id}-no",
        }
        for condition_id in condition_ids
    }
    book_calls: list[tuple[str, ...]] = []
    maintenance_started = threading.Event()
    maintenance_stop_seen = threading.Event()
    maintenance_release = threading.Event()
    maintenance_finished = threading.Event()
    history_waiting = threading.Event()
    risk_tick_seen = threading.Event()
    behavior = {"block_maintenance": True, "missing_no": False}

    class FakeTrading:
        config = SimpleNamespace(
            signer_address="0x" + "1" * 40,
            wallet_address="0x" + "2" * 40,
        )

        def lp_reward_catalog(
            self, *, stop_event: threading.Event | None = None
        ) -> dict[str, object]:
            if stop_event is not None and stop_event.is_set():
                return {
                    "state": "unknown",
                    "complete": False,
                    "checked_at": clock["now"],
                    "markets": (),
                }
            return {
                "state": "known",
                "complete": True,
                "checked_at": clock["now"],
                "daily_pool_usd": Decimal("2"),
                "markets": [
                    {
                        "condition_id": condition_id,
                        "rewards_min_size": Decimal("90"),
                        "rewards_max_spread": Decimal("10"),
                        "reward_active": True,
                        "daily_pool_usd": Decimal("2"),
                        "rewards_config": [
                            {
                                "id": f"reward-{condition_id}",
                                "asset_address": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
                                "start_date": "2026-01-01",
                                "end_date": "2026-12-31",
                                "rate_per_day": Decimal("2"),
                            }
                        ],
                    }
                    for condition_id in condition_ids
                ],
            }

        def lp_market_metadata(
            self,
            requested: object,
            *,
            stop_event: threading.Event | None = None,
        ) -> dict[str, dict[str, object]]:
            del stop_event
            condition_ids_requested = tuple(requested)  # type: ignore[arg-type]
            return {
                condition_id: {
                    "market_id": markets[condition_id]["market_id"],
                    "condition_id": condition_id,
                    "market_title": condition_id,
                    "market_url": f"https://polymarket.com/event/{condition_id}",
                    "accepting_orders": True,
                    "metadata_checked_at": clock["now"],
                    "fees_checked_at": clock["now"],
                    "exchange_type": "CLOB",
                    "tick_size": Decimal("0.01"),
                    "minimum_order_size": Decimal("1"),
                    "reward_min_size": Decimal("90"),
                    "reward_max_spread": Decimal("0.10"),
                    "fees_enabled": False,
                    "fee": Decimal("0"),
                    "taker_fee_rate": Decimal("0"),
                    "outcomes": {
                        "yes": {
                            "label": "YES",
                            "token_id": markets[condition_id]["yes_token"],
                        },
                        "no": {
                            "label": "NO",
                            "token_id": markets[condition_id]["no_token"],
                        },
                    },
                }
                for condition_id in condition_ids_requested
                if condition_id in markets
            }

        def lp_price_history(
            self,
            token_ids: object,
            *,
            start_ts: int,
            end_ts: int,
            fidelity: int,
            stop_event: threading.Event | None = None,
        ) -> dict[str, object]:
            del fidelity
            if stop_event is not None and stop_event.is_set():
                return {"state": "cancelled", "history": {}}
            return {
                "state": "known",
                "history": {
                    token_id: [
                        {"t": start_ts, "p": "0.50"},
                        {"t": end_ts, "p": "0.50"},
                    ]
                    for token_id in tuple(token_ids)  # type: ignore[arg-type]
                },
            }

        def lp_account_snapshot(self) -> dict[str, object]:
            return {
                "authenticated": True,
                "wallet_address": self.config.wallet_address,
                "balance": Decimal("100"),
                "allowance": Decimal("100"),
                "open_orders": [],
                "positions": [],
                "open_orders_complete": True,
                "positions_complete": True,
                "checked_at": clock["now"],
            }

        def lp_order_books(
            self,
            token_ids: object,
            *,
            stop_event: threading.Event | None = None,
        ) -> dict[str, dict[str, object]]:
            batch = tuple(token_ids)  # type: ignore[arg-type]
            book_calls.append(batch)
            if len(book_calls) == 2 and behavior["block_maintenance"]:
                maintenance_started.set()
                while not maintenance_release.is_set():
                    if stop_event is not None and stop_event.is_set():
                        maintenance_stop_seen.set()
                        return {}
                    maintenance_stop_seen.wait(0.01)
            now = clock["now"]
            received_at = (
                now - timedelta(seconds=59) if len(book_calls) == 1 else now
            )
            result: dict[str, dict[str, object]] = {}
            for condition_id, market in markets.items():
                for outcome in ("yes", "no"):
                    if (
                        len(book_calls) == 2
                        and behavior["missing_no"]
                        and outcome == "no"
                    ):
                        continue
                    token_id = market[f"{outcome}_token"]
                    if token_id not in batch:
                        continue
                    result[token_id] = {
                        "condition_id": condition_id,
                        "token_id": token_id,
                        "received_at": received_at,
                        "bids": [
                            {"price": Decimal("0.50"), "size": Decimal("1")},
                            {"price": Decimal("0.49"), "size": Decimal("100")},
                        ],
                        "asks": [{"price": Decimal("0.52"), "size": Decimal("100")}],
                    }
            if len(book_calls) == 2 and behavior["missing_no"]:
                maintenance_finished.set()
            return result

        def close(self) -> None:
            return None

    class FakeMonitor:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def stop(self) -> None:
            return None

    class FakeExecution:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def reconcile_startup(self) -> dict[str, object]:
            return {"state": "ready"}

        def lp_tick(self) -> dict[str, object]:
            risk_tick_seen.set()
            return {"state": "none"}

        def set_cross_venue_monitor(self, _monitor: object) -> None:
            return None

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        runtime_module,
        "PolymarketTradingClient",
        SimpleNamespace(from_keychain=lambda _config: FakeTrading()),
    )
    monkeypatch.setattr(runtime_module, "PolymarketMonitor", FakeMonitor)
    monkeypatch.setattr(runtime_module, "PredictionExecutionService", FakeExecution)
    monkeypatch.setattr(runtime_module, "load_trading_config", lambda _path: FakeTrading.config)

    def wait_for_history(stop_event: threading.Event, _seconds: float) -> bool:
        history_waiting.set()
        return stop_event.wait()

    runtime = PredictionRuntime(
        data_dir=tmp_path,
        prediction_config_path=tmp_path / "prediction.json",
        dashboard_url="http://127.0.0.1:8766/",
        mode="production",
        n_leg_paused=True,
        enable_n_leg_background=False,
        history_clock=read_clock,
        history_wait=wait_for_history,
    )
    runtime.start()
    try:
        assert runtime.state == "RUNNING"
        assert history_waiting.wait(timeout=5)
        assert runtime.lp is not None
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            published = runtime.lp.candidate_snapshot()
            if published.get("recommendations"):
                break
            time.sleep(0.01)
        else:
            raise AssertionError("initial candidate recommendation was not published")
        # The initial scan reads the whole two-market batch (both outcomes)
        # in one call.
        assert set(book_calls[0]) == {
            markets[condition_id][f"{outcome}_token"]
            for condition_id in condition_ids
            for outcome in ("yes", "no")
        }
        # Issue #146: the published books are already 59 seconds old, so the
        # 30-second source lead makes maintenance fire on its own about a
        # second after the scan publish; only the head's books are re-read.
        assert maintenance_started.wait(timeout=5)
        assert len(book_calls) == 2
        expected_head = {
            markets[condition_ids[0]]["yes_token"],
            markets[condition_ids[0]]["no_token"],
        }
        # Maintenance refreshes only the merged rank-one market's books.
        assert set(book_calls[1]) == expected_head
        assert risk_tick_seen.wait(timeout=2)
    finally:
        runtime.stop()
    assert runtime.state == "STOPPED"
    assert maintenance_stop_seen.is_set()
    calls_after_stop = len(book_calls)
    time.sleep(0.05)
    assert len(book_calls) == calls_after_stop

    # A partial maintenance result remains eligible on one direction, but the
    # missing direction must not turn the runtime into a 50ms polling loop.
    book_calls.clear()
    clock["now"] = initial_now
    clock_reads = 0
    behavior["block_maintenance"] = False
    behavior["missing_no"] = True
    maintenance_started = threading.Event()
    maintenance_stop_seen = threading.Event()
    maintenance_release = threading.Event()
    maintenance_finished = threading.Event()
    history_waiting = threading.Event()
    risk_tick_seen = threading.Event()
    runtime2 = PredictionRuntime(
        data_dir=tmp_path / "missing-direction",
        prediction_config_path=tmp_path / "missing-direction.json",
        dashboard_url="http://127.0.0.1:8766/",
        mode="production",
        n_leg_paused=True,
        enable_n_leg_background=False,
        history_clock=read_clock,
        history_wait=wait_for_history,
    )
    runtime2.start()
    try:
        assert runtime2.state == "RUNNING"
        assert history_waiting.wait(timeout=5)
        assert runtime2.lp is not None
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            published = runtime2.lp.candidate_snapshot()
            if published.get("recommendations"):
                break
            time.sleep(0.01)
        else:
            raise AssertionError("second candidate recommendation was not published")
        # Issue #146: the 30-second lead fires maintenance on its own because
        # the published books are already 59 seconds old.
        assert maintenance_finished.wait(timeout=5)
        assert len(book_calls) == 2
        reads_after_partial_refresh = clock_reads
        time.sleep(0.35)
        assert clock_reads - reads_after_partial_refresh <= 10
    finally:
        runtime2.stop()
    assert runtime2.state == "STOPPED"


def test_lp_observations_refresh_without_dashboard_and_stop_with_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_trader.prediction_runtime as runtime_module

    class Probe:
        def __init__(self, *, block_rate: bool = False) -> None:
            self.block_rate = block_rate
            self.observation_started = threading.Event()
            self.observation_finished = threading.Event()
            self.rate_started = threading.Event()
            self.rate_cancelled = threading.Event()
            self.lp_tick_seen = threading.Event()
            self.monitor_stopped = threading.Event()
            self.notification_sent = threading.Event()
            self.notifications: list[str] = []
            self.order_writes = 0
            self.cancellations = 0
            self.rate_thread_name = ""

    probe_holder: list[Probe] = []
    order = {
        "order_id": "manual-order",
        "condition_id": "condition-1",
        "token_id": "yes-token",
        "outcome": "YES",
        "side": "BUY",
        "status": "LIVE",
        "price": Decimal("0.50"),
        "original_size": Decimal("100"),
        "size_matched": Decimal("0"),
        "remaining_size": Decimal("100"),
        "reward_min_size": Decimal("40"),
        "fees_enabled": False,
        "market_title": "Will it happen?",
    }

    class FakeTrading:
        def __init__(self, probe: Probe) -> None:
            self.probe = probe
            self.config = SimpleNamespace(
                wallet_address="0x" + "4" * 40,
                signer_address="0x" + "5" * 40,
                predict=None,
            )

        def lp_account_snapshot(self) -> dict[str, object]:
            return {
                "authenticated": True,
                "checked_at": datetime.now(UTC),
                "open_orders": [dict(order)],
                "positions": [],
                "open_orders_complete": True,
                "positions_complete": True,
            }

        def get_order_scoring(self, _order_id: str) -> bool:
            return True

        def lp_reward_snapshot(self, reward_date: str, condition_id: str) -> dict[str, object]:
            return {"state": "unknown", "reward_date": reward_date, "condition_id": condition_id}

        def lp_reward_rates(
            self, *, stop_event: threading.Event | None = None
        ) -> dict[str, object]:
            self.probe.rate_thread_name = threading.current_thread().name
            self.probe.rate_started.set()
            if self.probe.block_rate:
                assert stop_event is not None
                if stop_event.wait(timeout=5):
                    self.probe.rate_cancelled.set()
                    return {
                        "state": "unknown",
                        "complete": False,
                        "checked_at": datetime.now(UTC),
                        "markets": {},
                    }
            checked_at = datetime.now(UTC)
            return {
                "state": "known",
                "complete": True,
                "checked_at": checked_at,
                "markets": {
                    "condition-1": {
                        "state": "known",
                        "hourly_reward_usd": Decimal("0.20"),
                        "currency": "USD",
                        "checked_at": checked_at,
                        "sources": ("native",),
                        "native": {
                            "state": "known",
                            "earning_percentage": Decimal("1"),
                            "hourly_reward_usd": Decimal("0.20"),
                            "currency": "USD",
                        },
                    }
                },
            }

        def lp_order_books(
            self, token_ids: tuple[str, ...]
        ) -> dict[str, dict[str, object]]:
            return {
                token: {
                    "condition_id": "condition-1",
                    "token_id": token,
                    "received_at": datetime.now(UTC),
                    "bids": [
                        {"price": Decimal("0.51"), "size": Decimal("20")},
                        {"price": Decimal("0.50"), "size": Decimal("100")},
                        {"price": Decimal("0.44"), "size": Decimal("1000")},
                    ],
                    "asks": [],
                }
                for token in token_ids
            }

        def create_limit_order(self, **_kwargs: object) -> None:
            self.probe.order_writes += 1

        def post_order(self, _order: object) -> None:
            self.probe.order_writes += 1

        def cancel_orders(self, **_kwargs: object) -> None:
            self.probe.cancellations += 1

        def close(self) -> None:
            pass

    class FakeLP:
        def __init__(self, _store: object, _trading: object, **_kwargs: object) -> None:
            self.probe = probe_holder[0]

        def set_mutation_guard(self, _guard: object) -> None:
            pass

        def refresh_candidates(self, **_kwargs: object) -> dict[str, object]:
            return {"state": "known", "complete": True, "candidates": []}

        def refresh_rewards(self, **_kwargs: object) -> dict[str, object]:
            return {"state": "known"}

        def candidate_snapshot(self) -> dict[str, object]:
            return {
                "state": "known",
                "complete": True,
                "candidates": [],
                "recommendations": [],
                "market_rewards": {},
            }

        def generate_due_report(self) -> None:
            pass

        def status(self, _session_id: str | None = None) -> dict[str, object]:
            return {"state": "none"}

        def close(self) -> None:
            pass

    class FakeMonitor:
        def __init__(self, **_kwargs: object) -> None:
            self.probe = probe_holder[0]

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
            self.probe.monitor_stopped.set()

    class FakeObservationMonitor:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

    class TestExecution(PredictionExecutionService):
        def reconcile_startup(self) -> dict[str, object]:
            return {"state": "ready"}

        def refresh_lp_observations(
            self, *, stop_event: threading.Event | None = None
        ) -> dict[str, object]:
            probe = probe_holder[0]
            probe.observation_started.set()
            try:
                return super().refresh_lp_observations(stop_event=stop_event)
            finally:
                probe.observation_finished.set()

        def lp_tick(self) -> dict[str, object]:
            probe_holder[0].lp_tick_seen.set()
            return {"state": "none"}

    monkeypatch.setattr(runtime_module, "_LP_TICK_SECONDS", 0.01)
    monkeypatch.setattr(runtime_module, "_LP_REWARD_SECONDS", 0.01)
    monkeypatch.setattr(runtime_module, "PolymarketLPService", FakeLP)
    monkeypatch.setattr(runtime_module, "PolymarketMonitor", FakeMonitor)
    monkeypatch.setattr(runtime_module, "PredictionObservationMonitor", FakeObservationMonitor)
    monkeypatch.setattr(runtime_module, "PredictionExecutionService", TestExecution)
    monkeypatch.setattr(runtime_module, "RelationCatalog", lambda _path: object())
    monkeypatch.setattr(runtime_module, "LlmRelationValidator", lambda _store: object())
    monkeypatch.setattr(runtime_module, "LlmTitleTranslator", lambda _store: object())
    monkeypatch.setattr(runtime_module, "ensure_same_event_same_venue_scope", lambda _store: False)
    monkeypatch.setattr(runtime_module, "load_trading_config", lambda _path: SimpleNamespace(
        signer_address="0x" + "5" * 40,
        wallet_address="0x" + "4" * 40,
        predict=None,
    ))
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
    monkeypatch.setattr(PredictionRuntime, "_wire_relation_lifecycle", lambda _self: None)
    monkeypatch.setattr(
        PredictionRuntime,
        "_configure_n_leg_shadow",
        lambda _self: (lambda *_args: None),
    )

    def new_runtime(probe: Probe, name: str) -> PredictionRuntime:
        probe_holder[:] = [probe]

        def post_json(
            _url: str,
            payload: dict[str, object],
            _timeout_seconds: float,
        ) -> dict[str, object]:
            text = payload["content"]["text"]
            probe.notifications.append(str(text))
            probe.notification_sent.set()
            return {"code": 0}

        return PredictionRuntime(
            data_dir=tmp_path / name,
            prediction_config_path=tmp_path / "prediction.json",
            dashboard_url="http://127.0.0.1:8766/",
            notifier=FeishuWebhookNotifier(
                webhook_url="https://feishu.invalid/hook", post_json=post_json
            ),
            cross_venue_monitor=_UnavailableCrossVenueMonitor("test-disabled"),
            solver_server_factory=lambda: object(),
            enable_n_leg_background=False,
        )

    normal_probe = Probe()
    normal = new_runtime(normal_probe, "normal-runtime")
    normal.start()
    try:
        assert normal.state == "RUNNING"
        assert normal_probe.observation_started.wait(timeout=2)
        assert normal_probe.notification_sent.wait(timeout=2)
        assert normal_probe.rate_thread_name == "prediction-lp-reward-monitor"
        assert normal_probe.lp_tick_seen.wait(timeout=2)
        assert len(normal_probe.notifications) == 1
        assert "LP 风险警告" in normal_probe.notifications[0]
        assert "达到 10% 警戒线" in normal_probe.notifications[0]
        assert normal_probe.order_writes == normal_probe.cancellations == 0
    finally:
        normal.stop()
    assert normal.state == "STOPPED"
    assert normal_probe.monitor_stopped.is_set()

    blocked_probe = Probe(block_rate=True)
    blocked = new_runtime(blocked_probe, "blocked-runtime")
    blocked.start()
    assert blocked_probe.rate_started.wait(timeout=2)
    assert blocked_probe.lp_tick_seen.wait(timeout=2)
    # Issue #146: concurrent observation refreshes coalesce onto the shared
    # dashboard snapshot pipeline, so some calls return early instead of
    # queueing behind the blocked rate read. The risk monitor must still
    # keep ticking while the rate read is parked.
    blocked.stop()
    assert blocked.state == "STOPPED"
    assert blocked_probe.rate_cancelled.is_set()
    assert blocked_probe.observation_finished.is_set()
    assert blocked_probe.monitor_stopped.is_set()
    assert blocked_probe.order_writes == blocked_probe.cancellations == 0

    # The real LP service must keep the durable observation path alive while
    # its external candidate catalog read is held.
    from open_trader.notifications import CompositeNotifier, MacOSNotifier
    from open_trader.polymarket_lp import PolymarketLPService as RealLPService

    class IsolationProbe:
        def __init__(self) -> None:
            self.catalog_started = threading.Event()
            self.catalog_cancelled = threading.Event()
            self.catalog_finished = threading.Event()
            self.observation_cycle = threading.Event()
            self.notification_sent = threading.Event()
            self.notifications: list[str] = []
            self.order_writes = 0
            self.cancellations = 0
            self.close_called = threading.Event()
            self.active_reads = 0
            self._lock = threading.Lock()

        def read_started(self) -> None:
            with self._lock:
                self.active_reads += 1

        def read_finished(self) -> None:
            with self._lock:
                self.active_reads -= 1

    isolation_probe = IsolationProbe()
    isolation_order = {
        "order_id": "manual-order",
        "condition_id": "condition-1",
        "token_id": "yes-token",
        "outcome": "YES",
        "side": "BUY",
        "status": "LIVE",
        "price": Decimal("0.50"),
        "original_size": Decimal("100"),
        "size_matched": Decimal("0"),
        "remaining_size": Decimal("100"),
        "reward_min_size": Decimal("40"),
        "fees_enabled": False,
        "market_title": "Will it happen?",
    }

    class IsolationTrading:
        def __init__(self) -> None:
            self.config = SimpleNamespace(
                wallet_address="0x" + "6" * 40,
                signer_address="0x" + "7" * 40,
                predict=None,
            )

        def account_snapshot(self) -> dict[str, object]:
            return {
                "wallet_address": self.config.wallet_address,
                "p_usd_balance": Decimal("100"),
                "p_usd_allowance": Decimal("100"),
                "open_order_ids": ["manual-order"],
                "positions": [],
                "checked_at": datetime.now(UTC),
            }

        def readiness_snapshot(self) -> dict[str, object]:
            return {
                "relayer_ready": True,
                "merge_ready": True,
                "checked_at": datetime.now(UTC),
            }

        def lp_account_snapshot(self) -> dict[str, object]:
            isolation_probe.read_started()
            try:
                isolation_probe.observation_cycle.set()
                return {
                    "authenticated": True,
                    "checked_at": datetime.now(UTC),
                    "open_orders": [dict(isolation_order)],
                    "positions": [],
                    "open_orders_complete": True,
                    "positions_complete": True,
                }
            finally:
                isolation_probe.read_finished()

        def get_order_scoring(self, _order_id: str) -> bool:
            return True

        def lp_reward_snapshot(
            self, reward_date: str, condition_id: str
        ) -> dict[str, object]:
            return {
                "state": "known",
                "reward_date": reward_date,
                "condition_id": condition_id,
                "market_amount": Decimal("0.20"),
            }

        def lp_reward_rates(
            self, *, stop_event: threading.Event | None = None
        ) -> dict[str, object]:
            del stop_event
            checked_at = datetime.now(UTC)
            return {
                "state": "known",
                "complete": True,
                "checked_at": checked_at,
                "markets": {
                    "condition-1": {
                        "state": "known",
                        "hourly_reward_usd": Decimal("0.20"),
                        "currency": "USD",
                        "checked_at": checked_at,
                    }
                },
            }

        def lp_order_books(
            self,
            token_ids: tuple[str, ...],
            *,
            stop_event: threading.Event | None = None,
        ) -> dict[str, dict[str, object]]:
            del stop_event
            checked_at = datetime.now(UTC)
            return {
                token: {
                    "condition_id": "condition-1",
                    "token_id": token,
                    "received_at": checked_at,
                    "bids": [
                        {"price": Decimal("0.51"), "size": Decimal("20")},
                        {"price": Decimal("0.50"), "size": Decimal("100")},
                        {"price": Decimal("0.44"), "size": Decimal("1000")},
                    ],
                    "asks": [],
                }
                for token in token_ids
            }

        def lp_reward_catalog(
            self, *, stop_event: threading.Event | None = None
        ) -> dict[str, object]:
            assert stop_event is not None
            isolation_probe.read_started()
            isolation_probe.catalog_started.set()
            try:
                assert stop_event.wait(timeout=5)
                isolation_probe.catalog_cancelled.set()
                return {
                    "state": "unknown",
                    "complete": False,
                    "checked_at": datetime.now(UTC),
                    "markets": (),
                }
            finally:
                isolation_probe.read_finished()
                isolation_probe.catalog_finished.set()

        def create_limit_order(self, **_kwargs: object) -> None:
            isolation_probe.order_writes += 1

        def post_order(self, _order: object) -> None:
            isolation_probe.order_writes += 1

        def post_orders(self, *_orders: object, **_kwargs: object) -> None:
            isolation_probe.order_writes += 1

        def cancel_order(self, *_args: object, **_kwargs: object) -> None:
            isolation_probe.cancellations += 1

        def cancel_orders(self, **_kwargs: object) -> None:
            isolation_probe.cancellations += 1

        def close(self) -> None:
            with isolation_probe._lock:
                assert isolation_probe.active_reads == 0
            assert isolation_probe.catalog_finished.is_set()
            isolation_probe.close_called.set()

    class NoopMacOSNotifier(MacOSNotifier):
        def notify(self, _title: str, _message: str) -> None:
            pass

    isolation_trading = IsolationTrading()

    def isolation_post_json(
        _url: str,
        payload: dict[str, object],
        _timeout_seconds: float,
    ) -> dict[str, object]:
        content = payload.get("content")
        text = content.get("text") if isinstance(content, dict) else ""
        isolation_probe.notifications.append(str(text))
        isolation_probe.notification_sent.set()
        return {"code": 0}

    isolation_notifier = CompositeNotifier(
        (
            NoopMacOSNotifier(),
            FeishuWebhookNotifier(
                webhook_url="https://feishu.invalid/hook",
                post_json=isolation_post_json,
            ),
        )
    )
    monkeypatch.setattr(runtime_module, "PolymarketLPService", RealLPService)
    monkeypatch.setattr(runtime_module, "PredictionExecutionService", PredictionExecutionService)
    monkeypatch.setattr(
        runtime_module,
        "PolymarketTradingClient",
        SimpleNamespace(from_keychain=lambda _config: isolation_trading),
    )
    isolated = PredictionRuntime(
        data_dir=tmp_path / "isolated-runtime",
        prediction_config_path=tmp_path / "prediction.json",
        dashboard_url="http://127.0.0.1:8766/",
        notifier=isolation_notifier,
        cross_venue_monitor=_UnavailableCrossVenueMonitor("test-disabled"),
        solver_server_factory=lambda: object(),
        enable_n_leg_background=False,
    )
    isolated.start()
    account_id = hashlib.sha256(
        isolation_trading.config.wallet_address.casefold().encode("utf-8")
    ).hexdigest()
    try:
        assert isolated.state == "RUNNING"
        assert isolation_probe.catalog_started.wait(timeout=2)
        checked_at_values: set[str] = set()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and len(checked_at_values) < 2:
            observations = isolated.store.lp_observations(account_id)  # type: ignore[union-attr]
            checked_at_values.update(
                str(row.get("checked_at"))
                for row in observations.values()
                if row.get("checked_at") is not None
            )
            if len(checked_at_values) < 2:
                isolation_probe.observation_cycle.wait(timeout=0.01)
                isolation_probe.observation_cycle.clear()
        assert len(checked_at_values) >= 2
        assert isolation_probe.notification_sent.wait(timeout=2)
        observations = isolated.store.lp_observations(account_id)  # type: ignore[union-attr]
        observation = observations["condition-1"]
        assert Decimal(str(observation["occupied_capital_usd"])) == Decimal("50")
        assert Decimal(str(observation["current_yield_pct_per_hour"])) == Decimal("0.4")
        assert Decimal(str(observation["risk_directions"][0]["stress_loss"])) == Decimal("6")
        assert Decimal(str(observation["risk_directions"][0]["loss_ratio"])) == Decimal("0.12")
        assert observation["risk_directions"][0]["warning"] is True
        assert observation["add_room"] == {"available": False, "reason": "risk_warning"}
        assert len(isolation_probe.notifications) == 1
        assert "LP 风险警告" in isolation_probe.notifications[0]
        assert "达到 10% 警戒线" in isolation_probe.notifications[0]
        assert isolation_probe.order_writes == isolation_probe.cancellations == 0
    finally:
        isolated.stop()
    assert isolated.state == "STOPPED"
    assert isolation_probe.catalog_cancelled.is_set()
    assert isolation_probe.catalog_finished.is_set()
    assert isolation_probe.close_called.is_set()
    assert not isolated.production_owner
    with isolation_probe._lock:
        assert isolation_probe.active_reads == 0


def test_lp_share_watch_runs_without_dashboard_and_stops_with_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_trader.prediction_runtime as runtime_module
    from open_trader.polymarket_trading import PolymarketTradingClient

    data_dir = tmp_path / "runtime-data"
    config = SimpleNamespace(
        signer_address="0x" + "1" * 40,
        wallet_address="0x" + "2" * 40,
        predict=None,
    )
    account_id = hashlib.sha256(
        config.wallet_address.casefold().encode("utf-8")
    ).hexdigest()
    controlled_clock = [0.0]
    probe = SimpleNamespace(
        lock=threading.Lock(),
        order_reads=0,
        active_order_reads=0,
        max_active_order_reads=0,
        first_order_read=threading.Event(),
        percentage_reads=0,
        first_percentage_read=threading.Event(),
        percentage_active=0,
        max_percentage_active=0,
        percentage_started=threading.Event(),
        percentage_release=threading.Event(),
        percentage_blocked=False,
        reward_rate_started=threading.Event(),
        reward_rate_cancelled=threading.Event(),
        orders_started=threading.Event(),
        orders_release=threading.Event(),
        orders_blocked=False,
        unknown_orders=False,
        trading_closed=threading.Event(),
        lp_closed=threading.Event(),
        notifications=[],
    )
    probe.percentage_release.set()
    probe.orders_release.set()
    orders = [
        {
            "id": "order-a",
            "market": "condition-a",
            "asset_id": "token-a",
            "side": "BUY",
            "status": "LIVE",
            "price": "0.50",
            "original_size": "10",
            "size_matched": "0",
        },
        {
            "id": "order-b",
            "market": "condition-b",
            "asset_id": "token-b",
            "side": "BUY",
            "status": "LIVE",
            "price": "0.50",
            "original_size": "10",
            "size_matched": "0",
        },
    ]

    class FakeSDK:
        def list_open_orders(self) -> list[dict[str, str]]:
            with probe.lock:
                probe.order_reads += 1
                probe.active_order_reads += 1
                probe.max_active_order_reads = max(
                    probe.max_active_order_reads, probe.active_order_reads
                )
                probe.first_order_read.set()
                if probe.orders_blocked:
                    probe.orders_started.set()
            try:
                if probe.unknown_orders:
                    raise RuntimeError("open orders unavailable")
                if probe.orders_blocked:
                    probe.orders_release.wait(timeout=5)
                return [dict(row) for row in orders]
            finally:
                with probe.lock:
                    probe.active_order_reads -= 1

    class RuntimeTrading(PolymarketTradingClient):
        def __init__(self) -> None:
            super().__init__(config, FakeSDK())

        def lp_account_snapshot(self) -> dict[str, object]:
            return {
                "authenticated": True,
                "checked_at": datetime.now(UTC),
                "open_orders": [dict(row) for row in orders],
                "positions": [],
                "open_orders_complete": True,
                "positions_complete": True,
            }

        def lp_reward_percentages(self) -> dict[str, object]:
            with probe.lock:
                probe.percentage_reads += 1
                probe.percentage_active += 1
                probe.max_percentage_active = max(
                    probe.max_percentage_active, probe.percentage_active
                )
                probe.first_percentage_read.set()
                if probe.percentage_blocked:
                    probe.percentage_started.set()
            try:
                if probe.percentage_blocked:
                    probe.percentage_release.wait(timeout=5)
                return {
                    "state": "known",
                    "scope": "account",
                    "maker_address": config.wallet_address,
                    "percentages": {
                        "condition-a": Decimal("7"),
                        "condition-b": Decimal("7"),
                    },
                    "checked_at": datetime.now(UTC),
                }
            finally:
                with probe.lock:
                    probe.percentage_active -= 1

        def lp_reward_rates(
            self, *, stop_event: threading.Event | None = None
        ) -> dict[str, object]:
            probe.reward_rate_started.set()
            if stop_event is not None:
                while not stop_event.wait(0.01):
                    pass
                probe.reward_rate_cancelled.set()
            return {
                "state": "unknown",
                "complete": False,
                "checked_at": datetime.now(UTC),
                "markets": {},
            }

        def lp_order_books(
            self,
            token_ids: tuple[str, ...],
            *,
            stop_event: threading.Event | None = None,
        ) -> dict[str, dict[str, object]]:
            del stop_event
            checked_at = datetime.now(UTC)
            return {
                token: {
                    "condition_id": "condition-a"
                    if token == "token-a"
                    else "condition-b",
                    "token_id": token,
                    "received_at": checked_at,
                    "bids": [],
                    "asks": [],
                }
                for token in token_ids
            }

        def get_order_scoring(self, _order_id: str) -> bool:
            return False

        def close(self) -> None:
            probe.trading_closed.set()

    class FakeLP:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def set_mutation_guard(self, _guard: object) -> None:
            pass

        def refresh_candidates(self, **_kwargs: object) -> dict[str, object]:
            return {"state": "known", "complete": True, "candidates": []}

        def refresh_rewards(self, **_kwargs: object) -> dict[str, object]:
            return {"state": "known"}

        def refresh_price_history(self, **_kwargs: object) -> dict[str, object]:
            return {"state": "known"}

        def candidate_snapshot(self) -> dict[str, object]:
            return {
                "state": "known",
                "complete": True,
                "candidates": [],
                "recommendations": [],
                "market_rewards": {},
            }

        def status(self, _session_id: str | None = None) -> dict[str, object]:
            return {"state": "none"}

        def close(self) -> None:
            probe.lp_closed.set()

    class FakeMonitor:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
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
            pass

    class FakeObservationMonitor:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

    class TestExecution(PredictionExecutionService):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)
            self._clock = lambda: controlled_clock[0]

        def reconcile_startup(self) -> dict[str, object]:
            return {"state": "ready"}

        def lp_tick(self) -> dict[str, object]:
            return {"state": "none"}

    class RecordingNotifier:
        def notify(self, title: str, message: str) -> None:
            probe.notifications.append((title, message))

    trading = RuntimeTrading()
    monkeypatch.setattr(runtime_module, "_LP_SHARE_WATCH_SECONDS", 0.01, raising=False)
    # Issue #146: the observation refresh coalesces onto the shared snapshot
    # pipeline, so retry it quickly until the snapshot thread has published.
    monkeypatch.setattr(runtime_module, "_LP_REWARD_SECONDS", 0.1)
    monkeypatch.setattr(runtime_module, "_LP_REWARD_STOP_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(runtime_module, "PolymarketLPService", FakeLP)
    monkeypatch.setattr(runtime_module, "PolymarketMonitor", FakeMonitor)
    monkeypatch.setattr(
        runtime_module, "PredictionObservationMonitor", FakeObservationMonitor
    )
    monkeypatch.setattr(runtime_module, "PredictionExecutionService", TestExecution)
    monkeypatch.setattr(runtime_module, "RelationCatalog", lambda _path: object())
    monkeypatch.setattr(runtime_module, "LlmRelationValidator", lambda _store: object())
    monkeypatch.setattr(runtime_module, "LlmTitleTranslator", lambda _store: object())
    monkeypatch.setattr(
        runtime_module, "ensure_same_event_same_venue_scope", lambda _store: False
    )
    monkeypatch.setattr(runtime_module, "load_trading_config", lambda _path: config)
    monkeypatch.setattr(
        runtime_module,
        "PolymarketTradingClient",
        SimpleNamespace(from_keychain=lambda _config: trading),
    )
    monkeypatch.setattr(
        runtime_module,
        "PredictTradingClient",
        SimpleNamespace(from_keychain=lambda _config: None),
    )
    monkeypatch.setattr(PredictionRuntime, "_wire_relation_lifecycle", lambda _self: None)
    monkeypatch.setattr(
        PredictionRuntime,
        "_configure_n_leg_shadow",
        lambda _self: (lambda *_args: None),
    )

    seed_store = PredictionArbitrageStore(data_dir)
    for condition_id, title in (("condition-a", "Market A"), ("condition-b", "Market B")):
        seed_store.save_lp_observation(
            account_id,
            condition_id,
            {
                "market_title": title,
                "checked_at": datetime.now(UTC),
                "share_alert": {
                    "enabled": True,
                    "paused": False,
                    "notification_sent": False,
                    "notification_pending": False,
                },
            },
        )

    runtime = PredictionRuntime(
        data_dir=data_dir,
        prediction_config_path=tmp_path / "prediction.json",
        dashboard_url="http://127.0.0.1:8766/",
        notifier=RecordingNotifier(),
        cross_venue_monitor=_UnavailableCrossVenueMonitor("test-disabled"),
        solver_server_factory=lambda: object(),
        enable_n_leg_background=False,
    )

    def wait_for(predicate: object, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():  # type: ignore[operator]
                return
            time.sleep(0.005)
        assert predicate()  # type: ignore[operator]

    try:
        runtime.start()
        assert runtime.state == "RUNNING"
        wait_for(probe.first_percentage_read.is_set)
        assert probe.percentage_reads == 1
        wait_for(probe.first_order_read.is_set)
        assert probe.order_reads >= 1
        wait_for(probe.reward_rate_started.is_set)

        controlled_clock[0] = 5.0
        time.sleep(0.05)
        assert probe.percentage_reads == 1

        controlled_clock[0] = 10.0
        wait_for(lambda: probe.percentage_reads == 2)

        probe.percentage_blocked = True
        probe.percentage_release.clear()
        probe.percentage_started.clear()
        controlled_clock[0] = 20.0
        wait_for(probe.percentage_started.is_set)
        assert probe.max_percentage_active == 1

        dashboard_done = threading.Event()
        dashboard_results: list[dict[str, object]] = []

        def read_dashboard() -> None:
            assert runtime.execution is not None
            # Issue #146: the page reads the published cache; the pipeline
            # (and the blocked percentage read) is owned by the snapshot
            # refresh, which is what must wait here.
            dashboard_results.append(
                runtime.execution.refresh_lp_dashboard_snapshot()
            )
            dashboard_done.set()

        dashboard_thread = threading.Thread(target=read_dashboard)
        dashboard_thread.start()
        assert not dashboard_done.wait(timeout=0.05)
        controlled_clock[0] = 35.0
        probe.percentage_release.set()
        dashboard_thread.join(timeout=1)
        assert dashboard_done.is_set()
        assert dashboard_results[0]["state"] == "ready"
        assert probe.percentage_reads == 3
        assert probe.max_percentage_active == 1

        probe.percentage_blocked = False
        controlled_clock[0] = 30.0
        assert runtime.execution is not None
        execution = runtime.execution

        def share_checked_at(condition_id: str) -> str:
            alert = execution.lp_share_watch_state().get(condition_id, {})
            return str(alert.get("last_share_checked_at") or "")

        wait_for(lambda: share_checked_at("condition-a") != "")
        pre_clear_attempt = (
            execution.lp_share_watch_state()
            .get("condition-a", {})
            .get("last_attempt_at")
        )
        wait_for(
            lambda: execution.lp_share_watch_state()
            .get("condition-a", {})
            .get("last_attempt_at")
            != pre_clear_attempt
        )
        cleared_checked_at = share_checked_at("condition-a")
        cleared_attempt_a = (
            execution.lp_share_watch_state()
            .get("condition-a", {})
            .get("last_attempt_at")
        )
        cleared_attempt_b = (
            execution.lp_share_watch_state()
            .get("condition-b", {})
            .get("last_attempt_at")
        )
        orders.clear()
        wait_for(
            lambda: (
                execution.lp_share_watch_state()
                .get("condition-a", {})
                .get("last_attempt_at")
                != cleared_attempt_a
                and execution.lp_share_watch_state()
                .get("condition-b", {})
                .get("last_attempt_at")
                != cleared_attempt_b
                and share_checked_at("condition-a") == cleared_checked_at
                and share_checked_at("condition-b") == cleared_checked_at
                and execution.lp_share_watch_state()
                .get("condition-a", {})
                .get("breach_started_at")
                is None
                and execution.lp_share_watch_state()
                .get("condition-b", {})
                .get("breach_started_at")
                is None
            )
        )
        cleared_state = execution.lp_share_watch_state()
        assert cleared_state["condition-a"]["last_share_percentage"] is not None
        assert cleared_state["condition-b"]["last_share_percentage"] is not None

        orders.append(
            {
                "id": "order-a-new",
                "market": "condition-a",
                "asset_id": "token-a",
                "side": "BUY",
                "status": "LIVE",
                "price": "0.50",
                "original_size": "10",
                "size_matched": "0",
            }
        )
        controlled_clock[0] = 46.0
        wait_for(lambda: share_checked_at("condition-a") > cleared_checked_at)
        resumed = execution.lp_share_watch_state()
        assert share_checked_at("condition-a") > cleared_checked_at
        assert share_checked_at("condition-b") == cleared_checked_at

        probe.unknown_orders = True
        controlled_clock[0] = 50.0
        previous_attempt = resumed["condition-a"].get("last_attempt_at")
        frozen_checked_at = resumed["condition-a"].get("last_share_checked_at")
        wait_for(
            lambda: execution.lp_share_watch_state()
            .get("condition-a", {})
            .get("last_attempt_at")
            != previous_attempt
        )
        unknown = execution.lp_share_watch_state()
        assert unknown["condition-a"]["last_share_percentage"] is not None
        assert unknown["condition-a"]["breach_started_at"] is None
        assert share_checked_at("condition-a") == str(frozen_checked_at or "")

        probe.unknown_orders = False
        probe.orders_started.clear()
        probe.orders_release.clear()
        probe.orders_blocked = True
        controlled_clock[0] = 60.0
        wait_for(probe.orders_started.is_set)
        percentage_reads_at_stop = probe.percentage_reads

        stop_done = threading.Event()
        stop_errors: list[BaseException] = []

        def stop_runtime() -> None:
            try:
                runtime.stop()
            except BaseException as exc:
                stop_errors.append(exc)
            finally:
                stop_done.set()

        stop_thread = threading.Thread(target=stop_runtime)
        stop_thread.start()
        assert stop_done.wait(timeout=1)
        stop_thread.join(timeout=1)
        assert stop_errors
        assert runtime.state == "STOPPING"
        assert runtime.production_owner is True
        assert runtime.store is not None
        assert not probe.trading_closed.is_set()
        assert not probe.lp_closed.is_set()

        probe.orders_blocked = False
        probe.orders_release.set()
        wait_for(lambda: probe.active_order_reads == 0)
        reads_after_release = probe.order_reads
        time.sleep(0.05)
        assert probe.order_reads == reads_after_release
        assert probe.percentage_reads == percentage_reads_at_stop
        runtime.stop()
        assert runtime.state == "STOPPED"
        assert runtime.production_owner is False
        assert probe.trading_closed.is_set()
        assert probe.lp_closed.is_set()
    finally:
        if runtime.state not in {"NEW", "STOPPED"}:
            probe.orders_blocked = False
            probe.orders_release.set()
            probe.percentage_release.set()
            runtime.stop()


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


def test_shadow_evidence_codex_counters_come_from_llm_attributes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """healthz 的 codex 计数必须读 validator 的 llm_calls/llm_successes。"""

    import contextlib

    import open_trader.prediction_runtime as runtime_module

    class CountingValidator:
        llm_calls = 5
        llm_successes = 3

    class FakeStore:
        def __init__(self, _data_dir: object) -> None:
            pass

        def close(self) -> None:
            pass

    class FakeClient:
        @classmethod
        def from_keychain(cls, _config: object) -> "FakeClient":
            return FakeClient()

        def close(self) -> None:
            pass

    class FakeMonitor:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def set_ready_observer(self, _observer: object) -> None:
            pass

        def set_observation_observer(self, _observer: object) -> None:
            pass

        def set_failure_observer(self, _observer: object) -> None:
            pass

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

    class FakeExecution:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def notify_ready_opportunity(self, *_args: object) -> None:
            pass

        notify_observation = notify_ready_opportunity
        notify_monitor_failure = notify_ready_opportunity

        def set_cross_venue_monitor(self, _monitor: object) -> None:
            pass

        def close(self) -> None:
            pass

    class FakeCrossMonitor:
        async def start(self) -> None:
            pass

        async def stop(self) -> None:
            pass

        def snapshot(self) -> dict[str, object]:
            return {"status": "ready"}

    class Owner:
        def acquire(self) -> None:
            pass

        def release(self) -> None:
            pass

    @contextlib.contextmanager
    def fake_guard(client: object, _guard: object):
        yield client

    monkeypatch.setattr(runtime_module, "PredictionArbitrageStore", FakeStore)
    monkeypatch.setattr(runtime_module, "PolymarketTradingClient", FakeClient)
    monkeypatch.setattr(runtime_module, "PredictTradingClient", FakeClient)
    monkeypatch.setattr(runtime_module, "load_trading_config", lambda _path: object())
    monkeypatch.setattr(runtime_module, "PolymarketMonitor", FakeMonitor)
    monkeypatch.setattr(runtime_module, "PredictionExecutionService", FakeExecution)
    monkeypatch.setattr(runtime_module, "LlmRelationValidator", lambda *_a, **_k: CountingValidator())
    monkeypatch.setattr(runtime_module, "LlmTitleTranslator", lambda *_a, **_k: object())
    monkeypatch.setattr(
        runtime_module,
        "_build_cross_venue_monitor",
        lambda **_kwargs: FakeCrossMonitor(),
    )
    monkeypatch.setattr(runtime_module, "PolymarketReadOnlyGuard", lambda *_a: object())
    monkeypatch.setattr(runtime_module, "PredictReadOnlyGuard", lambda *_a: object())
    monkeypatch.setattr(runtime_module, "guard_polymarket_client", fake_guard)
    monkeypatch.setattr(runtime_module, "guard_predict_client", fake_guard)

    runtime = PredictionRuntime(
        data_dir=tmp_path / "shadow",
        prediction_config_path=tmp_path / "prediction.json",
        dashboard_url="http://127.0.0.1:8766/",
        mode="shadow",
    )
    runtime._owner = Owner()  # type: ignore[assignment]
    try:
        runtime.start()
        assert runtime.state == "RUNNING"
        assert runtime.shadow_evidence["codex"]["relation"] == {
            "calls": 5,
            "successes": 3,
        }
    finally:
        runtime.stop()


def test_n_leg_pause_suppresses_shadow_background_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_trader.prediction_runtime as runtime_module

    external_calls: list[str] = []

    def fail_solver() -> object:
        external_calls.append("solver.construct")
        raise AssertionError("paused shadow must not construct the solver")

    class FakePolymarketTradingClient:
        @classmethod
        def from_keychain(cls, _config: object) -> object:
            external_calls.append("polymarket.construct")
            raise AssertionError("paused shadow must not construct a venue client")

    class FakePredictTradingClient:
        @classmethod
        def from_keychain(cls, _config: object) -> object:
            external_calls.append("predict.construct")
            raise AssertionError("paused shadow must not construct a Predict client")

    monkeypatch.setattr(
        runtime_module, "PolymarketTradingClient", FakePolymarketTradingClient
    )
    monkeypatch.setattr(runtime_module, "PredictTradingClient", FakePredictTradingClient)

    def make_runtime() -> PredictionRuntime:
        return PredictionRuntime(
            data_dir=tmp_path / "shadow",
            prediction_config_path=tmp_path / "prediction.json",
            dashboard_url="http://127.0.0.1:8766/",
            mode="shadow",
            solver_server_factory=fail_solver,
        )

    monkeypatch.setenv("OPEN_TRADER_NLEG_PAUSED", "1")
    runtime = make_runtime()
    runtime.start()
    try:
        assert runtime.state == "RUNNING"
        assert runtime.mode == "shadow"
        assert runtime.n_leg_paused is True
        assert runtime.production_owner is False
        assert runtime.store is not None
        assert runtime.solver_server is None
        assert runtime.monitor is None
        assert runtime.observation_monitor is None
        assert runtime.cross_venue_monitor is None
        assert runtime.n_leg_shadow is None
        assert runtime.predict_snapshot_refresher is None
        assert runtime.shadow_evidence == {
            "mode": "shadow",
            "guard_attempts": [],
            "first_violation": None,
            "codex": {
                "relation": {"calls": 0, "successes": 0},
                "cross_venue": {"calls": 0, "successes": 0},
            },
        }
        competing_owner = _RuntimeOwnershipLock(
            tmp_path / "shadow" / "prediction_arbitrage" / "runtime.lock"
        )
        with pytest.raises(PredictionRuntimeOwnershipError):
            competing_owner.acquire()
    finally:
        runtime.stop()

    restarted = make_runtime()
    restarted.start()
    try:
        assert restarted.state == "RUNNING"
        assert restarted.store is not None
        assert restarted.store.data_dir == tmp_path / "shadow"
    finally:
        restarted.stop()

    assert external_calls == []


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


def test_lp_runtime_stops_obsolete_sampling_and_keeps_exposure_risk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_trader.prediction_runtime as runtime_module

    now = datetime.now(UTC)
    risk_seen = threading.Event()
    candidate_scan_seen = threading.Event()
    history_started = threading.Event()
    release_history = threading.Event()
    history_finished = threading.Event()
    sampler_book_calls: list[tuple[str, ...]] = []
    history_calls: list[dict[str, object]] = []

    class MacOSNotifier:
        channel = "macos"

    class FeishuNotifier:
        channel = "feishu"

    class FakeTrading:
        def __init__(self) -> None:
            self.lp_snapshot_calls = 0

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
            return {
                "relayer_ready": True,
                "merge_ready": True,
                "checked_at": now,
            }

        def lp_reward_catalog(
            self, *, stop_event: threading.Event | None = None
        ) -> dict[str, object]:
            return {
                "state": "known",
                "complete": True,
                "checked_at": now,
                "markets": (
                    {
                        "condition_id": "history-condition",
                        "daily_pool_usd": Decimal("100"),
                        "reward_active": True,
                    },
                ),
            }

        def lp_market_metadata(
            self, condition_ids: tuple[str, ...], *, stop_event: object = None
        ) -> dict[str, object]:
            del stop_event
            return {
                "history-condition": {
                    "market_id": "history-market",
                    "condition_id": "history-condition",
                    "accepting_orders": True,
                    "reward_min_size": Decimal("20"),
                    "reward_max_spread": Decimal("0.10"),
                    "outcomes": {
                        "yes": {
                            "label": "YES",
                            "token_id": "history-token",
                        }
                    },
                }
            } if "history-condition" in condition_ids else {}

        def lp_price_history(
            self,
            token_ids: tuple[str, ...],
            *,
            start_ts: int,
            end_ts: int,
            fidelity: int,
            stop_event: object = None,
        ) -> dict[str, object]:
            del stop_event
            history_started.set()
            if not release_history.wait(timeout=5):
                raise AssertionError("held history read was not released")
            history_calls.append(
                {
                    "token_ids": token_ids,
                    "start_ts": start_ts,
                    "end_ts": end_ts,
                    "fidelity": fidelity,
                }
            )
            history_finished.set()
            return {
                "state": "known",
                "history": {
                    "history-token": [
                        {
                            "t": start_ts,
                            "p": Decimal("0.50"),
                        },
                        {"t": end_ts, "p": Decimal("0.52")},
                    ]
                },
            }

        def lp_account_snapshot(self) -> dict[str, object]:
            # Issue #146: the dashboard snapshot thread also reads the
            # account, so scan consumption is observed on the books read
            # (which only the candidate scan performs) instead of here.
            return {
                "authenticated": True,
                "balance": Decimal("100"),
                "allowance": Decimal("100"),
                "open_orders": [],
                "positions": [],
                "checked_at": now,
                "open_orders_complete": True,
                "positions_complete": True,
            }

        def lp_order_books(
            self, token_ids: tuple[str, ...], *, stop_event: object = None
        ) -> dict[str, object]:
            del stop_event
            sampler_book_calls.append(tuple(token_ids))
            candidate_scan_seen.set()
            return {}

        def lp_reward_snapshot(
            self,
            reward_date: str,
            condition_id: str,
            *,
            stop_event: threading.Event | None = None,
        ) -> dict[str, object]:
            del reward_date, condition_id, stop_event
            return {"state": "unknown"}

        def lp_snapshot(self, request: Mapping[str, object]) -> dict[str, object]:
            self.lp_snapshot_calls += 1
            risk_seen.set()
            token_id = str(request["token_id"])
            market_id = str(request["market_id"])
            condition_id = str(request["condition_id"])
            order = {
                "order_id": "exposure-entry",
                "id": "exposure-entry",
                "market_id": market_id,
                "condition_id": condition_id,
                "token_id": token_id,
                "side": "BUY",
                "status": "LIVE",
                "price": Decimal("0.50"),
                "original_size": Decimal("20"),
                "size_matched": Decimal("0"),
            }
            return {
                "account": {
                    "authenticated": True,
                    "open_orders": [order],
                    "positions": [],
                },
                "market": {
                    "market_id": market_id,
                    "condition_id": condition_id,
                    "token_id": token_id,
                    "outcome": str(request["outcome"]),
                    "accepting_orders": True,
                    "minimum_order_size": Decimal("1"),
                    "tick_size": Decimal("0.01"),
                    "fee": Decimal("0"),
                    "fees_enabled": False,
                    "taker_fee_rate": Decimal("0"),
                    "fee_exponent": Decimal("1"),
                },
                "book": {
                    "received_at": now,
                    "bids": [{"price": Decimal("0.49"), "size": Decimal("20")}],
                    "asks": [{"price": Decimal("0.51"), "size": Decimal("20")}],
                },
                "orders": [order],
                "trades": [],
            }

        def close(self) -> None:
            return None

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
            pass

    config = SimpleNamespace(
        signer_address="0x1111111111111111111111111111111111111111",
        wallet_address="0x2222222222222222222222222222222222222222",
        predict=None,
    )
    trading = FakeTrading()
    monkeypatch.setattr(runtime_module, "load_trading_config", lambda _path: config)
    monkeypatch.setattr(
        runtime_module,
        "PolymarketTradingClient",
        SimpleNamespace(from_keychain=lambda _config: trading),
    )
    monkeypatch.setattr(
        runtime_module,
        "PredictTradingClient",
        SimpleNamespace(from_keychain=lambda _config: None),
    )
    monkeypatch.setattr(runtime_module, "PolymarketMonitor", FakeMonitor)
    monkeypatch.setattr(runtime_module, "LlmRelationValidator", lambda *_a, **_k: object())
    monkeypatch.setattr(runtime_module, "LlmTitleTranslator", lambda *_a, **_k: object())
    real_lp_service = runtime_module.PolymarketLPService
    monkeypatch.setattr(
        runtime_module,
        "PolymarketLPService",
        lambda store, trading, *, owner_lock=None: real_lp_service(
            store,
            trading,
            owner_lock=owner_lock,
            clock=lambda: now,
        ),
    )
    monkeypatch.setattr(runtime_module, "_LP_TICK_SECONDS", 0.01)
    monkeypatch.setattr(runtime_module, "_LP_REWARD_SECONDS", 3600)
    monkeypatch.setattr(runtime_module, "_LP_REWARD_STOP_GRACE_SECONDS", 0.05)

    runtime = PredictionRuntime(
        data_dir=tmp_path,
        prediction_config_path=tmp_path / "prediction.json",
        dashboard_url="http://127.0.0.1:8766/",
        notifier=SimpleNamespace(
            _notifiers=(MacOSNotifier(), FeishuNotifier())
        ),
        cross_venue_monitor=_UnavailableCrossVenueMonitor("test-disabled"),
        solver_server_factory=lambda: object(),
        enable_n_leg_background=False,
    )
    try:
        runtime.start()
        assert runtime.state == "RUNNING"
        assert runtime.store is not None
        assert runtime.lp is not None
        assert runtime.execution is not None
        assert history_started.wait(timeout=2)
        preparing = runtime.lp.candidate_snapshot()
        deadline = time.monotonic() + 2
        while (
            preparing.get("retention_reason") is None
        ) and time.monotonic() < deadline:
            time.sleep(0.01)
            preparing = runtime.lp.candidate_snapshot()
        assert preparing.get("retention_reason") == "catalog_preparation_pending"
        assert preparing["recommendations"] == []
        assert preparing["selected_results"] == []
        assert preparing["selected_market_ids"] == []
        assert not candidate_scan_seen.is_set()

        runtime.store.lp_create_session(
            "exposure-session",
            "exposure-idempotency",
            state="entry_open",
            payload={
                "market_id": "outside-candidate-market",
                "condition_id": "outside-candidate-condition",
                "token_id": "outside-candidate-token",
                "outcome": "YES",
                "price": Decimal("0.50"),
                "quantity": Decimal("20"),
                "review_at": (now + timedelta(hours=1)).isoformat(),
                "entry_order_id": "exposure-entry",
                "owned_order_ids": ["exposure-entry"],
                "order_history": {
                    "exposure-entry": {
                        "order_id": "exposure-entry",
                        "token_id": "outside-candidate-token",
                        "side": "BUY",
                        "status": "LIVE",
                        "price": Decimal("0.50"),
                        "quantity": Decimal("20"),
                    }
                },
                "trade_events": [],
                "buy_filled_quantity": Decimal("0"),
                "buy_cost": Decimal("0"),
                "sold_quantity": Decimal("0"),
                "sold_revenue": Decimal("0"),
                "residual_quantity": Decimal("0"),
                "residual_exit_value": Decimal("0"),
                "fees": Decimal("0"),
                "fee_status": "known",
                "position_reconciled": True,
                "orders_terminal": False,
                "entry_cancel_requested": False,
                "stop_loss_latched": False,
                "scoring_status": "unknown",
                "reward_date": now.date().isoformat(),
            },
        )
        assert risk_seen.wait(timeout=2), "existing LP exposure was not reconciled"
        assert trading.lp_snapshot_calls > 0
        assert runtime._book_sampler_thread is None
        assert sampler_book_calls == []
        with pytest.raises(RuntimeError, match="history monitor thread did not stop"):
            runtime.stop()
        assert runtime.state == "STOPPING"
        assert runtime.lp is not None
        assert runtime.execution is not None
        assert runtime._prediction_trading is trading
        assert runtime.store is not None
        release_history.set()
        assert history_finished.wait(timeout=2)
        assert history_calls and history_calls[0]["token_ids"] == ("history-token",)
        deadline = time.monotonic() + 2
        history_summary = None
        while history_summary is None and time.monotonic() < deadline:
            history_summary = runtime.store.lp_price_history_summary(
                "history-condition", "history-token"
            )
            if history_summary is None:
                time.sleep(0.01)
        assert history_summary is not None
        assert history_summary["state"] == "known"
        assert runtime.store.lp_book_samples(
            "outside-candidate-condition",
            "outside-candidate-token",
            since=now - timedelta(minutes=1),
            until=now + timedelta(minutes=1),
        ) == []
        runtime.stop()
        assert runtime.state == "STOPPED"

    finally:
        release_history.set()
        if runtime.state not in {"NEW", "STOPPED"}:
            runtime.stop()


@pytest.mark.parametrize(
    ("retry_succeeds", "catalog_unknown"),
    [(False, False), (True, False), (False, True)],
)
def test_lp_preparation_retries_after_five_minutes_and_alerts_on_repeat_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    retry_succeeds: bool,
    catalog_unknown: bool,
) -> None:
    """A preparation transport failure keeps retrying with one fault notice."""

    import open_trader.prediction_runtime as runtime_module

    clock = [datetime(2026, 9, 18, 12, 0, tzinfo=UTC)]
    catalog_calls: list[datetime] = []
    notifications: list[tuple[str, str]] = []
    first_call = threading.Event()
    at_299 = threading.Event()
    release_299 = threading.Event()
    paused_wait = threading.Event()
    release_paused = threading.Event()
    second_failure_gate = [False]
    success_call = threading.Event()
    hourly_wait_after_success = threading.Event()
    history_wait_calls: list[float] = []
    paused_wait_count = [0]

    class FakeTrading:
        fail_catalog = True

        def attach_metadata_cache(self, _store: object) -> None:
            pass

        def lp_reward_catalog(
            self, *, stop_event: threading.Event | None = None
        ) -> dict[str, object]:
            del stop_event
            catalog_calls.append(clock[0])
            first_call.set()
            if self.fail_catalog:
                clock[0] += timedelta(seconds=75)
                if catalog_unknown:
                    return {
                        "state": "unknown",
                        "complete": False,
                        "checked_at": clock[0],
                        "markets": (),
                        "error_type": "TimeoutError",
                    }
                raise TimeoutError("upstream response body unavailable")
            success_call.set()
            return {
                "state": "known",
                "complete": True,
                "checked_at": clock[0],
                "markets": (),
            }

        def lp_market_metadata(
            self, _condition_ids: tuple[str, ...], *, stop_event: object = None
        ) -> dict[str, object]:
            del stop_event
            return {}

        def lp_price_history(
            self,
            _token_ids: tuple[str, ...],
            *,
            start_ts: int,
            end_ts: int,
            fidelity: int,
            stop_event: object = None,
        ) -> dict[str, object]:
            del start_ts, end_ts, fidelity, stop_event
            return {"state": "known", "history": {}}

        def lp_account_snapshot(self) -> dict[str, object]:
            return {
                "authenticated": True,
                "checked_at": clock[0],
                "open_orders": [],
                "positions": [],
                "open_orders_complete": True,
                "positions_complete": True,
            }

        def lp_order_books(
            self, _token_ids: tuple[str, ...], *, stop_event: object = None
        ) -> dict[str, object]:
            del stop_event
            return {}

        def account_snapshot(self) -> dict[str, object]:
            return {
                "wallet_address": "0x1111111111111111111111111111111111111111",
                "p_usd_balance": Decimal("100"),
                "p_usd_allowance": Decimal("100"),
                "open_order_ids": [],
                "positions": [],
                "checked_at": clock[0],
            }

        def readiness_snapshot(self) -> dict[str, object]:
            return {
                "relayer_ready": True,
                "merge_ready": True,
                "checked_at": clock[0],
            }

        def close(self) -> None:
            pass

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
            pass

    class Feishu:
        channel = "feishu"

        def notify(self, title: str, message: str) -> None:
            notifications.append((title, message))

    class TestExecution(PredictionExecutionService):
        def reconcile_startup(self) -> dict[str, object]:
            return {"state": "ready"}

    trading = FakeTrading()
    notifier = Feishu()
    monkeypatch.setattr(runtime_module, "load_trading_config", lambda _path: SimpleNamespace(
        signer_address="0x1111111111111111111111111111111111111111",
        wallet_address="0x2222222222222222222222222222222222222222",
        predict=None,
    ))
    monkeypatch.setattr(
        runtime_module,
        "PolymarketTradingClient",
        SimpleNamespace(from_keychain=lambda _config: trading),
    )
    monkeypatch.setattr(
        runtime_module,
        "PredictTradingClient",
        SimpleNamespace(from_keychain=lambda _config: None),
    )
    monkeypatch.setattr(runtime_module, "PolymarketMonitor", FakeMonitor)
    monkeypatch.setattr(runtime_module, "PredictionExecutionService", TestExecution)
    monkeypatch.setattr(runtime_module, "_LP_TICK_SECONDS", 3600)
    monkeypatch.setattr(runtime_module, "_LP_REWARD_SECONDS", 3600)
    monkeypatch.setattr(runtime_module, "_LP_SHARE_WATCH_SECONDS", 3600)
    monkeypatch.setattr(runtime_module, "_LP_REWARD_STOP_GRACE_SECONDS", 0.1)

    def history_wait(stop_event: threading.Event, seconds: float) -> bool:
        history_wait_calls.append(seconds)
        retry_deadline = datetime(2026, 9, 18, 12, 6, 15, tzinfo=UTC)
        if (
            len(catalog_calls) >= 2
            and not success_call.is_set()
            and not second_failure_gate[0]
        ):
            second_failure_gate[0] = True
            paused_wait.set()
            while not release_paused.wait(timeout=0.01):
                if stop_event.is_set():
                    return True
            release_paused.clear()
            return stop_event.is_set()
        if seconds >= 3600:
            paused_wait_count[0] += 1
            paused_wait.set()
            if success_call.is_set():
                hourly_wait_after_success.set()
            while not release_paused.wait(timeout=0.01):
                if stop_event.is_set():
                    return True
            release_paused.clear()
            return stop_event.is_set()
        if 0 < seconds < 3600:
            if (
                not at_299.is_set()
                and clock[0] + timedelta(seconds=seconds)
                >= retry_deadline - timedelta(seconds=1)
            ):
                clock[0] = retry_deadline - timedelta(seconds=1)
                at_299.set()
                while not release_299.wait(timeout=0.01):
                    if stop_event.is_set():
                        return True
                return stop_event.is_set()
            clock[0] += timedelta(seconds=seconds)
            return stop_event.is_set()
        raise AssertionError(f"unexpected history wait: {seconds}")

    def make_runtime() -> PredictionRuntime:
        data_dir = tmp_path / ("retry-success" if retry_succeeds else "retry-failure")
        return PredictionRuntime(
            data_dir=data_dir,
            prediction_config_path=data_dir / "prediction.json",
            dashboard_url="http://127.0.0.1:8766/",
            notifier=SimpleNamespace(_notifiers=(notifier,)),
            cross_venue_monitor=_UnavailableCrossVenueMonitor("test-disabled"),
            enable_n_leg_background=False,
            n_leg_paused=True,
            history_clock=lambda: clock[0],
            history_wait=history_wait,
        )

    runtime = make_runtime()
    runtime.start()
    try:
        assert first_call.wait(timeout=2)
        assert len(catalog_calls) == 1
        assert at_299.wait(timeout=2)
        assert len(catalog_calls) == 1
        preparation = runtime.lp.preparation_snapshot()  # type: ignore[union-attr]
        assert preparation["state"] == "waiting_retry"
        assert preparation["last_failure_at"] == "2026-09-18T12:01:15.000000Z"
        assert preparation["next_retry_at"] == "2026-09-18T12:06:15.000000Z"
        if catalog_unknown:
            assert preparation["last_error"] == "TimeoutError"

        runtime.stop()
    finally:
        release_299.set()
        if runtime.state not in {"STOPPED", "FAILED"}:
            runtime.stop()

    if retry_succeeds:
        # The unique retry can succeed at the exact five-minute deadline. It
        # clears the first failure without alerting and returns to the hourly
        # history cadence.
        trading.fail_catalog = False
        success_call.clear()
        runtime = make_runtime()
        runtime.start()
        try:
            assert success_call.wait(timeout=2)
            assert len(catalog_calls) == 2
            assert notifications == []
            assert hourly_wait_after_success.wait(timeout=2)
            preparation = runtime.lp.preparation_snapshot()  # type: ignore[union-attr]
            assert preparation["state"] == "ready"
            assert preparation["failure_count"] == 0
        finally:
            if runtime.state not in {"NEW", "STOPPED", "FAILED"}:
                runtime.stop()
        return

    # A reconstructed runtime honors the original due time. The retry can fail
    # again without converting a recoverable transport fault into a pause; the
    # fault episode sends one notice and continues on its bounded probe/retry
    # schedule.
    runtime = make_runtime()
    runtime.start()
    try:
        assert paused_wait.wait(timeout=2)
        assert len(catalog_calls) >= 2
        assert len(catalog_calls) == 2
        assert len(notifications) == 1
        preparation = runtime.lp.preparation_snapshot()  # type: ignore[union-attr]
        assert preparation["state"] == "waiting_retry"
        assert preparation["paused"] is False
        assert preparation["failure_count"] == 2
        if catalog_unknown:
            assert preparation["last_error"] == "TimeoutError"
            assert "TimeoutError" in notifications[0][1]
            assert "ValueError" not in notifications[0][1]
        assert "重启" not in notifications[0][1]
        assert "自动探测" in notifications[0][1]

        # A later bounded retry can recover without an explicit operator
        # action and returns to the normal hourly scheduler.
        trading.fail_catalog = False
        release_paused.set()
        assert success_call.wait(timeout=2)
        assert len(catalog_calls) == 3
        assert hourly_wait_after_success.wait(timeout=2), history_wait_calls
        assert runtime.lp.preparation_snapshot()["state"] == "ready"  # type: ignore[union-attr]
        assert runtime.lp.preparation_snapshot()["failure_count"] == 0  # type: ignore[union-attr]
    finally:
        release_paused.set()
        if runtime.state not in {"NEW", "STOPPED", "FAILED"}:
            runtime.stop()
        assert runtime.state == "STOPPED"


def test_lp_partial_preparation_uses_item_retry_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partial LP preparation wakes at the failed item's durable deadline."""

    import open_trader.prediction_runtime as runtime_module

    clock = [datetime(2026, 9, 18, 12, 0, tzinfo=UTC)]
    history_calls: list[tuple[str, ...]] = []
    history_wait_calls: list[float] = []
    notifications: list[tuple[str, str]] = []
    first_history_done = threading.Event()
    at_299 = threading.Event()
    release_299 = threading.Event()
    retry_history_done = threading.Event()
    notification_done = threading.Event()
    history_wait_called = threading.Event()
    hourly_wait = threading.Event()
    release_hourly = threading.Event()

    class FakeTrading:
        def attach_metadata_cache(self, _store: object) -> None:
            pass

        @staticmethod
        def _market(condition_id: str) -> dict[str, object]:
            return {
                "market_id": f"market-{condition_id[-1]}",
                "condition_id": condition_id,
                "accepting_orders": True,
                "reward_min_size": Decimal("20"),
                "reward_max_spread": Decimal("0.10"),
                "minimum_order_size": Decimal("20"),
                "tick_size": Decimal("0.01"),
                "fees_enabled": False,
                "taker_fee_rate": Decimal("0"),
                "fee_exponent": Decimal("1"),
                "metadata_checked_at": clock[0],
                "fees_checked_at": clock[0],
                "outcomes": {
                    "yes": {
                        "label": "YES",
                        "token_id": f"token-{condition_id[-1]}",
                    }
                },
            }

        def lp_reward_catalog(
            self,
            *,
            condition_ids: tuple[str, ...] | None = None,
            stop_event: object = None,
        ) -> dict[str, object]:
            del stop_event
            requested = ("condition-a", "condition-b") if condition_ids is None else condition_ids
            return {
                "state": "known",
                "complete": True,
                "checked_at": clock[0],
                "markets": [
                    {
                        "condition_id": condition_id,
                        "daily_pool_usd": Decimal("100"),
                        "reward_active": True,
                        "rewards_min_size": Decimal("20"),
                        "rewards_max_spread": Decimal("10"),
                    }
                    for condition_id in requested
                ],
            }

        def lp_market_metadata(
            self,
            condition_ids: tuple[str, ...],
            *,
            stop_event: object = None,
        ) -> dict[str, dict[str, object]]:
            del stop_event
            return {
                condition_id: self._market(condition_id)
                for condition_id in condition_ids
            }

        def lp_price_history(
            self,
            token_ids: tuple[str, ...],
            *,
            start_ts: int,
            end_ts: int,
            fidelity: int,
            stop_event: object = None,
        ) -> dict[str, object]:
            del fidelity, stop_event
            history_calls.append(token_ids)
            if token_ids == ("token-a", "token-b"):
                first_history_done.set()
                return {
                    "state": "partial",
                    "history": {
                        "token-a": [
                            {"t": start_ts, "p": Decimal("0.500")},
                            {"t": end_ts, "p": Decimal("0.505")},
                        ]
                    },
                    "errors": {"token-b": "IncompleteRead"},
                }
            if token_ids == ("token-b",):
                retry_history_done.set()
                return {
                    "state": "partial",
                    "history": {},
                    "errors": {"token-b": "IncompleteRead"},
                }
            raise AssertionError(f"unexpected history request: {token_ids}")

        def lp_account_snapshot(self) -> dict[str, object]:
            return {
                "authenticated": True,
                "balance": Decimal("10000"),
                "allowance": Decimal("10000"),
                "open_orders": [],
                "positions": [],
                "checked_at": clock[0],
                "open_orders_complete": True,
                "positions_complete": True,
            }

        def lp_order_books(
            self,
            token_ids: tuple[str, ...],
            *,
            stop_event: object = None,
        ) -> dict[str, dict[str, object]]:
            del stop_event
            return {
                token_id: {
                    "condition_id": f"condition-{token_id.removeprefix('token-')}",
                    "token_id": token_id,
                    "received_at": clock[0],
                    "bids": [
                        {"price": Decimal("0.50"), "size": Decimal("20")},
                        {"price": Decimal("0.49"), "size": Decimal("20")},
                    ],
                    "asks": [{"price": Decimal("0.52"), "size": Decimal("20")}],
                }
                for token_id in token_ids
            }

        def account_snapshot(self) -> dict[str, object]:
            return {
                "wallet_address": "0x1111111111111111111111111111111111111111",
                "p_usd_balance": Decimal("100"),
                "p_usd_allowance": Decimal("100"),
                "open_order_ids": [],
                "positions": [],
                "checked_at": clock[0],
            }

        def readiness_snapshot(self) -> dict[str, object]:
            return {
                "relayer_ready": True,
                "merge_ready": True,
                "checked_at": clock[0],
            }

        def close(self) -> None:
            pass

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
            pass

    class Feishu:
        channel = "feishu"

        def notify(self, title: str, message: str) -> None:
            notifications.append((title, message))
            notification_done.set()

    class TestExecution(PredictionExecutionService):
        def reconcile_startup(self) -> dict[str, object]:
            return {"state": "ready"}

        def refresh_lp_share_watch(
            self, *, stop_event: threading.Event | None = None
        ) -> dict[str, object]:
            del stop_event
            return {"state": "none"}

    trading = FakeTrading()
    notifier = Feishu()
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
        SimpleNamespace(from_keychain=lambda _config: trading),
    )
    monkeypatch.setattr(
        runtime_module,
        "PredictTradingClient",
        SimpleNamespace(from_keychain=lambda _config: None),
    )
    monkeypatch.setattr(runtime_module, "PolymarketMonitor", FakeMonitor)
    monkeypatch.setattr(runtime_module, "PredictionExecutionService", TestExecution)
    monkeypatch.setattr(runtime_module, "_LP_TICK_SECONDS", 3600)
    monkeypatch.setattr(runtime_module, "_LP_REWARD_SECONDS", 3600)
    monkeypatch.setattr(runtime_module, "_LP_SHARE_WATCH_SECONDS", 3600)
    monkeypatch.setattr(runtime_module, "_LP_REWARD_STOP_GRACE_SECONDS", 0.1)

    def history_wait(stop_event: threading.Event, seconds: float) -> bool:
        history_wait_calls.append(seconds)
        history_wait_called.set()
        if seconds >= 3600:
            hourly_wait.set()
            while not release_hourly.wait(timeout=0.01):
                if stop_event.is_set():
                    return True
            return stop_event.is_set()
        if 0 < seconds < 3600:
            retry_deadline = datetime(2026, 9, 18, 12, 5, tzinfo=UTC)
            if (
                not at_299.is_set()
                and clock[0] + timedelta(seconds=seconds)
                >= retry_deadline - timedelta(seconds=1)
            ):
                clock[0] = retry_deadline - timedelta(seconds=1)
                at_299.set()
                while not release_299.wait(timeout=0.01):
                    if stop_event.is_set():
                        return True
                return stop_event.is_set()
            clock[0] += timedelta(seconds=seconds)
            return stop_event.is_set()
        raise AssertionError(f"unexpected history wait: {seconds}")

    runtime = PredictionRuntime(
        data_dir=tmp_path / "partial-runtime",
        prediction_config_path=tmp_path / "partial-runtime" / "prediction.json",
        dashboard_url="http://127.0.0.1:8766/",
        notifier=SimpleNamespace(_notifiers=(notifier,)),
        cross_venue_monitor=_UnavailableCrossVenueMonitor("test-disabled"),
        enable_n_leg_background=False,
        n_leg_paused=True,
        history_clock=lambda: clock[0],
        history_wait=history_wait,
    )
    runtime_started = False
    try:
        runtime.start()
        runtime_started = True
        assert first_history_done.wait(timeout=2)
        assert history_wait_called.wait(timeout=2)
        assert history_wait_calls[0] == pytest.approx(60)
        assert at_299.wait(timeout=2)
        assert history_calls == [("token-a", "token-b")]
        preparation = runtime.lp.preparation_snapshot()  # type: ignore[union-attr]
        assert preparation["state"] == "partial"
        assert preparation["waiting_market_count"] == 1

        snapshot = runtime.lp.refresh_candidates(force=True)  # type: ignore[union-attr]
        for _ in range(200):
            if snapshot.get("scanning") is not True:
                break
            time.sleep(0.01)
            snapshot = runtime.lp.refresh_candidates(force=True)  # type: ignore[union-attr]
        # Issue #143 repair 2: after the bounded retry wait the cached
        # metadata is older than the 60-second candidate freshness window,
        # so the batch renews it once (targeted) before qualifying and
        # market-a is judged live and published as the only passer.
        assert snapshot.get("scanning") is not True
        assert snapshot["funnel"]["checked"] == 1
        assert snapshot["funnel"]["passed"] == 1
        assert snapshot["funnel"]["unknown"] == 0
        assert snapshot["funnel"]["batches"] == 1
        assert [row["condition_id"] for row in snapshot["candidates"]] == [
            "condition-a"
        ]

        release_299.set()
        assert retry_history_done.wait(timeout=2)
        assert notification_done.wait(timeout=2)
        assert history_calls == [("token-a", "token-b"), ("token-b",)]
        assert history_wait_calls[0] == pytest.approx(60)
        assert any(seconds == pytest.approx(1) for seconds in history_wait_calls)
        assert len(notifications) == 1
        assert "自动探测" in notifications[0][1]
        assert "按退避继续补全" in notifications[0][1]
        assert "手动恢复" not in notifications[0][1]
        assert not hourly_wait.is_set() or any(
            seconds == pytest.approx(1) for seconds in history_wait_calls
        )
    finally:
        release_299.set()
        release_hourly.set()
        if runtime_started and runtime.state not in {"STOPPED", "FAILED"}:
            runtime.stop()
        assert runtime.state == "STOPPED"
    assert history_calls == [("token-a", "token-b"), ("token-b",)]
    assert len(notifications) == 1


def test_lp_all_metadata_failures_keep_five_minute_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Metadata-only failures still wake at the item retry deadline."""

    import open_trader.prediction_runtime as runtime_module

    clock = [datetime(2026, 9, 18, 12, 0, tzinfo=UTC)]
    metadata_calls: list[tuple[str, ...]] = []
    history_calls: list[tuple[str, ...]] = []
    history_wait_calls: list[float] = []
    notifications: list[tuple[str, str]] = []
    at_299 = threading.Event()
    release_299 = threading.Event()
    paused_wait = threading.Event()
    notification_done = threading.Event()
    release_hourly = threading.Event()

    class FakeTrading:
        def attach_metadata_cache(self, _store: object) -> None:
            pass

        @staticmethod
        def _market(condition_id: str) -> dict[str, object]:
            return {
                "market_id": f"market-{condition_id[-1]}",
                "condition_id": condition_id,
                "accepting_orders": True,
                "reward_min_size": Decimal("20"),
                "reward_max_spread": Decimal("0.10"),
                "minimum_order_size": Decimal("20"),
                "tick_size": Decimal("0.01"),
                "fees_enabled": False,
                "taker_fee_rate": Decimal("0"),
                "fee_exponent": Decimal("1"),
                "metadata_checked_at": clock[0],
                "outcomes": {
                    "yes": {
                        "label": "YES",
                        "token_id": f"token-{condition_id[-1]}",
                    }
                },
            }

        def lp_reward_catalog(
            self,
            *,
            condition_ids: tuple[str, ...] | None = None,
            stop_event: object = None,
        ) -> dict[str, object]:
            del stop_event
            requested = ("condition-b",) if condition_ids is None else condition_ids
            return {
                "state": "known",
                "complete": True,
                "checked_at": clock[0],
                "markets": [
                    {
                        "condition_id": condition_id,
                        "daily_pool_usd": Decimal("100"),
                        "reward_active": True,
                        "rewards_min_size": Decimal("20"),
                        "rewards_max_spread": Decimal("10"),
                    }
                    for condition_id in requested
                ],
            }

        def lp_market_metadata_batch(
            self,
            requested: tuple[str, ...],
            *,
            stop_event: object = None,
        ) -> dict[str, object]:
            del stop_event
            metadata_calls.append(tuple(requested))
            return {
                "state": "known",
                "markets": {},
                "failed_ids": {
                    condition_id: "IncompleteRead" for condition_id in requested
                },
            }

        def lp_market_metadata(
            self,
            requested: tuple[str, ...],
            *,
            stop_event: object = None,
        ) -> dict[str, dict[str, object]]:
            del stop_event
            return {
                condition_id: self._market(condition_id) for condition_id in requested
            }

        def lp_price_history(
            self,
            token_ids: tuple[str, ...],
            *,
            start_ts: int,
            end_ts: int,
            fidelity: int,
            stop_event: object = None,
        ) -> dict[str, object]:
            del start_ts, end_ts, fidelity, stop_event
            history_calls.append(token_ids)
            raise AssertionError("metadata-only failure must not request history")

        def lp_account_snapshot(self) -> dict[str, object]:
            return {
                "authenticated": True,
                "balance": Decimal("10000"),
                "allowance": Decimal("10000"),
                "open_orders": [],
                "positions": [],
                "checked_at": clock[0],
                "open_orders_complete": True,
                "positions_complete": True,
            }

        def lp_order_books(
            self,
            token_ids: tuple[str, ...],
            *,
            stop_event: object = None,
        ) -> dict[str, dict[str, object]]:
            del stop_event
            return {
                token_id: {
                    "condition_id": "condition-b",
                    "token_id": token_id,
                    "received_at": clock[0],
                    "bids": [{"price": Decimal("0.50"), "size": Decimal("20")}],
                    "asks": [{"price": Decimal("0.52"), "size": Decimal("20")}],
                }
                for token_id in token_ids
            }

        def account_snapshot(self) -> dict[str, object]:
            return {
                "wallet_address": "0x1111111111111111111111111111111111111111",
                "p_usd_balance": Decimal("100"),
                "p_usd_allowance": Decimal("100"),
                "open_order_ids": [],
                "positions": [],
                "checked_at": clock[0],
            }

        def readiness_snapshot(self) -> dict[str, object]:
            return {
                "relayer_ready": True,
                "merge_ready": True,
                "checked_at": clock[0],
            }

        def close(self) -> None:
            pass

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
            pass

    class Feishu:
        channel = "feishu"

        def notify(self, title: str, message: str) -> None:
            notifications.append((title, message))
            notification_done.set()

    class TestExecution(PredictionExecutionService):
        def reconcile_startup(self) -> dict[str, object]:
            return {"state": "ready"}

        def refresh_lp_share_watch(
            self, *, stop_event: threading.Event | None = None
        ) -> dict[str, object]:
            del stop_event
            return {"state": "none"}

    trading = FakeTrading()
    notifier = Feishu()
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
        SimpleNamespace(from_keychain=lambda _config: trading),
    )
    monkeypatch.setattr(
        runtime_module,
        "PredictTradingClient",
        SimpleNamespace(from_keychain=lambda _config: None),
    )
    monkeypatch.setattr(runtime_module, "PolymarketMonitor", FakeMonitor)
    monkeypatch.setattr(runtime_module, "PredictionExecutionService", TestExecution)
    monkeypatch.setattr(runtime_module, "_LP_TICK_SECONDS", 3600)
    monkeypatch.setattr(runtime_module, "_LP_REWARD_SECONDS", 3600)
    monkeypatch.setattr(runtime_module, "_LP_SHARE_WATCH_SECONDS", 3600)
    monkeypatch.setattr(runtime_module, "_LP_REWARD_STOP_GRACE_SECONDS", 0.1)

    def history_wait(stop_event: threading.Event, seconds: float) -> bool:
        history_wait_calls.append(seconds)
        if len(metadata_calls) >= 2:
            paused_wait.set()
            while not release_hourly.wait(timeout=0.01):
                if stop_event.is_set():
                    return True
            return stop_event.is_set()
        if seconds >= 3600:
            paused_wait.set()
            return True
        if 0 < seconds < 3600:
            retry_deadline = datetime(2026, 9, 18, 12, 5, tzinfo=UTC)
            if (
                not at_299.is_set()
                and clock[0] + timedelta(seconds=seconds)
                >= retry_deadline - timedelta(seconds=1)
            ):
                clock[0] = retry_deadline - timedelta(seconds=1)
                at_299.set()
                while not release_299.wait(timeout=0.01):
                    if stop_event.is_set():
                        return True
                return stop_event.is_set()
            clock[0] += timedelta(seconds=seconds)
            return stop_event.is_set()
        raise AssertionError(f"unexpected history wait: {seconds}")

    runtime = PredictionRuntime(
        data_dir=tmp_path / "metadata-runtime",
        prediction_config_path=tmp_path / "metadata-runtime" / "prediction.json",
        dashboard_url="http://127.0.0.1:8766/",
        notifier=SimpleNamespace(_notifiers=(notifier,)),
        cross_venue_monitor=_UnavailableCrossVenueMonitor("test-disabled"),
        enable_n_leg_background=False,
        n_leg_paused=True,
        history_clock=lambda: clock[0],
        history_wait=history_wait,
    )
    runtime_started = False
    try:
        runtime.start()
        runtime_started = True
        assert at_299.wait(timeout=2) or paused_wait.wait(timeout=2)
        assert history_wait_calls[0] == pytest.approx(60)
        assert history_calls == []
        assert metadata_calls == [("condition-b",)]
        preparation = runtime.lp.preparation_snapshot()  # type: ignore[union-attr]
        assert preparation["state"] == "partial"
        assert preparation["waiting_market_count"] == 1

        release_299.set()
        assert paused_wait.wait(timeout=2)
        assert notification_done.wait(timeout=2)
        assert history_wait_calls[0] == pytest.approx(60)
        assert pytest.approx(60) in history_wait_calls
        assert not any(seconds >= 3600 for seconds in history_wait_calls)
        assert history_calls == []
        assert metadata_calls == [("condition-b",), ("condition-b",)]
        assert len(notifications) == 1
        preparation = runtime.lp.preparation_snapshot()  # type: ignore[union-attr]
        assert preparation["paused_market_count"] == 0
        assert preparation["waiting_market_count"] == 1
        assert preparation["state"] == "partial"
        release_hourly.set()
    finally:
        release_299.set()
        if runtime_started and runtime.state not in {"STOPPED", "FAILED"}:
            runtime.stop()
        assert runtime.state == "STOPPED"


def test_lp_confirmed_absent_catalog_item_finishes_pending_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A complete catalog omission retires one unspent pending market."""

    import open_trader.prediction_runtime as runtime_module

    clock = [datetime(2026, 9, 18, 12, 0, tzinfo=UTC)]
    data_dir = tmp_path / "confirmed-absent-runtime"
    metadata_calls: list[tuple[str, ...]] = []
    history_calls: list[tuple[str, ...]] = []
    history_wait_calls: list[float] = []
    notifications: list[tuple[str, str]] = []
    first_wait_started = threading.Event()
    normal_wait_started = threading.Event()
    final_wait_started = threading.Event()
    zero_wait_seen = threading.Event()
    release_normal_wait = threading.Event()
    normal_wait_count = [0]

    samples = [
        {"t": int((clock[0] - timedelta(hours=24)).timestamp()), "p": Decimal("0.500")},
        {"t": int(clock[0].timestamp()), "p": Decimal("0.505")},
    ]
    seeded_store = PredictionArbitrageStore(data_dir)
    seeded_store.lp_save_price_history(
        "condition-b",
        "token-b",
        samples,
        {
            "state": "known",
            "amplitude": Decimal("0.005"),
            "checked_at": clock[0],
            "window_start": clock[0] - timedelta(hours=24),
            "window_end": clock[0],
            "sample_count": 2,
            "valid_until": clock[0] + timedelta(hours=24),
        },
    )
    seeded_summary = seeded_store.lp_price_history_summary(
        "condition-b", "token-b", now=clock[0]
    )
    assert seeded_summary is not None

    class FakeTrading:
        def attach_metadata_cache(self, _store: object) -> None:
            pass

        @staticmethod
        def _market(condition_id: str) -> dict[str, object]:
            return {
                "market_id": f"market-{condition_id[-1]}",
                "condition_id": condition_id,
                "accepting_orders": True,
                "reward_min_size": Decimal("20"),
                "reward_max_spread": Decimal("0.10"),
                "minimum_order_size": Decimal("20"),
                "tick_size": Decimal("0.01"),
                "fees_enabled": False,
                "taker_fee_rate": Decimal("0"),
                "fee_exponent": Decimal("1"),
                "metadata_checked_at": clock[0],
                "outcomes": {
                    "yes": {
                        "label": "YES",
                        "token_id": f"token-{condition_id[-1]}",
                    }
                },
            }

        def lp_reward_catalog(
            self,
            *,
            condition_ids: tuple[str, ...] | None = None,
            stop_event: object = None,
        ) -> dict[str, object]:
            del condition_ids, stop_event
            if clock[0] == datetime(2026, 9, 18, 12, 0, tzinfo=UTC):
                markets = [
                    {
                        "condition_id": "condition-b",
                        "daily_pool_usd": Decimal("100"),
                        "reward_active": True,
                        "rewards_min_size": Decimal("20"),
                        "rewards_max_spread": Decimal("10"),
                    }
                ]
            else:
                markets = []
            return {
                "state": "known",
                "complete": True,
                "checked_at": clock[0],
                "markets": markets,
            }

        def lp_market_metadata_batch(
            self,
            requested: tuple[str, ...],
            *,
            stop_event: object = None,
        ) -> dict[str, object]:
            del stop_event
            metadata_calls.append(tuple(requested))
            if len(metadata_calls) == 1:
                return {
                    "state": "known",
                    "markets": {},
                    "failed_ids": {condition_id: "IncompleteRead" for condition_id in requested},
                }
            return {
                "state": "known",
                "markets": {
                    condition_id: self._market(condition_id) for condition_id in requested
                },
            }

        def lp_market_metadata(
            self,
            requested: tuple[str, ...],
            *,
            stop_event: object = None,
        ) -> dict[str, dict[str, object]]:
            del stop_event
            return {condition_id: self._market(condition_id) for condition_id in requested}

        def lp_price_history(
            self,
            token_ids: tuple[str, ...],
            *,
            start_ts: int,
            end_ts: int,
            fidelity: int,
            stop_event: object = None,
        ) -> dict[str, object]:
            del start_ts, end_ts, fidelity, stop_event
            history_calls.append(tuple(token_ids))
            raise AssertionError("catalog-confirmed absence must not request history")

        def lp_account_snapshot(self) -> dict[str, object]:
            return {
                "authenticated": True,
                "balance": Decimal("10000"),
                "allowance": Decimal("10000"),
                "open_orders": [],
                "positions": [],
                "checked_at": clock[0],
                "open_orders_complete": True,
                "positions_complete": True,
            }

        def lp_order_books(
            self,
            token_ids: tuple[str, ...],
            *,
            stop_event: object = None,
        ) -> dict[str, dict[str, object]]:
            del stop_event
            return {
                token_id: {
                    "condition_id": "condition-b",
                    "token_id": token_id,
                    "received_at": clock[0],
                    "bids": [{"price": Decimal("0.50"), "size": Decimal("20")}],
                    "asks": [{"price": Decimal("0.52"), "size": Decimal("20")}],
                }
                for token_id in token_ids
            }

        def account_snapshot(self) -> dict[str, object]:
            return {
                "wallet_address": "0x1111111111111111111111111111111111111111",
                "p_usd_balance": Decimal("100"),
                "p_usd_allowance": Decimal("100"),
                "open_order_ids": [],
                "positions": [],
                "checked_at": clock[0],
            }

        def readiness_snapshot(self) -> dict[str, object]:
            return {
                "relayer_ready": True,
                "merge_ready": True,
                "checked_at": clock[0],
            }

        def close(self) -> None:
            pass

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
            pass

    class Feishu:
        channel = "feishu"

        def notify(self, title: str, message: str) -> None:
            notifications.append((title, message))

    class TestExecution(PredictionExecutionService):
        def reconcile_startup(self) -> dict[str, object]:
            return {"state": "ready"}

        def refresh_lp_share_watch(
            self, *, stop_event: threading.Event | None = None
        ) -> dict[str, object]:
            del stop_event
            return {"state": "none"}

    trading = FakeTrading()
    notifier = Feishu()
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
        SimpleNamespace(from_keychain=lambda _config: trading),
    )
    monkeypatch.setattr(
        runtime_module,
        "PredictTradingClient",
        SimpleNamespace(from_keychain=lambda _config: None),
    )
    monkeypatch.setattr(runtime_module, "PolymarketMonitor", FakeMonitor)
    monkeypatch.setattr(runtime_module, "PredictionExecutionService", TestExecution)
    monkeypatch.setattr(runtime_module, "_LP_TICK_SECONDS", 3600)
    monkeypatch.setattr(runtime_module, "_LP_REWARD_SECONDS", 3600)
    monkeypatch.setattr(runtime_module, "_LP_SHARE_WATCH_SECONDS", 3600)
    monkeypatch.setattr(runtime_module, "_LP_REWARD_STOP_GRACE_SECONDS", 0.1)

    def history_wait(stop_event: threading.Event, seconds: float) -> bool:
        history_wait_calls.append(seconds)
        if seconds == pytest.approx(60):
            first_wait_started.set()
            clock[0] += timedelta(seconds=60)
            return False
        if seconds == pytest.approx(300):
            first_wait_started.set()
            clock[0] += timedelta(seconds=300)
            return False
        if seconds < 1:
            zero_wait_seen.set()
            return True
        if seconds == pytest.approx(3600):
            normal_wait_count[0] += 1
            if normal_wait_count[0] == 1:
                normal_wait_started.set()
                while not release_normal_wait.wait(timeout=0.01):
                    if stop_event.is_set():
                        return True
                clock[0] += timedelta(seconds=3600)
                return False
            final_wait_started.set()
            return True
        raise AssertionError(f"unexpected history wait: {seconds}")

    runtime = PredictionRuntime(
        data_dir=data_dir,
        prediction_config_path=data_dir / "prediction.json",
        dashboard_url="http://127.0.0.1:8766/",
        notifier=SimpleNamespace(_notifiers=(notifier,)),
        cross_venue_monitor=_UnavailableCrossVenueMonitor("test-disabled"),
        enable_n_leg_background=False,
        n_leg_paused=True,
        history_clock=lambda: clock[0],
        history_wait=history_wait,
    )
    runtime_started = False
    try:
        runtime.start()
        runtime_started = True
        assert first_wait_started.wait(timeout=2)
        assert normal_wait_started.wait(timeout=2) or zero_wait_seen.wait(timeout=2)
        assert not zero_wait_seen.is_set()
        assert history_wait_calls[0] == pytest.approx(60)
        assert any(seconds == pytest.approx(3600) for seconds in history_wait_calls)
        assert metadata_calls == [("condition-b",)]
        assert history_calls == []
        assert notifications == []
        assert runtime.store.lp_preparation_items() == []
        preparation = runtime.lp.preparation_snapshot()  # type: ignore[union-attr]
        assert preparation["paused_market_count"] == 0
        assert preparation["waiting_market_count"] == 0
        assert runtime.store.lp_price_history_summary(
            "condition-b", "token-b", now=clock[0]
        ) == seeded_summary
        restored_samples = runtime.store.lp_price_history_samples(
            "condition-b", "token-b"
        )
        assert [int(row["t"]) for row in restored_samples] == [
            int(row["t"]) for row in samples
        ]
        assert [Decimal(str(row["p"])) for row in restored_samples] == [
            row["p"] for row in samples
        ]

        release_normal_wait.set()
        assert final_wait_started.wait(timeout=2)
        assert metadata_calls == [("condition-b",)]
        assert history_calls == []
        assert runtime.store.lp_preparation_items() == []
    finally:
        release_normal_wait.set()
        if runtime_started and runtime.state not in {"STOPPED", "FAILED"}:
            runtime.stop()
        assert runtime.state == "STOPPED"


def test_lp_metadata_warmup_advances_beyond_one_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real adapter warms 1501 metadata IDs and stops without overlap."""

    import open_trader.polymarket_trading as trading_module
    import open_trader.prediction_runtime as runtime_module
    from open_trader.polymarket_trading import (
        PolymarketTradingClient,
        TradingConfig,
    )

    fixed_now = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
    clock = [fixed_now]

    class AdapterClock(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            return clock[0] if tz is None else clock[0].astimezone(tz)  # type: ignore[arg-type]

    monkeypatch.setattr(trading_module, "datetime", AdapterClock)
    # 执行侧 _age_seconds 用真实时钟对比适配器冻结时间戳；跨日运行会误判
    # account_unavailable。执行侧"现在"必须与适配器冻结时钟同源。
    import open_trader.prediction_arbitrage_execution as execution_module

    monkeypatch.setattr(execution_module, "_utc_now", lambda: clock[0])

    condition_ids = tuple(f"condition-{index:04d}" for index in range(1501))
    absent_id = condition_ids[1499]
    failed_id = condition_ids[1500]
    initial_ids = frozenset(condition_ids[:1500])
    positive_ids = frozenset(condition_ids[:1499])
    request_lock = threading.Lock()
    metadata_requests: list[tuple[str, ...]] = []
    dispatch_after_stop_signal: list[tuple[str, ...]] = []
    fail_last_once = [True]
    hold_next_metadata = [False]
    metadata_hold_batches: list[tuple[str, ...]] = []
    catalog_calls = [0]
    stop_event_ref: list[threading.Event | None] = [None]
    first_failure = threading.Event()
    retry_wait_entered = threading.Event()
    retry_release = threading.Event()
    preparation_ready = threading.Event()
    initial_wait_entered = threading.Event()
    allow_expired = threading.Event()
    metadata_hold_started = threading.Event()
    release_metadata = threading.Event()
    stop_started = threading.Event()
    stop_returned = threading.Event()
    wait_seconds: list[float] = []
    stop_errors: list[BaseException] = []

    def reward_row(condition_id: str) -> dict[str, object]:
        return {
            "condition_id": condition_id,
            "rewards_min_size": "1",
            "rewards_max_spread": "10",
            "rewards_config": [
                {
                    "id": f"reward-{condition_id}",
                    "asset_address": "0x2791bca1f2de4661ed88a30c99a7a9449aa84174",
                    "start_date": "2026-01-01",
                    "end_date": "2030-01-01",
                    "rate_per_day": "1",
                }
            ],
        }

    def market_row(condition_id: str) -> dict[str, object]:
        index = int(condition_id.rsplit("-", 1)[-1])
        return {
            "id": f"market-{index:04d}",
            "condition_id": condition_id,
            "question": f"Metadata market {index}",
            "slug": f"metadata-market-{index:04d}",
            "events": [],
            "state": {"accepting_orders": False},
            "trading": {
                "minimum_order_size": "1",
                "minimum_tick_size": "0.01",
                "fees_enabled": False,
            },
            "rewards": {
                "rewards_min_size": "1",
                "rewards_max_spread": "10",
            },
            "outcomes": {
                "yes": {
                    "label": "YES",
                    "token_id": f"token-{condition_id}-yes",
                },
                "no": {
                    "label": "NO",
                    "token_id": f"token-{condition_id}-no",
                },
            },
        }

    def open_history(_request: object, *, timeout: float) -> object:
        del timeout
        raise AssertionError("history must not be read without an accepting market")

    class PublicSDK:
        def list_current_rewards(self, *, sponsored: bool) -> list[object]:
            if sponsored:
                return []
            catalog_calls[0] += 1
            return [reward_row(condition_id) for condition_id in condition_ids]

        def list_market_rewards(
            self, *, condition_id: str, sponsored: bool
        ) -> list[object]:
            del sponsored
            return [reward_row(condition_id)] if condition_id in condition_ids else []

        def list_markets(
            self, *, condition_ids: object, page_size: int = 100
        ) -> list[object]:
            assert page_size == 100
            batch = tuple(str(value) for value in condition_ids)  # type: ignore[arg-type]
            assert 1 <= len(batch) <= 100
            should_fail = False
            should_hold = False
            with request_lock:
                stop_event = stop_event_ref[0]
                if stop_event is not None and stop_event.is_set():
                    dispatch_after_stop_signal.append(batch)
                metadata_requests.append(batch)
                if batch == (failed_id,) and fail_last_once[0]:
                    fail_last_once[0] = False
                    should_fail = True
                if hold_next_metadata[0] and len(metadata_hold_batches) < 8:
                    metadata_hold_batches.append(batch)
                    should_hold = True
                    if len(metadata_hold_batches) == 8:
                        metadata_hold_started.set()
            if should_fail:
                first_failure.set()
                raise TimeoutError("last metadata request unavailable")
            if should_hold:
                assert release_metadata.wait(timeout=60)
            return [market_row(condition_id) for condition_id in batch if condition_id != absent_id]

        def get_order_books(self, *, token_ids: object) -> list[object]:
            del token_ids
            return []

        def close(self) -> None:
            return None

    class AccountSDK:
        environment = SimpleNamespace(standard_exchange="0x" + "2" * 40)

        def get_balance_allowance(self, *, asset_type: str) -> Mapping[str, object]:
            assert asset_type == "COLLATERAL"
            return {
                "balance": "100000000",
                "allowances": {self.environment.standard_exchange: "100000000"},
            }

        def list_open_orders(self, *args: object, **kwargs: object) -> tuple[object, ...]:
            del args, kwargs
            return ()

        def list_account_trades(
            self, *args: object, **kwargs: object
        ) -> tuple[object, ...]:
            del args, kwargs
            return ()

        def list_positions(self, *args: object, **kwargs: object) -> tuple[object, ...]:
            del args, kwargs
            return ()

        def is_gasless_ready(self) -> bool:
            return True

        def merge_positions(self, *args: object, **kwargs: object) -> object:
            del args, kwargs
            return {"status": "not_called"}

    config = TradingConfig(
        signer_address="0x" + "3" * 40,
        wallet_address="0x" + "1" * 40,
        predict=None,
    )
    trading = PolymarketTradingClient(
        config,
        AccountSDK(),
        urlopen_fn=open_history,
        public_client_factory=PublicSDK,
    )
    monkeypatch.setattr(runtime_module, "load_trading_config", lambda _path: config)
    monkeypatch.setattr(
        runtime_module,
        "PolymarketTradingClient",
        SimpleNamespace(from_keychain=lambda _config: trading),
    )
    monkeypatch.setattr(
        runtime_module,
        "PredictTradingClient",
        SimpleNamespace(from_keychain=lambda _config: None),
    )

    class MacOSNotifier:
        channel = "macos"

        def notify(self, *_args: object, **_kwargs: object) -> None:
            return None

    class FeishuNotifier:
        channel = "feishu"

        def notify(self, *_args: object, **_kwargs: object) -> None:
            return None

    notifier = SimpleNamespace(_notifiers=(MacOSNotifier(), FeishuNotifier()))

    def history_wait(stop_event: threading.Event, seconds: float) -> bool:
        stop_event_ref[0] = stop_event
        wait_seconds.append(seconds)
        if seconds == 60:
            retry_wait_entered.set()
            if not retry_release.wait(timeout=60):
                return True
            return stop_event.is_set()
        if seconds == 300:
            retry_wait_entered.set()
            if not retry_release.wait(timeout=60):
                return True
            return stop_event.is_set()
        if seconds == 3600:
            if not initial_wait_entered.is_set():
                initial_wait_entered.set()
                preparation_ready.set()
                if not allow_expired.wait(timeout=60):
                    return True
            return stop_event.is_set()
        raise AssertionError(f"unexpected history wait: {seconds}")

    runtime = PredictionRuntime(
        data_dir=tmp_path,
        prediction_config_path=tmp_path / "prediction.json",
        dashboard_url="http://127.0.0.1:8766/",
        notifier=notifier,
        cross_venue_monitor=_UnavailableCrossVenueMonitor("test-disabled"),
        enable_n_leg_background=False,
        n_leg_paused=True,
        history_clock=lambda: clock[0],
        history_wait=history_wait,
    )
    stop_thread: threading.Thread | None = None
    try:
        runtime.start()
        assert runtime.state == "RUNNING"
        assert retry_wait_entered.wait(timeout=60)
        assert first_failure.is_set()
        lp = runtime.lp
        store = runtime.store
        assert lp is not None and store is not None

        first_preparation = lp.preparation_snapshot()
        assert first_preparation["state"] == "partial"
        assert first_preparation["paused"] is False
        assert first_preparation["attempt"] == 0
        assert first_preparation["failure_count"] == 0
        assert first_preparation["metadata_completed_count"] == 1500
        assert first_preparation["metadata_total_count"] == 1501
        preparation_items = store.lp_preparation_items()
        assert [item["condition_id"] for item in preparation_items] == [failed_id]
        assert preparation_items[0]["state"] == "waiting_retry"
        assert preparation_items[0]["retry_used"] is False
        assert preparation_items[0]["failure_count"] == 1
        assert preparation_items[0]["failed_at"] == (
            fixed_now.isoformat(timespec="microseconds").replace("+00:00", "Z")
        )
        assert preparation_items[0]["next_retry_at"] == (
            (fixed_now + timedelta(seconds=300))
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
        first_entries = store.lp_metadata_cache_entries(now=clock[0])
        assert set(first_entries) == set(initial_ids)
        assert len(first_entries) == 1500
        assert failed_id not in first_entries
        assert first_entries[absent_id][1] is None
        absent_expiry = first_entries[absent_id][0]
        expected_checked_at = "2026-09-18T12:00:00.000000Z"
        before_facts = {
            condition_id: (
                first_entries[condition_id][0],
                first_entries[condition_id][1]["metadata_checked_at"],  # type: ignore[index]
            )
            for condition_id in positive_ids
        }
        for condition_id, (expires_at, checked_at) in before_facts.items():
            assert checked_at == expected_checked_at
            assert expires_at == fixed_now.timestamp() + 43_200
        assert absent_expiry == fixed_now.timestamp() + 3_600
        assert absent_expiry > clock[0].timestamp()

        clock[0] += timedelta(seconds=300)
        retry_release.set()
        assert preparation_ready.wait(timeout=60)
        assert initial_wait_entered.wait(timeout=60)
        preparation = lp.preparation_snapshot()
        assert preparation["state"] == "ready"
        assert preparation["failure_count"] == 0
        assert preparation["total_count"] == 0
        with request_lock:
            retry_requests = tuple(metadata_requests)
        assert len(retry_requests) == 17
        assert retry_requests[-1] == (failed_id,)
        assert sum(len(batch) for batch in retry_requests) == 1502
        assert sum(failed_id in batch for batch in retry_requests) == 2
        assert all(len(batch) <= 100 for batch in retry_requests)
        assert all(
            sum(condition_id in batch for batch in retry_requests) == 1
            for condition_id in initial_ids
        )
        assert set(
            condition_id
            for batch in retry_requests
            for condition_id in batch
        ) == set(condition_ids)

        final_entries = store.lp_metadata_cache_entries(now=clock[0])
        assert set(final_entries) == set(condition_ids)
        assert len(final_entries) == 1501
        assert sum(payload is not None for _, payload in final_entries.values()) == 1500
        assert final_entries[absent_id][1] is None
        assert isinstance(final_entries[failed_id][1], Mapping)
        assert final_entries[absent_id][0] == absent_expiry
        for condition_id, (expires_at, checked_at) in before_facts.items():
            payload = final_entries[condition_id][1]
            assert isinstance(payload, Mapping)
            assert final_entries[condition_id][0] == expires_at
            assert payload["metadata_checked_at"] == checked_at
        failed_payload = final_entries[failed_id][1]
        assert isinstance(failed_payload, Mapping)
        assert failed_payload["metadata_checked_at"] == "2026-09-18T12:05:00.000000Z"
        assert final_entries[failed_id][0] == (
            fixed_now + timedelta(seconds=300)
        ).timestamp() + 43_200
        assert all(value in {60.0, 3600.0} for value in wait_seconds[:2])
        assert wait_seconds[:2] == [60.0, 3600.0]

        hold_next_metadata[0] = True
        clock[0] += timedelta(seconds=43200)
        allow_expired.set()
        assert metadata_hold_started.wait(timeout=60)
        with request_lock:
            assert len(metadata_hold_batches) == 8
            requests_before_busy = len(metadata_requests)
            expired_start = requests_before_busy - len(metadata_hold_batches)
        preparation_before_busy = lp.preparation_snapshot()
        busy = lp.refresh_price_history()
        assert busy["state"] == "busy"
        assert busy["preparation_outcome"] == "busy"
        with request_lock:
            assert len(metadata_requests) == requests_before_busy
        assert lp.preparation_snapshot()["attempt"] == preparation_before_busy["attempt"]

        def stop_runtime() -> None:
            stop_started.set()
            try:
                runtime.stop()
            except BaseException as exc:
                stop_errors.append(exc)
            finally:
                stop_returned.set()

        stop_thread = threading.Thread(target=stop_runtime, name="lp-runtime-stop")
        stop_thread.start()
        assert stop_started.wait(timeout=2)
        stop_event = stop_event_ref[0]
        assert stop_event is not None
        assert stop_event.wait(timeout=2)
        release_metadata.set()
        assert stop_returned.wait(timeout=60)
        stop_thread.join(timeout=2)
        assert not stop_thread.is_alive()
        assert not stop_errors
        assert runtime.state == "STOPPED"
        with request_lock:
            assert dispatch_after_stop_signal == []
            assert len(metadata_requests) == requests_before_busy
            assert len(metadata_hold_batches) == 8
            expired_requests = metadata_requests[expired_start:]
            assert len(expired_requests) == 8
            assert all(failed_id not in batch for batch in expired_requests)
    finally:
        retry_release.set()
        allow_expired.set()
        release_metadata.set()
        if stop_thread is not None:
            stop_thread.join(timeout=60)
        if runtime.state not in {"NEW", "STOPPED", "FAILED"}:
            runtime.stop()
    assert runtime.state == "STOPPED"
    assert catalog_calls[0] == 3


def test_lp_minute_risk_does_not_wait_for_hourly_catalog_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_trader.prediction_runtime as runtime_module

    now = [datetime(2026, 9, 17, 1, 0, tzinfo=UTC)]
    catalog_calls = 0
    full_metadata_calls = 0
    selected_metadata_calls: list[tuple[str, ...]] = []
    selected_reward_calls: list[tuple[str, ...]] = []
    selected_book_calls: list[tuple[str, ...]] = []
    history_prepared = threading.Event()
    catalog_blocked = threading.Event()
    release_catalog = threading.Event()
    risk_seen = threading.Event()
    reward_seen = threading.Event()
    metadata_reads = 0
    lock = threading.Lock()

    class FakeTrading:
        config = SimpleNamespace(
            signer_address="0x" + "1" * 40,
            wallet_address="0x" + "2" * 40,
            predict=None,
        )

        def account_snapshot(self) -> dict[str, object]:
            return {
                "wallet_address": self.config.wallet_address,
                "p_usd_balance": Decimal("1000"),
                "p_usd_allowance": Decimal("1000"),
                "open_order_ids": [],
                "positions": [],
                "checked_at": datetime.now(UTC),
            }

        def readiness_snapshot(self) -> dict[str, object]:
            return {
                "relayer_ready": "ready",
                "merge_ready": "ready",
                "checked_at": datetime.now(UTC),
            }

        def lp_reward_catalog(
            self,
            *,
            condition_ids: tuple[str, ...] | None = None,
            stop_event: threading.Event | None = None,
        ) -> dict[str, object]:
            nonlocal catalog_calls
            del stop_event
            if condition_ids is not None:
                selected_reward_calls.append(tuple(condition_ids))
                return {
                    "state": "known",
                    "complete": True,
                    "checked_at": now[0],
                    "markets": (
                        {
                            "condition_id": "condition-A",
                            "daily_pool_usd": Decimal("100"),
                            "reward_active": True,
                            "rewards_min_size": Decimal("20"),
                            "rewards_max_spread": Decimal("10"),
                        },
                    ),
                }
            with lock:
                catalog_calls += 1
                call_number = catalog_calls
            if call_number > 1:
                catalog_blocked.set()
                if not release_catalog.wait(timeout=5):
                    raise AssertionError("hourly catalog read was not released")
                return {
                    "state": "unknown",
                    "complete": False,
                    "checked_at": now[0],
                    "daily_pool_usd": None,
                    "markets": (),
                }
            history_prepared.set()
            return {
                "state": "known",
                "complete": True,
                "checked_at": now[0],
                "markets": (
                    {
                        "condition_id": "condition-A",
                        "daily_pool_usd": Decimal("100"),
                        "reward_active": True,
                    },
                ),
            }

        def lp_market_metadata(
            self,
            condition_ids: tuple[str, ...],
            *,
            stop_event: object = None,
        ) -> dict[str, dict[str, object]]:
            nonlocal full_metadata_calls, metadata_reads
            del stop_event
            requested = tuple(condition_ids)
            if not full_metadata_calls and not selected_metadata_calls:
                full_metadata_calls += 1
            else:
                selected_metadata_calls.append(requested)
                with lock:
                    metadata_reads += 1
                    if metadata_reads > 1:
                        now[0] += timedelta(seconds=61)
            return {
                "condition-A": {
                    "market_id": "market-A",
                    "condition_id": "condition-A",
                    "market_title": "Market A",
                        "accepting_orders": True,
                        "metadata_checked_at": now[0],
                        "fees_checked_at": now[0],
                        "tick_size": Decimal("0.01"),
                    "minimum_order_size": Decimal("20"),
                    "reward_min_size": Decimal("20"),
                    "reward_max_spread": Decimal("0.10"),
                    "fees_enabled": False,
                    "fee": Decimal("0"),
                    "taker_fee_rate": Decimal("0"),
                    "fee_exponent": Decimal("1"),
                    "outcomes": {
                        "yes": {"label": "YES", "token_id": "token-A"}
                    },
                }
            } if "condition-A" in requested else {}

        def lp_price_history(
            self,
            token_ids: tuple[str, ...],
            *,
            start_ts: int,
            end_ts: int,
            fidelity: int = 1,
            stop_event: object = None,
        ) -> dict[str, object]:
            del fidelity, stop_event
            return {
                "state": "known",
                "history": {
                    token_id: [
                        {"t": start_ts, "p": "0.50"},
                        {"t": end_ts, "p": "0.505"},
                    ]
                    for token_id in token_ids
                },
                "unknown_token_ids": [],
            }

        def lp_account_snapshot(self) -> dict[str, object]:
            return {
                "authenticated": True,
                "balance": Decimal("1000"),
                "allowance": Decimal("1000"),
                "open_orders": [],
                "positions": [],
                "checked_at": now[0],
                "open_orders_complete": True,
                "positions_complete": True,
            }

        def lp_order_books(
            self,
            token_ids: tuple[str, ...],
            *,
            stop_event: object = None,
        ) -> dict[str, dict[str, object]]:
            del stop_event
            selected_book_calls.append(tuple(token_ids))
            assert set(token_ids) == {"token-A"}
            risk_seen.set()
            return {
                "token-A": {
                    "condition_id": "condition-A",
                    "token_id": "token-A",
                    "received_at": now[0],
                    "bids": [
                        {"price": Decimal("0.50"), "size": Decimal("20")},
                        {"price": Decimal("0.49"), "size": Decimal("20")},
                    ],
                    "asks": [{"price": Decimal("0.52"), "size": Decimal("20")}],
                }
            }

        def lp_reward_snapshot(
            self,
            reward_date: str,
            condition_id: str,
            *,
            stop_event: threading.Event | None = None,
        ) -> dict[str, object]:
            del reward_date, condition_id, stop_event
            reward_seen.set()
            return {"state": "unknown"}

        def lp_reward_rates(
            self, *, stop_event: threading.Event | None = None
        ) -> dict[str, object]:
            del stop_event
            reward_seen.set()
            return {"state": "unknown", "markets": {}}

        def lp_snapshot(self, request: Mapping[str, object]) -> dict[str, object]:
            del request
            return {
                "account": {"authenticated": True, "open_orders": [], "positions": []},
                "book": {"received_at": now[0], "bids": [], "asks": []},
                "orders": [],
                "trades": [],
            }

        def close(self) -> None:
            return None

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
            pass

    config = FakeTrading.config
    trading = FakeTrading()
    from open_trader import polymarket_lp as lp_module

    real_lp_service = lp_module.PolymarketLPService
    monkeypatch.setattr(
        runtime_module,
        "PolymarketLPService",
        lambda store, exchange, **kwargs: real_lp_service(
            store,
            exchange,
            clock=lambda: now[0],
            **kwargs,
        ),
    )
    monkeypatch.setattr(
        lp_module,
        "LP_RECOMMENDATION_REFRESH_SECONDS",
        Decimal("0.01"),
    )
    monkeypatch.setattr(runtime_module, "load_trading_config", lambda _path: config)
    monkeypatch.setattr(
        runtime_module,
        "PolymarketTradingClient",
        SimpleNamespace(from_keychain=lambda _config: trading),
    )
    monkeypatch.setattr(
        runtime_module,
        "PredictTradingClient",
        SimpleNamespace(from_keychain=lambda _config: None),
    )
    monkeypatch.setattr(runtime_module, "PolymarketMonitor", FakeMonitor)
    monkeypatch.setattr(runtime_module, "LlmRelationValidator", lambda *_a, **_k: object())
    monkeypatch.setattr(runtime_module, "LlmTitleTranslator", lambda *_a, **_k: object())
    monkeypatch.setattr(runtime_module, "_LP_TICK_SECONDS", 3600)
    monkeypatch.setattr(runtime_module, "_LP_REWARD_SECONDS", 0.01)
    monkeypatch.setattr(runtime_module, "_LP_HISTORY_SECONDS", 0.01)

    class FakeMacOSNotifier:
        def notify(self, _title: str, _message: str) -> None:
            pass

    class FakeFeishuWebhookNotifier:
        def notify(self, _title: str, _message: str) -> None:
            pass

    notifier = SimpleNamespace(
        _notifiers=[FakeMacOSNotifier(), FakeFeishuWebhookNotifier()],
        notify=lambda _title, _message: None,
    )

    runtime = PredictionRuntime(
        data_dir=tmp_path,
        prediction_config_path=tmp_path / "prediction.json",
        dashboard_url="http://127.0.0.1:8766/",
        notifier=notifier,
        cross_venue_monitor=_UnavailableCrossVenueMonitor("test-disabled"),
        solver_server_factory=lambda: object(),
        enable_n_leg_background=False,
    )
    try:
        runtime.start()
        assert runtime.state == "RUNNING"
        assert history_prepared.wait(timeout=2)
        assert runtime.store is not None
        runtime.store.lp_create_session(
            "reward-session",
            "reward-idempotency",
            state="entry_open",
            payload={
                "condition_id": "condition-A",
                "reward_date": now[0].date().isoformat(),
                "review_at": "2099-01-01T00:00:00Z",
                # A priced session keeps its capital reservation computable;
                # an unpriced reservation makes every candidate evaluation
                # honestly unknown (account_facts_unknown).
                "price": Decimal("0.50"),
                "quantity": Decimal("20"),
            },
        )
        assert catalog_blocked.wait(timeout=2)
        assert risk_seen.wait(timeout=2)
        assert reward_seen.wait(timeout=2)
        assert runtime.lp is not None
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            snapshot = runtime.lp.candidate_snapshot()
            if snapshot.get("candidates") and snapshot.get("state") in {
                "ready",
                "incomplete",
            }:
                break
            time.sleep(0.01)
        else:
            raise AssertionError("initial candidate scan did not finish")
        # Issue #146: the maintenance wait is anchored on wall-clock time, so
        # pin the scheduler cadence to one second; the source-age trigger in
        # refresh_candidate_recommendations still gates every read.
        original_wait = runtime.lp.candidate_maintenance_wait_seconds

        def one_second_wait() -> float:
            return 0.01

        runtime.lp.candidate_maintenance_wait_seconds = one_second_wait
        now[0] += timedelta(seconds=61)
        # The maintenance monitor is parked on its wall-clock wait; wake it
        # so the fake-clock jump takes effect immediately.
        runtime._candidate_maintenance_wakeup.set()
        deadline = time.monotonic() + 2
        while len(selected_book_calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(selected_book_calls) >= 2
        assert catalog_calls == 2
        assert full_metadata_calls == 1
        # Risk and reward loops remain independent while candidate
        # maintenance refreshes only the selected condition's expired
        # metadata/reward facts and its book.
        assert selected_metadata_calls == [("condition-A",)]
        assert selected_reward_calls == [("condition-A",)]
        release_catalog.set()
        runtime.stop()
        assert runtime.state == "STOPPED"
    finally:
        release_catalog.set()
        if runtime.state not in {"NEW", "STOPPED"}:
            runtime.stop()


def test_lp_history_batches_are_bounded_and_persist_public_summaries(
    tmp_path: Path,
) -> None:
    from open_trader.polymarket_lp import PolymarketLPService
    from open_trader.polymarket_trading import PolymarketTradingClient, TradingConfig

    now = datetime(2026, 9, 17, 1, 0, tzinfo=UTC)
    conditions = tuple(f"condition-{index:02d}" for index in range(41))
    token_pairs = tuple(
        (f"token-{index:02d}-yes", f"token-{index:02d}-no")
        for index in range(40)
    ) + (("token-40-yes",),)
    expected_tokens = tuple(token for pair in token_pairs for token in pair)
    assert len(expected_tokens) == 81

    rewards = [
        {
            "condition_id": condition_id,
            "rewards_min_size": "20",
            "rewards_max_spread": "10",
            "rewards_config": [
                {
                    "id": f"reward-{index}",
                    "asset_address": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
                    "start_date": "2026-09-16",
                    "end_date": "2030-01-01",
                    "rate_per_day": "1",
                }
            ],
        }
        for index, condition_id in enumerate(conditions)
    ]
    markets = [
        {
            "id": f"market-{index:02d}",
            "condition_id": condition_id,
            "question": f"History market {index}",
            "slug": f"history-market-{index}",
            "events": [],
            "state": {"accepting_orders": True},
            "trading": {
                "minimum_order_size": "1",
                "minimum_tick_size": "0.01",
                "fees_enabled": False,
            },
            "rewards": {
                "rewards_min_size": "20",
                "rewards_max_spread": "10",
            },
            "outcomes": {
                "yes": {"label": "YES", "token_id": token_pairs[index][0]},
                **(
                    {"no": {"label": "NO", "token_id": token_pairs[index][1]}}
                    if len(token_pairs[index]) == 2
                    else {}
                ),
            },
        }
        for index, condition_id in enumerate(conditions)
    ]

    entered = threading.Event()
    fifth_entered = threading.Event()
    release = [threading.Event() for _ in range(5)]
    lock = threading.Lock()
    requests: list[tuple[str, ...]] = []
    active = 0
    max_active = 0

    def release_active() -> None:
        nonlocal active
        with lock:
            active -= 1

    class Response:
        def __init__(self, batch: tuple[str, ...], start_ts: int, end_ts: int) -> None:
            self.batch = batch
            self.start_ts = start_ts
            self.end_ts = end_ts

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_args: object) -> None:
            release_active()
            return None

        def read(self) -> bytes:
            return json.dumps(
                {
                    "history": {
                        token: [
                            {"t": self.start_ts, "p": "0.500"},
                            {"t": self.end_ts, "p": "0.505"},
                        ]
                        for token in self.batch
                    }
                }
            ).encode("utf-8")

    def open_history(request: object, *, timeout: float) -> Response:
        del timeout
        nonlocal active, max_active
        body = json.loads(request.data.decode("utf-8"))  # type: ignore[attr-defined]
        batch = tuple(body["markets"])
        assert batch
        assert len(batch) <= 20
        assert len(batch) == len(set(batch))
        assert set(batch).issubset(set(expected_tokens))
        start_ts = body["start_ts"]
        end_ts = body["end_ts"]
        with lock:
            requests.append(batch)
            index = len(requests) - 1
            active += 1
            max_active = max(max_active, active)
            if len(requests) == 4:
                entered.set()
        if index < 5:
            if index == 4:
                fifth_entered.set()
            assert release[index].wait(timeout=5)
        return Response(batch, start_ts, end_ts)

    class PublicSDK:
        def list_current_rewards(self, *, sponsored: bool) -> list[object]:
            return [] if sponsored else rewards

        def list_markets(
            self, *, condition_ids: object, page_size: int = 100
        ) -> list[object]:
            del page_size
            requested = set(condition_ids)  # type: ignore[arg-type]
            return [row for row in markets if row["condition_id"] in requested]

        def close(self) -> None:
            return None

    class AccountSDK:
        environment = SimpleNamespace(standard_exchange="standard-exchange")

    store = PredictionArbitrageStore(tmp_path)
    trading = PolymarketTradingClient(
        TradingConfig("signer", "wallet"),
        AccountSDK(),
        urlopen_fn=open_history,
        public_client_factory=PublicSDK,
    )
    lp = PolymarketLPService(store, trading, clock=lambda: now)
    driver_result: list[dict[str, object]] = []

    def refresh() -> None:
        driver_result.append(lp.refresh_price_history())

    driver = threading.Thread(target=refresh, name="n14-history-driver")
    driver.start()
    try:
        assert entered.wait(timeout=3), "history reader did not enter four bounded requests"
        with lock:
            assert len(requests) == 4
            assert active == 4
            assert max_active == 4
        busy = lp.refresh_price_history()
        assert busy["state"] == "busy"
        with lock:
            assert len(requests) == 4
        for event in release[:4]:
            event.set()
        assert fifth_entered.wait(timeout=3), "fifth history batch did not use a released slot"
        with lock:
            assert max_active == 4
        release[4].set()
        driver.join(timeout=8)
        assert not driver.is_alive()
        assert len(driver_result) == 1
        result = driver_result[0]
        assert result["state"] == "known"
        assert result["target_count"] == 81
        assert result["updated_count"] == 81
        assert result["unknown_count"] == 0
        assert result["request_count"] == 5
        with lock:
            assert len(requests) == 5
            assert max_active == 4
            assert set(token for batch in requests for token in batch) == set(expected_tokens)
            assert sum(len(batch) for batch in requests) == 81
        for condition_id, pair in zip(conditions, token_pairs, strict=True):
            for token_id in pair:
                summary = store.lp_price_history_summary(
                    condition_id, token_id, now=now
                )
                assert summary is not None
                assert summary["state"] == "known"
                assert Decimal(str(summary["amplitude"])) == Decimal("0.005")
                assert summary["sample_count"] == 2
                assert summary["window_start"] == (
                    now - timedelta(hours=24)
                ).isoformat(timespec="microseconds").replace("+00:00", "Z")
                assert summary["window_end"] == now.isoformat(
                    timespec="microseconds"
                ).replace("+00:00", "Z")
                assert summary["checked_at"] == now.isoformat(
                    timespec="microseconds"
                ).replace("+00:00", "Z")
        stop_event = threading.Event()
        stop_event.set()
        before_cancel = len(requests)
        cancelled = lp.refresh_price_history(stop_event=stop_event)
        assert cancelled["state"] == "cancelled"
        with lock:
            assert len(requests) == before_cancel
    finally:
        for event in release:
            event.set()
        driver.join(timeout=8)
        assert not driver.is_alive()


def test_lp_dashboard_today_orders_fail_open_and_non_lp_count(tmp_path: Path) -> None:
    """当天 LP 委托：三信号全部明确否定才排除；判不出时保留并只统计明确否定行。"""

    wallet = "0x" + "4" * 40
    account_id = hashlib.sha256(wallet.strip().casefold().encode("utf-8")).hexdigest()

    class FakeTrading:
        def __init__(self) -> None:
            self.config = SimpleNamespace(
                wallet_address=wallet,
                signer_address="0x" + "5" * 40,
                predict=None,
            )

        def lp_account_snapshot(self) -> dict[str, object]:
            return {
                "authenticated": True,
                "checked_at": datetime.now(UTC),
                "open_orders": [
                    {
                        "order_id": "nonlp-order",
                        "condition_id": "condition-nonlp",
                        "token_id": "nonlp-token",
                        "outcome": "YES",
                        "side": "BUY",
                        "status": "LIVE",
                        "price": Decimal("0.50"),
                        "original_size": Decimal("40"),
                        "size_matched": Decimal("0"),
                        "remaining_size": Decimal("40"),
                        "market_title": "No reward market",
                        "market_url": "https://polymarket.com/event/nonlp",
                    },
                    {
                        "order_id": "failopen-order",
                        "condition_id": "condition-failopen",
                        "token_id": "failopen-token",
                        "outcome": "YES",
                        "side": "BUY",
                        "status": "LIVE",
                        "price": Decimal("0.50"),
                        "original_size": Decimal("50"),
                        "size_matched": Decimal("10"),
                        "remaining_size": Decimal("40"),
                        "market_title": "Fail open market",
                        "market_url": "https://polymarket.com/event/failopen",
                    },
                    {
                        "order_id": "managed-order",
                        "condition_id": "condition-nonlp",
                        "token_id": "nonlp-token",
                        "outcome": "YES",
                        "side": "BUY",
                        "status": "LIVE",
                        "price": Decimal("0.50"),
                        "original_size": Decimal("5"),
                        "size_matched": Decimal("0"),
                        "remaining_size": Decimal("5"),
                        "market_title": "No reward market",
                        "market_url": "https://polymarket.com/event/nonlp",
                    },
                ],
                "positions": [
                    {
                        "condition_id": "condition-nonlp",
                        "token_id": "nonlp-token",
                        "outcome": "YES",
                        "size": Decimal("3"),
                        "average_price": Decimal("0.50"),
                        "market_title": "No reward market",
                        "market_url": "https://polymarket.com/event/nonlp",
                    }
                ],
                "open_orders_complete": True,
                "positions_complete": True,
            }

        def get_order_scoring(self, order_id: str) -> object:
            if order_id == "nonlp-order":
                return False
            raise RuntimeError("scoring unavailable")

        def lp_reward_snapshot(
            self, reward_date: str, condition_id: str
        ) -> dict[str, object]:
            return {
                "state": "unknown",
                "reward_date": reward_date,
                "condition_id": condition_id,
            }

    class FakeLP:
        def candidate_snapshot(self) -> dict[str, object]:
            return {
                "state": "known",
                "complete": False,
                "candidates": [],
                "recommendations": [],
                "market_rewards": {},
            }

        def status(self, _session_id: str | None = None) -> dict[str, object]:
            return {
                "state": "entry_open",
                "entry_order_id": "managed-order",
                "token_id": "session-token",
            }

    store = PredictionArbitrageStore(tmp_path / "data")
    store.save_lp_observation(
        account_id,
        "condition-nonlp",
        {
            "state": "unknown",
            "stage": "added",
            "stale": False,
            "reason": "reward_market_missing",
            "checked_at": datetime.now(UTC),
        },
    )
    service = PredictionExecutionService(
        store=store,
        monitor=object(),
        trading=FakeTrading(),
        notifier=NullNotifier(),
        lock_path=tmp_path / "execution.lock",
        lp=FakeLP(),
    )

    dashboard = service.refresh_lp_dashboard_snapshot()

    today_ids = [
        str(row["order_id"]) for row in dashboard["lp_orders_today"]
    ]
    # AC4: 观察缺失 + 奖励读取不完整 → fail-open 保留。
    assert "failopen-order" in today_ids
    # AC3: 会话管理行永不排除。
    assert "managed-order" in today_ids
    # AC3: 计分否定 + 奖励市场缺失（读取完整）+ 当天无奖励记录 → 排除。
    assert "nonlp-order" not in today_ids
    failopen_row = next(
        row
        for row in dashboard["lp_orders_today"]
        if row["order_id"] == "failopen-order"
    )
    assert failopen_row["quantity"] == Decimal("50")
    assert failopen_row["filled_quantity"] == Decimal("10")
    assert failopen_row["remaining_quantity"] == Decimal("40")
    assert failopen_row["price"] == Decimal("0.50")
    # AC3: non_lp_row_count 只统计明确否定且非会话管理的行（manual 订单 + 持仓）。
    assert dashboard["non_lp_row_count"] == 2


def test_lp_dashboard_today_orders_include_filled_orders_from_trades(
    tmp_path: Path,
) -> None:
    """AC2/AC5: 已离开挂单列表的全量成交订单按当天成交聚合回表；隔日成交不聚合。"""

    class FakeTrading:
        def __init__(self) -> None:
            self.config = SimpleNamespace(
                wallet_address="0x" + "4" * 40,
                signer_address="0x" + "5" * 40,
                predict=None,
            )
            self.trade_calls: list[tuple[str, ...]] = []
            self.trades_requested = threading.Event()

        def lp_account_snapshot(self) -> dict[str, object]:
            return {
                "authenticated": True,
                "checked_at": datetime(2026, 9, 16, 10, tzinfo=UTC),
                "open_orders": [],
                "positions": [],
                "open_orders_complete": True,
                "positions_complete": True,
            }

        def lp_market_metadata(
            self, condition_ids: object
        ) -> dict[str, dict[str, object]]:
            return {
                str(condition_id): {
                    "market_id": "market-1",
                    "market_title": "Filled LP market",
                    "market_url": "https://polymarket.com/event/filled",
                }
                for condition_id in condition_ids  # type: ignore[attr-defined]
            }

        def lp_account_trades(
            self, condition_ids: object, *, stop_event: threading.Event | None = None
        ) -> dict[str, object]:
            self.trade_calls.append(tuple(str(item) for item in condition_ids))  # type: ignore[attr-defined]
            self.trades_requested.set()
            return {
                "state": "known",
                "complete": True,
                "checked_at": datetime(2026, 9, 16, 10, 5, tzinfo=UTC),
                "trades": {
                    "condition-1": [
                        {
                            "id": "t1",
                            "condition_id": "condition-1",
                            "token_id": "yes-token",
                            "taker_order_id": "",
                            "side": "BUY",
                            "trader_side": "MAKER",
                            "price": Decimal("0.44"),
                            "size": Decimal("30"),
                            "status": "MATCHED",
                            "matched_at": datetime(2026, 9, 16, 2, 0, tzinfo=UTC),
                            "maker_orders": [
                                {
                                    "order_id": "filled-order-1",
                                    "token_id": "yes-token",
                                    "side": "BUY",
                                    "price": Decimal("0.44"),
                                    "matched_amount": Decimal("30"),
                                }
                            ],
                        },
                        {
                            "id": "t2",
                            "condition_id": "condition-1",
                            "token_id": "yes-token",
                            "taker_order_id": "",
                            "side": "BUY",
                            "trader_side": "MAKER",
                            "price": Decimal("0.46"),
                            "size": Decimal("20"),
                            "status": "MATCHED",
                            "matched_at": datetime(2026, 9, 16, 3, 0, tzinfo=UTC),
                            "maker_orders": [
                                {
                                    "order_id": "filled-order-1",
                                    "token_id": "yes-token",
                                    "side": "BUY",
                                    "price": Decimal("0.46"),
                                    "matched_amount": Decimal("20"),
                                }
                            ],
                        },
                        {
                            "id": "t3",
                            "condition_id": "condition-1",
                            "token_id": "yes-token",
                            "taker_order_id": "filled-order-2",
                            "side": "BUY",
                            "trader_side": "TAKER",
                            "price": Decimal("0.45"),
                            "size": Decimal("60"),
                            "status": "MATCHED",
                            "matched_at": datetime(2026, 9, 16, 2, 5, tzinfo=UTC),
                            "maker_orders": [],
                        },
                        {
                            "id": "t4",
                            "condition_id": "condition-1",
                            "token_id": "yes-token",
                            "taker_order_id": "filled-order-3",
                            "side": "BUY",
                            "trader_side": "TAKER",
                            "price": Decimal("0.50"),
                            "size": Decimal("99"),
                            "status": "MATCHED",
                            "matched_at": datetime(2026, 9, 15, 23, 0, tzinfo=UTC),
                            "maker_orders": [],
                        },
                    ],
                },
            }

    class FakeLP:
        def candidate_snapshot(self) -> dict[str, object]:
            return {
                "state": "known",
                "complete": True,
                "candidates": [],
                "recommendations": [],
                "market_rewards": {
                    "condition-1": {
                        "state": "known",
                        "reward_date": "2026-09-16",
                        "condition_id": "condition-1",
                        "market_amount": Decimal("0.20"),
                    }
                },
            }

        def status(self, _session_id: str | None = None) -> dict[str, object]:
            return {"state": "none"}

    trading = FakeTrading()
    service = PredictionExecutionService(
        store=PredictionArbitrageStore(tmp_path / "data"),
        monitor=object(),
        trading=trading,
        notifier=NullNotifier(),
        lock_path=tmp_path / "execution.lock",
        lp=FakeLP(),
    )

    first = service.refresh_lp_dashboard_snapshot()
    # AC4: 成交聚合异步补齐，读取本身不等待。
    assert first["lp_orders_today"] == []
    assert trading.trades_requested.wait(timeout=5)
    dashboard = first
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        dashboard = service.refresh_lp_dashboard_snapshot()
        filled_ids = {
            str(row["order_id"])
            for row in dashboard["lp_orders_today"]
            if row["state"] == "filled"
        }
        if {"filled-order-1", "filled-order-2"} <= filled_ids:
            break
        time.sleep(0.05)
    assert trading.trade_calls and trading.trade_calls[0] == ("condition-1",)
    rows = {
        str(row["order_id"]): row for row in dashboard["lp_orders_today"]
    }
    # AC5: matched_at 早于当日 UTC 00:00 的成交（filled-order-3）不参与当天聚合。
    assert set(rows) == {"filled-order-1", "filled-order-2"}
    order1 = rows["filled-order-1"]
    # AC2: maker 成交按订单聚合（30 + 20），最新成交时间取最大值。
    assert order1["filled_quantity"] == Decimal("50")
    assert order1["remaining_quantity"] == Decimal("0")
    assert order1["status"] == "MATCHED"
    assert order1["market_title"] == "Filled LP market"
    assert datetime.fromisoformat(
        str(order1["last_fill_at"]).replace("Z", "+00:00")
    ) == datetime(2026, 9, 16, 3, 0, tzinfo=UTC)
    order2 = rows["filled-order-2"]
    # AC2: taker 成交按 taker_order_id 归属。
    assert order2["filled_quantity"] == Decimal("60")
    assert order2["side"] == "BUY"


def test_lp_today_orders_maker_fills_exclude_counterparty_taker_order(
    tmp_path: Path,
) -> None:
    """R1: 我方是 maker 时，成交聚合只计我方 maker 订单；
    对手方 taker_order_id 绝不能计为我方成交（无回退归属）。"""

    class FakeTrading:
        def __init__(self) -> None:
            self.config = SimpleNamespace(
                wallet_address="0x" + "4" * 40,
                signer_address="0x" + "5" * 40,
                predict=None,
            )
            self.trade_calls: list[tuple[str, ...]] = []
            self.trades_requested = threading.Event()

        def lp_account_snapshot(self) -> dict[str, object]:
            return {
                "authenticated": True,
                "checked_at": datetime(2026, 9, 16, 10, tzinfo=UTC),
                "open_orders": [],
                "positions": [],
                "open_orders_complete": True,
                "positions_complete": True,
            }

        def lp_market_metadata(
            self, condition_ids: object
        ) -> dict[str, dict[str, object]]:
            return {
                str(condition_id): {
                    "market_id": "market-1",
                    "market_title": "Swept LP market",
                    "market_url": "https://polymarket.com/event/swept",
                }
                for condition_id in condition_ids  # type: ignore[attr-defined]
            }

        def lp_account_trades(
            self, condition_ids: object, *, stop_event: threading.Event | None = None
        ) -> dict[str, object]:
            self.trade_calls.append(tuple(str(item) for item in condition_ids))  # type: ignore[attr-defined]
            self.trades_requested.set()
            return {
                "state": "known",
                "complete": True,
                "checked_at": datetime(2026, 9, 16, 10, 5, tzinfo=UTC),
                "trades": {
                    "condition-1": [
                        {
                            "id": "sweep-1",
                            "condition_id": "condition-1",
                            "token_id": "yes-token",
                            # taker 扫单对手方订单；我方是 maker，绝不能计入。
                            "taker_order_id": "counterparty-taker-order",
                            "side": "SELL",
                            "trader_side": "MAKER",
                            "price": Decimal("0.50"),
                            "size": Decimal("100"),
                            "status": "CONFIRMED",
                            "matched_at": datetime(2026, 9, 16, 2, 0, tzinfo=UTC),
                            # 适配器按钱包过滤后只剩我方 maker 行（10 份）。
                            "maker_orders": [
                                {
                                    "order_id": "our-maker-order",
                                    "token_id": "yes-token",
                                    "side": "BUY",
                                    "price": Decimal("0.50"),
                                    "matched_amount": Decimal("10"),
                                }
                            ],
                        },
                        {
                            "id": "sweep-2",
                            "condition_id": "condition-1",
                            "token_id": "yes-token",
                            "taker_order_id": "counterparty-order-2",
                            "side": "SELL",
                            "trader_side": "MAKER",
                            "price": Decimal("0.50"),
                            "size": Decimal("77"),
                            "status": "CONFIRMED",
                            "matched_at": datetime(2026, 9, 16, 2, 30, tzinfo=UTC),
                            # 归属不明的 maker 行已在适配器被丢弃。
                            "maker_orders": [],
                        },
                    ],
                },
            }

    class FakeLP:
        def candidate_snapshot(self) -> dict[str, object]:
            return {
                "state": "known",
                "complete": True,
                "candidates": [],
                "recommendations": [],
                "market_rewards": {
                    "condition-1": {
                        "state": "known",
                        "reward_date": "2026-09-16",
                        "condition_id": "condition-1",
                        "market_amount": Decimal("0.20"),
                    }
                },
            }

        def status(self, _session_id: str | None = None) -> dict[str, object]:
            return {"state": "none"}

    trading = FakeTrading()
    service = PredictionExecutionService(
        store=PredictionArbitrageStore(tmp_path / "data"),
        monitor=object(),
        trading=trading,
        notifier=NullNotifier(),
        lock_path=tmp_path / "execution.lock",
        lp=FakeLP(),
    )

    first = service.refresh_lp_dashboard_snapshot()
    assert first["lp_orders_today"] == []
    assert trading.trades_requested.wait(timeout=5)
    dashboard = first
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        dashboard = service.refresh_lp_dashboard_snapshot()
        filled_ids = {
            str(row["order_id"])
            for row in dashboard["lp_orders_today"]
            if row["state"] == "filled"
        }
        if "our-maker-order" in filled_ids:
            break
        time.sleep(0.05)
    rows = {
        str(row["order_id"]): row for row in dashboard["lp_orders_today"]
    }
    # R1: 只有我方自己的订单出现在 lp_orders_today，外来/对手方 id 不出现。
    assert set(rows) == {"our-maker-order"}
    assert rows["our-maker-order"]["filled_quantity"] == Decimal("10")
    assert rows["our-maker-order"]["side"] == "BUY"


def test_lp_today_orders_fills_skip_failed_trades(tmp_path: Path) -> None:
    """R2: 同市场一笔 CONFIRMED 30 份 + 一笔 FAILED 77 份 → 只聚合 30。"""

    class FakeTrading:
        def __init__(self) -> None:
            self.config = SimpleNamespace(
                wallet_address="0x" + "4" * 40,
                signer_address="0x" + "5" * 40,
                predict=None,
            )
            self.trade_calls: list[tuple[str, ...]] = []
            self.trades_requested = threading.Event()

        def lp_account_snapshot(self) -> dict[str, object]:
            return {
                "authenticated": True,
                "checked_at": datetime(2026, 9, 16, 10, tzinfo=UTC),
                "open_orders": [],
                "positions": [],
                "open_orders_complete": True,
                "positions_complete": True,
            }

        def lp_market_metadata(
            self, condition_ids: object
        ) -> dict[str, dict[str, object]]:
            return {
                str(condition_id): {
                    "market_id": "market-1",
                    "market_title": "Status LP market",
                    "market_url": "https://polymarket.com/event/status",
                }
                for condition_id in condition_ids  # type: ignore[attr-defined]
            }

        def lp_account_trades(
            self, condition_ids: object, *, stop_event: threading.Event | None = None
        ) -> dict[str, object]:
            self.trade_calls.append(tuple(str(item) for item in condition_ids))  # type: ignore[attr-defined]
            self.trades_requested.set()
            return {
                "state": "known",
                "complete": True,
                "checked_at": datetime(2026, 9, 16, 10, 5, tzinfo=UTC),
                "trades": {
                    "condition-1": [
                        {
                            "id": "t-ok",
                            "condition_id": "condition-1",
                            "token_id": "yes-token",
                            "taker_order_id": "confirmed-order",
                            "side": "BUY",
                            "trader_side": "TAKER",
                            "price": Decimal("0.50"),
                            "size": Decimal("30"),
                            "status": "CONFIRMED",
                            "matched_at": datetime(2026, 9, 16, 2, 0, tzinfo=UTC),
                            "maker_orders": [],
                        },
                        {
                            "id": "t-failed",
                            "condition_id": "condition-1",
                            "token_id": "yes-token",
                            "taker_order_id": "failed-order",
                            "side": "BUY",
                            "trader_side": "TAKER",
                            "price": Decimal("0.50"),
                            "size": Decimal("77"),
                            "status": "FAILED",
                            "matched_at": datetime(2026, 9, 16, 2, 30, tzinfo=UTC),
                            "maker_orders": [],
                        },
                    ],
                },
            }

    class FakeLP:
        def candidate_snapshot(self) -> dict[str, object]:
            return {
                "state": "known",
                "complete": True,
                "candidates": [],
                "recommendations": [],
                "market_rewards": {
                    "condition-1": {
                        "state": "known",
                        "reward_date": "2026-09-16",
                        "condition_id": "condition-1",
                        "market_amount": Decimal("0.20"),
                    }
                },
            }

        def status(self, _session_id: str | None = None) -> dict[str, object]:
            return {"state": "none"}

    trading = FakeTrading()
    service = PredictionExecutionService(
        store=PredictionArbitrageStore(tmp_path / "data"),
        monitor=object(),
        trading=trading,
        notifier=NullNotifier(),
        lock_path=tmp_path / "execution.lock",
        lp=FakeLP(),
    )

    first = service.refresh_lp_dashboard_snapshot()
    assert first["lp_orders_today"] == []
    assert trading.trades_requested.wait(timeout=5)
    dashboard = first
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        dashboard = service.refresh_lp_dashboard_snapshot()
        filled_ids = {
            str(row["order_id"])
            for row in dashboard["lp_orders_today"]
            if row["state"] == "filled"
        }
        if "confirmed-order" in filled_ids:
            break
        time.sleep(0.05)
    rows = {
        str(row["order_id"]): row for row in dashboard["lp_orders_today"]
    }
    # R2: 只有落地的 CONFIRMED 成交计入成交量，FAILED 不计。
    assert set(rows) == {"confirmed-order"}
    assert rows["confirmed-order"]["filled_quantity"] == Decimal("30")


def test_lp_today_orders_prior_day_negative_observation_fails_open(
    tmp_path: Path,
) -> None:
    """R3: reward_market_missing 否定只在观察落在当前奖励日内才成立；
    昨日的否定观察（跨过北京 08:00 或观察停摆）不再排除今天的 LP 行。"""

    wallet = "0x" + "4" * 40
    account_id = hashlib.sha256(wallet.strip().casefold().encode("utf-8")).hexdigest()

    class FakeTrading:
        def __init__(self) -> None:
            self.config = SimpleNamespace(
                wallet_address=wallet,
                signer_address="0x" + "5" * 40,
                predict=None,
            )

        def lp_account_snapshot(self) -> dict[str, object]:
            return {
                "authenticated": True,
                "checked_at": datetime(2026, 9, 16, 10, tzinfo=UTC),
                "open_orders": [
                    {
                        "order_id": "prior-day-order",
                        "condition_id": "condition-prior-day",
                        "token_id": "prior-day-token",
                        "outcome": "YES",
                        "side": "BUY",
                        "status": "LIVE",
                        "price": Decimal("0.50"),
                        "original_size": Decimal("40"),
                        "size_matched": Decimal("0"),
                        "remaining_size": Decimal("40"),
                        "market_title": "Prior day negative market",
                        "market_url": "https://polymarket.com/event/prior-day",
                    },
                    {
                        "order_id": "in-day-order",
                        "condition_id": "condition-in-day",
                        "token_id": "in-day-token",
                        "outcome": "YES",
                        "side": "BUY",
                        "status": "LIVE",
                        "price": Decimal("0.50"),
                        "original_size": Decimal("40"),
                        "size_matched": Decimal("0"),
                        "remaining_size": Decimal("40"),
                        "market_title": "In day negative market",
                        "market_url": "https://polymarket.com/event/in-day",
                    },
                ],
                "positions": [],
                "open_orders_complete": True,
                "positions_complete": True,
            }

        def get_order_scoring(self, _order_id: str) -> object:
            return False

        def lp_reward_snapshot(
            self, reward_date: str, condition_id: str
        ) -> dict[str, object]:
            return {
                "state": "unknown",
                "reward_date": reward_date,
                "condition_id": condition_id,
            }

    class FakeLP:
        def candidate_snapshot(self) -> dict[str, object]:
            return {
                "state": "known",
                "complete": True,
                "candidates": [],
                "recommendations": [],
                "market_rewards": {},
            }

        def status(self, _session_id: str | None = None) -> dict[str, object]:
            return {"state": "none"}

    store = PredictionArbitrageStore(tmp_path / "data")
    # 同一观察形态，只有 checked_at 相差一天：昨日的否定失效，当日的仍否定。
    store.save_lp_observation(
        account_id,
        "condition-prior-day",
        {
            "state": "unknown",
            "stage": "added",
            "stale": False,
            "reason": "reward_market_missing",
            "checked_at": datetime(2026, 9, 15, 20, 0, tzinfo=UTC),
        },
    )
    store.save_lp_observation(
        account_id,
        "condition-in-day",
        {
            "state": "unknown",
            "stage": "added",
            "stale": False,
            "reason": "reward_market_missing",
            "checked_at": datetime(2026, 9, 16, 1, 0, tzinfo=UTC),
        },
    )
    service = PredictionExecutionService(
        store=store,
        monitor=object(),
        trading=FakeTrading(),
        notifier=NullNotifier(),
        lock_path=tmp_path / "execution.lock",
        lp=FakeLP(),
    )

    dashboard = service.refresh_lp_dashboard_snapshot()

    # R3: 昨日否定 → fail-open 保留；当日否定 → 照常排除。
    assert [str(row["order_id"]) for row in dashboard["lp_orders_today"]] == [
        "prior-day-order"
    ]
    assert dashboard["non_lp_row_count"] == 1


def test_lp_today_orders_cached_fills_follow_market_gate_and_session_management(
    tmp_path: Path,
) -> None:
    """R4: 明确否定市场的缓存成交不进表；会话成交单按会话归属标注。"""

    wallet = "0x" + "4" * 40
    account_id = hashlib.sha256(wallet.strip().casefold().encode("utf-8")).hexdigest()

    class FakeTrading:
        def __init__(self) -> None:
            self.config = SimpleNamespace(
                wallet_address=wallet,
                signer_address="0x" + "5" * 40,
                predict=None,
            )
            self.trade_calls: list[tuple[str, ...]] = []
            self.trades_requested = threading.Event()

        def lp_account_snapshot(self) -> dict[str, object]:
            return {
                "authenticated": True,
                "checked_at": datetime(2026, 9, 16, 10, tzinfo=UTC),
                "open_orders": [],
                "positions": [],
                "open_orders_complete": True,
                "positions_complete": True,
            }

        def lp_market_metadata(
            self, condition_ids: object
        ) -> dict[str, dict[str, object]]:
            return {
                str(condition_id): {
                    "market_id": f"market-{condition_id}",
                    "market_title": f"Cached LP market {condition_id}",
                    "market_url": f"https://polymarket.com/event/{condition_id}",
                }
                for condition_id in condition_ids  # type: ignore[attr-defined]
            }

        def lp_account_trades(
            self, condition_ids: object, *, stop_event: threading.Event | None = None
        ) -> dict[str, object]:
            self.trade_calls.append(tuple(str(item) for item in condition_ids))  # type: ignore[attr-defined]
            self.trades_requested.set()
            trades: dict[str, object] = {}
            for fills in (
                ("condition-neg", "neg-fill-order", "neg-token", "77"),
                ("condition-managed", "managed-fill-order", "managed-token", "30"),
                ("condition-manual", "manual-fill-order", "manual-token", "20"),
            ):
                market, order_id, token_id, size = fills
                trades[market] = [
                    {
                        "id": f"trade-{order_id}",
                        "condition_id": market,
                        "token_id": token_id,
                        "taker_order_id": order_id,
                        "side": "BUY",
                        "trader_side": "TAKER",
                        "price": Decimal("0.50"),
                        "size": Decimal(size),
                        "status": "CONFIRMED",
                        "matched_at": datetime(2026, 9, 16, 2, 0, tzinfo=UTC),
                        "maker_orders": [],
                    }
                ]
            return {
                "state": "known",
                "complete": True,
                "checked_at": datetime(2026, 9, 16, 10, 5, tzinfo=UTC),
                "trades": trades,
            }

    class FakeLP:
        def __init__(self) -> None:
            self.include_rewards = True

        def candidate_snapshot(self) -> dict[str, object]:
            market_rewards: dict[str, object] = {}
            if self.include_rewards:
                for condition_id in (
                    "condition-neg",
                    "condition-managed",
                    "condition-manual",
                ):
                    market_rewards[condition_id] = {
                        "state": "known",
                        "reward_date": "2026-09-16",
                        "condition_id": condition_id,
                        "market_amount": Decimal("0.20"),
                    }
            return {
                "state": "known",
                "complete": True,
                "candidates": [],
                "recommendations": [],
                "market_rewards": market_rewards,
            }

        def status(self, _session_id: str | None = None) -> dict[str, object]:
            return {
                "state": "entry_open",
                "entry_order_id": "managed-fill-order",
                "token_id": "managed-token",
            }

    store = PredictionArbitrageStore(tmp_path / "data")
    store.save_lp_observation(
        account_id,
        "condition-neg",
        {
            "state": "unknown",
            "stage": "added",
            "stale": False,
            "reason": "reward_market_missing",
            "checked_at": datetime(2026, 9, 16, 1, 0, tzinfo=UTC),
        },
    )
    lp = FakeLP()
    service = PredictionExecutionService(
        store=store,
        monitor=object(),
        trading=FakeTrading(),
        notifier=NullNotifier(),
        lock_path=tmp_path / "execution.lock",
        lp=lp,
    )

    # 第一轮装配：三个市场当天都有奖励记录，成交缓存异步补齐并进表。
    first = service.refresh_lp_dashboard_snapshot()
    assert first["lp_orders_today"] == []
    deadline = time.monotonic() + 5
    dashboard = first
    while time.monotonic() < deadline:
        dashboard = service.refresh_lp_dashboard_snapshot()
        if any(
            str(row["order_id"]) == "manual-fill-order"
            for row in dashboard["lp_orders_today"]
        ):
            break
        time.sleep(0.05)
    assert any(
        str(row["order_id"]) == "neg-fill-order"
        for row in dashboard["lp_orders_today"]
    )

    # 第二轮装配：奖励记录消失 + 明确否定观察 → 缓存成交行必须被同一门挡住。
    lp.include_rewards = False
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        dashboard = service.refresh_lp_dashboard_snapshot()
        if not any(
            str(row["order_id"]) == "neg-fill-order"
            for row in dashboard["lp_orders_today"]
        ):
            break
        time.sleep(0.05)
    rows = {
        str(row["order_id"]): row for row in dashboard["lp_orders_today"]
    }
    # R4: 明确否定市场的成交行不出现。
    assert "neg-fill-order" not in rows
    # R4: 会话成交单标 system_managed / read_only False；手工成交单保持 manual。
    assert set(rows) == {"managed-fill-order", "manual-fill-order"}
    assert rows["managed-fill-order"]["management"] == "system_managed"
    assert rows["managed-fill-order"]["read_only"] is False
    assert rows["manual-fill-order"]["management"] == "manual_read_only"
    assert rows["manual-fill-order"]["read_only"] is True


def test_lp_today_orders_trade_reads_throttled_until_ttl_expires(
    tmp_path: Path,
) -> None:
    """R5: 同一奖励日内按市场节流成交重读；TTL 内不再调用 lp_account_trades，
    过期后重新入队。"""

    class FakeTrading:
        def __init__(self) -> None:
            self.config = SimpleNamespace(
                wallet_address="0x" + "4" * 40,
                signer_address="0x" + "5" * 40,
                predict=None,
            )
            self.trade_calls: list[tuple[str, ...]] = []

        def lp_account_snapshot(self) -> dict[str, object]:
            return {
                "authenticated": True,
                "checked_at": datetime(2026, 9, 16, 10, tzinfo=UTC),
                "open_orders": [],
                "positions": [],
                "open_orders_complete": True,
                "positions_complete": True,
            }

        def lp_market_metadata(
            self, condition_ids: object
        ) -> dict[str, dict[str, object]]:
            return {
                str(condition_id): {
                    "market_id": "market-1",
                    "market_title": "Throttled LP market",
                    "market_url": "https://polymarket.com/event/throttled",
                }
                for condition_id in condition_ids  # type: ignore[attr-defined]
            }

        def lp_account_trades(
            self, condition_ids: object, *, stop_event: threading.Event | None = None
        ) -> dict[str, object]:
            self.trade_calls.append(tuple(str(item) for item in condition_ids))  # type: ignore[attr-defined]
            return {
                "state": "known",
                "complete": True,
                "checked_at": datetime(2026, 9, 16, 10, 5, tzinfo=UTC),
                "trades": {
                    "condition-1": [
                        {
                            "id": "t1",
                            "condition_id": "condition-1",
                            "token_id": "yes-token",
                            "taker_order_id": "throttled-order",
                            "side": "BUY",
                            "trader_side": "TAKER",
                            "price": Decimal("0.50"),
                            "size": Decimal("10"),
                            "status": "CONFIRMED",
                            "matched_at": datetime(2026, 9, 16, 2, 0, tzinfo=UTC),
                            "maker_orders": [],
                        }
                    ]
                },
            }

        def lp_reward_snapshot(
            self, reward_date: str, condition_id: str
        ) -> dict[str, object]:
            return {
                "state": "known",
                "reward_date": reward_date,
                "condition_id": condition_id,
                "market_amount": Decimal("0.20"),
            }

    class FakeLP:
        def candidate_snapshot(self) -> dict[str, object]:
            return {
                "state": "known",
                "complete": True,
                "candidates": [],
                "recommendations": [],
                "market_rewards": {
                    "condition-1": {
                        "state": "known",
                        "reward_date": "2026-09-16",
                        "condition_id": "condition-1",
                        "market_amount": Decimal("0.20"),
                    }
                },
            }

        def status(self, _session_id: str | None = None) -> dict[str, object]:
            return {"state": "none"}

    trading = FakeTrading()
    service = PredictionExecutionService(
        store=PredictionArbitrageStore(tmp_path / "data"),
        monitor=object(),
        trading=trading,
        notifier=NullNotifier(),
        lock_path=tmp_path / "execution.lock",
        lp=FakeLP(),
    )
    clock = {"now": 1000.0}
    service._clock = lambda: clock["now"]  # type: ignore[method-assign]

    def wait_for_reads(count: int) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if len(trading.trade_calls) >= count:
                return
            time.sleep(0.02)
        raise AssertionError(f"expected {count} trade reads, got {len(trading.trade_calls)}")

    # 第一次装配触发首次成交读取。
    service.refresh_lp_dashboard_snapshot()
    wait_for_reads(1)
    # TTL 内的后续装配不再重读该市场。
    service.refresh_lp_dashboard_snapshot()
    service.refresh_lp_dashboard_snapshot()
    time.sleep(0.3)
    assert len(trading.trade_calls) == 1
    # 注入时钟越过 TTL 后重新入队。
    clock["now"] += 61.0
    service.refresh_lp_dashboard_snapshot()
    wait_for_reads(2)
    assert trading.trade_calls[0] == ("condition-1",)
    assert trading.trade_calls[1] == ("condition-1",)


def test_lp_today_orders_fill_rows_resolve_outcome_from_metadata(
    tmp_path: Path,
) -> None:
    """R6: 已成交行按 metadata 的 outcomes/token_id 解析 outcome；解不出保持空。"""

    class FakeTrading:
        def __init__(self) -> None:
            self.config = SimpleNamespace(
                wallet_address="0x" + "4" * 40,
                signer_address="0x" + "5" * 40,
                predict=None,
            )
            self.trade_calls: list[tuple[str, ...]] = []
            self.trades_requested = threading.Event()

        def lp_account_snapshot(self) -> dict[str, object]:
            return {
                "authenticated": True,
                "checked_at": datetime(2026, 9, 16, 10, tzinfo=UTC),
                "open_orders": [],
                "positions": [],
                "open_orders_complete": True,
                "positions_complete": True,
            }

        def lp_market_metadata(
            self, condition_ids: object
        ) -> dict[str, dict[str, object]]:
            return {
                str(condition_id): {
                    "market_id": "market-1",
                    "market_title": "Outcome LP market",
                    "market_url": "https://polymarket.com/event/outcome",
                    "outcomes": {
                        "yes": {"label": "YES", "token_id": "yes-token"},
                    },
                }
                for condition_id in condition_ids  # type: ignore[attr-defined]
            }

        def lp_account_trades(
            self, condition_ids: object, *, stop_event: threading.Event | None = None
        ) -> dict[str, object]:
            self.trade_calls.append(tuple(str(item) for item in condition_ids))  # type: ignore[attr-defined]
            self.trades_requested.set()
            return {
                "state": "known",
                "complete": True,
                "checked_at": datetime(2026, 9, 16, 10, 5, tzinfo=UTC),
                "trades": {
                    "condition-1": [
                        {
                            "id": "t-yes",
                            "condition_id": "condition-1",
                            "token_id": "yes-token",
                            "taker_order_id": "outcome-order",
                            "side": "BUY",
                            "trader_side": "TAKER",
                            "price": Decimal("0.45"),
                            "size": Decimal("60"),
                            "status": "CONFIRMED",
                            "matched_at": datetime(2026, 9, 16, 2, 5, tzinfo=UTC),
                            "maker_orders": [],
                        },
                        {
                            "id": "t-unmapped",
                            "condition_id": "condition-1",
                            "token_id": "unmapped-token",
                            "taker_order_id": "unknown-outcome-order",
                            "side": "BUY",
                            "trader_side": "TAKER",
                            "price": Decimal("0.50"),
                            "size": Decimal("5"),
                            "status": "CONFIRMED",
                            "matched_at": datetime(2026, 9, 16, 2, 6, tzinfo=UTC),
                            "maker_orders": [],
                        },
                    ],
                },
            }

    class FakeLP:
        def candidate_snapshot(self) -> dict[str, object]:
            return {
                "state": "known",
                "complete": True,
                "candidates": [],
                "recommendations": [],
                "market_rewards": {
                    "condition-1": {
                        "state": "known",
                        "reward_date": "2026-09-16",
                        "condition_id": "condition-1",
                        "market_amount": Decimal("0.20"),
                    }
                },
            }

        def status(self, _session_id: str | None = None) -> dict[str, object]:
            return {"state": "none"}

    trading = FakeTrading()
    service = PredictionExecutionService(
        store=PredictionArbitrageStore(tmp_path / "data"),
        monitor=object(),
        trading=trading,
        notifier=NullNotifier(),
        lock_path=tmp_path / "execution.lock",
        lp=FakeLP(),
    )

    first = service.refresh_lp_dashboard_snapshot()
    assert first["lp_orders_today"] == []
    assert trading.trades_requested.wait(timeout=5)
    dashboard = first
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        dashboard = service.refresh_lp_dashboard_snapshot()
        filled_ids = {
            str(row["order_id"])
            for row in dashboard["lp_orders_today"]
            if row["state"] == "filled"
        }
        if "outcome-order" in filled_ids:
            break
        time.sleep(0.05)
    rows = {
        str(row["order_id"]): row for row in dashboard["lp_orders_today"]
    }
    # R6: token_id 命中 metadata outcomes → 真实 outcome（前端副标题渲染为
    # 「YES · 买入 · 已成交」而非 UNKNOWN）。
    assert rows["outcome-order"]["outcome"] == "YES"
    assert rows["outcome-order"]["market_title"] == "Outcome LP market"
    # R6: 解不出的 token 保持空 outcome（前端回落 UNKNOWN 路径）。
    assert rows["unknown-outcome-order"]["outcome"] is None


def test_lp_dashboard_today_orders_keep_scoring_orders_with_unknown_market(
    tmp_path: Path,
) -> None:
    """AC1: LIVE 挂单官方计分中、市场信号未知 → 进当天 LP 委托，数量取挂单数据。"""

    class FakeTrading:
        def __init__(self) -> None:
            self.config = SimpleNamespace(
                wallet_address="0x" + "4" * 40,
                signer_address="0x" + "5" * 40,
                predict=None,
            )

        def lp_account_snapshot(self) -> dict[str, object]:
            return {
                "authenticated": True,
                "checked_at": datetime.now(UTC),
                "open_orders": [
                    {
                        "order_id": "scoring-order",
                        "condition_id": "condition-scoring",
                        "token_id": "scoring-token",
                        "outcome": "YES",
                        "side": "BUY",
                        "status": "LIVE",
                        "price": Decimal("0.50"),
                        "original_size": Decimal("80"),
                        "size_matched": Decimal("40"),
                        "remaining_size": Decimal("40"),
                        "market_title": "Scoring LP market",
                        "market_url": "https://polymarket.com/event/scoring",
                    }
                ],
                "positions": [],
                "open_orders_complete": True,
                "positions_complete": True,
            }

        def get_order_scoring(self, _order_id: str) -> bool:
            return True

        def lp_reward_snapshot(
            self, reward_date: str, condition_id: str
        ) -> dict[str, object]:
            return {
                "state": "unknown",
                "reward_date": reward_date,
                "condition_id": condition_id,
            }

    class FakeLP:
        def candidate_snapshot(self) -> dict[str, object]:
            return {
                "state": "known",
                "complete": False,
                "candidates": [],
                "recommendations": [],
                "market_rewards": {},
            }

        def status(self, _session_id: str | None = None) -> dict[str, object]:
            return {"state": "none"}

    service = PredictionExecutionService(
        store=PredictionArbitrageStore(tmp_path / "data"),
        monitor=object(),
        trading=FakeTrading(),
        notifier=NullNotifier(),
        lock_path=tmp_path / "execution.lock",
        lp=FakeLP(),
    )

    dashboard = service.refresh_lp_dashboard_snapshot()

    # AC1: 计分信号命中即归属 LP，市场观察与奖励记录均未知也不排除。
    assert [str(row["order_id"]) for row in dashboard["lp_orders_today"]] == [
        "scoring-order"
    ]
    row = dashboard["lp_orders_today"][0]
    assert row["state"] == "open"
    assert row["quantity"] == Decimal("80")
    assert row["filled_quantity"] == Decimal("40")
    assert row["remaining_quantity"] == Decimal("40")
    assert row["price"] == Decimal("0.50")
    assert row["scoring_status"] == "true"
    assert row["scoring_last_success_at"] == row["scoring_checked_at"]
    assert row["side"] == "BUY"
    assert row["market_title"] == "Scoring LP market"
    assert dashboard["non_lp_row_count"] == 0


def test_lp_dashboard_payload_keeps_orders_and_positions_intact(
    tmp_path: Path,
) -> None:
    """AC6: orders/positions 载荷逐字段保持不变（风控与推荐排除依赖完整清单）。"""

    class FakeTrading:
        def __init__(self) -> None:
            self.config = SimpleNamespace(
                wallet_address="0x" + "4" * 40,
                signer_address="0x" + "5" * 40,
                predict=None,
            )

        def lp_account_snapshot(self) -> dict[str, object]:
            return {
                "authenticated": True,
                "checked_at": datetime.now(UTC),
                "open_orders": [
                    {
                        "order_id": "manual-order",
                        "condition_id": "condition-1",
                        "token_id": "yes-token",
                        "outcome": "YES",
                        "side": "BUY",
                        "status": "LIVE",
                        "price": Decimal("0.50"),
                        "original_size": Decimal("80"),
                        "size_matched": Decimal("40"),
                        "remaining_size": Decimal("40"),
                        "reward_min_size": Decimal("40"),
                        "reward_max_spread": Decimal("0.03"),
                        "fees_enabled": False,
                        "fee_exponent": Decimal("1"),
                        "taker_fee_rate": Decimal("0"),
                        "market_id": "market-1",
                        "market_title": "Intact LP market",
                        "market_url": "https://polymarket.com/event/intact",
                    }
                ],
                "positions": [
                    {
                        "condition_id": "condition-1",
                        "token_id": "yes-token",
                        "outcome": "YES",
                        "size": Decimal("20"),
                        "average_price": Decimal("0.50"),
                        "current_value": Decimal("10.4"),
                        "reward_min_size": Decimal("40"),
                        "reward_max_spread": Decimal("0.03"),
                        "fees_enabled": False,
                        "fee_exponent": Decimal("1"),
                        "taker_fee_rate": Decimal("0"),
                        "market_id": "market-1",
                        "market_title": "Intact LP market",
                        "market_url": "https://polymarket.com/event/intact",
                    }
                ],
                "open_orders_complete": True,
                "positions_complete": True,
            }

        def get_order_scoring(self, _order_id: str) -> bool:
            return False

        def lp_reward_snapshot(
            self, reward_date: str, condition_id: str
        ) -> dict[str, object]:
            return {
                "state": "unknown",
                "reward_date": reward_date,
                "condition_id": condition_id,
            }

    class FakeLP:
        def candidate_snapshot(self) -> dict[str, object]:
            return {
                "state": "known",
                "complete": False,
                "candidates": [],
                "recommendations": [],
                "market_rewards": {},
            }

        def status(self, _session_id: str | None = None) -> dict[str, object]:
            return {"state": "none"}

    service = PredictionExecutionService(
        store=PredictionArbitrageStore(tmp_path / "data"),
        monitor=object(),
        trading=FakeTrading(),
        notifier=NullNotifier(),
        lock_path=tmp_path / "execution.lock",
        lp=FakeLP(),
    )

    dashboard = service.refresh_lp_dashboard_snapshot()

    order = dict(dashboard["orders"][0])
    scoring_checked_at = order.pop("scoring_checked_at")
    assert isinstance(scoring_checked_at, str) and scoring_checked_at
    assert order.pop("scoring_last_success_at") == scoring_checked_at
    assert order == {
        "order_id": "manual-order",
        "market_id": "market-1",
        "condition_id": "condition-1",
        "market_title": "Intact LP market",
        "market_url": "https://polymarket.com/event/intact",
        "token_id": "yes-token",
        "outcome": "YES",
        "side": "BUY",
        "status": "LIVE",
        "price": Decimal("0.50"),
        "quantity": Decimal("80"),
        "filled_quantity": Decimal("40"),
        "remaining_quantity": Decimal("40"),
        "minimum_order_size": None,
        "reward_min_size": Decimal("40"),
        "min_scoring_size": None,
        "purpose": None,
        "reward_max_spread": Decimal("0.03"),
        "fees_enabled": False,
        "fee_exponent": Decimal("1"),
        "taker_fee_rate": Decimal("0"),
        "management": "manual_read_only",
        "read_only": True,
        "scoring_status": "false",
    }
    assert dashboard["positions"] == [
        {
            "market_id": "market-1",
            "condition_id": "condition-1",
            "market_title": "Intact LP market",
            "market_url": "https://polymarket.com/event/intact",
            "token_id": "yes-token",
            "outcome": "YES",
            "size": Decimal("20"),
            "average_price": Decimal("0.50"),
            "current_value": Decimal("10.4"),
            "reward_min_size": Decimal("40"),
            "reward_max_spread": Decimal("0.03"),
            "fees_enabled": False,
            "fee_exponent": Decimal("1"),
            "taker_fee_rate": Decimal("0"),
            "management": "manual_read_only",
            "read_only": True,
        }
    ]


def test_lp_dashboard_orders_carry_purpose_and_min_scoring_size(tmp_path: Path) -> None:
    """A6: BUY 原始数量 = 最小计分数量 → trial；大于 → formal；SELL 或规则缺失 → null。"""

    def order(order_id: str, condition_id: str, side: str, original: str, **rules: object) -> dict[str, object]:
        row: dict[str, object] = {
            "order_id": order_id,
            "condition_id": condition_id,
            "token_id": f"token-{order_id}",
            "outcome": "YES",
            "side": side,
            "status": "LIVE",
            "price": Decimal("0.50"),
            "original_size": Decimal(original),
            "size_matched": Decimal("0"),
            "remaining_size": Decimal(original),
            "market_title": f"Market {order_id}",
            "market_url": f"https://polymarket.com/event/{order_id}",
        }
        row.update(rules)
        return row

    class FakeTrading:
        def __init__(self) -> None:
            self.config = SimpleNamespace(
                wallet_address="0x" + "4" * 40,
                signer_address="0x" + "5" * 40,
                predict=None,
            )

        def lp_account_snapshot(self) -> dict[str, object]:
            return {
                "authenticated": True,
                "checked_at": datetime.now(UTC),
                "open_orders": [
                    order(
                        "trial-order",
                        "condition-trial",
                        "BUY",
                        "20",
                        minimum_order_size=Decimal("5"),
                        reward_min_size=Decimal("20"),
                    ),
                    order(
                        "formal-order",
                        "condition-formal",
                        "BUY",
                        "50",
                        minimum_order_size=Decimal("5"),
                        reward_min_size=Decimal("20"),
                    ),
                    order(
                        "sell-order",
                        "condition-sell",
                        "SELL",
                        "20",
                        minimum_order_size=Decimal("5"),
                        reward_min_size=Decimal("20"),
                    ),
                    order("rules-missing-order", "condition-missing", "BUY", "20"),
                ],
                "positions": [],
                "open_orders_complete": True,
                "positions_complete": True,
            }

        def get_order_scoring(self, _order_id: str) -> object:
            return True

        def lp_reward_snapshot(
            self, reward_date: str, condition_id: str
        ) -> dict[str, object]:
            return {
                "state": "unknown",
                "reward_date": reward_date,
                "condition_id": condition_id,
            }

    class FakeLP:
        def candidate_snapshot(self) -> dict[str, object]:
            return {
                "state": "known",
                "complete": False,
                "candidates": [],
                "recommendations": [],
                "market_rewards": {},
            }

        def status(self, _session_id: str | None = None) -> dict[str, object]:
            return {"state": "none"}

    store = PredictionArbitrageStore(tmp_path / "data")
    service = PredictionExecutionService(
        store=store,
        monitor=object(),
        trading=FakeTrading(),
        notifier=NullNotifier(),
        lock_path=tmp_path / "execution.lock",
        lp=FakeLP(),
    )

    dashboard = service.refresh_lp_dashboard_snapshot()

    orders = {
        str(row["order_id"]): row
        for row in dashboard["orders"]
    }
    assert orders["trial-order"]["purpose"] == "trial"
    assert orders["trial-order"]["min_scoring_size"] == Decimal("20")
    assert orders["formal-order"]["purpose"] == "formal"
    assert orders["formal-order"]["min_scoring_size"] == Decimal("20")
    assert orders["sell-order"]["purpose"] is None
    assert orders["sell-order"]["min_scoring_size"] == Decimal("20")
    assert orders["rules-missing-order"]["purpose"] is None
    assert orders["rules-missing-order"]["min_scoring_size"] is None

    today = {
        str(row["order_id"]): row
        for row in dashboard["lp_orders_today"]
    }
    assert today["trial-order"]["purpose"] == "trial"
    assert today["formal-order"]["purpose"] == "formal"
    assert today["sell-order"]["purpose"] is None
    assert today["rules-missing-order"]["purpose"] is None
    assert today["rules-missing-order"]["min_scoring_size"] is None
