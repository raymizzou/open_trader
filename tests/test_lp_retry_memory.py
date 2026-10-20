"""Retry resource contracts at public preparation and real SQLite boundaries."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import gc
import socket
import threading
import weakref

import pytest

from open_trader.polymarket_lp import PolymarketLPService
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from test_polymarket_lp import _LPCandidateQueryExchange

NOW = datetime(2026, 10, 7, tzinfo=UTC)


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("retry resource fixtures must not connect to a socket")
    monkeypatch.setattr(socket.socket, "connect", denied)


class WeakRow(dict):
    """Observe returned persistence values without retaining them."""


class WeakSnapshot(list):
    pass


def seed_mixed_retries(store, count=60):
    for index in range(count):
        store.lp_record_preparation_failure(
            f"condition-R{index:03}", generation=1,
            stage="metadata" if index % 2 else "history",
            error="RequestRejectedError" if index % 3 == 0 else "TransportError",
            failed_at=NOW,
        )
    store.lp_record_preparation_failure(
        "condition-M0", generation=1, stage="history", error="TransportError",
        failed_at=NOW - timedelta(minutes=5),
    )
    store.lp_save_preparation({
        "state": "partial", "stage": "history", "generation": 1,
        "paused": False, "next_retry_at": NOW,
    })


@pytest.mark.parametrize("exit_kind", ["normal", "cancel", "exception"])
def test_preparation_releases_consumed_retry_snapshots_before_history(tmp_path, exit_kind):
    store = PredictionArbitrageStore(tmp_path)
    seed_mixed_retries(store)
    # Keep existing paused alerts settled; history failure can claim new ones.
    store.lp_claim_preparation_item_alerts()
    before = {row["condition_id"]: row for row in store.lp_preparation_items()
              if row["condition_id"] != "condition-M0"}
    row_refs, snapshot_refs, boundary_counts, requested = [], [], [], set()
    stop = threading.Event()
    reader = store.lp_preparation_items

    def observed_read():
        rows = WeakSnapshot(WeakRow(row) for row in reader())
        row_refs.extend(weakref.ref(row) for row in rows)
        snapshot_refs.append(weakref.ref(rows))
        return rows

    store.lp_preparation_items = observed_read

    class Exchange(_LPCandidateQueryExchange):
        def lp_price_history(self, tokens, **kwargs):
            gc.collect()
            boundary_counts.append((sum(ref() is not None for ref in snapshot_refs),
                                    sum(ref() is not None for ref in row_refs)))
            requested.update(tokens)
            if exit_kind == "cancel":
                stop.set()
            if exit_kind == "exception":
                raise RuntimeError("offline history boundary failure")
            return super().lp_price_history(tokens, **kwargs)

    exchange = Exchange(NOW, {**{f"R{i:03}": Decimal("100") for i in range(60)},
                              "M0": Decimal("100")})
    service = PolymarketLPService(store, exchange, clock=lambda: NOW)
    result = service.refresh_price_history(stop_event=stop)
    gc.collect()
    assert all(ref() is None for ref in row_refs)
    assert all(ref() is None for ref in snapshot_refs)
    assert requested == {"token-condition-M0"}
    assert result["preparation_outcome"] == {
        "normal": "waiting_retry", "cancel": "cancelled", "exception": "failure",
    }[exit_kind]
    after = {row["condition_id"]: row for row in reader()}
    assert {key: after[key] for key in before} == before
    if exit_kind != "exception":
        assert "condition-M0" not in after
    else:
        assert after["condition-M0"]["paused"] is False
        assert after["condition-M0"]["retry_used"] is False
        assert after["condition-M0"]["state"] == "waiting_retry"
        assert after["condition-M0"]["failure_count"] == 2
        assert after["condition-M0"]["next_retry_at"] == "2026-10-07T00:10:00.000000Z"
    assert boundary_counts
    # A single currently used record is bounded; a population is not.
    assert all(snapshots == 0 and rows <= 1 for snapshots, rows in boundary_counts), boundary_counts


@pytest.mark.parametrize("variable_limit", [999, 7])
def test_due_retry_claim_preserves_stage_scope_and_atomic_order(tmp_path, monkeypatch, variable_limit):
    import sqlite3

    store = PredictionArbitrageStore(tmp_path)
    fixtures = (
        ("catalog-metadata", "metadata", 600, "TransportError"),
        ("catalog-history", "history", 600, "TransportError"),
        ("target-metadata", "metadata", 600, "TransportError"),
        ("target-history", "history", 300, "TransportError"),
        ("target-future", "history", 0, "TransportError"),
        ("target-paused", "history", 600, "RequestRejectedError"),
        ("target-spent", "history", 600, "TransportError"),
        ("target-retrying", "metadata", 600, "TransportError"),
        ("outside", "metadata", 600, "TransportError"),
    )
    for condition, stage, age, error in fixtures:
        store.lp_record_preparation_failure(
            condition, generation=3, stage=stage, error=error,
            failed_at=NOW - timedelta(seconds=age),
        )
    store.lp_claim_preparation_retries(
        now=NOW, condition_ids=("target-spent", "target-retrying"),
    )
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE lp_preparation_items SET state='waiting_retry',next_retry_at=? "
                           "WHERE condition_id='target-spent'", ("2026-10-06T23:55:00Z",))
    before = {row["condition_id"]: row for row in store.lp_preparation_items()}
    connect = sqlite3.connect

    def limited_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, variable_limit)
        return connection

    monkeypatch.setattr(sqlite3, "connect", limited_connect)
    targets = ("target-history", "target-metadata", "target-future", "target-paused",
               "target-spent", "target-retrying", "missing", "", " target-history ")
    catalog = ("catalog-history", "catalog-metadata", "target-metadata",
               *tuple(f"missing-{i}" for i in range(12)))
    with connect(store.path) as connection:
        connection.execute("""
            CREATE TRIGGER fail_scoped_retry BEFORE UPDATE ON lp_preparation_items
            WHEN NEW.condition_id='target-history' AND NEW.retry_used=1
            BEGIN SELECT RAISE(ABORT, 'late scoped claim failure'); END
        """)
    with pytest.raises(sqlite3.IntegrityError, match="late scoped claim failure"):
        store.lp_claim_preparation_retries(
            now=NOW, condition_ids=targets, metadata_condition_ids=catalog,
        )
    assert {row["condition_id"]: row for row in store.lp_preparation_items()} == before
    with connect(store.path) as connection:
        connection.execute("DROP TRIGGER fail_scoped_retry")
    claimed = store.lp_claim_preparation_retries(
        now=NOW, condition_ids=targets, metadata_condition_ids=catalog,
    )
    assert [row["condition_id"] for row in claimed] == [
        "catalog-metadata", "target-metadata", "target-history",
    ]
    for row in claimed:
        assert row["retry_used"] is True and row["state"] == "retrying"
        assert row["generation"] == 3 and row["failure_count"] == 1
        assert row["next_retry_at"] is None
        assert row["retry_started_at"] == "2026-10-07T00:00:00.000000Z"
    after = {row["condition_id"]: row for row in store.lp_preparation_items()}
    excluded = ("catalog-history", "target-future", "target-paused", "target-spent",
                "target-retrying", "outside")
    assert {key: after[key] for key in excluded} == {key: before[key] for key in excluded}
    assert store.lp_claim_preparation_retries(
        now=NOW, condition_ids=targets, metadata_condition_ids=catalog,
    ) == []


def test_history_retry_reads_do_not_scale_with_backfill_batches(tmp_path, monkeypatch):
    import sqlite3

    store = PredictionArbitrageStore(tmp_path)
    seed_mixed_retries(store)
    before = {row["condition_id"]: row for row in store.lp_preparation_items()
              if row["condition_id"] != "condition-M0"}
    hydrated = [0]
    boundary_hydration, requested = [], []
    connect = sqlite3.connect

    class ObservedConnection(sqlite3.Connection):
        @property
        def row_factory(self):
            return sqlite3.Connection.row_factory.__get__(self)

        @row_factory.setter
        def row_factory(self, factory):
            def observed(cursor, values):
                columns = {column[0] for column in cursor.description}
                if {"retry_used", "failure_count", "retry_started_at", "direction"} <= columns:
                    hydrated[0] += 1
                return values if factory is None else factory(cursor, values)
            sqlite3.Connection.row_factory.__set__(self, observed)

    def observed_connect(*args, **kwargs):
        return connect(*args, **kwargs, factory=ObservedConnection)

    monkeypatch.setattr(sqlite3, "connect", observed_connect)

    class Exchange(_LPCandidateQueryExchange):
        def lp_price_history(self, tokens, **kwargs):
            boundary_hydration.append(hydrated[0])
            requested.extend(tokens)
            return super().lp_price_history(tokens, **kwargs)

    exchange = Exchange(NOW, {**{f"R{i:03}": Decimal("100") for i in range(60)},
                              **{f"M{i}": Decimal("100") for i in range(181)}})
    service = PolymarketLPService(store, exchange, clock=lambda: NOW)
    result = service.refresh_price_history()
    assert result["state"] == "partial"
    assert result["target_count"] == result["updated_count"] == 181
    assert result["unknown_count"] == 0
    assert result["request_count"] == 11
    assert sorted(requested) == sorted(f"token-condition-M{i}" for i in range(181))
    assert len(requested) == len(set(requested)) == 181
    after = {row["condition_id"]: row for row in store.lp_preparation_items()}
    assert after == before
    assert len(boundary_hydration) == 11
    # Initial recovery scans are allowed; fixed-clock later batches have no
    # new due rows and must not hydrate a full population again.
    assert max(boundary_hydration) - min(boundary_hydration) == 0, boundary_hydration


@pytest.mark.parametrize("deadline", ["2026-10-06T23:55:00", "invalid"])
@pytest.mark.parametrize("blocked", ["waiting", "paused", "spent"])
def test_stage_scoped_claim_rejects_naive_deadline_without_spending_retries(tmp_path, deadline, blocked):
    import sqlite3

    store = PredictionArbitrageStore(tmp_path)
    for condition in ("valid-due", "invalid-deadline"):
        store.lp_record_preparation_failure(
            condition, generation=1, stage="history", error="TransportError",
            failed_at=NOW - timedelta(minutes=5),
        )
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE lp_preparation_items SET next_retry_at=?,paused=?,retry_used=? "
            "WHERE condition_id='invalid-deadline'",
            (deadline, int(blocked == "paused"), int(blocked == "spent")),
        )
    before = store.lp_preparation_items()
    with pytest.raises(ValueError, match="next_retry_at_invalid"):
        store.lp_claim_preparation_retries(
            now=NOW, condition_ids=("valid-due", "invalid-deadline"),
            metadata_condition_ids=(),
        )
    assert store.lp_preparation_items() == before
