from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from open_trader.prediction_arbitrage_store import PredictionArbitrageStore


UTC = timezone.utc


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def store(tmp_path: Path) -> PredictionArbitrageStore:
    return PredictionArbitrageStore(tmp_path / "data")


def signal_payload(market_id: str, started_at: str, *, event_id: str = "event-1") -> dict[str, object]:
    return {
        "market_id": market_id,
        "event_id": event_id,
        "question": "Will the event happen?",
        "started_at": started_at,
        "net_edge": Decimal("0.07"),
        "estimated_profit": Decimal("1.40"),
    }


def preview_payload(*, market_id: str = "market-1") -> dict[str, object]:
    return {
        "event_id": "event-1",
        "market_id": market_id,
        "quantity": Decimal("20"),
        "yes_max_price": Decimal("0.45"),
        "no_max_price": Decimal("0.48"),
        "total_max_cost": Decimal("18.60"),
    }


def cross_preview_payload(
    *,
    market_id: str = "cross-market-1",
    total_max_cost: Decimal = Decimal("20.00"),
    net_quantity: Decimal = Decimal("5"),
) -> dict[str, object]:
    canonical_cutoff = "2026-09-03T00:00:00Z"
    resolution_at = "2026-09-03T12:00:00Z"
    predict_book_timestamp = "2026-08-03T00:00:00Z"
    polymarket_book_timestamp = "2026-08-03T00:00:01Z"
    return {
        "execution_id": f"execution:{market_id}",
        "opportunity_id": f"cross:{market_id}:PREDICT_YES_POLYMARKET_NO",
        "event_id": "cross-event-1",
        "market_id": market_id,
        "market_type": "cross_venue_yes_no",
        "signal_episode_id": f"signal:{market_id}",
        "pair_id": market_id,
        "direction": "PREDICT_YES_POLYMARKET_NO",
        "quantity": net_quantity,
        "total_max_cost": total_max_cost,
        "minimum_payout": net_quantity,
        "minimum_profit": Decimal("0.50"),
        "annualized_yield": Decimal("0.16"),
        "canonical_cutoff": canonical_cutoff,
        "rules_fingerprints": {
            "predict.fun": "predict-fingerprint",
            "polymarket": "poly-fingerprint",
        },
        "approved_candidates": {
            "predict.fun": {
                "market_id": "predict-market",
                "condition_id": "predict-condition",
                "yes_token_id": "predict-yes",
                "no_token_id": "predict-no",
                "rules_fingerprint": "predict-fingerprint",
            },
            "polymarket": {
                "market_id": "poly-market",
                "condition_id": "poly-condition",
                "yes_token_id": "poly-yes",
                "no_token_id": "poly-no",
                "rules_fingerprint": "poly-fingerprint",
            },
        },
        "codex_approval": {
            "decision": "APPROVE",
            "cache_key": "cross-cache",
            "direct_outcome_mapping": {
                "predict_yes": "YES",
                "predict_no": "NO",
                "polymarket_yes": "YES",
                "polymarket_no": "NO",
            },
            "evidence": [
                {"exchange": "predict.fun", "quote": "same rules"},
                {"exchange": "polymarket", "quote": "same rules"},
            ],
        },
        "intent": {
            "intent_type": "cross_venue",
            "pair_id": market_id,
            "direction": "PREDICT_YES_POLYMARKET_NO",
            "quantity": net_quantity,
            "calculable_gas": Decimal("0.10"),
            "total_max_cost": total_max_cost,
            "maximum_fee": Decimal("0.15"),
            "minimum_payout": net_quantity,
            "minimum_profit": Decimal("0.50"),
            "annualized_yield": Decimal("0.16"),
            "canonical_cutoff": canonical_cutoff,
            "resolution_at": resolution_at,
            "actionable": True,
            "quote_available": True,
            "legs": [
                {
                    "exchange": "predict.fun",
                    "market_id": "predict-market",
                    "condition_id": "predict-condition",
                    "outcome": "YES",
                    "token_id": "predict-yes",
                    "settlement_asset": "USDT",
                    "requested_quantity": net_quantity,
                    "net_quantity": net_quantity,
                    "max_price": Decimal("0.45"),
                    "max_cost": Decimal("2.25"),
                    "maximum_fee": Decimal("0.05"),
                    "fee_asset": "USDT",
                    "book_timestamp": predict_book_timestamp,
                    "settlement_at": resolution_at,
                    "minimum_order_size": Decimal("1"),
                },
                {
                    "exchange": "polymarket",
                    "market_id": "poly-market",
                    "condition_id": "poly-condition",
                    "outcome": "NO",
                    "token_id": "poly-no",
                    "settlement_asset": "USDC",
                    "requested_quantity": net_quantity,
                    "net_quantity": net_quantity,
                    "max_price": Decimal("0.45"),
                    "max_cost": Decimal("2.25"),
                    "maximum_fee": Decimal("0.05"),
                    "fee_asset": "USDC",
                    "book_timestamp": polymarket_book_timestamp,
                    "settlement_at": resolution_at,
                    "minimum_order_size": Decimal("1"),
                },
            ],
        },
    }


def create_execution(
    tmp_path: Path,
    *,
    expires_at: str | None = None,
    idempotency_key: str = "request-1",
) -> tuple[PredictionArbitrageStore, dict[str, object]]:
    current = datetime.now(UTC)
    db = store(tmp_path)
    preview_id = db.create_preview(
        preview_payload(), expires_at=expires_at or iso(current + timedelta(seconds=10))
    )
    return db, db.consume_preview_and_create_execution(preview_id, idempotency_key)


def test_store_uses_expected_sqlite_path_and_safety_pragmas(tmp_path: Path) -> None:
    db = store(tmp_path)
    db.write_runtime({"heartbeat": "ok"})

    path = tmp_path / "data" / "prediction_arbitrage" / "prediction_arbitrage.sqlite3"
    assert path.is_file()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] > 0
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 9
        names = {
            row[1]
            for row in connection.execute("PRAGMA table_list")
                if not row[1].startswith("sqlite_")
        }
        indexes = {
            row[1]
            for row in connection.execute("PRAGMA index_list('signals')")
        }
        open_query_plan = connection.execute(
            "EXPLAIN QUERY PLAN "
            "SELECT * FROM signals WHERE ended_at IS NULL "
            "ORDER BY started_at DESC, signal_id DESC"
        ).fetchall()
        history_query_plan = connection.execute(
            "EXPLAIN QUERY PLAN "
            "SELECT * FROM signals ORDER BY started_at DESC, signal_id DESC"
        ).fetchall()
        query_plan = connection.execute(
            "EXPLAIN QUERY PLAN "
            "SELECT payload FROM signals WHERE market_id=? ORDER BY started_at DESC",
            ("market-1",),
        ).fetchall()
    assert names == {
        "runtime",
        "signals",
        "previews",
        "executions",
        "execution_legs",
        "incidents",
        "llm_cache",
        "llm_usage",
        "llm_provider_selection",
        "relation_state",
        "relation_scan_runs",
        "cross_execution_reservations",
        "validation_mode",
        "auto_eat_attempts",
        "cross_auto_state",
        "cross_auto_attempts",
        "safety_policy",
        "control_events",
        "schema_metadata",
        "n_leg_controls",
        "n_leg_lineage_claims",
        "n_leg_batches",
        "n_leg_transitions",
        "n_leg_qualification_policy",
        "n_leg_safety_config",
        "n_leg_execution_scopes",
    }
    assert "signals_market_started_at" in indexes
    assert "signals_started_at" in indexes
    assert "signals_open_started_at" in indexes
    assert any("signals_market_started_at" in row[3] for row in query_plan)
    assert any("signals_started_at" in row[3] for row in history_query_plan)
    assert any("signals_open_started_at" in row[3] for row in open_query_plan)


def test_first_safety_policy_enrollment_preserves_legacy_automatic_modes(
    tmp_path: Path,
) -> None:
    db = store(tmp_path)
    db.set_validation_mode("auto")
    db.set_cross_auto_mode("auto_submit", "operator_configured")
    db.arm_cross_auto()

    result = db.apply_safety_policy(
        {"policy_version": "v1", "max_normal_cost": "20"},
        git_sha="abc123",
    )

    assert result == {
        "state": "baseline_enrolled",
        "fingerprint": "bf39dc3e71ec56386ee2bc1fe90daa498fb243949b9679c9972ad4bb210e4e9c",
        "previous_fingerprint": None,
        "downgraded": False,
    }
    assert db.get_validation_mode() == "auto"
    assert db.cross_auto_state()["configured_mode"] == "auto_submit"
    assert db.cross_auto_state()["armed"] is True
    saved = db.safety_policy()
    assert saved is not None
    updated_at = saved.pop("updated_at")
    assert isinstance(updated_at, str) and updated_at.endswith("Z")
    assert saved == {
        "fingerprint": "bf39dc3e71ec56386ee2bc1fe90daa498fb243949b9679c9972ad4bb210e4e9c",
        "policy": {"policy_version": "v1", "max_normal_cost": "20"},
        "git_sha": "abc123",
    }
    event = db.latest_control_event("safety_policy", "production")
    assert event is not None
    assert event["outcome"] == "baseline_enrolled"
    assert event["payload"] == {
        "actor": "system",
        "after_fingerprint": "bf39dc3e71ec56386ee2bc1fe90daa498fb243949b9679c9972ad4bb210e4e9c",
        "before_fingerprint": None,
        "downgraded": False,
        "git_sha": "abc123",
    }


def test_identical_safety_policy_restart_is_a_no_op(tmp_path: Path) -> None:
    db = store(tmp_path)
    policy = {"policy_version": "v1", "max_normal_cost": "20"}
    db.apply_safety_policy(policy, git_sha="abc123")

    result = db.apply_safety_policy(policy, git_sha="def456")

    assert result == {
        "state": "unchanged",
        "fingerprint": "bf39dc3e71ec56386ee2bc1fe90daa498fb243949b9679c9972ad4bb210e4e9c",
        "previous_fingerprint": "bf39dc3e71ec56386ee2bc1fe90daa498fb243949b9679c9972ad4bb210e4e9c",
        "downgraded": False,
    }
    assert db.latest_control_event("safety_policy", "production")["outcome"] == (
        "baseline_enrolled"
    )


def test_changed_safety_policy_atomically_downgrades_automatic_modes(
    tmp_path: Path,
) -> None:
    db = store(tmp_path)
    db.set_validation_mode("auto")
    db.set_cross_auto_mode("auto_submit", "operator_configured")
    db.arm_cross_auto()
    db.apply_safety_policy(
        {"policy_version": "v1", "max_normal_cost": "20"},
        git_sha="abc123",
    )

    result = db.apply_safety_policy(
        {"policy_version": "v2", "max_normal_cost": "25"},
        git_sha="def456",
    )

    assert result == {
        "state": "downgraded",
        "fingerprint": "a90c3a8bc1439f0035ba56aed23cd4fed379f982e28d0eacba4a8cea6e9158e3",
        "previous_fingerprint": "bf39dc3e71ec56386ee2bc1fe90daa498fb243949b9679c9972ad4bb210e4e9c",
        "downgraded": True,
    }
    assert db.get_validation_mode() == "manual"
    assert db.cross_auto_state()["configured_mode"] == "manual_confirm"
    assert db.cross_auto_state()["armed"] is False
    event = db.latest_control_event("safety_policy", "production")
    assert event is not None
    assert event["outcome"] == "safety_policy_changed"
    assert event["payload"] == {
        "actor": "system",
        "after_fingerprint": "a90c3a8bc1439f0035ba56aed23cd4fed379f982e28d0eacba4a8cea6e9158e3",
        "before_fingerprint": "bf39dc3e71ec56386ee2bc1fe90daa498fb243949b9679c9972ad4bb210e4e9c",
        "downgraded": True,
        "git_sha": "def456",
    }


def test_llm_provider_selection_is_audited_and_fails_closed(
    tmp_path: Path,
) -> None:
    db = store(tmp_path)
    audit = {"actor": "local_operator", "git_sha": "abc123"}

    assert db.get_llm_provider() == "deepseek"
    assert db.set_llm_provider("zhipu", audit=audit) == "zhipu"
    assert db.get_llm_provider() == "zhipu"
    assert db.get_llm_provider(default="codex") == "zhipu"

    assert db.set_llm_provider("zhipu", audit=audit) == "zhipu"
    event = db.latest_control_event("set_llm_provider", "llm_provider_selection")
    assert event is not None
    assert event["outcome"] == "no_op"
    assert event["payload"] == {**audit, "before": "zhipu", "after": "zhipu"}

    with pytest.raises(ValueError, match="invalid llm provider"):
        db.set_llm_provider("unknown")
    assert db.get_llm_provider() == "zhipu"


def test_set_llm_provider_resolves_empty_table_against_caller_default(
    tmp_path: Path,
) -> None:
    db = store(tmp_path)
    audit = {"actor": "local_operator", "git_sha": "abc123"}

    # Empty table + caller default codex: selecting another provider (zhipu)
    # must durably write the row instead of collapsing into a no-op while
    # codex stays in effect.
    assert db.set_llm_provider("zhipu", default="codex", audit=audit) == "zhipu"
    assert db.get_llm_provider(default="codex") == "zhipu"
    event = db.latest_control_event("set_llm_provider", "llm_provider_selection")
    assert event is not None
    assert event["outcome"] == "succeeded"
    assert event["payload"] == {**audit, "before": "codex", "after": "zhipu"}

    # Empty table + matching default: a real no-op that writes no row, so a
    # later read with a different default still resolves to that default.
    fresh = PredictionArbitrageStore(tmp_path / "fresh")
    assert fresh.set_llm_provider("codex", default="codex", audit=audit) == "codex"
    assert fresh.get_llm_provider(default="zhipu") == "zhipu"
    no_op_event = fresh.latest_control_event(
        "set_llm_provider", "llm_provider_selection"
    )
    assert no_op_event is not None
    assert no_op_event["outcome"] == "no_op"

    # An unrecognized default falls back to the shipped default (deepseek),
    # keeping the before-resolution aligned with get_llm_provider.
    assert fresh.set_llm_provider("zhipu", default="bogus") == "zhipu"
    assert fresh.get_llm_provider(default="codex") == "zhipu"


def test_audited_validation_mode_and_pause_are_naturally_idempotent(
    tmp_path: Path,
) -> None:
    db = store(tmp_path)
    audit = {
        "actor": "local_operator",
        "git_sha": "abc123",
        "safety_fingerprint": "policy-1",
    }

    assert db.set_validation_mode("manual", audit=audit) == "manual"
    assert db.set_validation_mode("manual", audit=audit) == "manual"
    mode_event = db.latest_control_event("set_validation_mode", "validation_mode")
    assert mode_event is not None
    assert mode_event["outcome"] == "no_op"
    assert mode_event["payload"] == {
        **audit,
        "after": "manual",
        "before": "manual",
    }

    db.set_cross_auto_mode("auto_submit", "operator_configured")
    db.arm_cross_auto()
    first_pause = db.pause_cross_auto("operator_paused", audit=audit)
    second_pause = db.pause_cross_auto("operator_paused", audit=audit)
    assert second_pause == first_pause
    pause_event = db.latest_control_event("pause_cross_auto", "cross_auto")
    assert pause_event is not None
    assert pause_event["outcome"] == "no_op"
    assert pause_event["payload"]["before"] == pause_event["payload"]["after"]


def test_audit_failure_rolls_back_validation_mode_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = store(tmp_path)

    def fail_audit(*_args: object, **_kwargs: object) -> str:
        raise sqlite3.OperationalError("audit unavailable")

    monkeypatch.setattr(db, "_insert_control_event", fail_audit)

    with pytest.raises(sqlite3.OperationalError, match="audit unavailable"):
        db.set_validation_mode("manual", audit={"actor": "local_operator"})

    assert db.get_validation_mode() == "observe_only"
    assert db.latest_control_event("set_validation_mode", "validation_mode") is None


def test_control_event_transitions_from_started_to_one_terminal_outcome(
    tmp_path: Path,
) -> None:
    db = store(tmp_path)
    event_id = db.begin_control_event(
        action="cleanup_predict_allowance",
        target="predict_allowance",
        payload={"actor": "local_operator", "confirm": True},
    )

    started = db.latest_control_event(
        "cleanup_predict_allowance", "predict_allowance"
    )
    assert started is not None
    assert started["event_id"] == event_id
    assert started["outcome"] == "started"
    finished = db.finish_control_event(
        event_id,
        outcome="succeeded",
        payload={"before_allowance": "1", "after_allowance": "0"},
    )
    assert finished["outcome"] == "succeeded"
    assert finished["payload"] == {
        "actor": "local_operator",
        "confirm": True,
        "before_allowance": "1",
        "after_allowance": "0",
    }
    with pytest.raises(ValueError, match="already terminal"):
        db.finish_control_event(
            event_id,
            outcome="failed",
            payload={"reason": "late rewrite"},
        )


def test_cross_auto_pause_is_durable_and_runtime_writes_do_not_clear_it(
    tmp_path: Path,
) -> None:
    db = store(tmp_path)

    assert db.cross_auto_state() == {
        "configured_mode": "observe_only",
        "armed": False,
        "reason": "not_armed",
        "updated_at": None,
    }
    assert db.arm_cross_auto()["armed"] is True
    db.write_runtime({"heartbeat": "ok"})
    paused = db.pause_cross_auto("operator_paused")

    assert paused["armed"] is False
    assert paused["reason"] == "operator_paused"
    restored = PredictionArbitrageStore(tmp_path / "data")
    assert restored.cross_auto_state() == paused


def test_cross_auto_state_defaults_fail_closed_with_configured_mode(tmp_path: Path) -> None:
    assert store(tmp_path).cross_auto_state() == {
        "configured_mode": "observe_only",
        "armed": False,
        "reason": "not_armed",
        "updated_at": None,
    }


@pytest.mark.parametrize(
    "read_error",
    (sqlite3.DatabaseError("database disk image is malformed"), OSError("unreadable")),
)
def test_cross_auto_state_fails_closed_when_database_read_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    read_error: Exception,
) -> None:
    db = store(tmp_path)

    def fail_read() -> object:
        raise read_error

    monkeypatch.setattr(db, "_read_connection", fail_read)

    assert db.cross_auto_state() == {
        "configured_mode": "observe_only",
        "armed": False,
        "reason": "not_armed",
        "updated_at": None,
    }


def test_cross_auto_mode_and_pause_survive_new_store_instance(tmp_path: Path) -> None:
    db = store(tmp_path)

    assert db.set_cross_auto_mode("auto_submit", "operator_configured")["armed"] is False
    armed = db.arm_cross_auto()
    assert armed["configured_mode"] == "auto_submit"
    assert armed["armed"] is True
    paused = db.pause_cross_auto("operator_paused")
    assert paused["configured_mode"] == "auto_submit"
    assert paused["armed"] is False
    assert store(tmp_path).cross_auto_state() == paused


@pytest.mark.parametrize("configured_mode", ("observe_only", "manual_confirm"))
def test_cross_auto_state_fails_closed_for_armed_nonautomatic_modes(
    tmp_path: Path, configured_mode: str
) -> None:
    db = store(tmp_path)
    db.set_cross_auto_mode(configured_mode, "operator_configured")
    with sqlite3.connect(db.path) as connection:
        connection.execute(
            "UPDATE cross_auto_state SET configured_mode=?, armed=1",
            (configured_mode,),
        )

    assert db.cross_auto_state() == {
        "configured_mode": "observe_only",
        "armed": False,
        "reason": "not_armed",
        "updated_at": None,
    }


def test_old_armed_row_migrates_to_observe_only_and_unarmed(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    database_dir = data_dir / "prediction_arbitrage"
    database_dir.mkdir(parents=True)
    connection = sqlite3.connect(database_dir / "prediction_arbitrage.sqlite3")
    connection.executescript(
        """
        PRAGMA user_version=4;
        CREATE TABLE cross_auto_state(
            singleton INTEGER PRIMARY KEY CHECK (singleton=1),
            armed INTEGER NOT NULL CHECK (armed IN (0,1)),
            reason TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        INSERT INTO cross_auto_state VALUES (1, 1, 'armed', '2026-08-09T00:00:00Z');
        """
    )
    connection.commit()
    connection.close()

    migrated = PredictionArbitrageStore(data_dir).cross_auto_state()
    assert migrated["configured_mode"] == "observe_only"
    assert migrated["armed"] is False


def test_explicit_nonautomatic_mode_disarms(tmp_path: Path) -> None:
    db = store(tmp_path)

    db.arm_cross_auto()
    state = db.set_cross_auto_mode("manual_confirm", "operator_manual")
    assert state["configured_mode"] == "manual_confirm"
    assert state["armed"] is False


def test_cross_auto_attempt_claim_is_one_shot_across_store_instances(tmp_path: Path) -> None:
    setup = store(tmp_path)
    setup.arm_cross_auto()
    stores = [
        PredictionArbitrageStore(tmp_path / "data"),
        PredictionArbitrageStore(tmp_path / "data"),
    ]

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda db: db.claim_cross_auto_attempt("signal-1", "opportunity-1"),
                stores,
            )
        )

    assert sorted(result["state"] for result in results) == [
        "claimed",
        "signal_already_attempted",
    ]
    assert len(setup.cross_auto_attempts()) == 1


@pytest.mark.parametrize("configured_mode", ("manual_confirm", "observe_only"))
def test_cross_auto_claim_distinguishes_nonautomatic_mode_from_paused_auto(
    tmp_path: Path, configured_mode: str
) -> None:
    db = store(tmp_path)
    db.set_cross_auto_mode(configured_mode, "operator_configured")

    assert db.claim_cross_auto_attempt("signal-mode", "opportunity-mode") == {
        "state": "rejected",
        "reason": "configured_mode_not_auto_submit",
        "current": configured_mode,
    }

    db.set_cross_auto_mode("auto_submit", "operator_configured")
    assert db.claim_cross_auto_attempt("signal-paused", "opportunity-paused") == {
        "state": "rejected",
        "reason": "cross_auto_paused",
    }


def test_cross_auto_attempt_finishes_once_with_safe_rejection_facts(tmp_path: Path) -> None:
    db = store(tmp_path)
    db.arm_cross_auto()
    assert db.claim_cross_auto_attempt("signal-1", "opportunity-1") == {
        "state": "claimed"
    }

    finished = db.finish_cross_auto_attempt(
        "signal-1",
        decision="rejected",
        reason="cross_auto_paused",
        reason_zh="自动下单已暂停",
        current=Decimal("1"),
        limit=Decimal("0"),
        venue="cross_venue",
        operator_action_required=True,
        operator_action="prediction-arb cross-auto arm --data-dir /tmp/data",
        preview_id="preview-1",
        execution_id="execution-1",
        total_cost=Decimal("5"),
    )

    assert finished["reason_code"] == "cross_auto_paused"
    assert finished["current"] == "1"
    assert finished["total_cost"] == "5"
    with pytest.raises(KeyError, match="signal-1"):
        db.finish_cross_auto_attempt(
            "signal-1",
            decision="rejected",
            reason="cross_auto_paused",
            reason_zh="自动下单已暂停",
        )
    with sqlite3.connect(db.path) as connection:
        payload = json.loads(
            connection.execute(
                "SELECT payload FROM cross_auto_attempts WHERE signal_id='signal-1'"
            ).fetchone()[0]
        )
    assert set(payload) == {
        "reason_code",
        "reason_zh",
        "current",
        "limit",
        "venue",
        "operator_action_required",
        "operator_action",
        "signal_id",
        "opportunity_id",
    }


def test_store_sets_wal_once_instead_of_on_every_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_trader import prediction_arbitrage_store

    statements: list[str] = []
    real_connect = sqlite3.connect

    class TrackingConnection(sqlite3.Connection):
        def execute(self, sql: str, parameters=(), /):  # type: ignore[no-untyped-def]
            statements.append(sql)
            return super().execute(sql, parameters)

    def connect(*args, **kwargs):  # type: ignore[no-untyped-def]
        return real_connect(*args, **kwargs, factory=TrackingConnection)

    monkeypatch.setattr(prediction_arbitrage_store.sqlite3, "connect", connect)
    db = PredictionArbitrageStore(tmp_path / "data")
    assert sum("PRAGMA journal_mode=WAL" in sql for sql in statements) == 1

    statements.clear()
    assert db.load_llm_cache("missing") is None
    assert all("PRAGMA journal_mode=WAL" not in sql for sql in statements)


def test_runtime_round_trips_canonical_json_and_survives_restart(tmp_path: Path) -> None:
    payload = {"z": Decimal("1.20"), "a": {"amount": Decimal("2.00")}}
    store(tmp_path).write_runtime(payload)

    assert PredictionArbitrageStore(tmp_path / "data").load_runtime() == {
        "a": {"amount": "2.00"},
        "z": "1.20",
    }


def test_one_open_signal_per_market_and_close_creates_new_episode(tmp_path: Path) -> None:
    db = store(tmp_path)
    started = iso(datetime.now(UTC))
    first_id = db.upsert_signal(signal_payload("market-1", started))
    assert db.upsert_signal({**signal_payload("market-1", started), "net_edge": "0.08"}) == first_id
    assert len(db.signal_history("all")) == 1

    db.close_signal("market-1", ended_at=iso(datetime.now(UTC)), reason="book_stale")
    second_id = db.upsert_signal(signal_payload("market-1", iso(datetime.now(UTC))))
    assert second_id != first_id
    assert len(db.signal_history("all")) == 2
    assert db.signal_history("all")[0]["signal_id"] == second_id
    assert db.signal_history("all")[1]["ended_at"] is not None


def test_open_signal_history_excludes_closed_episodes(tmp_path: Path) -> None:
    db = store(tmp_path)
    first_id = db.upsert_signal(signal_payload("market-1", iso(datetime.now(UTC))))
    db.close_signal("market-1", ended_at=iso(datetime.now(UTC)), reason="book_stale")
    db.upsert_signal(signal_payload("market-2", iso(datetime.now(UTC))))

    assert [row["signal_id"] for row in db.open_signal_history()] != [first_id]
    assert [row["market_id"] for row in db.open_signal_history()] == ["market-2"]


def test_relation_state_and_scan_tables_survive_restart(tmp_path: Path) -> None:
    db = store(tmp_path)
    db.save_relation_state(
        {"relations": [{"relation_id": "r-1", "token_id": "public-token"}]},
        full_scanned_at="2026-07-31T00:00:00Z",
    )
    db.record_relation_scan(
        scope="full",
        status="completed",
        started_at="2026-07-31T00:00:00Z",
        completed_at="2026-07-31T00:00:02Z",
        payload={"relations_discovered": 1},
    )
    restarted = PredictionArbitrageStore(tmp_path / "data")
    assert restarted.load_relation_state()["relations"][0]["relation_id"] == "r-1"
    assert restarted.load_relation_state()["relations"][0]["token_id"] == "public-token"
    assert restarted.relation_scan_history(limit=1)[0]["scope"] == "full"


def test_activity_retention_does_not_delete_full_or_event_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 7, 31, 12, tzinfo=UTC)
    monkeypatch.setattr(
        "open_trader.prediction_arbitrage_store._utc_now", lambda: iso(now)
    )
    db = store(tmp_path)
    for scope in ("full", "event", "activity"):
        db.record_relation_scan(
            scope=scope,
            status="completed",
            event_id="e-1" if scope == "event" else None,
            started_at=iso(now - timedelta(days=8)),
            completed_at=iso(now - timedelta(days=8)),
            payload={"scope": scope},
        )
    db.record_relation_scan(
        scope="activity",
        status="completed",
        started_at=iso(now),
        completed_at=iso(now),
        payload={"scope": "new"},
    )
    assert [row["scope"] for row in db.relation_scan_history(limit=10)] == [
        "activity", "event", "full"
    ]


def test_open_signal_keeps_first_observation_and_initial_profit(tmp_path: Path) -> None:
    db = store(tmp_path)
    signal_id = db.upsert_signal({
        **signal_payload("relation-1", "2026-07-31T00:00:00Z"),
        "first_positive_at": "2026-07-31T00:00:00Z",
        "initial_profit": Decimal("0.10"),
        "peak_profit": Decimal("0.10"),
    })
    db.upsert_signal({
        **signal_payload("relation-1", "2026-07-31T00:00:01Z"),
        "first_positive_at": "2026-07-31T00:00:01Z",
        "initial_profit": Decimal("0.05"),
        "peak_profit": Decimal("0.20"),
    })
    row = db.signal(signal_id)
    assert row["started_at"] == "2026-07-31T00:00:00.000000Z"
    assert row["first_positive_at"] == "2026-07-31T00:00:00.000000Z"
    assert row["initial_profit"] == "0.10"
    assert row["peak_profit"] == "0.20"


def test_signal_persists_action_identity_and_live_fields(tmp_path: Path) -> None:
    db = store(tmp_path)
    started = "2026-07-31T00:00:00Z"
    signal_id = db.upsert_signal(
        {
            **signal_payload("market-1", started),
            "opportunity_id": "event-1:market-1",
            "first_positive_at": started,
            "initial_profit": Decimal("0.11"),
            "yes_max_price": Decimal("0.42"),
            "no_max_price": Decimal("0.47"),
            "yes_max_cost": Decimal("8.40"),
            "no_max_cost": Decimal("9.40"),
            "total_max_cost": Decimal("17.80"),
        }
    )
    db.upsert_signal(
        {
            **signal_payload("market-1", "2026-07-31T00:00:01Z"),
            "opportunity_id": "event-1:market-1",
            "first_positive_at": "2026-07-31T00:00:01Z",
            "initial_profit": Decimal("0.05"),
            "estimated_profit": Decimal("0.22"),
            "yes_max_price": Decimal("0.43"),
            "no_max_price": Decimal("0.46"),
            "yes_max_cost": Decimal("8.60"),
            "no_max_cost": Decimal("9.20"),
            "total_max_cost": Decimal("17.80"),
        }
    )

    row = db.signal(signal_id)
    assert row["opportunity_id"] == "event-1:market-1"
    assert row["yes_max_price"] == "0.43"
    assert row["no_max_price"] == "0.46"
    assert row["yes_max_cost"] == "8.60"
    assert row["no_max_cost"] == "9.20"
    assert row["total_max_cost"] == "17.80"
    assert row["estimated_profit"] == "0.22"
    assert row["started_at"] == "2026-07-31T00:00:00.000000Z"
    assert row["first_positive_at"] == "2026-07-31T00:00:00.000000Z"
    assert row["initial_profit"] == "0.11"


def test_signal_notification_sent_since_only_counts_successful_same_market(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_trader.prediction_arbitrage_store as store_module

    now = [datetime(2026, 7, 31, 12, tzinfo=UTC)]
    monkeypatch.setattr(store_module, "_utc_now", lambda: iso(now[0]))
    db = store(tmp_path)
    same_market = db.upsert_signal(signal_payload("market-1", iso(now[0])))
    failed = db.reserve_notification_attempt(same_market, lease_seconds=0)
    assert failed["state"] == "reserved"
    db.complete_notification_attempt(
        same_market, str(failed["lease_id"]), success=False
    )
    assert not db.notification_sent_since("market-1", now[0] - timedelta(minutes=1))

    now[0] += timedelta(minutes=5)
    delivered = db.reserve_notification_attempt(same_market, lease_seconds=0)
    assert delivered["state"] == "reserved"
    db.complete_notification_attempt(
        same_market, str(delivered["lease_id"]), success=True
    )
    assert db.notification_sent_since("market-1", now[0] - timedelta(minutes=1))
    assert not db.notification_sent_since("market-1", now[0] + timedelta(microseconds=1))

    other_market = db.upsert_signal(signal_payload("market-2", iso(now[0])))
    other = db.reserve_notification_attempt(other_market, lease_seconds=0)
    assert other["state"] == "reserved"
    db.complete_notification_attempt(
        other_market, str(other["lease_id"]), success=True
    )
    assert db.notification_sent_since("market-1", now[0] - timedelta(minutes=1))


def test_close_signal_persists_final_episode_values(tmp_path: Path) -> None:
    db = store(tmp_path)
    signal_id = db.upsert_signal(
        signal_payload("relation-1", "2026-07-31T00:00:00Z")
    )
    db.close_signal(
        "relation-1",
        ended_at="2026-07-31T00:00:00.250Z",
        reason="profit_non_positive",
        updates={
            "observed_duration_ms": 250,
            "final_profit": Decimal("-0.01"),
        },
    )
    assert db.signal(signal_id)["observed_duration_ms"] == 250


def test_notification_reservation_is_atomic_across_store_instances(tmp_path: Path) -> None:
    first = store(tmp_path)
    signal_id = first.upsert_signal(signal_payload("relation-1", iso(datetime.now(UTC))))
    stores = [PredictionArbitrageStore(tmp_path / "data"), PredictionArbitrageStore(tmp_path / "data")]

    def reserve(index: int) -> dict[str, object]:
        return stores[index].reserve_notification_attempt(signal_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, (0, 1)))

    assert [item["state"] for item in results].count("reserved") == 1
    assert [item["state"] for item in results].count("in_flight") == 1
    assert first.signal(signal_id)["notification_attempts"] == 1
    assert first.signal(signal_id)["notification_state"] == "pending"


def test_stale_notification_lease_is_reclaimed_after_restart(tmp_path: Path) -> None:
    first = store(tmp_path)
    signal_id = first.upsert_signal(signal_payload("relation-1", iso(datetime.now(UTC))))
    reserved = first.reserve_notification_attempt(signal_id, lease_seconds=0)
    assert reserved["state"] == "reserved"

    restarted = PredictionArbitrageStore(tmp_path / "data")
    reclaimed = restarted.reserve_notification_attempt(signal_id, lease_seconds=30)

    assert reclaimed["state"] == "reserved"
    assert reclaimed["lease_id"] != reserved["lease_id"]
    assert restarted.signal(signal_id)["notification_attempts"] == 2


def test_notification_attempts_stop_at_three_and_close_blocks_reservation(
    tmp_path: Path,
) -> None:
    db = store(tmp_path)
    signal_id = db.upsert_signal(signal_payload("relation-1", iso(datetime.now(UTC))))
    for _ in range(3):
        reserved = db.reserve_notification_attempt(signal_id, lease_seconds=0)
        assert reserved["state"] == "reserved"
        assert db.complete_notification_attempt(
            signal_id, str(reserved["lease_id"]), success=False
        )["state"] == "failed"
    assert db.reserve_notification_attempt(signal_id)["state"] == "exhausted"

    db.close_signal("relation-1", ended_at=iso(datetime.now(UTC)), reason="stale")
    assert db.reserve_notification_attempt(signal_id)["state"] == "closed"


def test_observation_notification_is_independent_and_completes_after_close(
    tmp_path: Path,
) -> None:
    db = store(tmp_path)
    signal_id = db.upsert_signal(signal_payload("relation-1", iso(datetime.now(UTC))))
    reserved = db.reserve_notification_attempt(
        signal_id, kind="observation", lease_seconds=0
    )
    assert reserved["state"] == "reserved"
    db.close_signal(
        "relation-1", ended_at=iso(datetime.now(UTC)), reason="data_unavailable"
    )
    completed = db.complete_notification_attempt(
        signal_id,
        str(reserved["lease_id"]),
        kind="observation",
        success=True,
    )
    assert completed["state"] == "sent"
    signal = db.signal(signal_id)
    assert signal is not None
    assert signal["observation_state"] == "sent"
    assert signal.get("notification_state", "pending") == "pending"
    assert db.reserve_notification_attempt(signal_id, kind="observation")["state"] == "closed"
    assert db.reserve_notification_attempt(signal_id)["state"] == "closed"


def test_preview_expires_after_ten_seconds_and_is_idempotent(tmp_path: Path) -> None:
    db = store(tmp_path)
    now = datetime.now(UTC)
    preview_id = db.create_preview(
        preview_payload(), expires_at=iso(now + timedelta(seconds=10))
    )
    execution = db.consume_preview_and_create_execution(preview_id, "request-1")
    assert execution["preview_id"] == preview_id
    assert db.consume_preview_and_create_execution(preview_id, "request-2") == execution

    expired_id = db.create_preview(preview_payload(market_id="market-2"), expires_at=iso(now - timedelta(microseconds=1)))
    with pytest.raises(ValueError, match="expired"):
        db.consume_preview_and_create_execution(expired_id, "request-3")


def test_duplicate_idempotency_key_returns_existing_execution(tmp_path: Path) -> None:
    db = store(tmp_path)
    now = datetime.now(UTC)
    preview_id = db.create_preview(preview_payload(), expires_at=iso(now + timedelta(seconds=10)))
    first = db.consume_preview_and_create_execution(preview_id, "same-request")
    second = db.consume_preview_and_create_execution("not-used", "same-request")
    assert second == first


def test_only_one_nonterminal_execution_is_allowed(tmp_path: Path) -> None:
    db = store(tmp_path)
    now = datetime.now(UTC)
    first_preview = db.create_preview(preview_payload(), expires_at=iso(now + timedelta(seconds=10)))
    db.consume_preview_and_create_execution(first_preview, "request-1")
    second_preview = db.create_preview(preview_payload(market_id="market-2"), expires_at=iso(now + timedelta(seconds=10)))
    with pytest.raises((ValueError, sqlite3.IntegrityError), match="active|execution|unique|non-terminal"):
        db.consume_preview_and_create_execution(second_preview, "request-2")

    execution = db.active_execution()
    assert execution is not None
    db.transition_execution(execution["execution_id"], state="complete", evidence={"step": "done"})
    assert db.active_execution() is None
    assert db.consume_preview_and_create_execution(second_preview, "request-2")["state"] == "validating"


def test_concurrent_instances_consume_preview_once(tmp_path: Path) -> None:
    setup = store(tmp_path)
    preview_id = setup.create_preview(
        preview_payload(), expires_at=iso(datetime.now(UTC) + timedelta(seconds=10))
    )
    stores = [PredictionArbitrageStore(tmp_path / "data"), PredictionArbitrageStore(tmp_path / "data")]

    def consume(index: int) -> object:
        try:
            return stores[index].consume_preview_and_create_execution(preview_id, f"request-{index}")
        except Exception as exc:  # noqa: BLE001 - the loser is intentionally observed.
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(consume, (0, 1)))
    assert all(isinstance(item, dict) for item in results)
    assert results[0] == results[1]
    assert setup.active_execution() is not None


def test_cross_preview_commits_one_execution_and_reservation_idempotently(
    tmp_path: Path,
) -> None:
    db = store(tmp_path)
    now = datetime.now(UTC)
    preview_id = db.create_preview(
        cross_preview_payload(total_max_cost=Decimal("20.00")),
        expires_at=iso(now + timedelta(seconds=10)),
    )

    first = db.consume_preview_and_create_execution(preview_id, "cross-request-1")
    assert first["preview_id"] == preview_id
    assert db.cross_unsettled_principal() == Decimal("20.00")
    assert db.consume_preview_and_create_execution(preview_id, "cross-request-2") == first

    duplicate_preview = db.create_preview(
        cross_preview_payload(market_id="cross-market-2"),
        expires_at=iso(now + timedelta(seconds=10)),
    )
    assert (
        db.consume_preview_and_create_execution(duplicate_preview, "cross-request-1")
        == first
    )
    with sqlite3.connect(db.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 1
        assert connection.execute(
            "SELECT amount, state FROM cross_execution_reservations"
        ).fetchone() == ("20.00", "reserved")


def test_cross_preview_no_ttl_keeps_legacy_expiry_and_consumes_valid_cross_payload(
    tmp_path: Path,
) -> None:
    db = store(tmp_path)
    now = datetime.now(UTC)
    legacy_id = db.create_preview(
        preview_payload(),
        expires_at=iso(now - timedelta(seconds=1)),
    )
    cross_id = db.create_preview(
        cross_preview_payload(total_max_cost=Decimal("10.50")),
        expires_at=iso(now - timedelta(hours=1)),
    )

    with pytest.raises(ValueError, match="preview_expired"):
        db.consume_preview_and_create_execution(legacy_id, "legacy")

    execution = db.consume_preview_and_create_execution(cross_id, "cross")

    assert execution["state"] == "validating"
    assert db.consume_preview_and_create_execution(cross_id, "cross-again") == execution


def test_cross_preview_no_ttl_rejects_invalid_cross_payload_before_reserving(
    tmp_path: Path,
) -> None:
    db = store(tmp_path)
    payload = cross_preview_payload(total_max_cost=Decimal("10.50"))
    payload["signal_episode_id"] = ""
    preview_id = db.create_preview(
        payload,
        expires_at=iso(datetime.now(UTC) + timedelta(hours=1)),
    )

    with pytest.raises(ValueError, match="cross_preview_invalid"):
        db.consume_preview_and_create_execution(preview_id, "cross-invalid")

    assert db.cross_unsettled_principal() == Decimal("0")
    with sqlite3.connect(db.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM cross_execution_reservations"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize(
    ("label", "mutate"),
    [
        ("opportunity_id", lambda payload: payload.pop("opportunity_id")),
        ("execution_id", lambda payload: payload.pop("execution_id")),
        ("signal_episode_id", lambda payload: payload.pop("signal_episode_id")),
        ("pair_id", lambda payload: payload.pop("pair_id")),
        ("direction", lambda payload: payload.pop("direction")),
        ("canonical_cutoff", lambda payload: payload.pop("canonical_cutoff")),
        ("rules_fingerprints", lambda payload: payload.pop("rules_fingerprints")),
        ("approved_candidates", lambda payload: payload.pop("approved_candidates")),
        ("codex_approval", lambda payload: payload.pop("codex_approval")),
        ("minimum_payout", lambda payload: payload.pop("minimum_payout")),
        ("minimum_profit", lambda payload: payload.pop("minimum_profit")),
        ("annualized_yield", lambda payload: payload.pop("annualized_yield")),
        ("intent_quantity", lambda payload: payload["intent"].pop("quantity")),
        (
            "intent_calculable_gas",
            lambda payload: payload["intent"].pop("calculable_gas"),
        ),
        ("intent_maximum_fee", lambda payload: payload["intent"].pop("maximum_fee")),
        (
            "intent_resolution_at",
            lambda payload: payload["intent"].pop("resolution_at"),
        ),
        ("intent_actionable", lambda payload: payload["intent"].pop("actionable")),
        (
            "intent_quote_available",
            lambda payload: payload["intent"].pop("quote_available"),
        ),
        (
            "intent_legs",
            lambda payload: payload["intent"].__setitem__("legs", payload["intent"]["legs"][:1]),
        ),
        (
            "intent_predict_market_id",
            lambda payload: payload["intent"]["legs"][0].pop("market_id"),
        ),
        (
            "intent_predict_requested_quantity",
            lambda payload: payload["intent"]["legs"][0].pop("requested_quantity"),
        ),
        (
            "intent_poly_max_cost",
            lambda payload: payload["intent"]["legs"][1].pop("max_cost"),
        ),
    ],
)
def test_expired_incomplete_cross_preview_keeps_legacy_ttl_and_never_reserves(
    tmp_path: Path,
    label: str,
    mutate,
) -> None:
    db = store(tmp_path)
    payload = cross_preview_payload(total_max_cost=Decimal("10.50"))
    mutate(payload)
    preview_id = db.create_preview(
        payload,
        expires_at=iso(datetime.now(UTC) - timedelta(hours=1)),
    )

    with pytest.raises(ValueError, match="preview_expired"):
        db.consume_preview_and_create_execution(preview_id, f"cross-expired-{label}")

    assert db.cross_unsettled_principal() == Decimal("0")
    with sqlite3.connect(db.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM cross_execution_reservations"
            ).fetchone()[0]
            == 0
        )


def test_cross_principal_cap_is_atomic_across_store_instances(tmp_path: Path) -> None:
    setup = store(tmp_path)
    now = datetime.now(UTC)
    seed_preview = setup.create_preview(
        cross_preview_payload(total_max_cost=Decimal("80.00")),
        expires_at=iso(now + timedelta(seconds=10)),
    )
    seed = setup.consume_preview_and_create_execution(seed_preview, "cross-seed")
    setup.transition_execution(
        str(seed["execution_id"]),
        state="holding_to_resolution",
        evidence={"positions": "held"},
    )
    previews = [
        setup.create_preview(
            cross_preview_payload(market_id=f"cross-market-{index}"),
            expires_at=iso(now + timedelta(seconds=10)),
        )
        for index in (2, 3)
    ]
    stores = [
        PredictionArbitrageStore(tmp_path / "data"),
        PredictionArbitrageStore(tmp_path / "data"),
    ]

    def consume(index: int) -> object:
        try:
            return stores[index].consume_preview_and_create_execution(
                previews[index], f"cross-race-{index}"
            )
        except Exception as exc:  # noqa: BLE001 - both cap rejections are observed.
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(consume, (0, 1)))

    assert sum(isinstance(item, dict) for item in results) == 1
    assert sum(isinstance(item, ValueError) for item in results) == 1
    assert next(str(item) for item in results if isinstance(item, ValueError)) == "cross_unsettled_cap"
    assert setup.cross_unsettled_principal() == Decimal("100.00")
    with sqlite3.connect(setup.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM cross_execution_reservations"
        ).fetchone()[0] == 2


def test_auto_cross_preview_requires_durable_arm_before_consuming(tmp_path: Path) -> None:
    db = store(tmp_path)
    payload = cross_preview_payload(total_max_cost=Decimal("5"))
    payload["auto_submit"] = True
    preview_id = db.create_preview(
        payload, expires_at=iso(datetime.now(UTC) + timedelta(seconds=10))
    )

    assert db.consume_preview_and_create_execution(preview_id, "auto-unarmed") == {
        "state": "rejected",
        "reason": "configured_mode_not_auto_submit",
        "current": "observe_only",
    }

    db.set_cross_auto_mode("auto_submit", "operator_configured")
    assert db.consume_preview_and_create_execution(preview_id, "auto-paused") == {
        "state": "rejected",
        "reason": "cross_auto_paused",
    }
    db.arm_cross_auto()
    assert db.consume_preview_and_create_execution(preview_id, "auto-armed")["state"] == "validating"


def test_auto_cross_daily_and_pair_gates_preserve_originating_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_trader import prediction_arbitrage_store

    db = store(tmp_path)
    db.arm_cross_auto()
    shanghai_noon = datetime(2026, 8, 8, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr(prediction_arbitrage_store, "_utc_now", lambda: iso(shanghai_noon))

    first_payload = cross_preview_payload(
        market_id="daily-first", total_max_cost=Decimal("100")
    )
    first_payload["auto_submit"] = True
    first_preview = db.create_preview(first_payload, expires_at=iso(shanghai_noon))
    first = db.consume_preview_and_create_execution(first_preview, "daily-first")
    db.transition_execution(
        str(first["execution_id"]), state="holding_to_resolution", evidence={"held": True}
    )
    assert db.cross_auto_daily_principal(now=shanghai_noon) == Decimal("100")

    next_payload = cross_preview_payload(
        market_id="daily-next", total_max_cost=Decimal("1")
    )
    next_payload["auto_submit"] = True
    next_preview = db.create_preview(next_payload, expires_at=iso(shanghai_noon))
    assert db.consume_preview_and_create_execution(next_preview, "daily-next") == {
        "state": "rejected",
        "reason": "cross_auto_daily_principal_cap",
    }

    assert db.cross_auto_daily_principal(
        now=datetime(2026, 8, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    ) == Decimal("0")

    pair_payload = cross_preview_payload(
        market_id="pair-1", total_max_cost=Decimal("1")
    )
    pair_payload["auto_submit"] = True
    pair_preview = db.create_preview(pair_payload, expires_at=iso(shanghai_noon))
    assert db.consume_preview_and_create_execution(pair_preview, "pair-daily-blocked") == {
        "state": "rejected",
        "reason": "cross_auto_daily_principal_cap",
    }


def test_auto_cross_pair_gate_and_no_submit_releases_daily_charge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_trader import prediction_arbitrage_store

    db = store(tmp_path)
    db.arm_cross_auto()
    shanghai_noon = datetime(2026, 8, 8, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr(prediction_arbitrage_store, "_utc_now", lambda: iso(shanghai_noon))

    payload = cross_preview_payload(market_id="pair-1", total_max_cost=Decimal("5"))
    payload["auto_submit"] = True
    first_preview = db.create_preview(payload, expires_at=iso(shanghai_noon))
    first = db.consume_preview_and_create_execution(first_preview, "pair-first")
    db.transition_execution(
        str(first["execution_id"]), state="holding_to_resolution", evidence={"held": True}
    )

    opposite = cross_preview_payload(market_id="pair-1", total_max_cost=Decimal("5"))
    opposite["auto_submit"] = True
    opposite["execution_id"] = "execution:pair-1-opposite"
    opposite["opportunity_id"] = "cross:pair-1:PREDICT_NO_POLYMARKET_YES"
    opposite["direction"] = "PREDICT_NO_POLYMARKET_YES"
    opposite["intent"]["direction"] = "PREDICT_NO_POLYMARKET_YES"
    opposite["intent"]["legs"][0]["outcome"] = "NO"
    opposite["intent"]["legs"][1]["outcome"] = "YES"
    opposite_preview = db.create_preview(opposite, expires_at=iso(shanghai_noon))
    assert db.consume_preview_and_create_execution(opposite_preview, "pair-opposite") == {
        "state": "rejected",
        "reason": "cross_pair_unsettled",
    }

    no_submit_payload = cross_preview_payload(
        market_id="no-submit", total_max_cost=Decimal("5")
    )
    no_submit_payload["auto_submit"] = True
    no_submit_preview = db.create_preview(no_submit_payload, expires_at=iso(shanghai_noon))
    no_submit = db.consume_preview_and_create_execution(no_submit_preview, "no-submit")
    db.transition_execution(
        str(no_submit["execution_id"]),
        state="both_rejected",
        evidence={
            "submitted": False,
            "positions": {"predict.fun": "0", "polymarket": "0"},
        },
    )
    db.release_cross_reservation(str(no_submit["execution_id"]), reason="no_submit")
    assert db.cross_auto_daily_principal(now=shanghai_noon) == Decimal("5")

    rejected_payload = cross_preview_payload(
        market_id="both-rejected", total_max_cost=Decimal("5")
    )
    rejected_payload["auto_submit"] = True
    rejected_preview = db.create_preview(rejected_payload, expires_at=iso(shanghai_noon))
    rejected = db.consume_preview_and_create_execution(rejected_preview, "both-rejected")
    db.transition_execution(
        str(rejected["execution_id"]),
        state="both_rejected",
        evidence={
            "positions": {"predict.fun": "0", "polymarket": "0"},
            "no_position_observed": True,
        },
    )
    db.release_cross_reservation(str(rejected["execution_id"]), reason="both_rejected")
    assert db.cross_auto_daily_principal(now=shanghai_noon) == Decimal("10")

    redeemed_payload = cross_preview_payload(
        market_id="redeemed", total_max_cost=Decimal("5")
    )
    redeemed_payload["auto_submit"] = True
    redeemed_preview = db.create_preview(redeemed_payload, expires_at=iso(shanghai_noon))
    redeemed = db.consume_preview_and_create_execution(redeemed_preview, "redeemed")
    db.transition_execution(
        str(redeemed["execution_id"]),
        state="holding_to_resolution",
        evidence={
            "phase": "holding_to_resolution",
            "positions": {"predict.fun": "5", "polymarket": "5"},
            "settlement_baseline": {"predict.fun": "90", "polymarket": "90"},
        },
    )
    db.transition_execution(
        str(redeemed["execution_id"]),
        state="complete",
        evidence={
            "positions": {"predict.fun": "0", "polymarket": "0"},
            "settlement_baseline": {"predict.fun": "90", "polymarket": "90"},
            "redemption": {
                "observed": True,
                "winner": {
                    "venue": "predict.fun",
                    "condition_id": "predict-condition",
                    "outcome": "YES",
                    "token_id": "predict-yes",
                    "quantity": "5",
                },
                "redeemed_collateral": {"predict.fun": "5"},
            },
        },
    )
    db.release_cross_reservation(str(redeemed["execution_id"]), reason="redeemed")
    assert db.cross_auto_daily_principal(now=shanghai_noon) == Decimal("15")


@pytest.mark.parametrize("existing_auto_submit", (False, True))
def test_cross_pair_gate_rejects_manual_preview_while_pair_is_unsettled(
    tmp_path: Path, existing_auto_submit: bool
) -> None:
    db = store(tmp_path)
    if existing_auto_submit:
        db.arm_cross_auto()
    first_payload = cross_preview_payload(market_id="manual-pair", total_max_cost=Decimal("5"))
    if existing_auto_submit:
        first_payload["auto_submit"] = True
    first_preview = db.create_preview(
        first_payload, expires_at=iso(datetime.now(UTC) + timedelta(seconds=10))
    )
    first = db.consume_preview_and_create_execution(first_preview, "manual-pair-first")
    db.transition_execution(
        str(first["execution_id"]), state="holding_to_resolution", evidence={"held": True}
    )

    manual_payload = cross_preview_payload(
        market_id="manual-pair", total_max_cost=Decimal("5")
    )
    manual_payload["execution_id"] = "execution:manual-pair-opposite"
    manual_payload["opportunity_id"] = "cross:manual-pair:PREDICT_NO_POLYMARKET_YES"
    manual_payload["direction"] = "PREDICT_NO_POLYMARKET_YES"
    manual_payload["intent"]["direction"] = "PREDICT_NO_POLYMARKET_YES"
    manual_payload["intent"]["legs"][0]["outcome"] = "NO"
    manual_payload["intent"]["legs"][1]["outcome"] = "YES"
    manual_preview = db.create_preview(
        manual_payload, expires_at=iso(datetime.now(UTC) + timedelta(seconds=10))
    )

    with pytest.raises(ValueError, match="cross_pair_unsettled"):
        db.consume_preview_and_create_execution(manual_preview, "manual-pair-opposite")


def test_cross_reservation_remains_until_an_allowed_final_release(tmp_path: Path) -> None:
    db = store(tmp_path)
    preview_id = db.create_preview(
        cross_preview_payload(total_max_cost=Decimal("10.50")),
        expires_at=iso(datetime.now(UTC) + timedelta(seconds=10)),
    )
    execution = db.consume_preview_and_create_execution(preview_id, "cross-final")
    execution_id = str(execution["execution_id"])

    for state in (
        "holding_to_resolution",
        "unknown_order",
        "directional_incident",
        "dust",
    ):
        db.transition_execution(execution_id, state=state, evidence={"state": state})
        assert db.cross_unsettled_principal() == Decimal("10.50")
    with pytest.raises(ValueError, match="release reason"):
        db.release_cross_reservation(execution_id, reason="holding_to_resolution")
    assert db.cross_unsettled_principal() == Decimal("10.50")

    db.transition_execution(
        execution_id,
        state="both_rejected",
        evidence={
            "positions": {"predict.fun": "0", "polymarket": "0"},
            "no_position_observed": True,
        },
    )
    db.release_cross_reservation(execution_id, reason="both_rejected")
    db.release_cross_reservation(execution_id, reason="both_rejected")
    assert db.cross_unsettled_principal() == Decimal("0")


def test_cross_reservation_rejects_unproven_release_reason(tmp_path: Path) -> None:
    db = store(tmp_path)
    preview_id = db.create_preview(
        cross_preview_payload(total_max_cost=Decimal("10.50")),
        expires_at=iso(datetime.now(UTC) + timedelta(seconds=10)),
    )
    execution_id = str(
        db.consume_preview_and_create_execution(preview_id, "cross-unproven")["execution_id"]
    )

    with pytest.raises(ValueError, match="proof"):
        db.release_cross_reservation(execution_id, reason="redeemed")
    assert db.cross_unsettled_principal() == Decimal("10.50")

    db.transition_execution(
        execution_id,
        state="both_rejected",
        evidence={"positions": "proven_zero", "redemption": "observed"},
    )
    with pytest.raises(ValueError, match="proof"):
        db.release_cross_reservation(execution_id, reason="both_rejected")
    assert db.cross_unsettled_principal() == Decimal("10.50")


def test_cross_reservation_releases_only_proven_no_submit_or_redemption(
    tmp_path: Path,
) -> None:
    db = store(tmp_path)
    now = datetime.now(UTC)
    no_submit_preview = db.create_preview(
        cross_preview_payload(total_max_cost=Decimal("10.50")),
        expires_at=iso(now + timedelta(seconds=10)),
    )
    no_submit_execution = db.consume_preview_and_create_execution(
        no_submit_preview, "cross-no-submit"
    )
    db.transition_execution(
        str(no_submit_execution["execution_id"]),
        state="both_rejected",
        evidence={
            "submitted": False,
            "positions": {"predict.fun": "0", "polymarket": "0"},
        },
    )
    db.release_cross_reservation(str(no_submit_execution["execution_id"]), reason="no_submit")
    assert db.cross_unsettled_principal() == Decimal("0")

    redeemed_preview = db.create_preview(
        cross_preview_payload(market_id="cross-redeemed", total_max_cost=Decimal("10.50")),
        expires_at=iso(now + timedelta(seconds=10)),
    )
    redeemed_execution = db.consume_preview_and_create_execution(
        redeemed_preview, "cross-redeemed"
    )
    db.transition_execution(
        str(redeemed_execution["execution_id"]),
        state="holding_to_resolution",
        evidence={
            "phase": "holding_to_resolution",
            "positions": {"predict.fun": "5", "polymarket": "5"},
            "settlement_baseline": {"predict.fun": "90", "polymarket": "90"},
        },
    )
    db.transition_execution(
        str(redeemed_execution["execution_id"]),
        state="complete",
        evidence={
            "positions": {"predict.fun": "0", "polymarket": "0"},
            "settlement_baseline": {"predict.fun": "90", "polymarket": "90"},
            "redemption": {
                "observed": True,
                "winner": {
                    "venue": "predict.fun",
                    "condition_id": "predict-condition",
                    "outcome": "YES",
                    "token_id": "predict-yes",
                    "quantity": "5",
                },
                "redeemed_collateral": {"predict.fun": "10.50"},
            },
        },
    )
    db.release_cross_reservation(str(redeemed_execution["execution_id"]), reason="redeemed")
    assert db.cross_unsettled_principal() == Decimal("0")


def test_cross_redemption_release_requires_the_observed_winning_venue_delta(
    tmp_path: Path,
) -> None:
    db = store(tmp_path)
    preview_id = db.create_preview(
        cross_preview_payload(total_max_cost=Decimal("10.50")),
        expires_at=iso(datetime.now(UTC) + timedelta(seconds=10)),
    )
    execution_id = str(
        db.consume_preview_and_create_execution(preview_id, "cross-winner-proof")["execution_id"]
    )
    db.transition_execution(
        execution_id,
        state="complete",
        evidence={
            "positions": {"predict.fun": "0", "polymarket": "0"},
            "redemption": {
                "observed": True,
                "winner": {
                    "venue": "predict.fun",
                    "condition_id": "predict-condition",
                    "outcome": "YES",
                    "token_id": "predict-yes",
                    "quantity": "5",
                },
                "redeemed_collateral": {"predict.fun": "0", "polymarket": "5"},
            },
        },
    )

    with pytest.raises(ValueError, match="proof"):
        db.release_cross_reservation(execution_id, reason="redeemed")

    assert db.cross_unsettled_principal() == Decimal("10.50")


def test_cross_release_sweep_recovers_a_proven_complete_after_a_crash(
    tmp_path: Path,
) -> None:
    db = store(tmp_path)
    preview_id = db.create_preview(
        cross_preview_payload(total_max_cost=Decimal("10.50")),
        expires_at=iso(datetime.now(UTC) + timedelta(seconds=10)),
    )
    execution_id = str(
        db.consume_preview_and_create_execution(preview_id, "cross-crash-retry")["execution_id"]
    )
    db.transition_execution(
        execution_id,
        state="holding_to_resolution",
        evidence={
            "phase": "holding_to_resolution",
            "positions": {"predict.fun": "5", "polymarket": "5"},
            "settlement_baseline": {"predict.fun": "90", "polymarket": "90"},
        },
    )
    db.transition_execution(
        execution_id,
        state="complete",
        evidence={
            "positions": {"predict.fun": "0", "polymarket": "0"},
            "settlement_baseline": {"predict.fun": "90", "polymarket": "90"},
            "redemption": {
                "observed": True,
                "winner": {
                    "venue": "predict.fun",
                    "condition_id": "predict-condition",
                    "outcome": "YES",
                    "token_id": "predict-yes",
                    "quantity": "5",
                },
                "redeemed_collateral": {"predict.fun": "5", "polymarket": "0"},
            },
        },
    )

    sweep = getattr(db, "release_proven_cross_completions", None)

    assert callable(sweep)
    assert sweep() == (execution_id,)
    assert sweep() == ()
    assert db.cross_unsettled_principal() == Decimal("0")


def test_cross_release_sweep_requires_exact_persisted_winner_net_quantity(
    tmp_path: Path,
) -> None:
    db = store(tmp_path)
    payload = cross_preview_payload(
        total_max_cost=Decimal("10.50"), net_quantity=Decimal("10")
    )
    preview_id = db.create_preview(
        payload, expires_at=iso(datetime.now(UTC) + timedelta(seconds=10))
    )
    execution_id = str(
        db.consume_preview_and_create_execution(preview_id, "cross-partial-sweep")["execution_id"]
    )
    db.transition_execution(
        execution_id,
        state="holding_to_resolution",
        evidence={
            "phase": "holding_to_resolution",
            "positions": {"predict.fun": "10", "polymarket": "10"},
            "settlement_baseline": {"predict.fun": "90", "polymarket": "90"},
        },
    )
    db.transition_execution(
        execution_id,
        state="complete",
        evidence={
            "positions": {"predict.fun": "0", "polymarket": "0"},
            "settlement_baseline": {"predict.fun": "90", "polymarket": "90"},
            "redemption": {
                "observed": True,
                "winner": {
                    "venue": "predict.fun",
                    "condition_id": "predict-condition",
                    "outcome": "YES",
                    "token_id": "predict-yes",
                    "quantity": "5",
                },
                "redeemed_collateral": {"predict.fun": "5", "polymarket": "0"},
            },
        },
    )

    assert db.release_proven_cross_completions() == ()
    assert db.cross_unsettled_principal() == Decimal("10.50")


def test_cross_release_sweep_recovers_exact_ten_unit_settlement_after_a_crash(
    tmp_path: Path,
) -> None:
    db = store(tmp_path)
    payload = cross_preview_payload(
        total_max_cost=Decimal("10.50"), net_quantity=Decimal("10")
    )
    preview_id = db.create_preview(
        payload, expires_at=iso(datetime.now(UTC) + timedelta(seconds=10))
    )
    execution_id = str(
        db.consume_preview_and_create_execution(preview_id, "cross-exact-ten-sweep")["execution_id"]
    )
    db.transition_execution(
        execution_id,
        state="holding_to_resolution",
        evidence={
            "phase": "holding_to_resolution",
            "positions": {"predict.fun": "10", "polymarket": "10"},
            "settlement_baseline": {"predict.fun": "90", "polymarket": "90"},
        },
    )
    db.transition_execution(
        execution_id,
        state="complete",
        evidence={
            "positions": {"predict.fun": "0", "polymarket": "0"},
            "settlement_baseline": {"predict.fun": "90", "polymarket": "90"},
            "redemption": {
                "observed": True,
                "winner": {
                    "venue": "predict.fun",
                    "condition_id": "predict-condition",
                    "outcome": "YES",
                    "token_id": "predict-yes",
                    "quantity": "10",
                },
                "redeemed_collateral": {"predict.fun": "10", "polymarket": "0"},
            },
        },
    )

    assert db.release_proven_cross_completions() == (execution_id,)
    assert db.cross_unsettled_principal() == Decimal("0")


def test_cross_release_sweep_requires_the_persisted_post_fill_baseline(
    tmp_path: Path,
) -> None:
    db = store(tmp_path)
    preview_id = db.create_preview(
        cross_preview_payload(total_max_cost=Decimal("10.50")),
        expires_at=iso(datetime.now(UTC) + timedelta(seconds=10)),
    )
    execution_id = str(
        db.consume_preview_and_create_execution(preview_id, "cross-preorder-baseline")
        ["execution_id"]
    )
    db.transition_execution(
        execution_id,
        state="complete",
        evidence={
            "positions": {"predict.fun": "0", "polymarket": "0"},
            "redemption": {
                "observed": True,
                "winner": {
                    "venue": "predict.fun",
                    "condition_id": "predict-condition",
                    "outcome": "YES",
                    "token_id": "predict-yes",
                    "quantity": "5",
                },
                "redeemed_collateral": {"predict.fun": "5", "polymarket": "0"},
            },
        },
    )

    assert db.release_proven_cross_completions() == ()
    assert db.cross_unsettled_principal() == Decimal("10.50")


def test_legacy_preview_execution_payload_has_no_cross_reservation(tmp_path: Path) -> None:
    db = store(tmp_path)
    preview_id = db.create_preview(
        preview_payload(), expires_at=iso(datetime.now(UTC) + timedelta(seconds=10))
    )

    execution = db.consume_preview_and_create_execution(preview_id, "legacy-request")

    assert execution == {
        "event_id": "event-1",
        "market_id": "market-1",
        "quantity": "20",
        "yes_max_price": "0.45",
        "no_max_price": "0.48",
        "total_max_cost": "18.60",
        "execution_id": execution["execution_id"],
        "preview_id": preview_id,
        "idempotency_key": "legacy-request",
        "state": "validating",
        "evidence": [],
        "created_at": execution["created_at"],
        "updated_at": execution["updated_at"],
    }
    assert db.cross_unsettled_principal() == Decimal("0")


def test_transition_appends_evidence_before_state_and_survives_restart(tmp_path: Path) -> None:
    db, execution = create_execution(tmp_path)
    execution_id = str(execution["execution_id"])
    db.transition_execution(execution_id, state="submitting", evidence={"step": "validated"})
    db.transition_execution(execution_id, state="complete", evidence={"step": "posted"})

    restored = PredictionArbitrageStore(tmp_path / "data").histories("executions")[0]
    assert restored["state"] == "complete"
    assert restored["evidence"] == [{"step": "validated"}, {"step": "posted"}]


def test_leg_identity_is_unique_per_execution_and_allows_remediation_labels(tmp_path: Path) -> None:
    db, execution = create_execution(tmp_path)
    execution_id = str(execution["execution_id"])
    db.record_leg(execution_id, {"label": "YES", "status": "filled"})
    db.record_leg(execution_id, {"label": "NO", "status": "rejected"})
    db.record_leg(execution_id, {"label": "remediation:SELL", "status": "filled"})
    with pytest.raises((ValueError, sqlite3.IntegrityError), match="label|unique"):
        db.record_leg(execution_id, {"label": "YES", "status": "duplicate"})

    PredictionArbitrageStore(tmp_path / "data")
    with sqlite3.connect(tmp_path / "data" / "prediction_arbitrage" / "prediction_arbitrage.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM execution_legs").fetchone()[0] == 3
        leg_ids = {
            row[0]
            for row in connection.execute(
                "SELECT leg_id FROM execution_legs ORDER BY leg_label"
            )
        }
    assert leg_ids == {
        f"{execution_id}:YES",
        f"{execution_id}:NO",
        f"{execution_id}:remediation:SELL",
    }


def test_acknowledgement_is_durable_and_never_deletes_incident_evidence(tmp_path: Path) -> None:
    db, execution = create_execution(tmp_path)
    incident_id = db.open_incident(execution["execution_id"], {"kind": "one_leg", "detail": "NO rejected"})
    assert db.unacknowledged_incident()["incident_id"] == incident_id
    db.acknowledge_incident(incident_id, {"operator": "ray", "note": "reconciled"})

    assert db.unacknowledged_incident() is None
    restored = PredictionArbitrageStore(tmp_path / "data").histories("incidents")[0]
    assert restored["incident_id"] == incident_id
    assert restored["acknowledged"] is True
    assert restored["acknowledgement"] == {"note": "reconciled", "operator": "ray"}
    assert restored["kind"] == "one_leg"


def test_signal_history_windows_have_exact_boundaries_and_newest_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
    monkeypatch.setattr("open_trader.prediction_arbitrage_store._utc_now", lambda: iso(now))
    db = store(tmp_path)
    for market, started in (
        ("new", now - timedelta(hours=1)),
        ("boundary24", now - timedelta(hours=24)),
        ("old24", now - timedelta(hours=24, microseconds=1)),
        ("boundary7d", now - timedelta(days=7)),
        ("old7d", now - timedelta(days=7, microseconds=1)),
    ):
        db.upsert_signal(signal_payload(market, iso(started)))

    assert [row["market_id"] for row in db.signal_history("24h")] == ["new", "boundary24"]
    assert [row["market_id"] for row in db.signal_history("7d")] == [
        "new",
        "boundary24",
        "old24",
        "boundary7d",
    ]
    assert [row["market_id"] for row in db.signal_history("all")] == [
        "new",
        "boundary24",
        "old24",
        "boundary7d",
        "old7d",
    ]
    with pytest.raises(ValueError, match="window"):
        db.signal_history("month")  # type: ignore[arg-type]


def test_signal_history_supports_thirty_day_annualized_distribution_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(
        "open_trader.prediction_arbitrage_store._utc_now", lambda: iso(now)
    )
    db = store(tmp_path)
    db.upsert_signal(
        signal_payload("boundary30d", iso(now - timedelta(days=30)))
    )
    db.upsert_signal(
        signal_payload(
            "older30d", iso(now - timedelta(days=30, microseconds=1))
        )
    )

    assert [row["market_id"] for row in db.signal_history("30d")] == [
        "boundary30d"
    ]


def test_llm_cache_survives_restart_and_replaces_the_same_fingerprint(
    tmp_path: Path,
) -> None:
    db = store(tmp_path)
    db.save_llm_cache(
        "cache-key",
        {
            "decision": "APPROVE",
            "relation": "B_IMPLIES_A",
            "summary": "初次校验",
        },
    )
    db.save_llm_cache(
        "cache-key",
        {
            "decision": "REJECT",
            "relation": "NONE",
            "summary": "规则已由调用方生成新指纹前不得覆盖",
        },
    )

    assert PredictionArbitrageStore(db.data_dir).load_llm_cache("cache-key") == {
        "decision": "REJECT",
        "relation": "NONE",
        "summary": "规则已由调用方生成新指纹前不得覆盖",
    }
    assert db.load_llm_cache("missing") is None


def test_llm_usage_24h_counts_calls_failures_hits_and_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(
        "open_trader.prediction_arbitrage_store._utc_now", lambda: iso(now)
    )
    db = store(tmp_path)
    db.record_llm_call(
        status="success",
        usage={
            "input_tokens": 100,
            "cached_input_tokens": 60,
            "output_tokens": 20,
            "reasoning_output_tokens": 5,
        },
    )
    db.record_llm_call(status="failed", usage={})
    db.record_llm_cache_hit()

    original_connection = db._connection
    materialized_rows = 0

    def guarded_connection() -> sqlite3.Connection:
        connection = original_connection()

        def one_row_only(
            cursor: sqlite3.Cursor, row: tuple[object, ...]
        ) -> sqlite3.Row:
            nonlocal materialized_rows
            materialized_rows += 1
            if materialized_rows > 1:
                raise AssertionError("usage summary must materialize one aggregate row")
            return sqlite3.Row(cursor, row)

        connection.row_factory = one_row_only
        return connection

    monkeypatch.setattr(db, "_connection", guarded_connection)

    assert db.llm_usage_24h() == {
        "calls": 2,
        "successes": 1,
        "failures": 1,
        "cache_hits": 1,
        "input_tokens": 100,
        "cached_input_tokens": 60,
        "output_tokens": 20,
        "reasoning_output_tokens": 5,
    }


def test_llm_usage_24h_breaks_down_by_provider(tmp_path: Path) -> None:
    db = store(tmp_path)
    db.record_llm_call(status="failed", usage={"provider": "codex"})
    db.record_llm_call(
        status="success",
        usage={"provider": "deepseek", "input_tokens": 10},
    )
    db.record_llm_call(status="success", usage={})
    db.record_llm_cache_hit()

    assert db.llm_usage_24h_by_provider() == {
        "codex": {
            "calls": 2,
            "successes": 1,
            "failures": 1,
            "cache_hits": 1,
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "reasoning_output_tokens": 0,
        },
        "deepseek": {
            "calls": 1,
            "successes": 1,
            "failures": 0,
            "cache_hits": 0,
            "input_tokens": 10,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "reasoning_output_tokens": 0,
        },
    }
    assert db.llm_usage_24h()["calls"] == 3
    assert db.llm_usage_24h()["cache_hits"] == 1


def test_llm_usage_window_includes_exact_boundary_and_excludes_older(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(
        "open_trader.prediction_arbitrage_store._utc_now", lambda: iso(current)
    )
    db = store(tmp_path)
    current -= timedelta(microseconds=1)
    db.record_llm_call(status="failed", usage={"output_tokens": 99})
    current += timedelta(microseconds=1)
    db.record_llm_call(status="success", usage={"input_tokens": 10})
    current += timedelta(hours=24)

    assert db.llm_usage_24h() == {
        "calls": 1,
        "successes": 1,
        "failures": 0,
        "cache_hits": 0,
        "input_tokens": 10,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
    }


@pytest.mark.parametrize("status", ["", "unknown", "SUCCESS"])
def test_llm_usage_rejects_unknown_status(
    tmp_path: Path, status: str
) -> None:
    with pytest.raises(ValueError, match="status"):
        store(tmp_path).record_llm_call(status=status, usage={})


def test_llm_usage_cache_hits_are_memory_only(tmp_path: Path) -> None:
    db = store(tmp_path)
    db.record_llm_cache_hit()
    db.record_llm_cache_hit(provider="deepseek")
    with db._read_connection() as connection:
        row = connection.execute(
            "SELECT COUNT(*) FROM llm_usage WHERE kind='cache_hit'"
        ).fetchone()
    assert int(row[0]) == 0
    assert db.llm_usage_24h()["cache_hits"] == 2
    by_provider = db.llm_usage_24h_by_provider()
    assert by_provider["codex"]["cache_hits"] == 1
    assert by_provider["deepseek"]["cache_hits"] == 1


def test_llm_usage_prune_keeps_recent_calls_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(
        "open_trader.prediction_arbitrage_store._utc_now", lambda: iso(now)
    )
    db = store(tmp_path)
    db.record_llm_call(status="success", usage={})
    with db._transaction() as connection:
        connection.execute(
            "INSERT INTO llm_usage(usage_id, kind, status, payload, created_at) "
            "VALUES (?, 'call', 'success', ?, ?)",
            ("old-call", "{}", iso(now - timedelta(days=8))),
        )
        connection.execute(
            "INSERT INTO llm_usage(usage_id, kind, status, payload, created_at) "
            "VALUES (?, 'cache_hit', 'success', ?, ?)",
            ("legacy-hit", '{"provider": "codex"}', iso(now - timedelta(hours=1))),
        )
    db.prune_llm_usage()
    with db._read_connection() as connection:
        rows = connection.execute(
            "SELECT usage_id, kind FROM llm_usage ORDER BY usage_id"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["kind"] == "call"
    assert rows[0]["usage_id"] != "old-call"
    # Startup also prunes, and the memory counter starts fresh on a new instance.
    db2 = store(tmp_path)
    assert db2.llm_usage_24h()["calls"] == 1
    assert db2.llm_usage_24h()["cache_hits"] == 0


def test_histories_are_newest_first_for_all_kinds(tmp_path: Path) -> None:
    db = store(tmp_path)
    now = datetime.now(UTC)
    db.upsert_signal(signal_payload("market-1", iso(now - timedelta(minutes=2))))
    db.upsert_signal(signal_payload("market-2", iso(now - timedelta(minutes=1))))
    assert db.histories("signals")[0]["market_id"] == "market-2"
    db2, execution = create_execution(tmp_path, idempotency_key="history-request")
    db2.transition_execution(execution["execution_id"], state="complete", evidence={"step": "done"})
    incident_1 = db2.open_incident(execution["execution_id"], {"kind": "merge"})
    incident_2 = db2.open_incident(execution["execution_id"], {"kind": "restart"})
    assert db2.histories("executions")[0]["execution_id"] == execution["execution_id"]
    assert db2.histories("incidents")[0]["incident_id"] == incident_2
    assert db2.histories("incidents")[1]["incident_id"] == incident_1


def test_sensitive_ticks_signed_orders_and_secrets_never_reach_sqlite(tmp_path: Path) -> None:
    db = store(tmp_path)
    db.write_runtime(
        {
            "raw_ticks": [{"price": "0.45", "size": "20", "sentinel": "raw-tick-secret"}],
            "api_secret": "api-secret-sentinel",
            "api_token": "api-token-sentinel",
            "private_key": "private-key-sentinel",
            "signed_order": "signed-payload-sentinel",
            "raw_websocket_message": "raw-websocket-sentinel",
            "order_payload": "order-payload-sentinel",
            "safe": "kept",
        }
    )
    db.upsert_signal(
        {
            **signal_payload("market-1", iso(datetime.now(UTC))),
            "signed_payload": "signed-payload-sentinel",
            "signature": "signature-sentinel",
        }
    )
    path = tmp_path / "data" / "prediction_arbitrage" / "prediction_arbitrage.sqlite3"
    with sqlite3.connect(path) as connection:
        rows = connection.execute("SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL").fetchall()
        values = connection.execute("SELECT payload FROM runtime UNION ALL SELECT payload FROM signals").fetchall()
    schema = " ".join(str(row[0]).lower() for row in rows)
    stored = " ".join(str(row[0]).lower() for row in values)
    for sentinel in (
        "raw-tick-secret",
        "signed-payload-sentinel",
        "signature-sentinel",
        "api-secret-sentinel",
        "api-token-sentinel",
        "private-key-sentinel",
        "raw-websocket-sentinel",
        "order-payload-sentinel",
    ):
        assert sentinel not in stored
    for forbidden in ("raw_ticks", "signed_order", "signature", "private_key", "api_secret"):
        assert forbidden not in schema


def test_decimal_strings_are_stored_without_exponents(tmp_path: Path) -> None:
    db = store(tmp_path)
    db.write_runtime({"amount": Decimal("1E+2"), "negative": Decimal("-0.50")})
    restored = db.load_runtime()
    assert restored == {"amount": "100", "negative": "-0.50"}
    raw = json.dumps(restored, sort_keys=True)
    assert "E" not in raw


def test_generic_payload_redacts_public_pair_token_ids_and_camel_case_order_payload(
    tmp_path: Path,
) -> None:
    db = store(tmp_path)
    db.write_runtime(
        {
            "yes_token_id": "yes-public-token-123",
            "no_token_id": "no-public-token-456",
            "orderPayload": "signed-order-payload-sentinel",
        }
    )

    restored = db.load_runtime()
    assert restored is not None
    assert "yes_token_id" not in restored
    assert "no_token_id" not in restored
    assert "orderPayload" not in restored


def test_relation_state_alone_preserves_public_pair_token_ids(tmp_path: Path) -> None:
    db = store(tmp_path)
    db.save_relation_state(
        {
            "relations": [
                {
                    "token_id": "single-public-token",
                    "yes_token_id": "yes-public-token",
                    "no_token_id": "no-public-token",
                    "api_token": "credential-sentinel",
                }
            ]
        },
        full_scanned_at="2026-07-31T00:00:00Z",
    )

    restored = db.load_relation_state()
    assert restored == {
        "relations": [
            {
                "no_token_id": "no-public-token",
                "token_id": "single-public-token",
                "yes_token_id": "yes-public-token",
            }
        ]
    }
    db.record_relation_scan(
        scope="activity",
        status="completed",
        started_at="2026-07-31T00:00:00Z",
        completed_at="2026-07-31T00:00:01Z",
        payload={"token_id": "scan-token", "safe": "kept"},
    )
    scan = db.relation_scan_history(limit=1)[0]
    assert "token_id" not in scan
    assert scan["safe"] == "kept"
    signal_id = db.upsert_signal(
        {
            **signal_payload("relation-1", "2026-07-31T00:00:00Z"),
            "token_id": "signal-token",
            "yes_token_id": "signal-yes",
            "no_token_id": "signal-no",
        }
    )
    signal = db.signal(signal_id)
    assert signal is not None
    assert {
        "token_id": "signal-token",
        "yes_token_id": "signal-yes",
        "no_token_id": "signal-no",
    }.items() <= signal.items()


def test_cross_venue_signal_episode_preserves_public_legs_and_rearms_after_close(
    tmp_path: Path,
) -> None:
    db = store(tmp_path)
    opportunity_id = "cross:public-pair:PREDICT_YES_POLYMARKET_NO"
    payload = {
        "opportunity_id": opportunity_id,
        "market_id": opportunity_id,
        "event_id": "public-pair",
        "market_type": "cross_venue_yes_no",
        "question": "Predict contract question / Polymarket contract question",
        "predict_question": "Predict contract question",
        "polymarket_question": "Polymarket contract question",
        "started_at": "2026-08-02T00:00:00Z",
        "first_positive_at": "2026-08-02T00:00:00Z",
        "trigger_total_max_cost": Decimal("9.45"),
        "trigger_minimum_profit": Decimal("0.55"),
        "legs": [
            {
                "exchange": "predict.fun",
                "outcome": "YES",
                "market_id": "public-predict-market",
                "condition_id": "public-predict-condition",
                "token_id": "public-predict-yes",
                "yes_token_id": "public-predict-yes",
                "no_token_id": "public-predict-no",
                "settlement_asset": "USDT",
                "api_key": "api-key-sentinel",
                "nested": {"private_key": "private-key-sentinel"},
            },
            {
                "exchange": "polymarket",
                "outcome": "NO",
                "market_id": "public-poly-market",
                "condition_id": "public-poly-condition",
                "token_id": "public-poly-no",
                "settlement_asset": "USDC",
                "proof": {"signature": "signature-sentinel", "jwt": "jwt-sentinel"},
            },
        ],
    }

    signal_id = db.upsert_signal(payload)
    assert db.upsert_signal(
        {
            **payload,
            "trigger_total_max_cost": Decimal("99.99"),
            "trigger_minimum_profit": Decimal("99.99"),
        }
    ) == signal_id
    signal = db.signal(signal_id)
    assert signal is not None
    assert signal["opportunity_id"] == opportunity_id
    assert signal["trigger_total_max_cost"] == "9.45"
    assert signal["trigger_minimum_profit"] == "0.55"
    assert signal["question"] == "Predict contract question / Polymarket contract question"
    assert signal["predict_question"] == "Predict contract question"
    assert signal["polymarket_question"] == "Polymarket contract question"
    assert signal["legs"] == [
        {
            "exchange": "predict.fun",
            "outcome": "YES",
            "market_id": "public-predict-market",
            "condition_id": "public-predict-condition",
            "token_id": "public-predict-yes",
            "yes_token_id": "public-predict-yes",
            "no_token_id": "public-predict-no",
            "settlement_asset": "USDT",
            "nested": {},
        },
        {
            "exchange": "polymarket",
            "outcome": "NO",
            "market_id": "public-poly-market",
            "condition_id": "public-poly-condition",
            "token_id": "public-poly-no",
            "settlement_asset": "USDC",
            "proof": {},
        },
    ]

    db.close_signal(
        opportunity_id,
        ended_at="2026-08-02T00:01:00Z",
        reason="data_unavailable",
        updates={
            "trigger_total_max_cost": Decimal("99.99"),
            "trigger_minimum_profit": Decimal("99.99"),
        },
    )
    closed = db.signal(signal_id)
    assert closed is not None
    assert closed["ended_reason"] == "data_unavailable"
    assert closed["trigger_total_max_cost"] == "9.45"
    assert closed["trigger_minimum_profit"] == "0.55"
    assert db.upsert_signal(payload) != signal_id


def test_missing_database_has_baseline_reader_generation_without_creation(tmp_path: Path) -> None:
    from open_trader.prediction_arbitrage_store import read_minimum_reader_generation

    database = tmp_path / "prediction_arbitrage" / "prediction_arbitrage.sqlite3"
    assert read_minimum_reader_generation(tmp_path) == 1
    assert not database.exists()


def test_missing_metadata_table_reads_baseline_without_mutating_database(tmp_path: Path) -> None:
    from open_trader.prediction_arbitrage_store import read_minimum_reader_generation

    database = tmp_path / "prediction_arbitrage" / "prediction_arbitrage.sqlite3"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE existing(value INTEGER)")
    before = database.read_bytes()

    assert read_minimum_reader_generation(tmp_path) == 1
    assert database.read_bytes() == before
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name='schema_metadata'"
        ).fetchone() is None


def test_store_persists_baseline_and_probe_reads_future_minimum(tmp_path: Path) -> None:
    from open_trader.prediction_arbitrage_store import read_minimum_reader_generation

    PredictionArbitrageStore(tmp_path)
    database = tmp_path / "prediction_arbitrage" / "prediction_arbitrage.sqlite3"
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT minimum_reader_generation FROM schema_metadata WHERE singleton=1"
        ).fetchone() == (1,)
        connection.execute(
            "UPDATE schema_metadata SET minimum_reader_generation=2 WHERE singleton=1"
        )

    assert read_minimum_reader_generation(tmp_path) == 2
    PredictionArbitrageStore(tmp_path)
    assert read_minimum_reader_generation(tmp_path) == 2


def test_reader_probe_closes_its_mode_ro_connection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import open_trader.prediction_arbitrage_store as store_module

    PredictionArbitrageStore(tmp_path)
    real_connect = sqlite3.connect
    observed: dict[str, object] = {}

    class RecordingConnection:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self.connection = connection

        def execute(self, statement: str, parameters: tuple[object, ...] = ()):
            return self.connection.execute(statement, parameters)

        def close(self) -> None:
            observed["closed"] = True
            self.connection.close()

    def connect(database: str, **kwargs: object) -> RecordingConnection:
        observed.update(uri=database, kwargs=kwargs)
        return RecordingConnection(real_connect(database, **kwargs))

    monkeypatch.setattr(store_module.sqlite3, "connect", connect)
    assert store_module.read_minimum_reader_generation(tmp_path) == 1
    assert str(observed["uri"]).endswith("?mode=ro")
    assert observed["kwargs"] == {"uri": True}
    assert observed["closed"] is True


def test_existing_metadata_table_without_singleton_fails_closed(tmp_path: Path) -> None:
    from open_trader.prediction_arbitrage_store import read_minimum_reader_generation

    database = tmp_path / "prediction_arbitrage" / "prediction_arbitrage.sqlite3"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE schema_metadata(singleton INTEGER PRIMARY KEY, minimum_reader_generation INTEGER NOT NULL)"
        )
    with pytest.raises(ValueError, match="generation is missing"):
        read_minimum_reader_generation(tmp_path)


def test_load_llm_cache_entries_returns_only_hit_keys(tmp_path: Path) -> None:
    db = store(tmp_path)
    db.save_llm_cache("key-a", {"decision": "APPROVE"})
    db.save_llm_cache("key-b", {"decision": "REJECT"})
    result = db.load_llm_cache_entries(["key-a", "key-b", "key-missing"])
    assert set(result.keys()) == {"key-a", "key-b"}
    assert result["key-a"] == {"decision": "APPROVE"}
    assert result["key-b"] == {"decision": "REJECT"}


def test_load_llm_cache_entries_chunks_over_900_keys(tmp_path: Path) -> None:
    db = store(tmp_path)
    # Insert 1001 entries
    payloads = {}
    for i in range(1001):
        key = f"chunk-key-{i}"
        payload = {"index": i}
        db.save_llm_cache(key, payload)
        payloads[key] = payload
    keys = list(payloads.keys())
    result = db.load_llm_cache_entries(keys)
    assert len(result) == 1001
    for key, payload in payloads.items():
        assert result[key] == payload


def test_load_llm_cache_entries_empty_and_whitespace(tmp_path: Path) -> None:
    db = store(tmp_path)
    assert db.load_llm_cache_entries([]) == {}
    assert db.load_llm_cache_entries(["", "  "]) == {}
    # Dedup and strip
    db.save_llm_cache("a", {"x": 1})
    result = db.load_llm_cache_entries(["  a  ", "  a  ", "b"])
    assert set(result.keys()) == {"a"}
