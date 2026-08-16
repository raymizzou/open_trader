"""Issue #82: runtime relation graph with deterministic Episode lineage.

Builds catalog-layer relation groups from the V2 relation catalog's current
generation. Only ACTIVE + model-complete relations are admitted, and they are
partitioned uniquely by venue-qualified endpoint connectivity so each relation
belongs to exactly one group. On catalog change only the affected groups are
atomically rebuilt and the deterministic Episode lineage mapping is advanced:

- same relation identity set -> ``PURE_UPDATE``, lineage reused;
- shared >= 1 stable ID with an old group -> structural change
  (``EXTEND``/``MERGE``/``SPLIT``), new lineage, all matching old groups are
  predecessors;
- no shared stable ID -> ``NEW``;
- old group with no new match -> ``REMOVE``, lineage closed.

The mapping is persisted in ``n_leg_episode_lineage`` /
``n_leg_episode_lineage_audit`` and recovered consistently on restart. This
layer never reads order books, solves, or calls an LLM.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(payload: object) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _qualified(endpoint: Mapping[str, object]) -> str:
    return f"{endpoint['venue']}:{endpoint['contract_id']}"


def _admissible(row: Mapping[str, object]) -> bool:
    """ACTIVE relations with a complete terminal/payout/capital model."""
    if row.get("activation") != "ACTIVE":
        return False
    model = row.get("model")
    if not isinstance(model, Mapping):
        return False
    return all(
        model.get(name) not in (None, "", [])
        for name in ("terminal_states", "payouts", "capital_release")
    )


def _generation_fingerprint(rows: Mapping[str, Mapping[str, object]]) -> str:
    """Whole-generation fingerprint over the admitted graph rows."""
    admitted = sorted(
        (
            identity,
            str(row.get("version_id", "")),
            sorted(_qualified(endpoint) for endpoint in row["endpoints"]),
        )
        for identity, row in rows.items()
        if _admissible(row)
    )
    return _digest(admitted)


def _relation_groups(
    rows: Mapping[str, Mapping[str, object]],
) -> list[tuple[tuple[str, ...], tuple[str, ...]]]:
    """Partition admitted relations by venue-qualified endpoint connectivity."""
    entries = [
        (
            identity,
            frozenset(_qualified(endpoint) for endpoint in row["endpoints"]),
        )
        for identity, row in rows.items()
        if _admissible(row)
    ]
    remaining = list(entries)
    groups: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    while remaining:
        identities = [remaining[0][0]]
        contracts = set(remaining[0][1])
        pending = remaining[1:]
        remaining = []
        changed = True
        while changed:
            changed = False
            for identity, endpoints in list(pending):
                if contracts & endpoints:
                    identities.append(identity)
                    contracts |= endpoints
                    pending.remove((identity, endpoints))
                    changed = True
        remaining.extend(pending)
        groups.append((tuple(sorted(identities)), tuple(sorted(contracts))))
    return groups


def _component_id(
    relation_identities: tuple[str, ...], contract_ids: tuple[str, ...]
) -> str:
    """Stable component ID = relation identities union venue-qualified contracts."""
    return _digest(
        {"relation_identities": sorted(relation_identities), "contract_ids": sorted(contract_ids)}
    )


def _lineage_id(
    predecessor_lineage_ids: tuple[str, ...],
    relation_identities: tuple[str, ...],
    contract_ids: tuple[str, ...],
) -> str:
    """Deterministic sha256 over sorted predecessors, identities, and contracts."""
    return _digest(
        {
            "predecessor_lineage_ids": sorted(predecessor_lineage_ids),
            "relation_identities": sorted(relation_identities),
            "contract_ids": sorted(contract_ids),
        }
    )


@dataclass(frozen=True)
class RuntimeComponent:
    component_id: str
    lineage_id: str
    generation: int
    change_kind: str
    relation_identities: tuple[str, ...]
    contract_ids: tuple[str, ...]
    predecessor_lineage_ids: tuple[str, ...]
    status: str


def _classify_change(
    new_groups: list[tuple[tuple[str, ...], tuple[str, ...]]],
    old_active: list[RuntimeComponent],
) -> dict[
    tuple[tuple[str, ...], tuple[str, ...]], tuple[str, list[RuntimeComponent]]
]:
    """Map each new group to (change_kind, predecessor components)."""
    old_by_identities: dict[tuple[str, ...], RuntimeComponent] = {}
    for component in old_active:
        old_by_identities[component.relation_identities] = component
    overlap_count: dict[str, int] = {}
    for identities, contracts in new_groups:
        stable = frozenset(identities) | frozenset(contracts)
        for component in old_active:
            if stable & (frozenset(component.relation_identities) | frozenset(component.contract_ids)):
                overlap_count[component.component_id] = overlap_count.get(component.component_id, 0) + 1
    result: dict[
        tuple[tuple[str, ...], tuple[str, ...]], tuple[str, list[RuntimeComponent]]
    ] = {}
    for identities, contracts in new_groups:
        exact = old_by_identities.get(identities)
        if exact is not None:
            result[(identities, contracts)] = ("PURE_UPDATE", [exact])
            continue
        stable = frozenset(identities) | frozenset(contracts)
        predecessors = [
            component
            for component in old_active
            if stable
            & (frozenset(component.relation_identities) | frozenset(component.contract_ids))
        ]
        if not predecessors:
            result[(identities, contracts)] = ("NEW", [])
        elif len(predecessors) >= 2:
            result[(identities, contracts)] = ("MERGE", predecessors)
        elif any(
            overlap_count.get(component.component_id, 0) >= 2 for component in predecessors
        ):
            result[(identities, contracts)] = ("SPLIT", predecessors)
        else:
            result[(identities, contracts)] = ("EXTEND", predecessors)
    return result


def _rebuild(
    previous: Mapping[str, RuntimeComponent],
    rows: Mapping[str, Mapping[str, object]],
    generation: int,
    code_version: str,
) -> tuple[list[RuntimeComponent], list[dict[str, object]]]:
    """Compute the new component mapping and audit trail for one generation."""
    old_active = [
        component
        for component in previous.values()
        if component.status == "ACTIVE"
    ]
    groups = _relation_groups(rows)
    classification = _classify_change(groups, old_active)
    kept_old: set[str] = set()
    new_components: list[RuntimeComponent] = []
    audits: list[dict[str, object]] = []
    for identities, contracts in groups:
        kind, predecessors = classification[(identities, contracts)]
        if kind == "PURE_UPDATE":
            old = predecessors[0]
            component_id = old.component_id
            lineage_id = old.lineage_id
            predecessor_ids = old.predecessor_lineage_ids
            kept_old.add(old.component_id)
        else:
            component_id = _component_id(identities, contracts)
            predecessor_ids = tuple(sorted(component.lineage_id for component in predecessors))
            lineage_id = _lineage_id(predecessor_ids, identities, contracts)
        new_components.append(
            RuntimeComponent(
                component_id=component_id,
                lineage_id=lineage_id,
                generation=generation,
                change_kind=kind,
                relation_identities=identities,
                contract_ids=contracts,
                predecessor_lineage_ids=predecessor_ids,
                status="ACTIVE",
            )
        )
        from_component = (
            "|".join(sorted(component.component_id for component in predecessors))
            if predecessors
            else ""
        )
        audits.append(
            {
                "action": kind,
                "from_component": from_component,
                "to_component": component_id,
                "generation": generation,
                "code_version": code_version,
            }
        )
    overlapping_old = {
        component.component_id
        for _, predecessors in classification.values()
        for component in predecessors
    }
    for old in old_active:
        if old.component_id in kept_old:
            continue
        if old.component_id in overlapping_old:
            new_components.append(
                RuntimeComponent(
                    component_id=old.component_id,
                    lineage_id=old.lineage_id,
                    generation=generation,
                    change_kind="CLOSED",
                    relation_identities=old.relation_identities,
                    contract_ids=old.contract_ids,
                    predecessor_lineage_ids=old.predecessor_lineage_ids,
                    status="CLOSED",
                )
            )
        else:
            new_components.append(
                RuntimeComponent(
                    component_id=old.component_id,
                    lineage_id=old.lineage_id,
                    generation=generation,
                    change_kind="REMOVE",
                    relation_identities=old.relation_identities,
                    contract_ids=old.contract_ids,
                    predecessor_lineage_ids=old.predecessor_lineage_ids,
                    status="CLOSED",
                )
            )
            audits.append(
                {
                    "action": "REMOVE",
                    "from_component": old.component_id,
                    "to_component": "",
                    "generation": generation,
                    "code_version": code_version,
                }
            )
    return new_components, audits


_SCHEMA = """
CREATE TABLE IF NOT EXISTS n_leg_episode_lineage (
    component_id TEXT PRIMARY KEY,
    lineage_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    change_kind TEXT NOT NULL,
    relation_identities TEXT NOT NULL,
    contract_ids TEXT NOT NULL,
    predecessor_lineage_ids TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS n_leg_episode_lineage_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    from_component TEXT NOT NULL,
    to_component TEXT NOT NULL,
    generation INTEGER NOT NULL,
    code_version TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS n_leg_episode_lineage_meta (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    generation INTEGER NOT NULL,
    generation_fingerprint TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class RuntimeGraphStore:
    """Persist the runtime graph and lineage mapping in the shared prediction SQLite."""

    def __init__(self, data_dir: str | Path) -> None:
        self.path = (
            Path(data_dir)
            / "prediction_arbitrage"
            / "prediction_arbitrage.sqlite3"
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(_SCHEMA)

    def load(self) -> tuple[int, str, dict[str, RuntimeComponent]]:
        with sqlite3.connect(self.path) as connection:
            meta = connection.execute(
                "SELECT generation, generation_fingerprint"
                " FROM n_leg_episode_lineage_meta WHERE singleton=1"
            ).fetchone()
            generation = int(meta[0]) if meta else 0
            fingerprint = str(meta[1]) if meta else ""
            rows = connection.execute(
                "SELECT component_id, lineage_id, generation, change_kind,"
                " relation_identities, contract_ids, predecessor_lineage_ids, status"
                " FROM n_leg_episode_lineage"
            ).fetchall()
        components = {
            str(component_id): RuntimeComponent(
                component_id=str(component_id),
                lineage_id=str(lineage_id),
                generation=int(generation_value),
                change_kind=str(change_kind),
                relation_identities=tuple(json.loads(relation_identities)),
                contract_ids=tuple(json.loads(contract_ids)),
                predecessor_lineage_ids=tuple(json.loads(predecessor_lineage_ids)),
                status=str(status),
            )
            for component_id, lineage_id, generation_value, change_kind,
                relation_identities, contract_ids, predecessor_lineage_ids, status in rows
        }
        return generation, fingerprint, components

    def save(
        self,
        components: list[RuntimeComponent],
        audits: list[dict[str, object]],
        generation: int,
        fingerprint: str,
    ) -> None:
        """Atomically persist the whole new mapping; unchanged rows are untouched."""
        now = _utc_now()
        active = [component for component in components if component.status == "ACTIVE"]
        closed = [component for component in components if component.status == "CLOSED"]
        with sqlite3.connect(self.path) as connection:
            for component in active:
                connection.execute(
                    """
                    INSERT INTO n_leg_episode_lineage(
                        component_id, lineage_id, generation, change_kind,
                        relation_identities, contract_ids, predecessor_lineage_ids,
                        status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(component_id) DO UPDATE SET
                        lineage_id=excluded.lineage_id,
                        generation=excluded.generation,
                        change_kind=excluded.change_kind,
                        relation_identities=excluded.relation_identities,
                        contract_ids=excluded.contract_ids,
                        predecessor_lineage_ids=excluded.predecessor_lineage_ids,
                        status=excluded.status,
                        updated_at=excluded.updated_at
                    """,
                    (
                        component.component_id,
                        component.lineage_id,
                        component.generation,
                        component.change_kind,
                        json.dumps(list(component.relation_identities), sort_keys=True),
                        json.dumps(list(component.contract_ids), sort_keys=True),
                        json.dumps(list(component.predecessor_lineage_ids), sort_keys=True),
                        component.status,
                        now,
                        now,
                    ),
                )
            for component in closed:
                connection.execute(
                    "UPDATE n_leg_episode_lineage SET status=?, generation=?,"
                    " updated_at=? WHERE component_id=?",
                    ("CLOSED", component.generation, now, component.component_id),
                )
            connection.executemany(
                """
                INSERT INTO n_leg_episode_lineage_audit(
                    action, from_component, to_component, generation,
                    code_version, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        str(audit["action"]),
                        str(audit["from_component"]),
                        str(audit["to_component"]),
                        int(audit["generation"]),
                        str(audit["code_version"]),
                        now,
                    )
                    for audit in audits
                ],
            )
            connection.execute(
                """
                INSERT INTO n_leg_episode_lineage_meta(
                    singleton, generation, generation_fingerprint, updated_at
                ) VALUES (1, ?, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    generation=excluded.generation,
                    generation_fingerprint=excluded.generation_fingerprint,
                    updated_at=excluded.updated_at
                """,
                (generation, fingerprint, now),
            )


class RuntimeRelationGraph:
    """Catalog-layer runtime relation graph with deterministic Episode lineage.

    ``generation_source`` is any callable returning the V2 catalog's
    ``current_generation()`` mapping (identity -> row). ``refresh()`` applies
    only the affected groups when the generation fingerprint changes and
    persists the new lineage mapping atomically.
    """

    def __init__(
        self,
        generation_source: Callable[[], Mapping[str, Mapping[str, object]]],
        data_dir: str | Path,
        *,
        code_version: str = "",
        generation_meta_source: Callable[[], Mapping[str, object]] | None = None,
    ) -> None:
        self._generation_source = generation_source
        self._generation_meta_source = generation_meta_source
        self._store = RuntimeGraphStore(data_dir)
        self._code_version = code_version
        self._lock = threading.Lock()

    def refresh(self) -> dict[str, object]:
        """Advance to the catalog's current generation when it changed."""
        rows = dict(self._generation_source())
        fingerprint = _generation_fingerprint(rows)
        catalog_generation = 0
        if self._generation_meta_source is not None:
            catalog_generation = int(
                dict(self._generation_meta_source()).get("generation", 0)
            )
        with self._lock:
            stored_generation, stored_fingerprint, previous = self._store.load()
            if stored_generation == catalog_generation and catalog_generation > 0:
                return {"generation": catalog_generation, "fingerprint": fingerprint}
            if stored_fingerprint == fingerprint and previous:
                bumped = [
                    replace(component, generation=catalog_generation)
                    for component in previous.values()
                    if component.status == "ACTIVE"
                ]
                self._store.save(bumped, [], catalog_generation, fingerprint)
                return {"generation": catalog_generation, "fingerprint": fingerprint}
            components, audits = _rebuild(
                previous, rows, catalog_generation, self._code_version
            )
            self._store.save(components, audits, catalog_generation, fingerprint)
            return {"generation": catalog_generation, "fingerprint": fingerprint}

    def current_generation(self) -> dict[str, object]:
        """Monotonic generation number and fingerprint of the applied graph."""
        with self._lock:
            generation, fingerprint, _ = self._store.load()
            return {"generation": generation, "fingerprint": fingerprint}

    def components(self) -> dict[str, RuntimeComponent]:
        """Current ACTIVE components keyed by stable component ID."""
        with self._lock:
            _, _, components = self._store.load()
            return {
                component.component_id: component
                for component in components.values()
                if component.status == "ACTIVE"
            }
