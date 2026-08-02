from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

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
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
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
        "relation_state",
        "relation_scan_runs",
    }
    assert "signals_market_started_at" in indexes
    assert "signals_started_at" in indexes
    assert "signals_open_started_at" in indexes
    assert any("signals_market_started_at" in row[3] for row in query_plan)
    assert any("signals_started_at" in row[3] for row in history_query_plan)
    assert any("signals_open_started_at" in row[3] for row in open_query_plan)


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


def test_preview_expires_after_ten_seconds_and_is_single_use(tmp_path: Path) -> None:
    db = store(tmp_path)
    now = datetime.now(UTC)
    preview_id = db.create_preview(
        preview_payload(), expires_at=iso(now + timedelta(seconds=10))
    )
    execution = db.consume_preview_and_create_execution(preview_id, "request-1")
    assert execution["preview_id"] == preview_id
    with pytest.raises(ValueError, match="consumed"):
        db.consume_preview_and_create_execution(preview_id, "request-2")

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
    assert sum(isinstance(item, dict) for item in results) == 1
    assert sum(isinstance(item, Exception) for item in results) == 1
    assert setup.active_execution() is not None


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
