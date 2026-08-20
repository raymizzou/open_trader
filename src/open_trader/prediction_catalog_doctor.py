"""Issue #99: read-only relation-generation doctor and confirmed cleanup.

``report(catalog)`` replays the production compile seam
(``relation_generation_problem``) over the current generation rows and
iteratively attributes failures to merge conflicts or stale capital release
rows, proposing the smallest deterministic removal set. Attribution runs
only on the seam's admission set (ACTIVE and model-complete rows, see
``relation_row_admitted``), so cause-marked UNKNOWN members never lift the
merged ``as_of``, never contribute conflict holders, and never enter any
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
from .prediction_n_leg import canonical_json, canonical_payload, problem_from_payload

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
    model = row.get("model")
    if not isinstance(model, Mapping):
        return None
    payload = model.get("problem")
    if not isinstance(payload, Mapping):
        return None
    try:
        return problem_from_payload(payload)
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


def _diagnose_stale(
    rows: Mapping[str, Mapping[str, object]], message: str
) -> tuple[dict[str, object], list[str]] | None:
    """Attribute a STALE validation error to rows holding a terminal atom
    that releases capital strictly before the merged max ``as_of``.

    Mirrors the oracle (prediction_n_leg.validate_problem) atom by atom: an
    atom is stale only when ``capital_release_at < problem.as_of``, and the
    merged problem's ``as_of`` is the max row ``as_of``, so a row is stale
    iff at least one of its atoms releases before the merged ``as_of``;
    equality is fresh. A row whose ``as_of`` is early but whose every atom
    releases at or after the merged ``as_of`` is fresh and must never be
    proposed for removal. Only admitted rows (ACTIVE and model-complete)
    contribute to the merged ``as_of`` or to staleness, so a cause-marked
    UNKNOWN row with a later ``as_of`` can never make an oracle-fresh row
    look stale.
    """
    if _STALE_CODE not in message:
        return None
    rows = _admitted_rows(rows)
    merged_as_of: datetime | None = None
    for row in rows.values():
        problem = _problem_of(row)
        if problem is None:
            continue
        if merged_as_of is None or problem.as_of > merged_as_of:
            merged_as_of = problem.as_of
    stale: list[dict[str, object]] = []
    for identity, row in rows.items():
        problem = _problem_of(row)
        if problem is None or merged_as_of is None:
            continue
        stale_releases = sorted(
            {
                atom.capital_release_at.isoformat()
                for state in problem.terminal_state_sets
                for atom in state.atoms
                if atom.capital_release_at is not None
                and atom.capital_release_at < merged_as_of
            }
        )
        if not stale_releases:
            continue
        stale.append(
            {
                "identity": str(identity),
                "as_of": problem.as_of.isoformat(),
                "stale_releases": stale_releases,
            }
        )
    if not stale:
        return None
    finding = {
        "merged_as_of": merged_as_of.isoformat(),
        "identities": stale,
    }
    removal = [str(item["identity"]) for item in stale]
    return finding, removal


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
