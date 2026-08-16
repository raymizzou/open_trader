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
from collections.abc import Iterator, MutableMapping
from contextlib import contextmanager
from datetime import datetime, timezone

ALLOWED_VENUES = frozenset({"polymarket", "predict.fun"})
RELATION_TYPES = frozenset({"IMPLIES", "MUTUALLY_EXCLUSIVE", "EXACTLY_ONE"})
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
    if relation_type == "IMPLIES" and len(endpoints) != 2:
        raise ValueError("IMPLIES requires exactly two endpoints")

    sigs = [_canonical_endpoint(endpoint) for endpoint in endpoints]
    ordered = sigs if relation_type == "IMPLIES" else sorted(sigs)
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


class SqliteCatalogStore(MutableMapping):
    """SQLite-backed persistence seam for ``RelationCatalogV2``.

    Implements the same four logical keys (versions / approved / generation /
    causes) over four new ``catalog_v2_*`` tables. Writes are buffered in an
    overlay inside one ``BEGIN IMMEDIATE`` transaction and flushed together
    with a new append-only generation snapshot; ``begin_read``/``end_read``
    give callers one consistent committed snapshot. Legacy
    ``relation_catalog_*`` v1 tables are never read or written.
    """

    _VERSION_COLUMNS = frozenset({
        "payload", "identity", "version_fp", "status", "occurrence_count",
        "activation_status", "activation_diagnostic",
    })

    def __init__(self, db_path: str | os.PathLike[str]) -> None:
        # Callers are serialized by RelationCatalogV2's lock, so one connection
        # may be shared across threads (pooled readers/writers in tests).
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.isolation_level = None  # manual transactions
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._create_tables()
        self._overlay: dict[str, dict] | None = None
        self._cache: dict[str, dict] | None = None

    def _create_tables(self) -> None:
        self._conn.executescript(
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
            """
        )
        # Pre-existing v2 databases created before the facade's activation
        # metadata columns; keep additive so old data stays readable.
        for column in ("activation_status TEXT", "activation_diagnostic TEXT"):
            try:
                self._conn.execute(
                    f"ALTER TABLE catalog_v2_versions ADD COLUMN {column}"
                )
            except sqlite3.OperationalError:
                pass

    # -- transactions ------------------------------------------------------

    def begin_write(self) -> None:
        if self._overlay is not None:
            raise RuntimeError("nested write transaction")
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._overlay = self._load_state()
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._cache = None

    def commit_write(self) -> None:
        if self._overlay is None:
            raise RuntimeError("no active write transaction")
        self._flush(self._overlay)
        self._conn.execute("COMMIT")
        self._overlay = None

    def rollback_write(self) -> None:
        if self._overlay is None:
            return
        self._conn.execute("ROLLBACK")
        self._overlay = None
        self._cache = None

    def begin_read(self) -> None:
        if self._overlay is not None or self._cache is not None:
            return
        self._conn.execute("BEGIN")
        try:
            self._cache = self._load_state()
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    def end_read(self) -> None:
        self._cache = None

    # -- state materialization --------------------------------------------

    def _state(self) -> dict[str, dict]:
        if self._overlay is not None:
            return self._overlay
        if self._cache is not None:
            return self._cache
        self._conn.execute("BEGIN")
        try:
            state = self._load_state()
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")
        return state

    def _load_state(self) -> dict[str, dict]:
        row = self._conn.execute(
            "SELECT members FROM catalog_v2_generations ORDER BY generation_id DESC LIMIT 1"
        ).fetchone()
        generation: dict[str, dict] = json.loads(row[0]) if row else {}
        versions: dict[str, dict] = {}
        for version_id, identity, version_fp, payload, status, occurrence_count, activation_status, activation_diagnostic, meta in self._conn.execute(
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
        for identity, version_id, approved_fingerprint, actor, git_sha in self._conn.execute(
            "SELECT identity, version_id, approved_fingerprint, actor, git_sha FROM catalog_v2_approvals"
        ):
            approved[identity] = {
                "version_id": version_id,
                "approved_fingerprints": approved_fingerprint,
                "actor": actor,
                "git_sha": git_sha,
            }
        causes: dict[tuple[str, str, str], bool] = {}
        for identity, producer, scope in self._conn.execute(
            "SELECT identity, producer, scope FROM catalog_v2_causes"
        ):
            causes[(identity, producer, scope)] = True
        latest: dict[str, str] = {}
        for identity, version_id in self._conn.execute(
            "SELECT identity, version_id FROM catalog_v2_latest"
        ):
            latest[identity] = version_id
        return {
            "versions": versions,
            "approved": approved,
            "generation": generation,
            "causes": causes,
            "latest": latest,
        }

    def _flush(self, state: dict[str, dict]) -> None:
        now = _now()
        versions, approved, generation, causes, latest = (
            state["versions"],
            state["approved"],
            state["generation"],
            state["causes"],
            state.get("latest", {}),
        )
        self._conn.execute("DELETE FROM catalog_v2_versions")
        for version_id, record in versions.items():
            meta = {k: v for k, v in record.items() if k not in self._VERSION_COLUMNS}
            self._conn.execute(
                "INSERT INTO catalog_v2_versions "
                "(version_id, identity, version_fp, payload, status, occurrence_count, activation_status, activation_diagnostic, created_at, updated_at, meta) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
        self._conn.execute("DELETE FROM catalog_v2_approvals")
        for identity, record in approved.items():
            self._conn.execute(
                "INSERT INTO catalog_v2_approvals "
                "(identity, version_id, approved_fingerprint, actor, git_sha, approved_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    identity,
                    record["version_id"],
                    record["approved_fingerprints"],
                    record["actor"],
                    record["git_sha"],
                    now,
                ),
            )
        self._conn.execute("DELETE FROM catalog_v2_causes")
        for (identity, producer, scope), _ in causes.items():
            self._conn.execute(
                "INSERT INTO catalog_v2_causes (identity, producer, scope, created_at) VALUES (?, ?, ?, ?)",
                (identity, producer, scope, now),
            )
        self._conn.execute("DELETE FROM catalog_v2_latest")
        for identity, version_id in latest.items():
            self._conn.execute(
                "INSERT INTO catalog_v2_latest (identity, version_id) VALUES (?, ?)",
                (identity, version_id),
            )
        self._conn.execute(
            "INSERT INTO catalog_v2_generations (members, created_at) VALUES (?, ?)",
            (json.dumps(generation, sort_keys=True), now),
        )

    # -- MutableMapping ----------------------------------------------------

    def __getitem__(self, key: str) -> dict:
        return self._state()[key]

    def __setitem__(self, key: str, value: object) -> None:
        if self._overlay is None:
            raise RuntimeError("writes require a write transaction")
        self._overlay[key] = value  # type: ignore[assignment]

    def __delitem__(self, key: str) -> None:
        if self._overlay is None:
            raise RuntimeError("writes require a write transaction")
        del self._overlay[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._state())

    def __len__(self) -> int:
        return len(self._state())


class RelationCatalogV2:
    """In-memory v2 relation catalog.

    The optional ``store`` is a persistence seam (dict in tests, SQLite later).
    A single lock protects all mutations and the current_generation snapshot.
    """

    def __init__(self, store: MutableMapping | None = None) -> None:
        self.store: MutableMapping = store if store is not None else {}
        self._lock = threading.Lock()  # ponytail: global lock; shard if throughput matters

    @contextmanager
    def _write(self) -> Iterator[None]:
        begin = getattr(self.store, "begin_write", None)
        if begin is None:
            yield
            return
        begin()
        try:
            yield
            self.store.commit_write()
        except BaseException:
            self.store.rollback_write()
            raise

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
        with self._lock:
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
        with self._lock:
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
        with self._lock:
            with self._write():
                versions = self.store.setdefault("versions", {})
                if version_id not in versions:
                    raise ValueError(f"unknown version: {version_id}")
                versions[version_id]["status"] = "REJECTED"
                versions[version_id]["reject_reason"] = reason
                versions[version_id]["reject_note"] = note
                return {"version_id": version_id, "status": "REJECTED"}

    def revoke(self, version_id: str, *, actor: str, git_sha: str) -> dict:
        with self._lock:
            with self._write():
                versions = self.store.setdefault("versions", {})
                if version_id not in versions:
                    raise ValueError(f"unknown version: {version_id}")
                identity = versions[version_id]["identity"]
                self.store.setdefault("causes", {})[(identity, "revoked", version_id)] = True
                return {"version_id": version_id, "identity": identity, "status": "UNKNOWN"}

    def replace(self, change_set: list, *, actor: str, git_sha: str) -> dict:
        with self._lock:
            with self._write():
                self._generation()  # fail closed on any tampered active payload
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

                new_generation: dict[str, dict] = {}
                blocked: list[dict[str, str]] = []
                inconsistent = False
                for component in _relation_groups(entries):
                    contracts = {
                        contract
                        for entry in component
                        for contract in _entry_contracts(entry)
                    }
                    if len(contracts) > GROUP_BUDGET:
                        blocked.extend(
                            {"identity": identity, "reason": "UNSUPPORTED_SIZE"}
                            for identity, _, _ in component
                        )
                        continue
                    if not _satisfiable(component):
                        inconsistent = True
                        blocked.extend(
                            {"identity": identity, "reason": "ACTIVATION_BLOCKED_INCONSISTENT"}
                            for identity, _, _ in component
                        )
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
                self.store["generation"] = new_generation
                return {
                    "status": "ACTIVATION_BLOCKED_INCONSISTENT" if inconsistent else "ACTIVE",
                    "blocked": blocked,
                }

    def authoritative_reconcile(
        self, producer: str, scope: str, complete_facts: list
    ) -> dict:
        # ponytail: producer/scope labels are recorded but not enforced (payload has no producer/scope field); enforce when #52 adds a second producer.
        with self._lock:
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
        with self._lock:
            return self._generation()

    def admit(self, producer_facts: object) -> bool:
        with self._lock:
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
