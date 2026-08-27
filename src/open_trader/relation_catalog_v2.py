"""Issue #78 v2 relation catalog core.

Minimal public API ``RelationCatalogV2`` encoded from the nine confirmed
Issue #78 decisions; the invariant regression matrix in
``tests/test_relation_catalog_v2.py`` is the acceptance contract. Persistence
is a pluggable mapping seam: plain dicts in tests, ``SqliteCatalogStore`` for
the SQLite backend.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from collections.abc import ItemsView, Iterator, KeysView, MutableMapping, ValuesView
from contextlib import contextmanager
from datetime import datetime, timezone

from .prediction_monitor_selection import relation_generation_problem

ALLOWED_VENUES = frozenset({"polymarket", "predict.fun"})
RELATION_TYPES = frozenset({
    "IMPLIES", "MUTUALLY_EXCLUSIVE", "EXACTLY_ONE", "NATIVE_COMPLEMENT",
})
GROUP_BUDGET = 7  # ponytail: #49 scale_16 per-group endpoint ceiling


def _fp(obj: object) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


def _canonical_endpoint(endpoint: object) -> str:
    if not isinstance(endpoint, dict):
        raise ValueError(f"endpoint must be a dict, got {type(endpoint).__name__}")
    venue = endpoint.get("venue")
    if venue not in ALLOWED_VENUES:
        raise ValueError(f"unsupported venue: {venue!r}")
    contract_id = endpoint.get("contract_id")
    if not isinstance(contract_id, str) or not contract_id:
        raise ValueError("endpoint requires a non-empty contract_id")
    return f"{venue}:{contract_id}"


# Fields that must not affect identity or version: decision 1/2 exclusions.
_EXCLUDED_FIELDS = frozenset({"discovered_at", "group_item_threshold", "rules_hash", "event_id"})


def _canonicalize(payload: object) -> tuple[str, dict]:
    """Return (identity, version fingerprint fields) for a discovery payload."""
    if not isinstance(payload, dict):
        raise ValueError("payload must be a dict")
    relation_type = payload.get("relation_type")
    if relation_type not in RELATION_TYPES:
        raise ValueError(f"unknown relation_type: {relation_type!r}")
    endpoints = payload.get("endpoints")
    if not isinstance(endpoints, list) or len(endpoints) < 2:
        raise ValueError("endpoints must contain at least two endpoints")
    if relation_type in {"IMPLIES", "NATIVE_COMPLEMENT"} and len(endpoints) != 2:
        raise ValueError(f"{relation_type} requires exactly two endpoints")

    sigs = [_canonical_endpoint(endpoint) for endpoint in endpoints]
    if relation_type == "IMPLIES":
        ordered = sigs
    elif relation_type == "NATIVE_COMPLEMENT":
        # Identity orders the token-level endpoints by contract_id only, so
        # the identity reads NATIVE_COMPLEMENT|polymarket:<sorted token>.
        ordered = sorted(sigs, key=lambda sig: sig.split(":", 1)[-1])
    else:
        ordered = sorted(sigs)
    identity = relation_type + "|" + "|".join(ordered)

    by_sig = {sig: endpoint for sig, endpoint in zip(sigs, endpoints)}
    version_fields = {
        key: value for key, value in payload.items() if key not in _EXCLUDED_FIELDS
    }
    version_fields["endpoints"] = [by_sig[sig] for sig in ordered]
    return identity, version_fields


def _fingerprints(version_fields: dict) -> str:
    """Frozen fingerprint recomputed from the canonical payload.

    One fingerprint over the whole canonical payload is strictly stronger than
    three partial source/semantics/model slices: it fails closed on any tamper
    (including a newly added top-level key), which is what the matrix asserts.
    """
    return _fp(version_fields)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class _TrackedDict(dict):
    """dict whose flush diffs against a frozen base snapshot.

    The write overlay holds ``_TrackedDict`` values for the small eager
    tables (approved / causes / latest / generation); ``commit_write``
    compares current content against ``_base`` (the content at transaction
    start) and persists only the difference.
    """

    def __init__(self, base: dict | None = None) -> None:
        base = dict(base) if base else {}
        super().__init__(base)
        self._base: dict = base

    def replace_all(self, value: dict) -> None:
        """Replace the whole dict (store-level ``store[key] = dict``)."""
        dict.clear(self)
        dict.update(self, value)

    def diffs(self) -> tuple[list[tuple[object, object]], list[object]]:
        """Return (changed-or-added items, keys removed) relative to ``_base``."""
        upsert = [
            (key, value)
            for key, value in self.items()
            if key not in self._base or self._base[key] != value
        ]
        removed = [key for key in self._base if key not in self]
        return upsert, removed


class _VersionRecord(dict):
    """One materialized version row; any in-place mutation marks it dirty."""

    def __init__(self, initial: dict | None = None) -> None:
        super().__init__(initial if initial is not None else {})
        self._row_dirty = False

    def __setitem__(self, key: object, value: object) -> None:
        self._row_dirty = True
        dict.__setitem__(self, key, value)

    def __delitem__(self, key: object) -> None:
        self._row_dirty = True
        dict.__delitem__(self, key)


class _LazyVersions(dict):
    """Row-level lazy mapping over ``catalog_v2_versions`` for write txns.

    Rows are fetched on demand by ``version_id``; mutation (whole-row
    assignment, record field assignment, deletion) is tracked so
    ``commit_write`` persists only dirty rows and the delete set. Iteration
    materializes every row once.
    """

    def __init__(self, conn: sqlite3.Connection, base_keys: set[str]) -> None:
        super().__init__()
        self._conn = conn
        self._base_keys = base_keys
        self._dirty: set[str] = set()
        self._deleted: set[str] = set()

    def _record_from_row(self, row: tuple) -> _VersionRecord:
        _, identity, version_fp, payload, status, occurrence_count, activation_status, activation_diagnostic, meta = row
        record = _VersionRecord({
            "payload": json.loads(payload),
            "identity": identity,
            "version_fp": version_fp,
            "status": status,
            "occurrence_count": occurrence_count,
            **json.loads(meta),
        })
        if activation_status is not None:
            record["activation_status"] = activation_status
        if activation_diagnostic is not None:
            record["activation_diagnostic"] = activation_diagnostic
        return record

    def __getitem__(self, key: str) -> _VersionRecord:
        if key in self._deleted:
            raise KeyError(key)
        try:
            return dict.__getitem__(self, key)
        except KeyError:
            row = self._conn.execute(
                "SELECT version_id, identity, version_fp, payload, status, "
                "occurrence_count, activation_status, activation_diagnostic, meta "
                "FROM catalog_v2_versions WHERE version_id = ?",
                (key,),
            ).fetchone()
            if row is None:
                raise KeyError(key)
            record = self._record_from_row(row)
            dict.__setitem__(self, key, record)
            return record

    def __contains__(self, key: object) -> bool:
        try:
            self[str(key)]
            return True
        except KeyError:
            return False

    def get(self, key: str, default: object = None) -> object:
        try:
            return self[key]
        except KeyError:
            return default

    def __setitem__(self, key: str, value: object) -> None:
        if not isinstance(value, _VersionRecord):
            value = _VersionRecord(value)
        self._dirty.add(key)
        self._deleted.discard(key)
        dict.__setitem__(self, key, value)

    def __delitem__(self, key: str) -> None:
        dict.__delitem__(self, key)
        self._dirty.discard(key)
        if key in self._base_keys:
            self._deleted.add(key)

    def update(self, *args: object, **kwargs: object) -> None:
        for key, value in dict(*args, **kwargs).items():
            self[key] = value  # type: ignore[index]

    def _materialize_all(self) -> None:
        rows = self._conn.execute(
            "SELECT version_id, identity, version_fp, payload, status, "
            "occurrence_count, activation_status, activation_diagnostic, meta "
            "FROM catalog_v2_versions"
        ).fetchall()
        for row in rows:
            version_id = str(row[0])
            if dict.__contains__(self, version_id) or version_id in self._deleted:
                continue
            dict.__setitem__(self, version_id, self._record_from_row(row))

    def __iter__(self) -> Iterator[str]:
        self._materialize_all()
        return dict.__iter__(self)

    def __len__(self) -> int:
        self._materialize_all()
        return dict.__len__(self)

    def items(self) -> ItemsView[str, dict]:
        self._materialize_all()
        return dict.items(self)

    def keys(self) -> KeysView[str]:
        self._materialize_all()
        return dict.keys(self)

    def values(self) -> ValuesView[dict]:
        self._materialize_all()
        return dict.values(self)

    def dirty_keys(self) -> set[str]:
        """version_ids that must be upserted at commit."""
        dirty = set(self._dirty)
        for key, record in dict.items(self):
            if isinstance(record, _VersionRecord) and record._row_dirty:
                dirty.add(key)
        return dirty

    def deleted_keys(self) -> set[str]:
        return set(self._deleted)

    def replace_all(self, value: dict) -> None:
        value = dict(value)
        new_keys = set(value)
        self._deleted = (self._deleted | (self._base_keys - new_keys)) - new_keys
        self._dirty = new_keys
        dict.clear(self)
        for key, record in value.items():
            dict.__setitem__(
                self,
                key,
                record if isinstance(record, _VersionRecord) else _VersionRecord(record),
            )


class SqliteCatalogStore(MutableMapping):
    """SQLite-backed persistence seam for ``RelationCatalogV2``.

    Implements the same four logical keys (versions / approved / generation /
    causes) over four new ``catalog_v2_*`` tables. Writes are buffered in an
    overlay inside one ``BEGIN IMMEDIATE`` transaction and flushed together
    with a new append-only generation snapshot; ``begin_read``/``end_read``
    give callers one consistent committed snapshot. Legacy
    ``relation_catalog_*`` v1 tables are never read or written.

    Since issue #98, writes are incremental: only rows touched by the
    transaction are flushed, and ``catalog_v2_generations`` rows encode the
    generation as deltas between anchor rows instead of a full snapshot per
    transaction.
    """

    _VERSION_COLUMNS = frozenset({
        "payload", "identity", "version_fp", "status", "occurrence_count",
        "activation_status", "activation_diagnostic",
    })
    # Issue #98: append an anchor row (full generation snapshot) every this
    # many membership changes; everything in between is one delta row each.
    # Injectable class constant so tests can cross the threshold cheaply.
    _ANCHOR_EVERY = 1000

    def __init__(self, db_path: str | os.PathLike[str]) -> None:
        self._path = str(db_path)
        self._local = threading.local()
        conn = sqlite3.connect(self._path, check_same_thread=False)
        try:
            conn.isolation_level = None  # manual transactions
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            self._create_tables(conn)
        finally:
            conn.close()

    def _connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "connection", None)
        if conn is None:
            conn = sqlite3.connect(self._path, check_same_thread=False)
            conn.isolation_level = None  # manual transactions
            conn.execute("PRAGMA busy_timeout=5000")
            self._local.connection = conn
        return conn

    def _create_tables(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS catalog_v2_versions (
                version_id TEXT PRIMARY KEY,
                identity TEXT NOT NULL,
                version_fp TEXT NOT NULL,
                payload TEXT NOT NULL,
                status TEXT NOT NULL,
                occurrence_count INTEGER NOT NULL,
                activation_status TEXT,
                activation_diagnostic TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                meta TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS catalog_v2_approvals (
                identity TEXT PRIMARY KEY,
                version_id TEXT NOT NULL,
                approved_fingerprint TEXT NOT NULL,
                actor TEXT NOT NULL,
                git_sha TEXT NOT NULL,
                approved_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS catalog_v2_generations (
                generation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                members TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS catalog_v2_causes (
                identity TEXT NOT NULL,
                producer TEXT NOT NULL,
                scope TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (identity, producer, scope)
            );
            CREATE TABLE IF NOT EXISTS catalog_v2_latest (
                identity TEXT PRIMARY KEY,
                version_id TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS catalog_v2_meta (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                generation_number INTEGER NOT NULL
            );
            """
        )
        # Pre-existing v2 databases created before the facade's activation
        # metadata columns; keep additive so old data stays readable.
        for column in ("activation_status TEXT", "activation_diagnostic TEXT"):
            try:
                conn.execute(
                    f"ALTER TABLE catalog_v2_versions ADD COLUMN {column}"
                )
            except sqlite3.OperationalError:
                pass

    def write_audit(
        self,
        action: str,
        identity: str,
        version_id: str,
        actor: str,
        git_sha: str,
        note: str = "",
    ) -> None:
        """Append one operator-facing audit row.

        Rows live in ``catalog_v2_audit``: the legacy v1-era
        ``relation_catalog_audit`` table (different schema, historical rows)
        must never be written or dropped. The table is created lazily here
        (not in ``_create_tables``) so that merely opening a catalog —
        including a read-only smoke open against production data — never
        writes to the database.
        """
        with sqlite3.connect(self._path) as connection:
            connection.execute("PRAGMA busy_timeout=5000")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS catalog_v2_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    identity TEXT NOT NULL,
                    version_id TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    git_sha TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "INSERT INTO catalog_v2_audit "
                "(action, identity, version_id, actor, git_sha, note, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (action, identity, version_id, actor, git_sha, note, _now()),
            )

    # -- transactions ------------------------------------------------------

    def begin_write(self) -> None:
        if getattr(self._local, "overlay", None) is not None:
            raise RuntimeError("nested write transaction")
        conn = self._connection()
        conn.execute("BEGIN IMMEDIATE")
        try:
            self._local.overlay = self._load_write_state(conn)
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        self._local.cache = None

    def commit_write(self) -> None:
        overlay = getattr(self._local, "overlay", None)
        if overlay is None:
            raise RuntimeError("no active write transaction")
        conn = self._connection()
        try:
            self._flush(overlay, conn)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            self._local.overlay = None
            self._local.cache = None

    def rollback_write(self) -> None:
        if getattr(self._local, "overlay", None) is None:
            return
        self._connection().execute("ROLLBACK")
        self._local.overlay = None
        self._local.cache = None

    def begin_read(self) -> None:
        if (
            getattr(self._local, "overlay", None) is not None
            or getattr(self._local, "cache", None) is not None
        ):
            return
        conn = self._connection()
        conn.execute("BEGIN")
        try:
            self._local.cache = self._load_state(conn)
            conn.execute("COMMIT")
        except BaseException:
            self._local.cache = None
            conn.execute("ROLLBACK")
            raise

    def end_read(self) -> None:
        self._local.cache = None

    def prepared_identities(self) -> set[str]:
        """Identities holding PENDING/APPROVED versions, without loading payloads."""
        rows = self._connection().execute(
            "SELECT DISTINCT identity FROM catalog_v2_versions "
            "WHERE status IN ('PENDING', 'APPROVED')"
        ).fetchall()
        return {str(row[0]) for row in rows}

    def generation_rowid_max(self) -> int:
        """Membership watermark for the in-process contract index (#98).

        Every generation membership change appends a row to
        ``catalog_v2_generations``, so the max rowid moves exactly when any
        process (or connection) changed the ACTIVE set. Manual payload tamper
        that bypasses a generation row is intentionally not detected here.
        """
        row = self._connection().execute(
            "SELECT COALESCE(MAX(generation_id), 0) FROM catalog_v2_generations"
        ).fetchone()
        return int(row[0])

    # -- state materialization --------------------------------------------

    def _state(self) -> dict[str, dict]:
        overlay = getattr(self._local, "overlay", None)
        if overlay is not None:
            return overlay
        cache = getattr(self._local, "cache", None)
        if cache is not None:
            return cache
        conn = self._connection()
        conn.execute("BEGIN")
        try:
            state = self._load_state(conn)
            conn.execute("COMMIT")
        except BaseException:
            self._local.cache = None
            conn.execute("ROLLBACK")
            raise
        self._local.cache = state
        return state

    def _scan_generation(self, conn: sqlite3.Connection) -> tuple[dict[str, dict], int]:
        """Decode the latest generation from ``catalog_v2_generations`` rows.

        Rows are append-only. Scanning newest-first, the first row that is an
        anchor (``kind == "anchor"``, or any legacy row without ``kind``,
        which was a full snapshot) is the base; every newer row is a delta to
        replay on top of it. Returns (latest generation, deltas since the
        newest anchor).
        """
        rows = conn.execute(
            "SELECT members FROM catalog_v2_generations ORDER BY generation_id DESC"
        )
        generation: dict[str, dict] = {}
        deltas: list[dict] = []
        for (members,) in rows:
            data = json.loads(members)
            if isinstance(data, dict) and data.get("kind") == "delta":
                deltas.append(data)
                continue
            if isinstance(data, dict):
                generation = data["members"] if data.get("kind") == "anchor" else data
            break
        for delta in reversed(deltas):
            for identity, entry in (delta.get("added") or {}).items():
                generation[identity] = entry
            for identity in delta.get("removed") or []:
                generation.pop(identity, None)
        return generation, len(deltas)

    def _load_state(self, conn: sqlite3.Connection) -> dict[str, dict]:
        generation, _ = self._scan_generation(conn)
        versions: dict[str, dict] = {}
        for version_id, identity, version_fp, payload, status, occurrence_count, activation_status, activation_diagnostic, meta in conn.execute(
            "SELECT version_id, identity, version_fp, payload, status, occurrence_count, activation_status, activation_diagnostic, meta "
            "FROM catalog_v2_versions"
        ):
            versions[version_id] = {
                "payload": json.loads(payload),
                "identity": identity,
                "version_fp": version_fp,
                "status": status,
                "occurrence_count": occurrence_count,
                **json.loads(meta),
            }
            if activation_status is not None:
                versions[version_id]["activation_status"] = activation_status
            if activation_diagnostic is not None:
                versions[version_id]["activation_diagnostic"] = activation_diagnostic
        approved: dict[str, dict] = {}
        for identity, version_id, approved_fingerprint, actor, git_sha in conn.execute(
            "SELECT identity, version_id, approved_fingerprint, actor, git_sha FROM catalog_v2_approvals"
        ):
            approved[identity] = {
                "version_id": version_id,
                "approved_fingerprints": approved_fingerprint,
                "actor": actor,
                "git_sha": git_sha,
            }
        causes: dict[tuple[str, str, str], bool] = {}
        for identity, producer, scope in conn.execute(
            "SELECT identity, producer, scope FROM catalog_v2_causes"
        ):
            causes[(identity, producer, scope)] = True
        latest: dict[str, str] = {}
        for identity, version_id in conn.execute(
            "SELECT identity, version_id FROM catalog_v2_latest"
        ):
            latest[identity] = version_id
        meta_row = conn.execute(
            "SELECT generation_number FROM catalog_v2_meta WHERE singleton=1"
        ).fetchone()
        generation_number = int(meta_row[0]) if meta_row else 0
        return {
            "versions": versions,
            "approved": approved,
            "generation": generation,
            "causes": causes,
            "latest": latest,
            "generation_number": generation_number,
        }

    def _load_write_state(self, conn: sqlite3.Connection) -> dict[str, dict]:
        """Lightweight overlay for a write transaction.

        Only the latest generation state, ``generation_number``, the small
        ``latest`` table and the version id set are loaded up front; version
        rows are materialized lazily and the tracked values diff against
        their load-time base at commit.
        """
        generation, deltas_since_anchor = self._scan_generation(conn)
        meta_row = conn.execute(
            "SELECT generation_number FROM catalog_v2_meta WHERE singleton=1"
        ).fetchone()
        generation_number = int(meta_row[0]) if meta_row else 0
        base_keys = {
            str(row[0]) for row in conn.execute("SELECT version_id FROM catalog_v2_versions")
        }
        approved: dict[str, dict] = {}
        for identity, version_id, approved_fingerprint, actor, git_sha in conn.execute(
            "SELECT identity, version_id, approved_fingerprint, actor, git_sha "
            "FROM catalog_v2_approvals"
        ):
            approved[identity] = {
                "version_id": version_id,
                "approved_fingerprints": approved_fingerprint,
                "actor": actor,
                "git_sha": git_sha,
            }
        causes: dict[tuple[str, str, str], bool] = {}
        for identity, producer, scope in conn.execute(
            "SELECT identity, producer, scope FROM catalog_v2_causes"
        ):
            causes[(identity, producer, scope)] = True
        latest: dict[str, str] = {}
        for identity, version_id in conn.execute(
            "SELECT identity, version_id FROM catalog_v2_latest"
        ):
            latest[identity] = version_id
        tracked_generation = _TrackedDict(generation)
        tracked_generation._deltas_since_anchor = deltas_since_anchor
        return {
            "versions": _LazyVersions(conn, base_keys),
            "approved": _TrackedDict(approved),
            "generation": tracked_generation,
            "causes": _TrackedDict(causes),
            "latest": _TrackedDict(latest),
            "generation_number": generation_number,
        }

    def _flush(self, state: dict[str, dict], conn: sqlite3.Connection) -> None:
        now = _now()
        versions, approved, generation, causes, latest, generation_number = (
            state["versions"],
            state["approved"],
            state["generation"],
            state["causes"],
            state.get("latest", {}),
            int(state.get("generation_number", 0)),
        )
        for version_id in versions.dirty_keys():
            record = versions[version_id]
            meta = {k: v for k, v in record.items() if k not in self._VERSION_COLUMNS}
            conn.execute(
                "INSERT INTO catalog_v2_versions "
                "(version_id, identity, version_fp, payload, status, occurrence_count, activation_status, activation_diagnostic, created_at, updated_at, meta) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(version_id) DO UPDATE SET "
                "identity=excluded.identity, version_fp=excluded.version_fp, "
                "payload=excluded.payload, status=excluded.status, "
                "occurrence_count=excluded.occurrence_count, "
                "activation_status=excluded.activation_status, "
                "activation_diagnostic=excluded.activation_diagnostic, "
                "updated_at=excluded.updated_at, meta=excluded.meta",
                (
                    version_id,
                    record["identity"],
                    record["version_fp"],
                    json.dumps(record["payload"], sort_keys=True, default=str),
                    record["status"],
                    record["occurrence_count"],
                    record.get("activation_status"),
                    record.get("activation_diagnostic"),
                    now,
                    now,
                    json.dumps(meta, sort_keys=True, default=str),
                ),
            )
        for version_id in versions.deleted_keys():
            conn.execute(
                "DELETE FROM catalog_v2_versions WHERE version_id = ?", (version_id,)
            )
        approved_upsert, approved_removed = approved.diffs()
        for identity, record in approved_upsert:
            conn.execute(
                "INSERT INTO catalog_v2_approvals "
                "(identity, version_id, approved_fingerprint, actor, git_sha, approved_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(identity) DO UPDATE SET "
                "version_id=excluded.version_id, "
                "approved_fingerprint=excluded.approved_fingerprint, "
                "actor=excluded.actor, git_sha=excluded.git_sha, "
                "approved_at=excluded.approved_at",
                (
                    identity,
                    record["version_id"],
                    record["approved_fingerprints"],
                    record["actor"],
                    record["git_sha"],
                    now,
                ),
            )
        for identity in approved_removed:
            conn.execute(
                "DELETE FROM catalog_v2_approvals WHERE identity = ?", (identity,)
            )
        causes_upsert, causes_removed = causes.diffs()
        for (identity, producer, scope), _ in causes_upsert:
            conn.execute(
                "INSERT INTO catalog_v2_causes (identity, producer, scope, created_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(identity, producer, scope) DO UPDATE SET created_at=excluded.created_at",
                (identity, producer, scope, now),
            )
        for (identity, producer, scope) in causes_removed:
            conn.execute(
                "DELETE FROM catalog_v2_causes WHERE identity = ? AND producer = ? AND scope = ?",
                (identity, producer, scope),
            )
        latest_upsert, latest_removed = latest.diffs()
        for identity, version_id in latest_upsert:
            conn.execute(
                "INSERT INTO catalog_v2_latest (identity, version_id) VALUES (?, ?) "
                "ON CONFLICT(identity) DO UPDATE SET version_id=excluded.version_id",
                (identity, version_id),
            )
        for identity in latest_removed:
            conn.execute(
                "DELETE FROM catalog_v2_latest WHERE identity = ?", (identity,)
            )
        added, removed = generation.diffs()
        if added or removed:
            deltas_since_anchor = int(getattr(generation, "_deltas_since_anchor", 0))
            if deltas_since_anchor + 1 >= self._ANCHOR_EVERY:
                snapshot: dict[str, object] = {
                    "kind": "anchor",
                    "members": dict(generation),
                }
            else:
                snapshot = {
                    "kind": "delta",
                    "added": {str(identity): entry for identity, entry in added},
                    "removed": sorted(str(identity) for identity in removed),
                }
            conn.execute(
                "INSERT INTO catalog_v2_generations (members, created_at) VALUES (?, ?)",
                (json.dumps(snapshot, sort_keys=True), now),
            )
        conn.execute(
            "INSERT INTO catalog_v2_meta (singleton, generation_number) VALUES (1, ?) "
            "ON CONFLICT(singleton) DO UPDATE SET generation_number=excluded.generation_number",
            (generation_number,),
        )

    # -- MutableMapping ----------------------------------------------------

    def __getitem__(self, key: str) -> dict:
        return self._state()[key]

    def __setitem__(self, key: str, value: object) -> None:
        if getattr(self._local, "overlay", None) is None:
            raise RuntimeError("writes require a write transaction")
        overlay = self._local.overlay
        if key == "generation_number" or not isinstance(value, dict):
            overlay[key] = value  # type: ignore[index]
            return
        current = overlay.get(key)
        if isinstance(current, (_TrackedDict, _LazyVersions)):
            current.replace_all(value)  # type: ignore[arg-type]
        else:
            overlay[key] = value  # type: ignore[index]

    def __delitem__(self, key: str) -> None:
        if getattr(self._local, "overlay", None) is None:
            raise RuntimeError("writes require a write transaction")
        del self._local.overlay[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._state())

    def __len__(self) -> int:
        return len(self._state())


class RelationCatalogV2:
    """In-memory v2 relation catalog.

    The optional ``store`` is a persistence seam (dict in tests, SQLite later);
    SQLite stores provide thread-local transaction isolation.
    """

    def __init__(self, store: MutableMapping | None = None) -> None:
        self.store: MutableMapping = store if store is not None else {}
        # Issue #98: in-process derived state (canonical contract -> set of
        # ACTIVE identities, compiled-problem observation key -> set of ACTIVE
        # identities, and the ACTIVE-set aggregates max as_of / min terminal
        # release / valuation units) built from the current generation's
        # payloads, guarded by the store's membership watermark. The watermark
        # is the max catalog_v2_generations rowid for SQLite stores (any
        # process's membership change appends a row) and the bumped
        # generation_number for plain-dict stores. The state is rebuilt from
        # the transaction snapshot whenever the watermark moves and is
        # maintained incrementally after this process's own commits. One
        # watermark token guards all of it (same lifecycle).
        self._contract_index: dict[str, set[str]] = {}
        self._key_index: dict[str, set[str]] = {}
        self._aggregates: dict[str, object] = {
            "max_as_of": None,
            "min_release": None,
            "units": frozenset(),
        }
        self._contract_index_token: object = None
        # Review R2: the in-process index/aggregate state above is shared
        # mutable state across service request threads (ThreadingHTTPServer).
        # One catalog-level lock guards every rebuild, apply, sync, invalidate
        # and activation-path read of it; the watermark/rebuild logic cannot
        # repair a lost read-modify-write once the token matches, so the
        # mutations must be atomic. Coarse granularity is deliberate.
        self._index_lock = threading.Lock()

    @contextmanager
    def _write(self) -> Iterator[None]:
        begin = getattr(self.store, "begin_write", None)
        if begin is None:
            yield
            self._bump_generation()
            return
        begin()
        try:
            yield
            self._bump_generation()
            self.store.commit_write()
        except BaseException:
            self.store.rollback_write()
            self._invalidate_contract_index()
            raise

    def _bump_generation(self) -> None:
        current = self.store.get("generation_number", 0)
        self.store["generation_number"] = int(current) + 1

    def bump_generation(self) -> None:
        """Bump ``generation_number`` by one for a write transaction the
        caller composes itself (facade ``_approve_many`` manages its own
        transaction; every write transaction bumps exactly once, symmetric
        with ``_write``)."""
        self._bump_generation()

    @contextmanager
    def _read(self) -> Iterator[None]:
        begin = getattr(self.store, "begin_read", None)
        if begin is None:
            yield
            return
        begin()
        try:
            yield
        finally:
            self.store.end_read()

    # -- mutations ---------------------------------------------------------

    def ingest(self, payload: object) -> dict:
        with self._write():
            identity, version_fields = _canonicalize(payload)
            version_fp = _fp(version_fields)
            version_id = "v-" + _fp({"identity": identity, "fingerprint": version_fp})
            versions = self.store.setdefault("versions", {})
            if version_id in versions:
                versions[version_id]["occurrence_count"] += 1
                status = versions[version_id]["status"]
            else:
                versions[version_id] = {
                    "payload": payload,
                    "identity": identity,
                    "version_fp": version_fp,
                    "status": "PENDING",
                    "occurrence_count": 1,
                }
                self.store.setdefault("latest", {})[identity] = version_id
                status = "PENDING"
            return {
                "identity": identity,
                "version_id": version_id,
                "status": status,
                "occurrence_count": versions[version_id]["occurrence_count"],
            }

    def approve(self, version_id: str, *, actor: str, git_sha: str) -> dict:
        with self._write():
            versions = self.store.setdefault("versions", {})
            if version_id not in versions:
                raise ValueError(f"unknown version: {version_id}")
            identity, version_fields = _canonicalize(versions[version_id]["payload"])
            self.store.setdefault("approved", {})[identity] = {
                "version_id": version_id,
                "actor": actor,
                "git_sha": git_sha,
                "approved_fingerprints": _fingerprints(version_fields),
            }
            versions[version_id]["status"] = "APPROVED"
            self.store.setdefault("generation", {})[identity] = {
                "version_id": version_id,
                "status": "ACTIVE",
            }
            return {"version_id": version_id, "identity": identity, "status": "APPROVED"}

    def reject(
        self,
        version_id: str,
        *,
        reason: str,
        actor: str,
        git_sha: str,
        note: str = "",
    ) -> dict:
        with self._write():
            versions = self.store.setdefault("versions", {})
            if version_id not in versions:
                raise ValueError(f"unknown version: {version_id}")
            versions[version_id]["status"] = "REJECTED"
            versions[version_id]["reject_reason"] = reason
            versions[version_id]["reject_note"] = note
            return {"version_id": version_id, "status": "REJECTED"}

    def reject_many(
        self,
        version_ids: list[str],
        *,
        reason: str,
        actor: str,
        git_sha: str,
        note: str = "",
    ) -> dict:
        """Reject many versions in one write transaction (one flush)."""
        with self._write():
            versions = self.store.setdefault("versions", {})
            for version_id in version_ids:
                if version_id not in versions:
                    raise ValueError(f"unknown version: {version_id}")
                versions[version_id]["status"] = "REJECTED"
                versions[version_id]["reject_reason"] = reason
                versions[version_id]["reject_note"] = note
                versions[version_id]["reject_actor"] = actor
                versions[version_id]["reject_git_sha"] = git_sha
            return {"rejected": len(version_ids)}

    def revoke(self, version_id: str, *, actor: str, git_sha: str) -> dict:
        with self._write():
            versions = self.store.setdefault("versions", {})
            if version_id not in versions:
                raise ValueError(f"unknown version: {version_id}")
            identity = versions[version_id]["identity"]
            self.store.setdefault("causes", {})[(identity, "revoked", version_id)] = True
            return {"version_id": version_id, "identity": identity, "status": "UNKNOWN"}

    def expire_members(
        self,
        identities: list[str],
        *,
        actor: str,
        git_sha: str,
    ) -> dict:
        """Remove generation members whose facts expired (#96), never via revoke.

        Expiry is its own lifecycle outcome: each member's version record is
        marked ``EXPIRED`` (with the rotating actor/sha recorded on it), a
        cause ledger entry keyed under producer ``expired`` — distinct from
        ``revoked`` — poisons exactly that identity, and the identity leaves
        the stored generation so the next published set is prospective
        without it. An unknown identity raises after earlier members of the
        same call were already mutated inside this transaction; the caller's
        rollback discards all of them.
        """
        with self._write():
            return self._expire_members_locked(
                identities, actor=actor, git_sha=git_sha
            )

    def _expire_members_locked(
        self,
        identities: list[str],
        *,
        actor: str,
        git_sha: str,
    ) -> dict:
        """Core expire_members assuming the caller holds one write transaction.

        Composable seam for the facade's ``expire_stale_members`` rotation:
        the facade embeds the drops and its APPROVED+blocked review-state
        reset in one store transaction of its own (mirroring
        ``_activate_many_locked``/``revoke_locked``), so a failure after the
        drops are applied rolls all of it back instead of committing expiry
        while losing the reset. Same mutation contract as above; like the
        facade-held callers, this seam does not bump or commit — callers
        compose ``bump_generation`` once per transaction.
        """
        generation = self.store.setdefault("generation", {})
        versions = self.store.setdefault("versions", {})
        expired: list[dict] = []
        for identity in identities:
            entry = generation.get(str(identity))
            if entry is None:
                raise ValueError(f"identity is not a generation member: {identity}")
            version_id = str(entry["version_id"])
            record = versions[version_id]
            record["status"] = "EXPIRED"
            record["activation_status"] = "EXPIRED"
            record["expire_actor"] = actor
            record["expire_git_sha"] = git_sha
            self.store.setdefault("causes", {})[(str(identity), "expired", version_id)] = True
            generation.pop(str(identity))
            expired.append({"identity": str(identity), "version_id": version_id})
        return {"expired": len(expired), "members": expired}

    def revoke_locked(self, version_ids: list[str], *, actor: str, git_sha: str) -> dict:
        """Core revoke of many ACTIVE versions assuming the caller holds one
        write transaction (composable seam for the facade's batch shape)."""
        versions = self.store.setdefault("versions", {})
        revoked: list[dict] = []
        for version_id in version_ids:
            if version_id not in versions:
                raise ValueError(f"unknown version: {version_id}")
            identity = versions[version_id]["identity"]
            self.store.setdefault("causes", {})[(identity, "revoked", version_id)] = True
            revoked.append({"version_id": version_id, "identity": identity})
        return {"revoked": len(revoked), "members": revoked}

    def replace(
        self,
        change_set: list,
        *,
        actor: str,
        git_sha: str,
        preserve_existing: bool = False,
    ) -> dict:
        """Atomically publish a generation; fail closed on a non-compiling set.

        Beyond the per-component ``_satisfiable``/``GROUP_BUDGET`` checks, the
        whole prospective ACTIVE set is replayed through the compile seam
        (``relation_generation_problem``) before the generation is committed.
        A ``ValueError`` from the seam (merge conflicts or stale capital
        release) blocks every change-set identity that was not already in the
        previous generation (snapshot at method entry) with
        ``ACTIVATION_BLOCKED_INCONSISTENT``; previously-existing members keep
        their original approval and ACTIVE status — the current generation is
        left untouched.
        """
        with self._write():
            self._generation()  # fail closed on any tampered active payload
            previous_generation = dict(self.store.get("generation", {}))
            versions = self.store.setdefault("versions", {})
            entries: list[tuple[str, str, dict]] = []
            for payload in change_set:
                identity, version_fields = _canonicalize(payload)
                version_fp = _fp(version_fields)
                version_id = "v-" + _fp({"identity": identity, "fingerprint": version_fp})
                if version_id not in versions:
                    versions[version_id] = {
                        "payload": payload,
                        "identity": identity,
                        "version_fp": version_fp,
                        "status": "PENDING",
                        "occurrence_count": 1,
                    }
                entries.append((identity, version_id, version_fields))

            if preserve_existing:
                incoming_versions = {
                    identity: version_id for identity, version_id, _ in entries
                }
                preserve_blocked: list[dict[str, str]] = []
                for identity, generation_entry in previous_generation.items():
                    incoming_version_id = incoming_versions.get(identity)
                    if incoming_version_id is not None:
                        if incoming_version_id == generation_entry["version_id"]:
                            continue
                        preserve_blocked.append(
                            {
                                "identity": identity,
                                "reason": "ACTIVATION_BLOCKED_INCONSISTENT",
                            }
                        )
                        entries = [entry for entry in entries if entry[0] != identity]
                        version_id = generation_entry["version_id"]
                        version = versions[version_id]
                        stored_identity, version_fields = _canonicalize(version["payload"])
                        entries.append((stored_identity, version_id, version_fields))
                        continue
                    version_id = generation_entry["version_id"]
                    version = versions[version_id]
                    stored_identity, version_fields = _canonicalize(version["payload"])
                    entries.append((stored_identity, version_id, version_fields))
                if preserve_blocked:
                    return {
                        "status": "ACTIVATION_BLOCKED_INCONSISTENT",
                        "blocked": preserve_blocked,
                    }

            approved_before = dict(self.store.get("approved", {}))
            status_before = {
                version_id: versions[version_id]["status"]
                for _, version_id, _ in entries
            }
            new_generation: dict[str, dict] = {}
            blocked: list[dict[str, str]] = preserve_blocked if preserve_existing else []
            inconsistent = False
            approved_version_fields: dict[str, dict] = {}
            for component in _relation_groups(entries):
                contracts = {
                    contract
                    for entry in component
                    for contract in _entry_contracts(entry)
                }
                reason = None
                if len(contracts) > GROUP_BUDGET:
                    reason = "UNSUPPORTED_SIZE"
                elif not _satisfiable(component):
                    reason = "ACTIVATION_BLOCKED_INCONSISTENT"
                    inconsistent = True
                if reason is not None:
                    for identity, version_id, version_fields in component:
                        previous = previous_generation.get(identity)
                        if (
                            preserve_existing
                            and previous is not None
                            and previous["version_id"] == version_id
                        ):
                            new_generation[identity] = previous
                            approved_version_fields[identity] = version_fields
                        else:
                            blocked.append({"identity": identity, "reason": reason})
                    continue
                approved = self.store.setdefault("approved", {})
                for identity, version_id, version_fields in component:
                    approved[identity] = {
                        "version_id": version_id,
                        "actor": actor,
                        "git_sha": git_sha,
                        "approved_fingerprints": _fingerprints(version_fields),
                    }
                    versions[version_id]["status"] = "APPROVED"
                    new_generation[identity] = {"version_id": version_id, "status": "ACTIVE"}
                    approved_version_fields[identity] = version_fields

            compile_failed = False
            compiled: tuple | None = None
            if new_generation:
                # Full compile precheck over the prospective ACTIVE set. The
                # rows mirror what the compile seam consumes for a live
                # generation (relation_catalog facade's row shape).
                rows = {
                    identity: {
                        "activation": "ACTIVE",
                        "model": {
                            name: version_fields.get(name)
                            for name in ("terminal_states", "payouts", "capital_release", "problem")
                        },
                    }
                    for identity, version_fields in approved_version_fields.items()
                }
                try:
                    compiled = relation_generation_problem(rows)
                except ValueError:
                    # Fail closed: block every identity that was not already
                    # in the previous generation; members of the previous
                    # generation keep their original approval and ACTIVE
                    # status. The store is rolled back to the pre-call
                    # snapshot, so the current generation is unchanged.
                    compile_failed = True
                    inconsistent = True
                    previous_ids = set(previous_generation)
                    blocked.extend(
                        {"identity": identity, "reason": "ACTIVATION_BLOCKED_INCONSISTENT"}
                        for identity in approved_version_fields
                        if identity not in previous_ids
                    )
                    self.store["approved"] = approved_before
                    for version_id, status in status_before.items():
                        versions[version_id]["status"] = status
            event_gate_blocked = False
            if not compile_failed and new_generation:
                # Issue #102: single venue and one event_identity_basis per
                # compiled component, computed with the solver's own merge
                # rule (build_relation_components) over the compiled product
                # the compile precheck already produced. New identities in a
                # violating component are blocked with the precise cause and
                # their per-identity approval/status mutations are rolled
                # back (the pre-call snapshot convention of the compile
                # precheck); previous members stay ACTIVE and clean
                # components in the same batch publish normally.
                _, components = compiled
                contract_info: dict[str, list[tuple[str, str | None]]] = {}
                for _, version_fields in approved_version_fields.items():
                    for endpoint in version_fields["endpoints"]:
                        contract_id = str(endpoint["contract_id"])
                        basis = endpoint.get("event_identity_basis")
                        if not isinstance(basis, str):
                            basis = None
                        contract_info.setdefault(contract_id, []).append(
                            (str(endpoint["venue"]), basis)
                        )
                previous_ids = set(previous_generation)
                version_by_identity = {
                    identity: version_id for identity, version_id, _ in entries
                }
                for component in components:
                    component_contracts = set(component.contract_ids)
                    venues: set[str] = set()
                    bases: set[str] = set()
                    missing: set[str] = set()
                    for contract_id in component_contracts:
                        infos = contract_info.get(contract_id)
                        if not infos:
                            missing.add(contract_id)
                            continue
                        for venue, basis in infos:
                            venues.add(venue)
                            if basis is None:
                                missing.add(contract_id)
                            else:
                                bases.add(basis)
                    reason = None
                    detail = ""
                    if missing:
                        reason = "ACTIVATION_BLOCKED_EVENT_IDENTITY_MISSING"
                        detail = (
                            f"{reason}: contracts without event_identity_basis "
                            f"{json.dumps(sorted(missing))}"
                        )
                    elif len(venues) > 1 or len(bases) > 1:
                        reason = "ACTIVATION_BLOCKED_CROSS_EVENT"
                        detail = (
                            f"{reason}: component contracts "
                            f"{json.dumps(sorted(component_contracts))} venues "
                            f"{json.dumps(sorted(venues))} bases "
                            f"{json.dumps(sorted(map(str, bases)))}"
                        )
                    if reason is None:
                        continue
                    for identity, version_fields in approved_version_fields.items():
                        if identity in previous_ids:
                            continue
                        if not (
                            {str(endpoint["contract_id"]) for endpoint in version_fields["endpoints"]}
                            & component_contracts
                        ):
                            continue
                        blocked.append({
                            "identity": identity,
                            "reason": reason,
                            "detail": detail,
                        })
                        event_gate_blocked = True
                        new_generation.pop(identity, None)
                        if identity in approved_before:
                            self.store.setdefault("approved", {})[identity] = approved_before[identity]
                        else:
                            self.store.setdefault("approved", {}).pop(identity, None)
                        versions[version_by_identity[identity]]["status"] = (
                            status_before[version_by_identity[identity]]
                        )
            if not compile_failed:
                self.store["generation"] = new_generation
            return {
                "status": (
                    "ACTIVATION_BLOCKED_INCONSISTENT"
                    if (inconsistent or event_gate_blocked or (preserve_existing and blocked))
                    else "ACTIVE"
                ),
                "blocked": blocked,
            }

    # -- incremental activation (issue #98) ---------------------------------

    def activate_many(
        self,
        payloads: list,
        *,
        actor: str,
        git_sha: str,
    ) -> dict:
        """Atomically validate and activate many relations in one write txn.

        The judgment mirrors ``replace(preserve_existing=True)`` per entry
        with the oracle's three-layer semantics: the contract-connected
        component of the new payload (via the in-process contract index)
        drives the per-component GROUP_BUDGET / ``_satisfiable`` checks and
        the component compile precheck; the ACTIVE-set aggregates
        (max as_of / min terminal release / valuation units) reproduce the
        whole-set stale-capital and valuation-unit predicates; and the
        contract ∪ compiled-problem-observation-key closure of the new
        payload drives the issue #102 event gate. Untouched ACTIVE components
        keep their approval and generation entries. An identity already ACTIVE
        with a different version blocks with ACTIVATION_BLOCKED_INCONSISTENT;
        the same version is an idempotent no-op. Returns the
        ``replace``-shaped status/blocked pair plus a per-identity ``results``
        map for the facade.
        """
        with self._write():
            result = self._activate_many_locked(payloads, actor=actor, git_sha=git_sha)
        token = result.pop("_token")
        approved_new = result.pop("_approved_new", [])
        self._sync_contract_index(token, approved_new)
        return result

    def _activate_many_locked(
        self,
        payloads: list,
        *,
        actor: str,
        git_sha: str,
    ) -> dict:
        """Core activate_many assuming the write transaction is already held.

        Composable seam for the S3 facade: call inside the facade's own write
        transaction. Review R2: the whole judgment runs under the catalog
        index lock, so the in-process index/aggregate reads (component
        closure, whole-set aggregates) and writes (rebuild, per-entry apply)
        are atomic with respect to other request threads' post-commit
        ``_sync_contract_index`` maintenance; a facade that embeds this
        method simply lets the next call rebuild on the watermark mismatch.
        """
        with self._index_lock:
            return self._activate_many_locked_unlocked(
                payloads, actor=actor, git_sha=git_sha
            )

    def _activate_many_locked_unlocked(
        self,
        payloads: list,
        *,
        actor: str,
        git_sha: str,
    ) -> dict:
        """Core activate_many assuming the write transaction is already held.

        Composable seam for the S3 facade: call inside the facade's own write
        transaction. The index and aggregate state is (re)built from the
        transaction snapshot when the membership watermark moved; a facade
        that embeds this method simply lets the next call rebuild on the
        watermark mismatch. The caller holds ``_index_lock``.
        """
        token = self._index_token()
        if token != self._contract_index_token:
            self._rebuild_contract_index()
            self._contract_index_token = token
        generation = self.store.setdefault("generation", {})
        versions = self.store.setdefault("versions", {})
        approved = self.store.setdefault("approved", {})
        blocked: list[dict[str, str]] = []
        results: dict[str, dict[str, object]] = {}
        inconsistent = False
        event_gate_blocked = False
        approved_new: list[tuple[str, list[str], set[str], object]] = []
        for payload in payloads:
            identity, version_fields = _canonicalize(payload)
            version_fp = _fp(version_fields)
            version_id = "v-" + _fp({"identity": identity, "fingerprint": version_fp})
            if version_id not in versions:
                versions[version_id] = {
                    "payload": payload,
                    "identity": identity,
                    "version_fp": version_fp,
                    "status": "PENDING",
                    "occurrence_count": 1,
                }
            existing = generation.get(identity)
            if existing is not None:
                if existing["version_id"] == version_id:
                    results[identity] = {"version_id": version_id, "status": "ALREADY_ACTIVE"}
                    continue
                reason = "ACTIVATION_BLOCKED_INCONSISTENT"
                blocked.append({"identity": identity, "reason": reason})
                results[identity] = {
                    "version_id": version_id,
                    "status": "BLOCKED",
                    "reason": reason,
                }
                continue
            component = self._affected_component(
                identity, version_id, version_fields, generation, versions
            )
            reason = None
            detail = ""
            contracts = {contract for entry in component for contract in _entry_contracts(entry)}
            if len(contracts) > GROUP_BUDGET:
                reason = "UNSUPPORTED_SIZE"
            elif not _satisfiable(component):
                reason = "ACTIVATION_BLOCKED_INCONSISTENT"
                inconsistent = True
            elif not self._global_activation_ok(version_fields):
                # Whole-set compile predicates a component-only compile cannot
                # observe: stale capital release across contract-disjoint
                # members and one shared valuation unit. replace()'s
                # whole-set precheck blocks the same candidate with
                # ACTIVATION_BLOCKED_INCONSISTENT.
                reason = "ACTIVATION_BLOCKED_INCONSISTENT"
                inconsistent = True
            if reason is None:
                # Compile precheck over the merged component only (the rows
                # mirror replace()'s row shape); a ValueError from the seam
                # blocks this identity with ACTIVATION_BLOCKED_INCONSISTENT.
                rows = {
                    ident: {
                        "activation": "ACTIVE",
                        "model": {
                            name: fields.get(name)
                            for name in ("terminal_states", "payouts", "capital_release", "problem")
                        },
                    }
                    for ident, _, fields in component
                }
                try:
                    relation_generation_problem(rows)
                except ValueError:
                    reason = "ACTIVATION_BLOCKED_INCONSISTENT"
                    inconsistent = True
                if reason is None and rows:
                    # Issue #102 event gate over the event component, the
                    # contract ∪ problem-observation-key closure of this
                    # identity, judged with the same merged components the
                    # compile seam builds (build_relation_components joins
                    # contracts by observation-key fingerprint). A merge
                    # conflict between key-connected members fails the rows
                    # compile, which replace()'s whole-set precheck reports
                    # as ACTIVATION_BLOCKED_INCONSISTENT as well.
                    event_component = self._event_component(
                        identity, version_id, version_fields, generation, versions
                    )
                    event_rows = {
                        ident: {
                            "activation": "ACTIVE",
                            "model": {
                                name: fields.get(name)
                                for name in ("terminal_states", "payouts", "capital_release", "problem")
                            },
                        }
                        for ident, _, fields in event_component
                    }
                    try:
                        _, components = relation_generation_problem(event_rows)
                    except ValueError:
                        reason = "ACTIVATION_BLOCKED_INCONSISTENT"
                        inconsistent = True
                    if reason is None:
                        contract_info: dict[str, list[tuple[str, str | None]]] = {}
                        for _, _, fields in event_component:
                            for endpoint in fields["endpoints"]:
                                contract_id = str(endpoint["contract_id"])
                                basis = endpoint.get("event_identity_basis")
                                if not isinstance(basis, str):
                                    basis = None
                                contract_info.setdefault(contract_id, []).append(
                                    (str(endpoint["venue"]), basis)
                                )
                        identity_contract_ids = {
                            str(endpoint["contract_id"]) for endpoint in version_fields["endpoints"]
                        }
                        for component_obj in components:
                            component_contracts = set(component_obj.contract_ids)
                            venues: set[str] = set()
                            bases: set[str] = set()
                            missing: set[str] = set()
                            for contract_id in component_contracts:
                                infos = contract_info.get(contract_id)
                                if not infos:
                                    missing.add(contract_id)
                                    continue
                                for venue, basis in infos:
                                    venues.add(venue)
                                    if basis is None:
                                        missing.add(contract_id)
                                    else:
                                        bases.add(basis)
                            gate_reason = None
                            gate_detail = ""
                            if missing:
                                gate_reason = "ACTIVATION_BLOCKED_EVENT_IDENTITY_MISSING"
                                gate_detail = (
                                    f"{gate_reason}: contracts without event_identity_basis "
                                    f"{json.dumps(sorted(missing))}"
                                )
                            elif len(venues) > 1 or len(bases) > 1:
                                gate_reason = "ACTIVATION_BLOCKED_CROSS_EVENT"
                                gate_detail = (
                                    f"{gate_reason}: component contracts "
                                    f"{json.dumps(sorted(component_contracts))} venues "
                                    f"{json.dumps(sorted(venues))} bases "
                                    f"{json.dumps(sorted(map(str, bases)))}"
                                )
                            if gate_reason is not None and identity_contract_ids & component_contracts:
                                blocked.append({
                                    "identity": identity,
                                    "reason": gate_reason,
                                    "detail": gate_detail,
                                })
                                event_gate_blocked = True
                                reason = gate_reason
                                detail = gate_detail
            if reason is not None:
                if not detail:
                    blocked.append({"identity": identity, "reason": reason})
                results[identity] = {
                    "version_id": version_id,
                    "status": "BLOCKED",
                    "reason": reason,
                    **( {"detail": detail} if detail else {}),
                }
                continue
            approved[identity] = {
                "version_id": version_id,
                "actor": actor,
                "git_sha": git_sha,
                "approved_fingerprints": _fingerprints(version_fields),
            }
            versions[version_id]["status"] = "APPROVED"
            generation[identity] = {"version_id": version_id, "status": "ACTIVE"}
            results[identity] = {"version_id": version_id, "status": "APPROVED"}
            index_aggregate = _problem_aggregate(version_fields)
            if index_aggregate is _INVALID_PROBLEM_DATES:
                # Unreachable for approved identities (the sentinel blocks
                # before approval); stay defensive and skip the contribution.
                index_aggregate = None
            index_entry = (
                identity,
                [_canonical_endpoint(endpoint) for endpoint in version_fields["endpoints"]],
                _problem_observation_keys(version_fields),
                index_aggregate,
            )
            approved_new.append(index_entry)
            # Review R1: publish this entry's index contribution immediately
            # so later entries in the same batch see earlier batch members in
            # the component closure, the event closure and the whole-set
            # aggregates (matching the sequential per-entry judgment).
            self._apply_index_entry(*index_entry)
        return {
            "status": (
                "ACTIVATION_BLOCKED_INCONSISTENT"
                if (inconsistent or event_gate_blocked or blocked)
                else "ACTIVE"
            ),
            "blocked": blocked,
            "results": results,
            "_token": token,
            "_approved_new": approved_new,
        }

    def _affected_component(
        self,
        identity: str,
        version_id: str,
        version_fields: dict,
        generation: dict,
        versions: MutableMapping,
    ) -> list[tuple[str, str, dict]]:
        """Contract-connected closure of a new identity.

        Seed with the new payload's canonical endpoints, expand through the
        contract index to ACTIVE identities sharing a contract, load their
        stored payloads, and repeat until convergence (GROUP_BUDGET bounds
        active components, so the closure stays small). This is the closure
        the per-component checks (GROUP_BUDGET / ``_satisfiable``) and the
        component compile precheck judge, mirroring replace()'s
        ``_relation_groups`` contract groups.
        """
        entries = [(identity, version_id, version_fields)]
        seen: set[str] = {identity}
        frontier = list(version_fields["endpoints"])
        while frontier:
            contract = _canonical_endpoint(frontier.pop())
            for other in self._contract_index.get(contract, ()):
                if other in seen:
                    continue
                seen.add(other)
                entry = generation.get(other)
                if entry is None:
                    continue  # index is rebuilt on watermark mismatch; stay defensive
                other_version = versions[entry["version_id"]]
                other_identity, other_fields = _canonicalize(other_version["payload"])
                entries.append((other_identity, entry["version_id"], other_fields))
                frontier.extend(other_fields["endpoints"])
        return entries

    def _event_component(
        self,
        identity: str,
        version_id: str,
        version_fields: dict,
        generation: dict,
        versions: MutableMapping,
    ) -> list[tuple[str, str, dict]]:
        """Contract- and observation-key-connected closure of a new identity.

        The #102 event gate judges the same merged component the compile seam
        builds (``build_relation_components`` joins contracts by their
        observation-key fingerprint and by relations), so the gate's component
        expands through the contract index and the problem-key index together:
        members reachable only through a shared compiled-problem observation
        key are included even when contract-disjoint. Neighbors' keys are read
        on demand from their payloads' problems (the closure is
        GROUP_BUDGET-bounded), while the key index supplies the ACTIVE
        membership lookup for each key.
        """
        entries = [(identity, version_id, version_fields)]
        seen: set[str] = {identity}
        frontier_contracts = list(version_fields["endpoints"])
        frontier_keys = _problem_observation_keys(version_fields)
        while frontier_contracts or frontier_keys:
            while frontier_contracts:
                contract = _canonical_endpoint(frontier_contracts.pop())
                for other in self._contract_index.get(contract, ()):
                    if other in seen:
                        continue
                    seen.add(other)
                    entry = generation.get(other)
                    if entry is None:
                        continue  # index is rebuilt on watermark mismatch; stay defensive
                    other_version = versions[entry["version_id"]]
                    other_identity, other_fields = _canonicalize(other_version["payload"])
                    entries.append((other_identity, entry["version_id"], other_fields))
                    frontier_contracts.extend(other_fields["endpoints"])
                    frontier_keys |= _problem_observation_keys(other_fields)
            while frontier_keys:
                key = frontier_keys.pop()
                for other in self._key_index.get(key, ()):
                    if other in seen:
                        continue
                    seen.add(other)
                    entry = generation.get(other)
                    if entry is None:
                        continue  # index is rebuilt on watermark mismatch; stay defensive
                    other_version = versions[entry["version_id"]]
                    other_identity, other_fields = _canonicalize(other_version["payload"])
                    entries.append((other_identity, entry["version_id"], other_fields))
                    frontier_contracts.extend(other_fields["endpoints"])
                    frontier_keys |= _problem_observation_keys(other_fields)
        return entries

    def _global_activation_ok(self, version_fields: dict) -> bool:
        """Whole-set compile predicates a component-only compile cannot see.

        The compile seam merges every ACTIVE problem and validates the merged
        problem: a candidate whose as_of postdates any ACTIVE terminal release
        (or whose earliest terminal release predates any ACTIVE as_of) makes
        the merged problem stale (``STALE_CAPITAL_RELEASE_AT``), and a
        candidate with a valuation unit different from the ACTIVE set fails
        the merge ("compiled problems must share one valuation unit").
        replace() blocks such candidates with
        ACTIVATION_BLOCKED_INCONSISTENT via its whole-set precheck; here the
        same judgment comes from the ACTIVE-set aggregates.
        """
        aggregate = _problem_aggregate(version_fields)
        if aggregate is _INVALID_PROBLEM_DATES:
            # R2: dates present but unparseable or timezone-less (naive) are
            # predicate failures — mirroring replace()'s compile-seam
            # rejection of the same payload — and block the candidate with
            # ACTIVATION_BLOCKED_INCONSISTENT; fully absent dates keep the
            # legacy skip below.
            return False
        if aggregate is None:
            return True
        candidate_as_of, candidate_min_release, candidate_unit = aggregate
        state = self._aggregates
        if state["units"] and candidate_unit not in state["units"]:
            return False
        if state["max_as_of"] is not None and state["max_as_of"] > candidate_min_release:
            return False
        if state["min_release"] is not None and candidate_as_of > state["min_release"]:
            return False
        return True

    def _index_token(self) -> object:
        """Store membership watermark at this instant."""
        rowid_max = getattr(self.store, "generation_rowid_max", None)
        if rowid_max is not None:
            return int(rowid_max())
        return int(self.store.get("generation_number", 0))

    def _index_delta(self, changed: bool) -> int:
        """How far this process's own write txn moves the watermark.

        SQLite stores append exactly one generations row when membership
        changed (and none otherwise); dict stores bump generation_number on
        every write txn.
        """
        if getattr(self.store, "generation_rowid_max", None) is not None:
            return 1 if changed else 0
        return 1

    def _rebuild_contract_index(self) -> None:
        """Build contract/observation-key -> ACTIVE identities plus the ACTIVE
        aggregates from the current generation payloads (one watermark guards
        all of them). The caller holds ``_index_lock``."""
        index: dict[str, set[str]] = {}
        key_index: dict[str, set[str]] = {}
        max_as_of: datetime | None = None
        min_release: datetime | None = None
        units: set[str] = set()
        generation = self.store.get("generation", {})
        versions = self.store.get("versions", {})
        for identity, entry in generation.items():
            version = versions[entry["version_id"]]
            payload = version["payload"]
            for endpoint in payload["endpoints"]:
                index.setdefault(_canonical_endpoint(endpoint), set()).add(identity)
            for key in _problem_observation_keys(payload):
                key_index.setdefault(key, set()).add(identity)
            aggregate = _problem_aggregate(payload)
            if aggregate is None or aggregate is _INVALID_PROBLEM_DATES:
                continue
            as_of, release, unit = aggregate
            max_as_of = as_of if max_as_of is None else max(max_as_of, as_of)
            min_release = release if min_release is None else min(min_release, release)
            units.add(unit)
        self._contract_index = index
        self._key_index = key_index
        self._aggregates = {
            "max_as_of": max_as_of,
            "min_release": min_release,
            "units": frozenset(units),
        }

    def _apply_index_entry(
        self, identity: str, contracts: list[str], keys: set[str], aggregate: object
    ) -> None:
        """Incrementally add one approved identity to the in-process index.

        Shared by the post-commit sync and the per-entry batch-internal
        publish in ``_activate_many_locked``; every contribution is
        idempotent (set membership, max/min, unit union), so applying the
        same entry twice is harmless. The caller holds ``_index_lock`` (the
        read-modify-writes on the shared aggregates must be atomic).
        """
        for contract in contracts:
            self._contract_index.setdefault(contract, set()).add(identity)
        for key in keys:
            self._key_index.setdefault(key, set()).add(identity)
        if aggregate is not None:
            as_of, release, unit = aggregate
            state = self._aggregates
            state["max_as_of"] = (
                as_of if state["max_as_of"] is None else max(state["max_as_of"], as_of)
            )
            state["min_release"] = (
                release
                if state["min_release"] is None
                else min(state["min_release"], release)
            )
            state["units"] = frozenset(state["units"] | {unit})

    def _invalidate_contract_index(self) -> None:
        """Discard the in-process index after a rolled-back write transaction.

        Review R1: batch-internal index increments from a failed transaction
        must not survive; the next activation rebuilds from the committed
        snapshot on the watermark mismatch.
        """
        with self._index_lock:
            self._contract_index_token = None

    def _sync_contract_index(self, token: object, approved_new: list) -> None:
        """Post-commit index maintenance for this process's own changes.

        Incrementally add the approved identities' contract and problem
        observation-key mappings plus their aggregate contribution and sync
        the watermark only when no other writer interleaved (the watermark
        moved exactly by this txn's own delta); otherwise rebuild from the
        committed state so foreign membership changes are never missed.
        Review R2: runs under the catalog index lock (a post-commit window
        holds no write transaction, so another request thread's in-transaction
        apply could otherwise interleave read-modify-writes here).
        """
        with self._index_lock:
            current = self._index_token()
            if current != token + self._index_delta(bool(approved_new)):
                self._rebuild_contract_index()
                self._contract_index_token = current
                return
            for identity, contracts, keys, aggregate in approved_new:
                self._apply_index_entry(identity, contracts, keys, aggregate)
            self._contract_index_token = current

    def authoritative_reconcile(
        self, producer: str, scope: str, complete_facts: list
    ) -> dict:
        # ponytail: producer/scope labels are recorded but not enforced (payload has no producer/scope field); enforce when #52 adds a second producer.
        with self._write():
            causes = self.store.setdefault("causes", {})
            known: set[str] = set()
            for fact in complete_facts:
                try:
                    known.add(_canonicalize(fact)[0])
                except ValueError:
                    continue  # incomplete/invalid facts cannot clear a cause
            for identity in list(self.store.get("generation", {})):
                key = (identity, producer, scope)
                if identity in known:
                    causes.pop(key, None)
                else:
                    causes[key] = True
            return {"reconciled": f"{producer}:{scope}"}

    # -- reads -------------------------------------------------------------

    def current_generation(self) -> dict[str, dict]:
        return self._generation()

    def admit(self, producer_facts: object) -> bool:
        try:
            identity = _canonicalize(producer_facts)[0]
            entry = self._generation().get(identity)
            if entry is None or entry["status"] != "ACTIVE":
                return False
            frozen = self.store["approved"][identity]
            _, version_fields = _canonicalize(producer_facts)
            return _fingerprints(version_fields) == frozen["approved_fingerprints"]
        except (KeyError, TypeError, ValueError):
            return False

    # -- internals ---------------------------------------------------------

    def _generation(self) -> dict[str, dict]:
        """Snapshot of active versions with tamper freeze check and UNKNOWN status."""
        with self._read():
            generation = self.store.get("generation", {})
            approved = self.store.get("approved", {})
            versions = self.store.get("versions", {})
            causes = self.store.get("causes", {})
            result: dict[str, dict] = {}
            for identity, entry in generation.items():
                version_id = entry["version_id"]
                frozen = approved.get(identity)
                version = versions.get(version_id)
                if frozen is None or version is None:
                    raise ValueError(f"generation invariant violated for {identity}")
                stored_identity, version_fields = _canonicalize(version["payload"])
                if stored_identity != identity or _fingerprints(version_fields) != frozen["approved_fingerprints"]:
                    raise ValueError(f"tampered payload for {identity}")
                status = "UNKNOWN" if _relation_group_unknown(identity, generation, versions, causes) else "ACTIVE"
                result[identity] = {"version_id": version_id, "status": status}
            return result


def _entry_contracts(entry: tuple[str, str, dict]) -> set[str]:
    return {_canonical_endpoint(endpoint) for endpoint in entry[2]["endpoints"]}


def _problem_observation_keys(version_fields: object) -> set[str]:
    """Settlement observation keys of a compiled problem's terminal state sets.

    Mirrors the compile seam's identity join source
    (``build_relation_components`` joins contracts by
    ``fingerprint(TerminalStateSet.settlement_observation_key)``), read from
    the compiled problem payload along the same field paths as
    ``problem_from_payload`` (``problem.terminal_state_sets[].settlement_
    observation_key``); payloads without a compiled problem carry no keys and
    never join through this dimension.
    """
    keys: set[str] = set()
    if not isinstance(version_fields, dict):
        return keys
    problem = version_fields.get("problem")
    if not isinstance(problem, dict):
        return keys
    for state in problem.get("terminal_state_sets", ()):
        if not isinstance(state, dict):
            continue
        key = state.get("settlement_observation_key")
        if isinstance(key, dict):
            keys.add(_fp(key))
        elif isinstance(key, str) and key:
            keys.add(key)
    return keys


def _parse_datetime(value: object) -> datetime | None:
    """Parse a canonical UTC ISO datetime string (``...Z`` or ``+00:00``).

    Timezone-less ("naive") strings and unparseable values return None; the
    caller distinguishes an absent key (legacy skip) from a present but
    invalid value (predicate failure) by key presence.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


#: Sentinel returned by ``_problem_aggregate`` when a date key is present but
#: its value is unparseable or timezone-less (naive): a predicate failure that
#: must block the candidate with ACTIVATION_BLOCKED_INCONSISTENT (mirroring
#: replace()'s compile-seam rejection of the same payload) instead of
#: silently skipping the check.
_INVALID_PROBLEM_DATES = object()


def _problem_aggregate(version_fields: object) -> tuple[datetime, datetime, str] | object | None:
    """(as_of, earliest terminal release, valuation unit) of a compiled problem.

    Raw-payload read along the same field paths as ``problem_from_payload``
    (``problem.as_of``, ``problem.valuation_unit_id``, per-terminal-state atom
    ``capital_release_at``); payloads without a compiled problem contribute
    nothing to the aggregate guard. R2: a date key that IS present but whose
    value is unparseable or timezone-less returns the ``_INVALID_PROBLEM_DATES``
    sentinel; fully absent dates keep the legacy ``None`` (skip) behavior.
    """
    if not isinstance(version_fields, dict):
        return None
    problem = version_fields.get("problem")
    if not isinstance(problem, dict):
        return None
    if "as_of" in problem and _parse_datetime(problem.get("as_of")) is None:
        return _INVALID_PROBLEM_DATES
    for state in problem.get("terminal_state_sets", ()):
        if not isinstance(state, dict):
            continue
        for atom in state.get("atoms", ()):
            if isinstance(atom, dict) and "capital_release_at" in atom:
                if _parse_datetime(atom.get("capital_release_at")) is None:
                    return _INVALID_PROBLEM_DATES
    as_of = _parse_datetime(problem.get("as_of"))
    unit = problem.get("valuation_unit_id")
    releases: list[datetime] = []
    for state in problem.get("terminal_state_sets", ()):
        if not isinstance(state, dict):
            continue
        for atom in state.get("atoms", ()):
            release = (
                _parse_datetime(atom.get("capital_release_at"))
                if isinstance(atom, dict)
                else None
            )
            if release is not None:
                releases.append(release)
    if as_of is None or not isinstance(unit, str) or not releases:
        return None
    return as_of, min(releases), unit


def _relation_groups(entries: list[tuple[str, str, dict]]) -> list[list[tuple[str, str, dict]]]:
    remaining = list(entries)
    components: list[list[tuple[str, str, dict]]] = []
    while remaining:
        seed = remaining.pop(0)
        component = [seed]
        seed_contracts = _entry_contracts(seed)
        changed = True
        while changed:
            changed = False
            for entry in list(remaining):
                if seed_contracts & _entry_contracts(entry):
                    component.append(entry)
                    seed_contracts |= _entry_contracts(entry)
                    remaining.remove(entry)
                    changed = True
        components.append(component)
    return components


def _satisfiable(component: list[tuple[str, str, dict]]) -> bool:
    """Bounded boolean enumeration over the component's contract atoms."""
    # ponytail: boolean YES/NO only; VOID/REFUND/SPLIT terminal semantics deferred to #52 N-leg oracle.
    atoms = sorted(
        {contract for entry in component for contract in _entry_contracts(entry)}
    )
    index = {atom: i for i, atom in enumerate(atoms)}
    for mask in range(1 << len(atoms)):
        if all(_relation_holds(entry, mask, index) for entry in component):
            return True
    return False


def _relation_holds(entry: tuple[str, str, dict], mask: int, index: dict[str, int]) -> bool:
    relation_type = entry[2]["relation_type"]
    contracts = [
        _canonical_endpoint(endpoint)
        for endpoint in entry[2]["endpoints"]
    ]
    truth = {contract: bool(mask & (1 << index[contract])) for contract in contracts}
    values = [truth[contract] for contract in contracts]
    if relation_type == "IMPLIES":
        return not values[0] or values[1]
    if relation_type == "MUTUALLY_EXCLUSIVE":
        return sum(values) <= 1
    if relation_type == "NATIVE_COMPLEMENT":
        return sum(values) == 1
    return sum(values) == 1  # EXACTLY_ONE


def _relation_group_unknown(
    identity: str,
    generation: dict[str, dict],
    versions: MutableMapping,
    causes: MutableMapping,
) -> bool:
    if not causes:
        return False
    component_ids = _relation_group_ids(identity, generation, versions)
    return any(cause_identity in component_ids for (cause_identity, *_) in causes)


def _relation_group_ids(
    identity: str, generation: dict[str, dict], versions: MutableMapping
) -> set[str]:
    id_contracts: dict[str, set[str]] = {}
    for ident, entry in generation.items():
        payload = versions[entry["version_id"]]["payload"]
        id_contracts[ident] = {
            _canonical_endpoint(endpoint) for endpoint in payload["endpoints"]
        }
    component = {identity}
    frontier = [identity]
    while frontier:
        current = frontier.pop()
        for other, contracts in id_contracts.items():
            if other not in component and contracts & id_contracts[current]:
                component.add(other)
                frontier.append(other)
    return component
