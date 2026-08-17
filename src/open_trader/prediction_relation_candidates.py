"""Issue #90: turn connected threshold IMPLIES components into PENDING candidates.

The real ``relation_state`` stores pairwise threshold ``IMPLIES`` relations.
This module groups them into connected components per event (union-find on
``condition_id``), keeps only bounded components (3..10 contracts), and lets the
monitor auto-ingest each fully-complete component once as PENDING catalog
versions. Nothing here approves or activates; that stays the operator's call.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from typing import Sequence

from .relation_catalog import _threshold_complete_model


@dataclass(frozen=True, slots=True)
class RelationCandidateComponent:
    event_id: str
    contract_ids: tuple[str, ...]
    relations: tuple[object, ...]
    complete_count: int
    fingerprint: str


def _relation_fingerprint(relation: object) -> str:
    market_a = getattr(relation, "market_a")
    market_b = getattr(relation, "market_b")
    payload = {
        "event_id": getattr(relation, "event_id"),
        "condition_id_a": getattr(market_a, "condition_id"),
        "condition_id_b": getattr(market_b, "condition_id"),
        "relation": getattr(relation, "relation"),
        "rules_hash_a": getattr(relation, "rules_hash_a"),
        "rules_hash_b": getattr(relation, "rules_hash_b"),
        "leg_a": getattr(getattr(relation, "buy_leg_a"), "outcome"),
        "leg_b": getattr(getattr(relation, "buy_leg_b"), "outcome"),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _component_fingerprint(relations: Sequence[object]) -> str:
    encoded = json.dumps(
        sorted(_relation_fingerprint(relation) for relation in relations),
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _event_components(
    event_id: str,
    relations: Sequence[object],
    min_contracts: int,
    max_contracts: int,
) -> list[RelationCandidateComponent]:
    parent: dict[str, str] = {}

    def find(node: str) -> str:
        parent.setdefault(node, node)
        root = node
        while parent[root] != root:
            root = parent[root]
        while parent[node] != node:
            next_node = parent[node]
            parent[node] = root
            node = next_node
        return root

    def union(a: str, b: str) -> None:
        root_a, root_b = find(a), find(b)
        if root_a != root_b:
            parent[root_b] = root_a

    edges: list[tuple[str, object]] = []
    for relation in relations:
        condition_a = str(getattr(getattr(relation, "market_a"), "condition_id"))
        condition_b = str(getattr(getattr(relation, "market_b"), "condition_id"))
        union(condition_a, condition_b)
        edges.append((condition_a, relation))

    by_root: dict[str, list[object]] = defaultdict(list)
    for condition_id, relation in edges:
        by_root[find(condition_id)].append(relation)

    components: list[RelationCandidateComponent] = []
    for member_relations in by_root.values():
        contract_ids = sorted(
            {
                str(getattr(getattr(relation, "market_a"), "condition_id"))
                for relation in member_relations
            }
            | {
                str(getattr(getattr(relation, "market_b"), "condition_id"))
                for relation in member_relations
            }
        )
        if not min_contracts <= len(contract_ids) <= max_contracts:
            continue
        ordered = tuple(
            sorted(member_relations, key=lambda relation: str(getattr(relation, "relation_id")))
        )
        components.append(
            RelationCandidateComponent(
                event_id=event_id,
                contract_ids=tuple(contract_ids),
                relations=ordered,
                complete_count=sum(
                    1 for relation in ordered if _threshold_complete_model(relation) is not None
                ),
                fingerprint=_component_fingerprint(ordered),
            )
        )
    return components


def group_relation_candidates(
    relations: Sequence[object],
    min_contracts: int = 3,
    max_contracts: int = 10,
) -> list[RelationCandidateComponent]:
    """Union-find connected components of pairwise relations, bounded and sorted."""
    by_event: dict[str, list[object]] = defaultdict(list)
    for relation in relations:
        by_event[str(getattr(relation, "event_id"))].append(relation)

    components: list[RelationCandidateComponent] = []
    for event_id in sorted(by_event):
        components.extend(
            _event_components(event_id, by_event[event_id], min_contracts, max_contracts)
        )
    components.sort(
        key=lambda component: (
            component.event_id,
            -len(component.contract_ids),
            component.fingerprint,
        )
    )
    return components


def prepare_relation_candidates(
    catalog: object | None,
    relations: Sequence[object],
    *,
    max_components: int = 1,
    prepared_fingerprints: set[str] | None = None,
) -> dict[str, object]:
    """Ingest at most ``max_components`` complete components as PENDING versions."""
    prepared: list[dict[str, object]] = []
    incomplete: list[dict[str, object]] = []
    skipped = 0
    for component in group_relation_candidates(relations):
        if component.complete_count != len(component.relations):
            incomplete.append(
                {
                    "status": "INCOMPLETE_COMPONENT",
                    "event_id": component.event_id,
                    "contract_ids": list(component.contract_ids),
                    "fingerprint": component.fingerprint,
                    "complete_count": component.complete_count,
                    "relation_count": len(component.relations),
                }
            )
            continue
        if prepared_fingerprints is not None and component.fingerprint in prepared_fingerprints:
            skipped += 1
            continue
        if len(prepared) >= max_components:
            break
        version_ids: list[str] = []
        if catalog is not None:
            for relation in component.relations:
                version_ids.append(
                    str(catalog.ingest_threshold_relation(relation)["version_id"])
                )
        prepared.append(
            {
                "status": "PREPARED",
                "event_id": component.event_id,
                "contract_ids": list(component.contract_ids),
                "fingerprint": component.fingerprint,
                "version_ids": version_ids,
                "relation_count": len(component.relations),
            }
        )

    if prepared:
        status = "PREPARED"
    elif incomplete:
        status = "INCOMPLETE_COMPONENT"
    elif skipped:
        status = "SKIPPED"
    else:
        status = "EMPTY"
    return {
        "status": status,
        "prepared": len(prepared),
        "incomplete": len(incomplete),
        "skipped": skipped,
        "components": prepared,
        "incomplete_components": incomplete,
        "version_ids": [version_id for item in prepared for version_id in item["version_ids"]],
        "fingerprint": prepared[-1]["fingerprint"] if prepared else None,
    }
