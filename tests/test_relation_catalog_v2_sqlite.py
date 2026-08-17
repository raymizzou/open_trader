"""Issue #78 step 2: SQLite persistence backend for ``RelationCatalogV2``.

The in-memory invariant matrix in ``test_relation_catalog_v2.py`` stays
untouched; these tests cover reopen persistence, direct-SQL tamper fail-closed,
v1 table ignorance, and cross-connection atomic snapshots.
"""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from open_trader.relation_catalog_v2 import RelationCatalogV2, SqliteCatalogStore
from test_relation_catalog_v2 import _endpoint, _payload


def _catalog(db_path: str) -> RelationCatalogV2:
    return RelationCatalogV2(SqliteCatalogStore(db_path))


def _approve(catalog: RelationCatalogV2, payload: dict[str, object]) -> dict[str, object]:
    result = catalog.ingest(payload)
    catalog.approve(result["version_id"], actor="auditor", git_sha="a" * 40)
    return result


def test_sqlite_reopen_preserves_state(tmp_path) -> None:
    db_path = str(tmp_path / "catalog.db")
    catalog = _catalog(db_path)
    _approve(catalog, _payload())
    candidate = catalog.ingest(
        _payload(endpoints=[_endpoint("polymarket", "cZ"), _endpoint("predict.fun", "cW")])
    )
    rejected = catalog.ingest(_payload(discovery_source="llm"))
    catalog.reject(rejected["version_id"], reason="bad source", actor="auditor", git_sha="a" * 40)
    catalog.revoke(candidate["version_id"], actor="auditor", git_sha="a" * 40)

    reopened = _catalog(db_path)
    assert reopened.current_generation() == catalog.current_generation()
    assert reopened.admit(_payload()) is True
    versions = reopened.store["versions"]
    assert versions[rejected["version_id"]]["status"] == "REJECTED"
    assert versions[rejected["version_id"]]["reject_reason"] == "bad source"
    assert versions[candidate["version_id"]]["status"] == "PENDING"


def test_sqlite_tampered_payload_fails_closed(tmp_path) -> None:
    db_path = str(tmp_path / "catalog.db")
    catalog = _catalog(db_path)
    version_id = catalog.ingest(_payload())["version_id"]
    catalog.approve(version_id, actor="auditor", git_sha="a" * 40)

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT payload FROM catalog_v2_versions WHERE version_id = ?", (version_id,)
        ).fetchone()
        tampered = json.loads(row[0])
        tampered["capital_release"] = "tampered"
        conn.execute(
            "UPDATE catalog_v2_versions SET payload = ? WHERE version_id = ?",
            (json.dumps(tampered, sort_keys=True), version_id),
        )

    reopened = _catalog(db_path)
    with pytest.raises(ValueError):
        reopened.current_generation()
    assert reopened.admit(_payload()) is False


def test_sqlite_v1_tables_ignored(tmp_path) -> None:
    db_path = str(tmp_path / "catalog.db")
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE relation_catalog_approvals (identity TEXT PRIMARY KEY, version_id TEXT)")
        conn.execute("INSERT INTO relation_catalog_approvals VALUES ('legacy:1', 'legacy-v1')")
        conn.execute("CREATE TABLE relation_catalog_generations (identity TEXT PRIMARY KEY, status TEXT)")
        conn.execute("INSERT INTO relation_catalog_generations VALUES ('legacy:1', 'ACTIVE')")
    catalog = _catalog(db_path)
    assert catalog.current_generation() == {}


def test_sqlite_concurrent_writer_reader_see_complete_generations(tmp_path) -> None:
    db_path = str(tmp_path / "catalog.db")
    writer = _catalog(db_path)
    g1_payloads = [
        _payload(relation_type="EXACTLY_ONE", endpoints=[_endpoint("polymarket", "cA"), _endpoint("predict.fun", "cB")]),
        _payload(relation_type="EXACTLY_ONE", endpoints=[_endpoint("polymarket", "cB"), _endpoint("predict.fun", "cC")]),
    ]
    g2_payloads = [
        _payload(relation_type="EXACTLY_ONE", endpoints=[_endpoint("polymarket", "cD"), _endpoint("predict.fun", "cE")]),
        _payload(relation_type="EXACTLY_ONE", endpoints=[_endpoint("polymarket", "cE"), _endpoint("predict.fun", "cF")]),
    ]
    g1_ids = {_approve(writer, payload)["identity"] for payload in g1_payloads}
    g2_ids = {_approve(writer, payload)["identity"] for payload in g2_payloads}
    assert g1_ids.isdisjoint(g2_ids)

    reader = _catalog(db_path)
    snapshots: list[frozenset[str]] = []
    errors: list[BaseException] = []

    def writer_loop() -> None:
        for _ in range(20):
            writer.replace(g1_payloads, actor="auditor", git_sha="a" * 40)
            writer.replace(g2_payloads, actor="auditor", git_sha="a" * 40)

    def reader_loop() -> None:
        for _ in range(60):
            try:
                snapshots.append(frozenset(reader.current_generation()))
            except BaseException as exc:  # pragma: no cover - atomicity must not tear
                errors.append(exc)

    with ThreadPoolExecutor(max_workers=3) as pool:
        for future in (pool.submit(writer_loop), pool.submit(reader_loop), pool.submit(reader_loop)):
            future.result()

    assert not errors
    assert snapshots
    assert all(snapshot in (g1_ids, g2_ids, g1_ids | g2_ids) for snapshot in snapshots)


def test_sqlite_read_rolls_back_when_commit_raises(tmp_path) -> None:
    db_path = str(tmp_path / "catalog.db")
    catalog = _catalog(db_path)
    store = catalog.store
    real_conn = sqlite3.connect(db_path, check_same_thread=False)
    real_conn.isolation_level = None

    class ExplodingConnection:
        def execute(self, sql, *args, **kwargs):
            if isinstance(sql, str) and sql.strip().upper() == "COMMIT":
                raise sqlite3.OperationalError("cannot commit")
            return real_conn.execute(sql, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(store, "_connection", lambda: ExplodingConnection())
        with pytest.raises(sqlite3.OperationalError):
            catalog.current_generation()
    real_conn.close()

    assert catalog.current_generation() == {}


def test_sqlite_thread_local_readers_share_one_catalog(tmp_path) -> None:
    db_path = str(tmp_path / "catalog.db")
    catalog = _catalog(db_path)
    _approve(catalog, _payload())
    expected = catalog.current_generation()
    snapshots: list[dict[str, dict]] = []
    errors: list[BaseException] = []

    def read_loop() -> None:
        for _ in range(100):
            try:
                snapshots.append(catalog.current_generation())
            except BaseException as exc:  # pragma: no cover - readers must not fail
                errors.append(exc)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(read_loop) for _ in range(8)]
        for future in futures:
            future.result()

    assert not errors
    assert snapshots == [expected] * len(snapshots)


def test_sqlite_thread_local_writers_fail_cleanly(tmp_path) -> None:
    db_path = str(tmp_path / "catalog.db")
    catalog = _catalog(db_path)
    payloads = [
        _payload(
            relation_type="EXACTLY_ONE",
            endpoints=[
                _endpoint("polymarket", f"writer-{i}-A"),
                _endpoint("predict.fun", f"writer-{i}-B"),
            ],
        )
        for i in range(16)
    ]
    results: list[dict[str, object]] = []
    errors: list[BaseException] = []

    def write_loop(payload: dict[str, object]) -> None:
        try:
            results.append(catalog.ingest(payload))
        except BaseException as exc:
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(write_loop, payload) for payload in payloads]
        for future in futures:
            future.result()

    locked = [
        exc
        for exc in errors
        if isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).lower()
    ]
    assert locked == errors
    assert len(_catalog(db_path).store["versions"]) == len(results)


def test_sqlite_thread_local_mixed_reads_and_writes(tmp_path) -> None:
    db_path = str(tmp_path / "catalog.db")
    catalog = _catalog(db_path)
    _approve(catalog, _payload())
    expected = catalog.current_generation()
    payloads = [
        _payload(
            relation_type="EXACTLY_ONE",
            endpoints=[
                _endpoint("polymarket", f"mixed-{i}-A"),
                _endpoint("predict.fun", f"mixed-{i}-B"),
            ],
        )
        for i in range(16)
    ]
    snapshots: list[dict[str, dict]] = []
    read_errors: list[BaseException] = []
    write_errors: list[BaseException] = []

    def read_loop() -> None:
        for _ in range(100):
            try:
                snapshots.append(catalog.current_generation())
            except BaseException as exc:  # pragma: no cover - readers must not fail
                read_errors.append(exc)

    def write_loop(payload: dict[str, object]) -> None:
        try:
            catalog.ingest(payload)
        except BaseException as exc:
            write_errors.append(exc)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(read_loop) for _ in range(4)]
        futures += [pool.submit(write_loop, payload) for payload in payloads]
        for future in futures:
            future.result()

    assert not read_errors
    assert snapshots == [expected] * len(snapshots)
    locked = [
        exc
        for exc in write_errors
        if isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).lower()
    ]
    assert locked == write_errors
