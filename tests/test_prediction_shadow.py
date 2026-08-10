from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest

from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from open_trader.prediction_shadow import main, seed_shadow_store
from open_trader.prediction_read_only import (
    PolymarketReadOnlyGuard,
    PredictReadOnlyGuard,
    ReadOnlyViolation,
    guard_polymarket_client,
    guard_predict_client,
)


def _populated_store(data_dir: Path) -> PredictionArbitrageStore:
    store = PredictionArbitrageStore(data_dir)
    store.save_relation_state(
        {"relations": [{"relation_id": "relation-1"}]},
        full_scanned_at="2026-08-10T00:00:00Z",
    )
    store.save_llm_cache("cached-1", {"decision": "approve"})
    store.save_llm_cache("cached-2", {"decision": "reject"})
    with store._transaction() as connection:  # production-shaped forbidden row
        connection.execute(
            "INSERT INTO signals(signal_id, market_id, payload, started_at, updated_at) "
            "VALUES ('signal-1', 'market-1', '{}', '2026-08-10T00:00:00Z', '2026-08-10T00:00:00Z')"
        )
    return store


def _populate_forbidden_categories(store: PredictionArbitrageStore) -> None:
    now = "2026-08-10T00:00:00Z"
    with sqlite3.connect(store.path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.executescript(
            f"""
            INSERT INTO runtime(singleton, payload, updated_at)
            VALUES (1, '{{"breaker":{{"open":true}}}}', '{now}');
            INSERT INTO previews(preview_id, payload, created_at, expires_at)
            VALUES ('preview-1', '{{}}', '{now}', '{now}');
            INSERT INTO executions(
                execution_id, preview_id, idempotency_key, state, payload, evidence,
                created_at, updated_at
            ) VALUES ('execution-1', 'preview-1', 'idempotency-1', 'complete', '{{}}', '[]', '{now}', '{now}');
            INSERT INTO cross_execution_reservations(
                execution_id, amount, state, created_at
            ) VALUES ('execution-1', '1', 'reserved', '{now}');
            INSERT INTO execution_legs(
                leg_id, execution_id, leg_label, payload, created_at
            ) VALUES ('leg-1', 'execution-1', 'A', '{{}}', '{now}');
            INSERT INTO incidents(
                incident_id, execution_id, payload, created_at, updated_at
            ) VALUES ('incident-1', 'execution-1', '{{}}', '{now}', '{now}');
            INSERT INTO llm_usage(usage_id, kind, status, payload, created_at)
            VALUES ('usage-1', 'relation', 'success', '{{}}', '{now}');
            INSERT INTO relation_scan_runs(
                scan_id, scope, status, payload, started_at, completed_at
            ) VALUES ('scan-1', 'full', 'completed', '{{}}', '{now}', '{now}');
            INSERT INTO validation_mode(singleton, mode, updated_at)
            VALUES (1, 'auto', '{now}');
            INSERT INTO auto_eat_attempts(
                attempt_id, signal_id, market_id, decision, reason, created_at
            ) VALUES ('attempt-1', 'signal-1', 'market-1', 'reject', 'test', '{now}');
            INSERT INTO cross_auto_state(singleton, configured_mode, armed, reason, updated_at)
            VALUES (1, 'auto_submit', 1, 'test', '{now}');
            INSERT INTO cross_auto_attempts(
                signal_id, opportunity_id, decision, reason, payload, preview_id,
                execution_id, created_at, updated_at
            ) VALUES ('signal-1', 'opportunity-1', 'reject', 'test', '{{}}', 'preview-1', 'execution-1', '{now}', '{now}');
            """
        )


def _file_snapshot(path: Path) -> tuple[bool, int | None, int | None, bytes | None]:
    if not path.exists():
        return (False, None, None, None)
    stat = path.stat()
    return (True, stat.st_mtime_ns, stat.st_size, path.read_bytes())


def test_seed_shadow_store_copies_only_relations_and_llm_cache(tmp_path: Path) -> None:
    source = _populated_store(tmp_path / "production")

    report = seed_shadow_store(
        source_data_dir=source.data_dir,
        shadow_data_dir=tmp_path / "shadow",
    )
    destination = PredictionArbitrageStore(tmp_path / "shadow")

    assert destination.load_relation_state() == source.load_relation_state()
    assert destination.load_llm_cache("cached-1") == {"decision": "approve"}
    assert report["relation_state_rows"] == 1
    assert report["llm_cache_rows"] == 2
    assert destination.histories("executions") == []
    assert destination.histories("incidents") == []
    assert destination.signal_history("all") == []
    with sqlite3.connect(f"file:{destination.path}?mode=ro", uri=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM signals").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM relation_scan_runs").fetchone() == (0,)


def test_seed_shadow_store_excludes_every_forbidden_category_and_preserves_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _populated_store(tmp_path / "production")
    _populate_forbidden_categories(source)
    wal_path = Path(f"{source.path}-wal")
    with sqlite3.connect(source.path) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    wal_path.unlink(missing_ok=True)
    source_files = {
        path: _file_snapshot(path)
        for path in (
            source.path,
            Path(f"{source.path}-wal"),
        )
    }
    import open_trader.prediction_shadow as shadow_module

    connect_calls: list[tuple[object, dict[str, object]]] = []
    read_only_connections: list[sqlite3.Connection] = []
    connect = shadow_module.sqlite3.connect

    def recording_connect(database: object, *args: object, **kwargs: object):
        connect_calls.append((database, dict(kwargs)))
        connection = connect(database, *args, **kwargs)
        if "mode=ro" in str(database):
            read_only_connections.append(connection)
        return connection

    monkeypatch.setattr(shadow_module.sqlite3, "connect", recording_connect)
    report = seed_shadow_store(
        source_data_dir=source.data_dir,
        shadow_data_dir=tmp_path / "shadow",
    )
    destination = PredictionArbitrageStore(tmp_path / "shadow")
    forbidden = {
        "runtime", "validation_mode", "cross_auto_state", "signals", "previews",
        "executions", "cross_execution_reservations", "execution_legs", "incidents",
        "llm_usage", "relation_scan_runs", "auto_eat_attempts", "cross_auto_attempts",
    }
    with sqlite3.connect(destination.path) as connection:
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in forbidden
        }
        relation_rows = connection.execute(
            "SELECT singleton, payload, full_scanned_at, updated_at FROM relation_state ORDER BY singleton"
        ).fetchall()
        cache_rows = connection.execute(
            "SELECT cache_key, payload, created_at FROM llm_cache ORDER BY cache_key"
        ).fetchall()

    assert all(count == 0 for count in counts.values())
    assert any(
        str(database).endswith("prediction_arbitrage.sqlite3?mode=ro&immutable=1")
        and kwargs.get("uri") is True
        for database, kwargs in connect_calls
    )
    assert any(
        str(database) == f"{source.path.resolve().as_uri()}?mode=ro&immutable=1"
        for database, _kwargs in connect_calls
    )
    assert read_only_connections
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        read_only_connections[0].execute("SELECT 1")
    expected_digest = hashlib.sha256(
        json.dumps(
            {"relation_state": relation_rows, "llm_cache": cache_rows},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert report["sha256"] == expected_digest
    unchanged = {
        str(path): (
            _file_snapshot(path)[:3],
            snapshot[:3],
            _file_snapshot(path)[3] == snapshot[3],
        )
        for path, snapshot in source_files.items()
        if _file_snapshot(path) != snapshot
    }
    assert unchanged == {}


def test_seed_shadow_store_reads_latest_rows_from_live_wal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _populated_store(tmp_path / "production")
    connection = sqlite3.connect(source.path)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE relation_state SET payload=?, updated_at=? WHERE singleton=1",
            ('{"relations":[{"relation_id":"wal-relation"}]}', "2026-08-10T00:01:00Z"),
        )
        connection.execute(
            "INSERT OR REPLACE INTO llm_cache(cache_key, payload, created_at) VALUES (?, ?, ?)",
            ("wal-cache", '{"decision":"approve"}', "2026-08-10T00:01:00Z"),
        )
        connection.commit()
        assert Path(f"{source.path}-wal").exists()
        source_files = {
            path: _file_snapshot(path)
            for path in (
                source.path,
                Path(f"{source.path}-wal"),
            )
        }
        import open_trader.prediction_shadow as shadow_module

        connect_calls: list[object] = []
        connect = shadow_module.sqlite3.connect

        def recording_connect(database: object, *args: object, **kwargs: object):
            connect_calls.append(database)
            return connect(database, *args, **kwargs)

        monkeypatch.setattr(shadow_module.sqlite3, "connect", recording_connect)

        seed_shadow_store(
            source_data_dir=source.data_dir,
            shadow_data_dir=tmp_path / "shadow",
        )
        destination = PredictionArbitrageStore(tmp_path / "shadow")
        assert destination.load_relation_state() == {
            "relations": [{"relation_id": "wal-relation"}]
        }
        assert destination.load_llm_cache("wal-cache") == {"decision": "approve"}
        assert all(_file_snapshot(path) == snapshot for path, snapshot in source_files.items())
        assert f"{source.path.resolve().as_uri()}?mode=ro" in {
            str(database) for database in connect_calls
        }
    finally:
        connection.close()


def test_seed_shadow_store_rejects_same_database(tmp_path: Path) -> None:
    source = _populated_store(tmp_path / "production")

    with pytest.raises(ValueError, match="different databases"):
        seed_shadow_store(
            source_data_dir=source.data_dir,
            shadow_data_dir=source.data_dir,
        )


def test_seed_shadow_store_rejects_identical_uncreated_paths(tmp_path: Path) -> None:
    same_data_dir = tmp_path / "same"

    with pytest.raises(ValueError, match="different databases"):
        seed_shadow_store(
            source_data_dir=same_data_dir,
            shadow_data_dir=same_data_dir,
        )


def test_seed_shadow_store_rejects_hardlinked_database(tmp_path: Path) -> None:
    source = _populated_store(tmp_path / "production")
    hardlink_dir = tmp_path / "hardlink" / "prediction_arbitrage"
    hardlink_dir.mkdir(parents=True)
    os.link(source.path, hardlink_dir / source.path.name)

    with pytest.raises(ValueError, match="different databases"):
        seed_shadow_store(
            source_data_dir=source.data_dir,
            shadow_data_dir=hardlink_dir.parent,
        )


def test_shadow_seed_cli_writes_the_report(tmp_path: Path) -> None:
    source = _populated_store(tmp_path / "production")
    report_path = tmp_path / "reports" / "seed.json"

    assert main(
        [
            "--source-data-dir", str(source.data_dir),
            "--shadow-data-dir", str(tmp_path / "shadow"),
            "--report", str(report_path),
        ]
    ) == 0

    assert json.loads(report_path.read_text(encoding="utf-8"))["llm_cache_rows"] == 2


def test_shadow_seed_cli_replaces_report_atomically_and_preserves_on_failure(
    tmp_path: Path,
) -> None:
    source = _populated_store(tmp_path / "production")
    report_path = tmp_path / "reports" / "seed.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text("old-report", encoding="utf-8")

    assert main(
        [
            "--source-data-dir", str(source.data_dir),
            "--shadow-data-dir", str(tmp_path / "shadow"),
            "--report", str(report_path),
        ]
    ) == 0
    assert report_path.read_text(encoding="utf-8") != "old-report"
    assert list(report_path.parent.iterdir()) == [report_path]

    report_path.write_text("keep-on-failure", encoding="utf-8")
    with pytest.raises(ValueError, match="different databases"):
        main(
            [
                "--source-data-dir", str(source.data_dir),
                "--shadow-data-dir", str(source.data_dir),
                "--report", str(report_path),
            ]
        )
    assert report_path.read_text(encoding="utf-8") == "keep-on-failure"
    assert list(report_path.parent.iterdir()) == [report_path]


def test_shared_nested_read_only_guards_block_both_fake_sdk_mutations() -> None:
    attempts: list[dict[str, object]] = []
    network_calls: list[str] = []

    class FakePolymarketTransport:
        def cancel_all(self) -> None:
            network_calls.append("polymarket.cancel_all")

    class FakePolymarketClient:
        def __init__(self) -> None:
            self._client = FakePolymarketTransport()

        def cancel_all(self) -> None:
            self._client.cancel_all()

    class FakeBuilder:
        pass

    class FakePredictClient:
        def __init__(self) -> None:
            self._builder = FakeBuilder()

        def submit_order(self) -> None:
            network_calls.append("predict.submit_order")

    polymarket = FakePolymarketClient()
    predict = FakePredictClient()
    with guard_polymarket_client(
        polymarket, PolymarketReadOnlyGuard(attempts.append)
    ), guard_predict_client(predict, PredictReadOnlyGuard(attempts.append)):
        with pytest.raises(ReadOnlyViolation):
            polymarket.cancel_all()
        with pytest.raises(ReadOnlyViolation):
            predict.submit_order()

    assert network_calls == []
    assert [attempt["venue"] for attempt in attempts] == ["polymarket", "predict"]
    assert all(set(attempt) == {"venue", "kind", "method", "call_chain"} for attempt in attempts)
    assert all(len(attempt["call_chain"]) <= 12 for attempt in attempts)
