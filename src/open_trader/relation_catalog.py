"""Versioned admission catalog for production-discovered market relations."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator, Mapping
from uuid import uuid4


_SCHEMA = "open_trader.relation_catalog.v1"
_REASONS = frozenset({
    "source_evidence_insufficient", "relation_semantics_wrong",
    "model_incomplete_or_wrong", "identity_mismatch", "rules_changed", "other",
})
_COMPLETENESS = frozenset({"COMPLETE", "INCOMPLETE"})
_RELATION_TYPES = frozenset({"IMPLIES", "MUTUALLY_EXCLUSIVE", "EXACTLY_ONE"})
_ACTIVATION_BLOCKED = frozenset({"ACTIVATION_BLOCKED_INCONSISTENT", "UNSUPPORTED_SIZE"})
_BUDGET = 10


class RelationConflictError(ValueError):
    """The reviewed version is not the catalog version the operator opened."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _timestamp(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _object(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    return {str(key): item for key, item in value.items()}


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    return value.strip()


def _fact(value: object) -> object:
    """Exclude occurrence timestamps from the stable evidence identity."""
    if isinstance(value, Mapping):
        return {
            str(key): _fact(item)
            for key, item in value.items()
            if str(key) not in {"observed_at", "discovered_at", "seen_at", "updated_at"}
        }
    if isinstance(value, (list, tuple)):
        return [_fact(item) for item in value]
    return value


def _normalise_discovery(value: Mapping[str, object]) -> dict[str, object]:
    allowed = {
        "discovery_source", "discovered_at", "relation_type", "semantics",
        "source_evidence", "model", "markets",
    }
    if set(value) != allowed:
        raise ValueError("relation discovery fields are invalid")
    source = _string(value["discovery_source"], "discovery_source")
    relation_type = _string(value["relation_type"], "relation_type")
    if relation_type not in _RELATION_TYPES:
        raise ValueError("relation_type is invalid")
    semantics = _object(value["semantics"], "semantics")
    if not _string(semantics.get("statement"), "semantics.statement"):
        raise ValueError("semantics.statement is required")
    evidence_value = value["source_evidence"]
    if not isinstance(evidence_value, list) or not evidence_value:
        raise ValueError("source_evidence must be a non-empty array")
    evidence = [_object(item, "source_evidence item") for item in evidence_value]
    model = _object(value["model"], "model")
    completeness = _string(model.get("completeness"), "model.completeness")
    if completeness not in _COMPLETENESS:
        raise ValueError("model.completeness is invalid")
    if completeness == "COMPLETE":
        for name in ("terminal_states", "payouts", "capital_release"):
            if name not in model or model[name] in (None, "", []):
                raise ValueError(f"model.{name} is required for COMPLETE")
    raw_markets = value["markets"]
    if not isinstance(raw_markets, list) or len(raw_markets) < 2:
        raise ValueError("markets must contain at least two endpoints")
    markets: list[dict[str, object]] = []
    for raw in raw_markets:
        market = _object(raw, "market")
        required = {
            "venue", "contract_id", "title", "market_date", "expires_at",
            "event_identity_basis", "settlement_observation_key", "settlement_rules",
            "cancellation_rules",
        }
        if set(market) != required:
            raise ValueError("market fields are invalid")
        clean = {name: _string(market[name], f"market.{name}") for name in required - {"market_date", "expires_at"}}
        clean["market_date"] = _timestamp(market["market_date"], "market.market_date")
        clean["expires_at"] = _timestamp(market["expires_at"], "market.expires_at")
        markets.append(clean)
    endpoints = sorted((str(item["venue"]).casefold(), str(item["contract_id"])) for item in markets)
    if len(set(endpoints)) != len(endpoints):
        raise ValueError("market endpoints must be unique")
    return {
        "schema_version": _SCHEMA,
        "discovery_source": source,
        "discovered_at": _timestamp(value["discovered_at"], "discovered_at"),
        "relation_type": relation_type,
        "semantics": semantics,
        "source_evidence": evidence,
        "model": model,
        "markets": sorted(markets, key=lambda item: (str(item["venue"]).casefold(), str(item["contract_id"]))),
        "relation_id": f"relation:{_digest(endpoints)}",
    }


class RelationCatalog:
    """SQLite-backed public domain API; readers consume only `current_generation`."""

    def __init__(self, data_dir: Path, *, component_budget: int = _BUDGET) -> None:
        if type(component_budget) is not int or component_budget < 2:
            raise ValueError("component_budget must be at least two")
        self.path = Path(data_dir) / "prediction_arbitrage" / "prediction_arbitrage.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.component_budget = component_budget
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS relation_catalog_versions (
                    relation_version_id TEXT PRIMARY KEY,
                    relation_id TEXT NOT NULL,
                    previous_relation_version_id TEXT,
                    source_fingerprint TEXT NOT NULL,
                    semantics_fingerprint TEXT NOT NULL,
                    model_fingerprint TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    approval_status TEXT NOT NULL,
                    activation_status TEXT NOT NULL,
                    activation_diagnostic TEXT,
                    active_generation INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS relation_catalog_version_identity
                ON relation_catalog_versions(relation_id, source_fingerprint, semantics_fingerprint, model_fingerprint);
                CREATE INDEX IF NOT EXISTS relation_catalog_versions_relation
                ON relation_catalog_versions(relation_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS relation_catalog_evidence (
                    occurrence_id TEXT PRIMARY KEY,
                    relation_version_id TEXT NOT NULL REFERENCES relation_catalog_versions(relation_version_id),
                    payload TEXT NOT NULL,
                    observed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS relation_catalog_audit (
                    audit_id TEXT PRIMARY KEY,
                    relation_version_id TEXT,
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    git_sha TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS relation_catalog_generations (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    generation INTEGER NOT NULL,
                    unknown_components TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                INSERT INTO relation_catalog_generations(singleton, generation, unknown_components, updated_at)
                VALUES(1, 0, '[]', '') ON CONFLICT(singleton) DO NOTHING;
                CREATE TABLE IF NOT EXISTS relation_catalog_generation_members (
                    generation INTEGER NOT NULL,
                    relation_id TEXT NOT NULL,
                    relation_version_id TEXT NOT NULL REFERENCES relation_catalog_versions(relation_version_id),
                    PRIMARY KEY(generation, relation_id)
                );
            """)

    def _connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, isolation_level=None, timeout=5, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    @staticmethod
    def _audit(connection: sqlite3.Connection, *, version_id: str | None, action: str, actor: str, git_sha: str, payload: Mapping[str, object]) -> None:
        connection.execute(
            "INSERT INTO relation_catalog_audit VALUES(?,?,?,?,?,?,?)",
            (uuid4().hex, version_id, action, actor, git_sha, _canonical(payload), _now()),
        )

    def ingest(self, discovery: Mapping[str, object], *, git_sha: str = "") -> dict[str, object]:
        payload = _normalise_discovery(discovery)
        source_fingerprint = _digest(_fact(payload["source_evidence"]))
        semantics_fingerprint = _digest({"relation_type": payload["relation_type"], "semantics": payload["semantics"], "markets": [{key: market[key] for key in ("venue", "contract_id", "event_identity_basis", "settlement_observation_key", "settlement_rules", "cancellation_rules")} for market in payload["markets"]]})
        model_fingerprint = _digest(payload["model"])
        now = _now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM relation_catalog_versions WHERE relation_id=? AND source_fingerprint=? AND semantics_fingerprint=? AND model_fingerprint=?",
                (payload["relation_id"], source_fingerprint, semantics_fingerprint, model_fingerprint),
            ).fetchone()
            if row is not None:
                version_id = str(row["relation_version_id"])
                connection.execute("INSERT INTO relation_catalog_evidence VALUES(?,?,?,?)", (uuid4().hex, version_id, _canonical({"discovered_at": payload["discovered_at"], "source_evidence": payload["source_evidence"]}), now))
                self._audit(connection, version_id=version_id, action="duplicate_discovery", actor="system", git_sha=git_sha, payload={"suppressed": row["approval_status"] == "REJECTED"})
                return {"created": False, "suppressed": row["approval_status"] == "REJECTED", "relation_version_id": version_id}
            previous = connection.execute("SELECT relation_version_id FROM relation_catalog_versions WHERE relation_id=? ORDER BY created_at DESC LIMIT 1", (payload["relation_id"],)).fetchone()
            version_id = f"rv:{uuid4().hex}"
            connection.execute(
                "INSERT INTO relation_catalog_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (version_id, payload["relation_id"], None if previous is None else previous[0], source_fingerprint, semantics_fingerprint, model_fingerprint, _canonical(payload), "PENDING", "PENDING", None, None, now, now),
            )
            connection.execute("INSERT INTO relation_catalog_evidence VALUES(?,?,?,?)", (uuid4().hex, version_id, _canonical({"discovered_at": payload["discovered_at"], "source_evidence": payload["source_evidence"]}), now))
            self._audit(connection, version_id=version_id, action="discovered", actor="system", git_sha=git_sha, payload={"relation_id": payload["relation_id"]})
            return {"created": True, "suppressed": False, "relation_version_id": version_id}

    def ingest_threshold_relation(self, relation: object, *, git_sha: str = "") -> dict[str, object]:
        """Adapt the existing deterministic Polymarket discovery codec once."""
        market_a = getattr(relation, "market_a")
        market_b = getattr(relation, "market_b")
        discovered_at = _now()
        def market(value: object) -> dict[str, object]:
            end_date = _string(getattr(value, "end_date"), "threshold end_date")
            return {
                "venue": "Polymarket", "contract_id": _string(getattr(value, "condition_id"), "condition_id"),
                "title": _string(getattr(value, "question"), "question"), "market_date": discovered_at,
                "expires_at": end_date, "event_identity_basis": _string(getattr(value, "event_id"), "event_id"),
                "settlement_observation_key": _string(getattr(value, "resolution_source") or getattr(value, "condition_id"), "resolution_source"),
                "settlement_rules": _string(getattr(value, "rules"), "rules"), "cancellation_rules": "not supplied by threshold discovery",
            }
        return self.ingest({
            "discovery_source": "deterministic_rule", "discovered_at": discovered_at,
            "relation_type": "IMPLIES", "semantics": {"statement": str(getattr(relation, "relation")), "direction": str(getattr(relation, "relation"))},
            "source_evidence": [{"event_id": getattr(relation, "event_id"), "rules_hash_a": getattr(relation, "rules_hash_a"), "rules_hash_b": getattr(relation, "rules_hash_b")}],
            "model": {"completeness": "INCOMPLETE"}, "markets": [market(market_a), market(market_b)],
        }, git_sha=git_sha)

    @staticmethod
    def _row(row: sqlite3.Row, occurrences: int = 0) -> dict[str, object]:
        payload = json.loads(str(row["payload"]))
        return {
            "relation_version_id": row["relation_version_id"], "relation_id": row["relation_id"],
            "previous_relation_version_id": row["previous_relation_version_id"],
            "source_evidence_fingerprint": row["source_fingerprint"],
            "relation_semantics_fingerprint": row["semantics_fingerprint"],
            "compiled_model_fingerprint": row["model_fingerprint"],
            "approval_status": row["approval_status"], "activation_status": row["activation_status"],
            "activation_diagnostic": row["activation_diagnostic"], "active_generation": row["active_generation"],
            "created_at": row["created_at"], "updated_at": row["updated_at"], "occurrences": occurrences,
            "discovery_source": payload["discovery_source"], "discovered_at": payload["discovered_at"],
            "relation_type": payload["relation_type"], "semantics": payload["semantics"],
            "model": payload["model"], "markets": payload["markets"],
        }

    def list(self, view: str) -> list[dict[str, object]]:
        filters = {
            "pending": "approval_status='PENDING'",
            "approved_active": "approval_status='APPROVED' AND activation_status NOT IN ('ACTIVATION_BLOCKED_INCONSISTENT','UNSUPPORTED_SIZE')",
            "activation_blocked": "approval_status='APPROVED' AND activation_status IN ('ACTIVATION_BLOCKED_INCONSISTENT','UNSUPPORTED_SIZE')",
            "history": "approval_status IN ('REJECTED','REVOKED') OR activation_status='SUPERSEDED'",
        }
        if view not in filters:
            raise ValueError("relation catalog view is invalid")
        with self._connection() as connection:
            rows = connection.execute(f"SELECT v.*, COUNT(e.occurrence_id) AS occurrences FROM relation_catalog_versions v LEFT JOIN relation_catalog_evidence e USING(relation_version_id) WHERE {filters[view]} GROUP BY v.relation_version_id ORDER BY v.created_at DESC, v.relation_version_id DESC").fetchall()
        result = [self._row(row, int(row["occurrences"])) for row in rows]
        if view == "pending":
            active_endpoints = {
                str(market["contract_id"])
                for relation in self.current_generation()["relations"]
                for market in relation["markets"]
            }
            result.sort(key=lambda item: str(item["discovered_at"]), reverse=True)
            result.sort(key=lambda item: item["model"].get("completeness") != "COMPLETE")
            result.sort(key=lambda item: not bool(active_endpoints & {str(market["contract_id"]) for market in item["markets"]}))
        return result

    def pending_count(self) -> int:
        with self._connection() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM relation_catalog_versions WHERE approval_status='PENDING'").fetchone()[0])

    def detail(self, relation_version_id: str) -> dict[str, object]:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM relation_catalog_versions WHERE relation_version_id=?", (relation_version_id,)).fetchone()
            if row is None:
                raise ValueError("relation version not found")
            result = self._row(row, int(connection.execute("SELECT COUNT(*) FROM relation_catalog_evidence WHERE relation_version_id=?", (relation_version_id,)).fetchone()[0]))
            result["evidence"] = [json.loads(str(item[0])) for item in connection.execute("SELECT payload FROM relation_catalog_evidence WHERE relation_version_id=? ORDER BY observed_at, occurrence_id", (relation_version_id,))]
            result["audit"] = [json.loads(str(item[0])) | {"action": item[1], "actor": item[2], "git_sha": item[3], "created_at": item[4]} for item in connection.execute("SELECT payload, action, actor, git_sha, created_at FROM relation_catalog_audit WHERE relation_version_id=? ORDER BY created_at, audit_id", (relation_version_id,))]
            return result

    def _require_expected(self, row: sqlite3.Row, expected: Mapping[str, object]) -> None:
        required = {"relation_version_id", "source_evidence_fingerprint", "relation_semantics_fingerprint", "compiled_model_fingerprint"}
        if set(expected) != required or any(str(expected[key]) != str(row[{"relation_version_id": "relation_version_id", "source_evidence_fingerprint": "source_fingerprint", "relation_semantics_fingerprint": "semantics_fingerprint", "compiled_model_fingerprint": "model_fingerprint"}[key]]) for key in required):
            raise RelationConflictError("relation version changed; refresh before deciding")

    def _members(self, connection: sqlite3.Connection) -> list[sqlite3.Row]:
        generation = int(connection.execute("SELECT generation FROM relation_catalog_generations WHERE singleton=1").fetchone()[0])
        return connection.execute("SELECT v.* FROM relation_catalog_generation_members m JOIN relation_catalog_versions v USING(relation_version_id) WHERE m.generation=?", (generation,)).fetchall()

    def _activation_diagnostic(self, connection: sqlite3.Connection, row: sqlite3.Row, *, replacing_relation_id: str | None = None) -> str | None:
        payload = json.loads(str(row["payload"]))
        model = payload["model"]
        if model["completeness"] != "COMPLETE":
            return "INCOMPLETE_MODEL"
        members = self._members(connection)
        if replacing_relation_id is None and any(member["relation_id"] == row["relation_id"] and member["relation_version_id"] != row["relation_version_id"] for member in members):
            return "ACTIVATION_BLOCKED_INCONSISTENT"
        endpoints = {str(market["contract_id"]) for market in payload["markets"]}
        connected = set(endpoints)
        changed = True
        while changed:
            changed = False
            for member in members:
                member_payload = json.loads(str(member["payload"]))
                member_endpoints = {str(market["contract_id"]) for market in member_payload["markets"]}
                if connected & member_endpoints and not member_endpoints <= connected:
                    connected.update(member_endpoints)
                    changed = True
        if len(connected) > self.component_budget:
            return "UNSUPPORTED_SIZE"
        return None

    def _publish(self, connection: sqlite3.Connection, row: sqlite3.Row, *, unknown_components: list[list[str]] | None = None) -> int:
        old_generation = int(connection.execute("SELECT generation FROM relation_catalog_generations WHERE singleton=1").fetchone()[0])
        new_generation = old_generation + 1
        members = self._members(connection)
        by_relation = {str(member["relation_id"]): str(member["relation_version_id"]) for member in members}
        old_version = by_relation.get(str(row["relation_id"]))
        by_relation[str(row["relation_id"])] = str(row["relation_version_id"])
        for relation_id, version_id in by_relation.items():
            connection.execute("INSERT INTO relation_catalog_generation_members VALUES(?,?,?)", (new_generation, relation_id, version_id))
        if old_version and old_version != row["relation_version_id"]:
            connection.execute("UPDATE relation_catalog_versions SET activation_status='SUPERSEDED', updated_at=? WHERE relation_version_id=? AND approval_status!='REVOKED'", (_now(), old_version))
        now = _now()
        connection.execute("UPDATE relation_catalog_versions SET activation_status='ACTIVE', active_generation=?, updated_at=? WHERE relation_version_id=?", (new_generation, now, row["relation_version_id"]))
        connection.execute("UPDATE relation_catalog_generations SET generation=?, unknown_components=?, updated_at=? WHERE singleton=1", (new_generation, _canonical(unknown_components or []), now))
        return new_generation

    def approve(self, relation_version_id: str, expected: Mapping[str, object], *, actor: str, git_sha: str) -> dict[str, object]:
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM relation_catalog_versions WHERE relation_version_id=?", (relation_version_id,)).fetchone()
            if row is None:
                raise ValueError("relation version not found")
            self._require_expected(row, expected)
            if row["approval_status"] != "PENDING":
                raise RelationConflictError("relation version is no longer pending")
            diagnostic = self._activation_diagnostic(connection, row)
            activation = "INCOMPLETE" if diagnostic == "INCOMPLETE_MODEL" else diagnostic or "ACTIVE"
            connection.execute("UPDATE relation_catalog_versions SET approval_status='APPROVED', activation_status=?, activation_diagnostic=?, updated_at=? WHERE relation_version_id=?", (activation, None if diagnostic is None else diagnostic, _now(), relation_version_id))
            row = connection.execute("SELECT * FROM relation_catalog_versions WHERE relation_version_id=?", (relation_version_id,)).fetchone()
            assert row is not None
            generation = None if diagnostic else self._publish(connection, row)
            self._audit(connection, version_id=relation_version_id, action="approved" if diagnostic is None else "activation_blocked", actor=actor, git_sha=git_sha, payload={"activation_status": activation, "generation": generation})
            return {"relation_version_id": relation_version_id, "approval_status": "APPROVED", "activation_status": activation, "generation": generation}

    def reject(self, relation_version_id: str, expected: Mapping[str, object], *, reason: str, note: str = "", actor: str, git_sha: str) -> dict[str, object]:
        if reason not in _REASONS or len(note) > 1000:
            raise ValueError("relation decision reason or note is invalid")
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM relation_catalog_versions WHERE relation_version_id=?", (relation_version_id,)).fetchone()
            if row is None:
                raise ValueError("relation version not found")
            self._require_expected(row, expected)
            if row["approval_status"] != "PENDING":
                raise RelationConflictError("relation version is no longer pending")
            connection.execute("UPDATE relation_catalog_versions SET approval_status='REJECTED', activation_status='REJECTED', updated_at=? WHERE relation_version_id=?", (_now(), relation_version_id))
            self._audit(connection, version_id=relation_version_id, action="rejected", actor=actor, git_sha=git_sha, payload={"reason": reason, "note": note})
            return {"relation_version_id": relation_version_id, "approval_status": "REJECTED"}

    def revoke(self, relation_version_id: str, expected: Mapping[str, object], *, reason: str, note: str = "", actor: str, git_sha: str) -> dict[str, object]:
        if reason not in _REASONS or len(note) > 1000:
            raise ValueError("relation decision reason or note is invalid")
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM relation_catalog_versions WHERE relation_version_id=?", (relation_version_id,)).fetchone()
            if row is None:
                raise ValueError("relation version not found")
            self._require_expected(row, expected)
            if row["activation_status"] != "ACTIVE":
                raise RelationConflictError("relation version is not active")
            old_generation = int(connection.execute("SELECT generation FROM relation_catalog_generations WHERE singleton=1").fetchone()[0])
            new_generation = old_generation + 1
            members = self._members(connection)
            for member in members:
                if member["relation_version_id"] != relation_version_id:
                    connection.execute("INSERT INTO relation_catalog_generation_members VALUES(?,?,?)", (new_generation, member["relation_id"], member["relation_version_id"]))
            payload = json.loads(str(row["payload"]))
            unknown = [sorted(str(market["contract_id"]) for market in payload["markets"])]
            now = _now()
            connection.execute("UPDATE relation_catalog_versions SET approval_status='REVOKED', activation_status='REVOKED', updated_at=? WHERE relation_version_id=?", (now, relation_version_id))
            connection.execute("UPDATE relation_catalog_generations SET generation=?, unknown_components=?, updated_at=? WHERE singleton=1", (new_generation, _canonical(unknown), now))
            self._audit(connection, version_id=relation_version_id, action="revoked", actor=actor, git_sha=git_sha, payload={"reason": reason, "note": note, "generation": new_generation})
            return {"relation_version_id": relation_version_id, "approval_status": "REVOKED", "generation": new_generation}

    def replace(self, active_expected: Mapping[str, object], candidate_expected: Mapping[str, object], *, reason: str, note: str = "", actor: str, git_sha: str) -> dict[str, object]:
        """Atomically revoke one current fact while publishing its replacement."""
        if reason not in _REASONS or len(note) > 1000:
            raise ValueError("relation decision reason or note is invalid")
        with self._transaction() as connection:
            active_id = _string(active_expected.get("relation_version_id"), "active relation_version_id")
            candidate_id = _string(candidate_expected.get("relation_version_id"), "candidate relation_version_id")
            active = connection.execute("SELECT * FROM relation_catalog_versions WHERE relation_version_id=?", (active_id,)).fetchone()
            candidate = connection.execute("SELECT * FROM relation_catalog_versions WHERE relation_version_id=?", (candidate_id,)).fetchone()
            if active is None or candidate is None:
                raise ValueError("relation version not found")
            self._require_expected(active, active_expected)
            self._require_expected(candidate, candidate_expected)
            if active["activation_status"] != "ACTIVE" or candidate["approval_status"] != "APPROVED":
                raise RelationConflictError("change set versions are no longer eligible")
            diagnostic = self._activation_diagnostic(connection, candidate, replacing_relation_id=str(active["relation_id"]))
            if diagnostic is not None:
                raise ValueError("replacement candidate is not activatable")
            now = _now()
            connection.execute("UPDATE relation_catalog_versions SET approval_status='REVOKED', activation_status='REVOKED', updated_at=? WHERE relation_version_id=?", (now, active_id))
            connection.execute("UPDATE relation_catalog_versions SET approval_status='APPROVED', activation_status='PENDING', updated_at=? WHERE relation_version_id=?", (now, candidate_id))
            candidate = connection.execute("SELECT * FROM relation_catalog_versions WHERE relation_version_id=?", (candidate_id,)).fetchone()
            assert candidate is not None
            generation = self._publish(connection, candidate)
            self._audit(connection, version_id=active_id, action="replaced", actor=actor, git_sha=git_sha, payload={"reason": reason, "note": note, "replacement_relation_version_id": candidate_id, "generation": generation})
            self._audit(connection, version_id=candidate_id, action="replacement_activated", actor=actor, git_sha=git_sha, payload={"replaced_relation_version_id": active_id, "generation": generation})
            return {"revoked_relation_version_id": active_id, "activated_relation_version_id": candidate_id, "generation": generation}

    def current_generation(self) -> dict[str, object]:
        with self._connection() as connection:
            generation = connection.execute("SELECT * FROM relation_catalog_generations WHERE singleton=1").fetchone()
            assert generation is not None
            rows = connection.execute("SELECT v.* FROM relation_catalog_generation_members m JOIN relation_catalog_versions v USING(relation_version_id) WHERE m.generation=? ORDER BY m.relation_id", (generation["generation"],)).fetchall()
            return {"generation": generation["generation"], "unknown_components": json.loads(str(generation["unknown_components"])), "relations": [self._row(row) for row in rows]}
