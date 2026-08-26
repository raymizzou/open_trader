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

from open_trader.relation_catalog import RelationCatalog
from open_trader.relation_catalog_v2 import RelationCatalogV2, SqliteCatalogStore
from test_relation_catalog import discovery
from test_relation_catalog_v2 import _endpoint, _payload


def _catalog(db_path: str) -> RelationCatalogV2:
    return RelationCatalogV2(SqliteCatalogStore(db_path))


def _approve(catalog: RelationCatalogV2, payload: dict[str, object]) -> dict[str, object]:
    result = catalog.ingest(payload)
    catalog.approve(result["version_id"], actor="auditor", git_sha="a" * 40)
    return result


# Issue #102: the facade keeps event_identity_basis in the stored v2 payload,
# and legacy rows without a basis survive the SQLite round-trip byte-identical.

def test_sqlite_converted_payload_keeps_event_identity_basis(tmp_path) -> None:
    catalog = RelationCatalog(tmp_path)
    version_id = catalog.ingest(discovery())["version_id"]

    reopened = RelationCatalog(tmp_path)
    stored = reopened._versions()[version_id]["payload"]
    assert all(
        str(endpoint["event_identity_basis"]) == "event-a"
        for endpoint in stored["endpoints"]
    )
    assert json.dumps(stored, sort_keys=True) == json.dumps(
        catalog._versions()[version_id]["payload"], sort_keys=True
    )


def test_sqlite_legacy_row_without_basis_round_trips_byte_identical(tmp_path) -> None:
    db_path = str(tmp_path / "catalog.db")
    catalog = _catalog(db_path)
    legacy = _payload()  # pre-#102 rows never carried event_identity_basis
    result = _approve(catalog, legacy)
    record = catalog.store["versions"][result["version_id"]]

    reopened = _catalog(db_path)
    record_again = reopened.store["versions"][result["version_id"]]
    assert json.dumps(record_again["payload"], sort_keys=True) == json.dumps(
        record["payload"], sort_keys=True
    )
    assert record_again["version_fp"] == record["version_fp"]
    assert record_again["identity"] == record["identity"]


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


# Issue #98 slice S1: incremental persistence. T1.1 pins the generation
# snapshot encoding: rows append only on membership change, as deltas between
# anchor rows; legacy full-snapshot rows (no "kind") read back as anchors.

def _generation_rows(db_path: str) -> list[dict[str, object]]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT members FROM catalog_v2_generations ORDER BY generation_id ASC"
        ).fetchall()
    return [json.loads(row[0]) for row in rows]


def _decode_latest(rows: list[dict[str, object]]) -> dict[str, dict[str, str]]:
    """Reconstruct the latest generation from raw rows: last anchor + later deltas."""
    generation: dict[str, dict[str, str]] = {}
    deltas: list[dict[str, object]] = []
    for data in reversed(rows):
        if isinstance(data, dict) and data.get("kind") == "delta":
            deltas.append(data)
            continue
        if isinstance(data, dict) and data.get("kind") == "anchor":
            generation = dict(data["members"])  # type: ignore[arg-type]
        elif isinstance(data, dict):
            generation = dict(data)
        break
    for delta in reversed(deltas):
        for identity, entry in (delta.get("added") or {}).items():
            generation[str(identity)] = entry
        for identity in delta.get("removed") or []:
            generation.pop(str(identity), None)
    return generation


def _generation_entry(identity: str, version_id: str) -> dict[str, str]:
    return {"version_id": version_id, "status": "ACTIVE"}


def test_sqlite_generations_append_only_on_membership_change(
    tmp_path, monkeypatch
) -> None:
    """T1.1: ingest/reject append nothing; each approve appends one delta;
    the anchor-interval threshold (injectable constant) emits anchor rows."""
    db_path = str(tmp_path / "catalog.db")
    catalog = _catalog(db_path)

    # ingest + reject never change generation membership: no rows at all
    ingested = catalog.ingest(_payload(discovery_source="llm"))
    catalog.reject(
        ingested["version_id"], reason="bad source", actor="auditor", git_sha="a" * 40
    )
    assert _generation_rows(db_path) == []

    # every approve changes membership -> exactly one row per approve
    monkeypatch.setattr(SqliteCatalogStore, "_ANCHOR_EVERY", 3)
    payloads = [
        _payload(
            relation_type="EXACTLY_ONE",
            endpoints=[
                _endpoint("polymarket", f"inc-{i}-A"),
                _endpoint("predict.fun", f"inc-{i}-B"),
            ],
        )
        for i in range(5)
    ]
    results = [_approve(catalog, payload) for payload in payloads]
    expected = {
        result["identity"]: _generation_entry(result["identity"], result["version_id"])
        for result in results
    }
    rows = _generation_rows(db_path)
    assert len(rows) == len(payloads)
    assert [data.get("kind") for data in rows] == [
        "delta", "delta", "anchor", "delta", "delta",
    ]
    assert rows[2] == {
        "kind": "anchor",
        "members": {
            results[i]["identity"]: expected[results[i]["identity"]]
            for i in range(3)
        },
    }

    # a reject-only transaction after the anchors appends nothing
    rejected = catalog.ingest(_payload(discovery_source="llm"))
    catalog.reject(
        rejected["version_id"], reason="bad source", actor="auditor", git_sha="a" * 40
    )
    assert len(_generation_rows(db_path)) == len(payloads)

    # anchor + replay reconstruction matches the online generation
    assert _decode_latest(_generation_rows(db_path)) == catalog.store["generation"]
    assert catalog.store["generation"] == expected

    # re-approving the same identity (no membership change) appends nothing
    _approve(catalog, payloads[0])
    assert len(_generation_rows(db_path)) == len(payloads)


def test_sqlite_legacy_generation_row_reads_as_anchor(tmp_path, monkeypatch) -> None:
    """T1.1: pre-S1 full-snapshot rows (no ``kind``) decode as anchors and
    later delta rows replay on top of them."""
    db_path = str(tmp_path / "catalog.db")
    catalog = _catalog(db_path)
    first = _approve(catalog, _payload())

    # rewrite the generations table into legacy format: full snapshot, no kind
    legacy_members = {
        first["identity"]: _generation_entry(first["identity"], first["version_id"])
    }
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM catalog_v2_generations")
        conn.execute(
            "INSERT INTO catalog_v2_generations (members, created_at) VALUES (?, ?)",
            (json.dumps(legacy_members, sort_keys=True), "2026-08-15T00:00:00+00:00"),
        )

    reopened = _catalog(db_path)
    assert reopened.store["generation"] == legacy_members

    # a later delta appends after the legacy anchor and replays on reopen
    monkeypatch.setattr(SqliteCatalogStore, "_ANCHOR_EVERY", 1000)
    second = _approve(
        reopened,
        _payload(endpoints=[_endpoint("polymarket", "cZ"), _endpoint("predict.fun", "cW")]),
    )
    assert reopened.store["generation"] == {
        first["identity"]: legacy_members[first["identity"]],
        second["identity"]: _generation_entry(second["identity"], second["version_id"]),
    }


# T1.3: only rows touched by a write transaction produce SQL; untouched
# version rows keep their stored updated_at, touched rows get a new one.

def test_sqlite_reject_only_transaction_rewrites_only_dirty_rows(tmp_path) -> None:
    db_path = str(tmp_path / "catalog.db")
    catalog = _catalog(db_path)
    keep = _approve(catalog, _payload())
    touched = _approve(
        catalog,
        _payload(endpoints=[_endpoint("polymarket", "cZ"), _endpoint("predict.fun", "cW")]),
    )
    with sqlite3.connect(db_path) as conn:
        keep_ts = conn.execute(
            "SELECT updated_at FROM catalog_v2_versions WHERE version_id = ?",
            (keep["version_id"],),
        ).fetchone()[0]
        touched_ts = conn.execute(
            "SELECT updated_at FROM catalog_v2_versions WHERE version_id = ?",
            (touched["version_id"],),
        ).fetchone()[0]

    catalog.reject(
        touched["version_id"], reason="bad source", actor="auditor", git_sha="a" * 40
    )

    with sqlite3.connect(db_path) as conn:
        keep_after = conn.execute(
            "SELECT updated_at FROM catalog_v2_versions WHERE version_id = ?",
            (keep["version_id"],),
        ).fetchone()[0]
        touched_after = conn.execute(
            "SELECT updated_at FROM catalog_v2_versions WHERE version_id = ?",
            (touched["version_id"],),
        ).fetchone()[0]
    assert keep_after == keep_ts
    assert touched_after != touched_ts


# T1.2: a mixed incremental write history reopens per-key identical, including
# meta columns, occurrence_count, and activation markers.

def test_sqlite_incremental_writes_reopen_equivalent(tmp_path) -> None:
    db_path = str(tmp_path / "catalog.db")
    catalog = _catalog(db_path)
    first = _approve(catalog, _payload())
    catalog.ingest(_payload())  # occurrence_count -> 2 for the first version
    candidate = catalog.ingest(
        _payload(endpoints=[_endpoint("polymarket", "cZ"), _endpoint("predict.fun", "cW")])
    )
    rejected = catalog.ingest(_payload(discovery_source="llm"))
    catalog.reject(
        rejected["version_id"], reason="bad source", actor="auditor", git_sha="a" * 40
    )
    catalog.revoke(candidate["version_id"], actor="auditor", git_sha="a" * 40)

    # meta field and activation marker via the store's public write transaction
    store = catalog.store
    store.begin_write()
    store["versions"][rejected["version_id"]]["custom_flag"] = "meta-value"
    store["versions"][candidate["version_id"]]["activation_status"] = "SUPERSEDED"
    store.commit_write()

    online = {
        "versions": catalog.store["versions"],
        "approved": catalog.store["approved"],
        "generation": catalog.store["generation"],
        "causes": catalog.store["causes"],
        "latest": catalog.store["latest"],
        "generation_number": catalog.store["generation_number"],
    }
    reopened = _catalog(db_path)
    assert reopened.store["versions"] == online["versions"]
    assert reopened.store["approved"] == online["approved"]
    assert reopened.store["generation"] == online["generation"]
    assert reopened.store["causes"] == online["causes"]
    assert reopened.store["latest"] == online["latest"]
    assert reopened.store["generation_number"] == online["generation_number"]
