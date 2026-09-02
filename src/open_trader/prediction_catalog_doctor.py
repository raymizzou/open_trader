"""Issue #99: read-only relation-generation doctor and confirmed cleanup.

``report(catalog)`` replays the production compile seam
(``relation_generation_problem``) over the current generation rows and
iteratively attributes failures to merge conflicts or stale capital release
rows, proposing the smallest deterministic removal set. Since issue #110 the
stale attribution is component-scoped: each component (the oracle's contract
+ observation-key connectivity) is judged on its own timeline
(``component_as_of``, the max member ``as_of``), so contract-disjoint
components can never flag each other. Attribution runs
only on the seam's admission set (ACTIVE and model-complete rows, see
``relation_row_admitted``), so cause-marked UNKNOWN members never lift any
component timeline, never contribute conflict holders, and never enter any
proposal; the report lists their count separately as ``excluded``. The
module never writes anything; applying the proposal goes through
``RelationCatalog.rebuild_generation`` with an explicit identity list.

Row shape consumed here is the facade's ``current_generation()`` shape::

    {identity: {"activation": "ACTIVE", "model": {
        "terminal_states": ..., "payouts": ..., "capital_release": ...,
        "problem": {...}}}}
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from .prediction_monitor_selection import relation_generation_problem, relation_row_admitted
from .prediction_n_leg import (
    canonical_json,
    canonical_payload,
    canonicalize_directional_actions,
    fingerprint,
    problem_from_payload,
)

#: Prefixes of the compile seam's merge-conflict messages, mapped to the
#: collection inside the decoded problem that defines the conflicting key.
_CONFLICT_LABELS = {
    "action ": "action",
    "terminal state set ": "terminal state set",
    "relation ": "relation",
    "forbidden atom combination ": "forbidden atom combination",
    "qualification constraint ": "qualification constraint",
}

#: Seam validation code raised when a terminal atom releases capital strictly
#: before the merged problem's ``as_of`` (prediction_n_leg.validate_problem).
_STALE_CODE = "STALE_CAPITAL_RELEASE_AT"


def _parse_conflict(message: str) -> tuple[str, str] | None:
    """Return (label, key) for a ``'<key>' conflicts across compiled
    relations`` message, or None when the message is not a merge conflict."""
    parts = message.split("'")
    if len(parts) < 3:
        return None
    label = _CONFLICT_LABELS.get(parts[0])
    if label is None:
        return None
    return label, parts[1]


def _holders_for(problem: object, label: str, key: str) -> list[object]:
    if label == "action":
        return [action for action in problem.actions if action.action_id == key]
    if label == "terminal state set":
        return [
            state
            for state in problem.terminal_state_sets
            if state.market_contract_id == key
        ]
    if label == "relation":
        return [
            relation
            for relation in problem.constraint_model.relations
            if relation.constraint_id == key
        ]
    if label == "forbidden atom combination":
        return [
            combo
            for combo in problem.constraint_model.forbidden_atom_combinations
            if combo.constraint_id == key
        ]
    if label == "qualification constraint":
        return [
            constraint
            for constraint in problem.qualification_constraints
            if constraint.constraint_id == key
        ]
    return []


def _problem_of(row: Mapping[str, object]) -> object | None:
    """Decode one row's stored problem payload exactly like the merge seam.

    Issue #111: the decode passes through the shared
    ``canonicalize_directional_actions`` normalization (the seam's single
    decode hook, ``prediction_monitor_selection._member_problems``), so legacy
    stored payloads yield the same canonical ``{venue}:{contract}:{side}``
    action identities the seam's conflict messages name, and holder lookups
    stay aligned with the conflict keys. Rows whose payload cannot be decoded
    or normalized contribute no holders — the seam fails closed on them
    before any attribution could apply.
    """
    model = row.get("model")
    if not isinstance(model, Mapping):
        return None
    payload = model.get("problem")
    if not isinstance(payload, Mapping):
        return None
    try:
        return canonicalize_directional_actions(problem_from_payload(payload))
    except ValueError:
        return None


def _admitted_rows(
    rows: Mapping[str, Mapping[str, object]]
) -> dict[str, Mapping[str, object]]:
    """Restrict attribution to the compile seam's admission set.

    Only ACTIVE, model-complete rows participate in diagnosis, exactly like
    the seam filter in ``relation_generation_problem``; cause-marked UNKNOWN
    members never contribute to merged ``as_of``, conflict holders, or the
    removal proposal.
    """
    return {
        identity: row
        for identity, row in rows.items()
        if relation_row_admitted(row)
    }


def _diagnose_conflict(
    rows: Mapping[str, Mapping[str, object]], message: str
) -> tuple[dict[str, object], list[str]] | None:
    """Attribute a merge-conflict message to per-identity holders.

    Only admitted rows (ACTIVE and model-complete) contribute holders.
    Deterministic tie-break for equal-sized payload groups: the group with
    the lexicographically smallest canonical serialization is kept.
    """
    rows = _admitted_rows(rows)
    parsed = _parse_conflict(message)
    if parsed is None:
        return None
    label, key = parsed
    holders: list[dict[str, object]] = []
    for identity, row in rows.items():
        problem = _problem_of(row)
        if problem is None:
            continue
        for item in _holders_for(problem, label, key):
            payload = canonical_payload(item)
            holders.append(
                {
                    "identity": str(identity),
                    "side": (
                        str(item.side.value)
                        if label == "action" and hasattr(item, "side")
                        else None
                    ),
                    "payload": payload,
                    "payload_serialized": canonical_json(payload),
                }
            )
    if not holders:
        return None
    groups: dict[str, list[dict[str, object]]] = {}
    for holder in holders:
        groups.setdefault(str(holder["payload_serialized"]), []).append(holder)
    ordered = sorted(
        groups.values(),
        key=lambda group: (len(group), str(group[0]["payload_serialized"])),
    )
    largest = len(ordered[-1])
    if len(ordered) == 1:
        removal: list[str] = []
    elif all(len(group) == largest for group in ordered):
        removal = [
            str(holder["identity"])
            for group in ordered[1:]
            for holder in group
        ]
    else:
        removal = [
            str(holder["identity"])
            for group in ordered
            if len(group) < largest
            for holder in group
        ]
    finding = {
        "key": key,
        "label": label,
        "holders": holders,
        "removal": removal,
    }
    return finding, removal


def _component_groups(
    rows: Mapping[str, Mapping[str, object]]
) -> list[dict[str, object]]:
    """Group admitted rows by the compile seam's component connectivity.

    Mirrors ``build_relation_components``' join rules over the rows' decoded
    problems: contracts join through shared settlement-observation-key
    fingerprints, explicit relations and forbidden-atom combinations. The
    observation-key join is cross-row: fingerprints are accumulated over every
    admitted row's states before joining, matching the oracle's merged-state
    grouping — the same observation key on different contracts in different
    rows is one component (issue #110 review). Returns one entry per component
    with its contracts and member identities; a row whose problem spans
    several components is a member of each.
    """
    problems = {
        identity: problem
        for identity, row in rows.items()
        if (problem := _problem_of(row)) is not None
    }
    parent: dict[str, str] = {}

    def find(contract: str) -> str:
        while parent[contract] != contract:
            parent[contract] = parent[parent[contract]]
            contract = parent[contract]
        return contract

    def join(contracts: list[str]) -> None:
        for contract in contracts[1:]:
            parent.setdefault(contract, contract)
            head, tail = find(contracts[0]), find(contract)
            if head != tail:
                parent[tail] = head

    key_contracts: dict[str, list[str]] = {}
    for problem in problems.values():
        for state in problem.terminal_state_sets:
            parent.setdefault(state.market_contract_id, state.market_contract_id)
            key_contracts.setdefault(
                fingerprint(state.settlement_observation_key), []
            ).append(state.market_contract_id)
    for contracts in key_contracts.values():
        join(sorted(contracts))
    for problem in problems.values():
        for relation in problem.constraint_model.relations:
            join(list(relation.contract_ids))
        atoms_to_contract = {
            atom.atom_id: state.market_contract_id
            for state in problem.terminal_state_sets
            for atom in state.atoms
        }
        for forbidden in problem.constraint_model.forbidden_atom_combinations:
            join(
                sorted(
                    {
                        atoms_to_contract[atom_id]
                        for atom_id in forbidden.atom_ids
                        if atom_id in atoms_to_contract
                    }
                )
            )
    groups: dict[str, dict[str, object]] = {}
    for identity, problem in problems.items():
        for state in problem.terminal_state_sets:
            root = find(state.market_contract_id)
            group = groups.setdefault(root, {"contracts": set(), "identities": set()})
            group["contracts"].add(state.market_contract_id)  # type: ignore[union-attr]
            group["identities"].add(identity)  # type: ignore[union-attr]
    return [groups[root] for root in sorted(groups)]


def _diagnose_stale(
    rows: Mapping[str, Mapping[str, object]], message: str
) -> tuple[dict[str, object], list[str]] | None:
    """Attribute a STALE validation error to its own component's timeline.

    Issue #110: staleness is a component predicate. Each component (the
    oracle's own contract + observation-key connectivity) is judged on its
    own timeline — component ``as_of`` is the maximum member ``as_of`` — and
    only a row with an atom releasing strictly before its component's
    ``as_of`` is stale; equality is fresh. Contract-disjoint components can
    never make each other stale, so the former whole-catalog attribution
    (every early-settling row flagged against one late ``merged_as_of``) is
    gone. Only admitted rows (ACTIVE and model-complete) contribute, so a
    cause-marked UNKNOWN row can never lift a component's timeline.
    """
    if _STALE_CODE not in message:
        return None
    rows = _admitted_rows(rows)
    for group in _component_groups(rows):
        component_as_of: datetime | None = None
        member_problems: dict[str, object] = {}
        for identity in sorted(group["identities"]):  # type: ignore[union-attr]
            problem = _problem_of(rows[identity])
            if problem is None:
                continue
            member_problems[identity] = problem
            if component_as_of is None or problem.as_of > component_as_of:
                component_as_of = problem.as_of
        if component_as_of is None:
            continue
        component_contracts = group["contracts"]
        stale: list[dict[str, object]] = []
        for identity, problem in member_problems.items():
            stale_releases = sorted(
                {
                    atom.capital_release_at.isoformat()
                    for state in problem.terminal_state_sets
                    if state.market_contract_id in component_contracts
                    for atom in state.atoms
                    if atom.capital_release_at is not None
                    and atom.capital_release_at < component_as_of
                }
            )
            if stale_releases:
                stale.append(
                    {
                        "identity": identity,
                        "as_of": problem.as_of.isoformat(),
                        "stale_releases": stale_releases,
                    }
                )
        if stale:
            finding = {
                "component_as_of": component_as_of.isoformat(),
                "contracts": sorted(component_contracts),
                "identities": sorted(group["identities"]),
                "stale_identities": stale,
            }
            removal = [str(item["identity"]) for item in stale]
            return finding, removal
    return None


def report(catalog: object) -> dict[str, object]:
    """Diagnose the current generation without writing anything.

    Returns ``compiles`` (whether the final diagnosed set compiles), per-key
    ``conflicts``, ``stale`` identities, the cumulative ``proposed_removal``
    list, the ``remaining`` admitted-member count, the number of ``excluded``
    non-admitted members, and an ``error`` message when the last failure was
    not remediable.
    """
    generation = catalog.current_generation()
    admitted = _admitted_rows(generation)
    excluded = len(generation) - len(admitted)
    current = dict(admitted)
    conflicts: list[dict[str, object]] = []
    stale: list[dict[str, object]] = []
    proposed: list[str] = []
    error: str | None = None
    while True:
        try:
            relation_generation_problem(current)
            compiles = True
            break
        except ValueError as exc:
            message = str(exc)
            conflict = _diagnose_conflict(current, message)
            if conflict is not None:
                finding, removal = conflict
                conflicts.append(finding)
            else:
                stale_finding = _diagnose_stale(current, message)
                if stale_finding is None:
                    compiles = False
                    error = message
                    break
                finding, removal = stale_finding
                stale.append(finding)
            if not removal:
                compiles = False
                error = message
                break
            proposed.extend(removal)
            removed = set(removal)
            current = {
                identity: row
                for identity, row in current.items()
                if identity not in removed
            }
            if not current:
                compiles = False
                break
    return {
        "compiles": compiles,
        "conflicts": conflicts,
        "stale": stale,
        "proposed_removal": proposed,
        "remaining": len(current),
        "excluded": excluded,
        "error": error,
    }
