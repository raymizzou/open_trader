"""V1-compatible public API surface backed by the v2 relation catalog core.

``RelationCatalog`` keeps the v1 constructor and method signatures consumed by
the Prediction Service and Dashboard, but stores and reads only the v2
``catalog_v2_*`` tables through ``RelationCatalogV2``/``SqliteCatalogStore``.
Identity, the frozen version fingerprint, approval freeze, generation
snapshots, and the cause ledger are all v2 invariants; v1 state is never read
or written.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Mapping, Sequence

from .relation_catalog_v2 import (
    RelationCatalogV2,
    SqliteCatalogStore,
    _canonicalize,
)
from .prediction_n_leg import (
    OBSERVATION_SCHEMA_V1,
    PROBLEM_SCHEMA_V1,
    ActionPayout,
    ActionSide,
    ArbitrageProblem,
    CandidateAction,
    ConstraintModel,
    ExecutableCostSlice,
    RelationConstraint,
    RelationKind,
    SettlementObservationKey,
    TerminalAtom,
    TerminalKind,
    TerminalStateSet,
    canonical_payload,
    problem_from_payload,
    validate_problem,
)


_SCHEMA = "open_trader.relation_catalog.v1"
_REASONS = frozenset({
    "source_evidence_insufficient", "relation_semantics_wrong",
    "model_incomplete_or_wrong", "identity_mismatch", "rules_changed", "other",
})
_COMPLETENESS = frozenset({"COMPLETE", "INCOMPLETE"})
_RELATION_TYPES = frozenset({"IMPLIES", "MUTUALLY_EXCLUSIVE", "EXACTLY_ONE"})
_ACTIVATION_BLOCKED = frozenset({"ACTIVATION_BLOCKED_INCONSISTENT", "UNSUPPORTED_SIZE"})
_GROUP_BUDGET = 10

#: The six-state review vocabulary shared by the catalog views, counts, and UI.
REVIEW_STATES = (
    "PENDING_APPROVAL",
    "APPROVED_MODEL_INCOMPLETE",
    "COMPILED_PENDING_ACTIVATION",
    "ACTIVATION_BLOCKED",
    "ACTIVATED",
    "SOURCE_CHANGED_REAPPROVAL",
)

#: Lowercase ``list(view)`` keys, one per review state.
REVIEW_STATE_VIEWS = {
    "pending_approval": "PENDING_APPROVAL",
    "approved_model_incomplete": "APPROVED_MODEL_INCOMPLETE",
    "compiled_pending_activation": "COMPILED_PENDING_ACTIVATION",
    "activation_blocked": "ACTIVATION_BLOCKED",
    "activated": "ACTIVATED",
    "source_changed_reapproval": "SOURCE_CHANGED_REAPPROVAL",
}

#: Legacy API aliases; ``pending`` maps to the identical review state.
_LEGACY_VIEW_ALIASES = {"pending": "PENDING_APPROVAL"}

_DIRECTION_CODES = frozenset({"A_IMPLIES_B", "B_IMPLIES_A", "A_TO_B", "B_TO_A"})


def review_state(record: Mapping[str, object]) -> str | None:
    """Map one stored version record onto the six-state review vocabulary.

    ``PENDING`` versions are awaiting approval; ``APPROVED`` versions split by
    activation status and compiled-model presence; ``REJECTED``/``REVOKED``
    versions map to ``None`` (history only).
    """

    status = str(record.get("status") or "")
    activation = str(record.get("activation_status") or "")
    payload = record.get("payload")
    compiled = bool(payload.get("terminal_states")) if isinstance(payload, Mapping) else False
    if status == "PENDING":
        return "PENDING_APPROVAL"
    if status != "APPROVED":
        return None
    if activation == "ACTIVE":
        return "ACTIVATED"
    if activation in _ACTIVATION_BLOCKED:
        return "ACTIVATION_BLOCKED"
    if activation == "SUPERSEDED":
        return "SOURCE_CHANGED_REAPPROVAL"
    return "COMPILED_PENDING_ACTIVATION" if compiled else "APPROVED_MODEL_INCOMPLETE"


def _derive_statement(
    statement: str,
    endpoints: Sequence[Mapping[str, object]],
    *,
    roles: Sequence[object] = (),
) -> tuple[str, str]:
    """Derive the human-readable sentence for one stored direction-code statement.

    ``roles`` are the per-endpoint ``"A"|"B"`` letters; the endpoint whose role
    equals the direction code's antecedent letter becomes the antecedent, so the
    stored endpoint order can never flip the implication. Without two distinct
    A/B roles the raw direction code is returned unchanged — direction is never
    guessed from endpoint order. Returns ``(statement, direction_code)``; non
    direction-code statements are returned unchanged with an empty direction
    code.
    """

    direction = statement if statement in _DIRECTION_CODES else ""
    if not direction or len(endpoints) < 2:
        return statement, ""
    letters = [str(item) for item in roles[:2]]
    if (
        len(roles) < 2
        or letters[0] not in ("A", "B")
        or letters[1] not in ("A", "B")
        or letters[0] == letters[1]
    ):
        return statement, direction
    antecedent_letter = "B" if direction in {"B_IMPLIES_A", "B_TO_A"} else "A"
    consequent_letter = "A" if antecedent_letter == "B" else "B"
    by_letter = {
        letters[index]: endpoints[index]
        for index in range(2)
    }
    title_first = str(by_letter[antecedent_letter].get("title", ""))
    title_second = str(by_letter[consequent_letter].get("title", ""))
    return (
        f"{antecedent_letter}『{title_first}』为 YES ⇒ {consequent_letter}『{title_second}』必须 YES",
        direction,
    )


class RelationConflictError(ValueError):
    """The reviewed version is not the catalog version the operator opened."""


def _valid_role_letters(letters: Sequence[object]) -> bool:
    """Two distinct ``A``/``B`` letters — one antecedent and one consequent."""

    values = [str(item) for item in letters]
    return (
        len(values) == 2
        and values[0] in ("A", "B")
        and values[1] in ("A", "B")
        and values[0] != values[1]
    )


def _semantics_endpoint_roles(
    semantics: Mapping[str, object],
    endpoints: Sequence[Mapping[str, object]],
    direction: str,
) -> dict[str, str]:
    """Map contract_id → ``"A"|"B"`` from the discovery payload's semantics.

    ``_normalise_discovery`` reorders markets by ``(venue, contract_id)``, so
    the antecedent/consequent contract ids ride inside ``semantics`` (which
    passes normalisation verbatim) instead of relying on market order.
    """

    if direction not in _DIRECTION_CODES:
        return {}
    antecedent_id = semantics.get("antecedent_contract_id")
    consequent_id = semantics.get("consequent_contract_id")
    if not isinstance(antecedent_id, str) or not isinstance(consequent_id, str):
        return {}
    ids = [str(endpoint.get("contract_id", "")) for endpoint in endpoints]
    if len(set(ids)) != 2 or set(ids) != {antecedent_id, consequent_id}:
        return {}
    antecedent_letter = "B" if direction in {"B_IMPLIES_A", "B_TO_A"} else "A"
    consequent_letter = "A" if antecedent_letter == "B" else "B"
    return {antecedent_id: antecedent_letter, consequent_id: consequent_letter}


def _model_endpoint_roles(
    payload: Mapping[str, object],
    contract_ids: Sequence[str],
    direction: str,
) -> list[str]:
    """Read-time role letters for legacy rows, from the compiled IMPLIES constraint.

    ``_threshold_complete_model`` builds the constraint antecedent-first and
    canonical serialisation preserves ``contract_ids`` order for IMPLIES
    (``_sort_values``), so ``contract_ids[0] -> contract_ids[1]`` is the stored
    proof of direction. Anything ambiguous — missing model, several IMPLIES
    constraints, contract sets that do not match the row — resolves to no roles;
    direction is never guessed.
    """

    if direction not in _DIRECTION_CODES or len(set(contract_ids)) != 2:
        return []
    problem = payload.get("problem")
    if not isinstance(problem, Mapping):
        return []
    constraint_model = problem.get("constraint_model")
    if not isinstance(constraint_model, Mapping):
        return []
    relations = constraint_model.get("relations")
    if not isinstance(relations, list):
        return []
    implied: list[list[str]] = []
    for relation in relations:
        if not isinstance(relation, Mapping) or str(relation.get("kind")) != "IMPLIES":
            continue
        ids = relation.get("contract_ids")
        if (
            isinstance(ids, list)
            and len(ids) == 2
            and all(isinstance(item, str) for item in ids)
        ):
            implied.append([str(ids[0]), str(ids[1])])
    if len(implied) != 1 or set(implied[0]) != set(contract_ids):
        return []
    antecedent_id = implied[0][0]
    antecedent_letter = "B" if direction in {"B_IMPLIES_A", "B_TO_A"} else "A"
    consequent_letter = "A" if antecedent_letter == "B" else "B"
    letters = [
        antecedent_letter if cid == antecedent_id else consequent_letter
        for cid in contract_ids
    ]
    return letters if _valid_role_letters(letters) else []


def _row_endpoints_and_roles(
    payload: Mapping[str, object],
) -> tuple[list[dict[str, object]], list[str]]:
    """Row endpoints plus their role letters: stored roles, else compiled model.

    Stored endpoint roles are authoritative. Legacy rows without them fall
    back to the compiled IMPLIES constraint (read-time annotation only — the
    stored payload is never mutated); rows that cannot be proven keep no roles
    so the direction code is displayed as-is.
    """

    endpoints: list[dict[str, object]] = [
        dict(endpoint) for endpoint in payload["endpoints"]
    ]
    stored = [endpoint.get("role") for endpoint in endpoints]
    if _valid_role_letters(stored):
        return endpoints, [str(item) for item in stored]
    statement = str(payload.get("statement", ""))
    if statement not in _DIRECTION_CODES:
        return endpoints, []
    contract_ids = [str(endpoint.get("contract_id", "")) for endpoint in endpoints]
    model_roles = _model_endpoint_roles(payload, contract_ids, statement)
    if not model_roles:
        return endpoints, []
    for endpoint, letter in zip(endpoints, model_roles):
        endpoint["role"] = letter
    return endpoints, model_roles


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


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


def _stored_payload_complete(payload: Mapping[str, object]) -> bool:
    """Mirror the read-model completeness rule over a stored v2 payload."""
    return all(
        payload.get(name) not in (None, "", [])
        for name in ("terminal_states", "payouts", "capital_release")
    )


def _threshold_complete_model(relation: object) -> dict[str, object] | None:
    """Deterministically compile a COMPLETE threshold model, or None when facts are missing."""
    markets = (getattr(relation, "market_a"), getattr(relation, "market_b"))
    legs = (getattr(relation, "buy_leg_a"), getattr(relation, "buy_leg_b"))
    rules_hashes = (getattr(relation, "rules_hash_a"), getattr(relation, "rules_hash_b"))
    direction = str(getattr(relation, "relation"))
    if direction not in {"A_IMPLIES_B", "B_IMPLIES_A", "A_TO_B", "B_TO_A"}:
        return None
    sources = [str(getattr(market, "resolution_source") or "").strip() for market in markets]
    end_dates = [str(getattr(market, "end_date") or "").strip() for market in markets]
    if not all(sources) or not all(end_dates) or not all(str(item or "").strip() for item in rules_hashes):
        return None
    try:
        release_dates = [_utc(value) for value in end_dates]
    except (TypeError, ValueError):
        return None
    order = (1, 0) if direction in {"B_IMPLIES_A", "B_TO_A"} else (0, 1)
    contracts: list[str] = []
    actions: list[CandidateAction] = []
    states: list[TerminalStateSet] = []
    payouts: dict[str, dict[str, int]] = {}
    for index in order:
        market, leg, rules_hash = markets[index], legs[index], rules_hashes[index]
        condition_id = str(getattr(market, "condition_id"))
        if str(getattr(leg, "outcome")) == "YES":
            side = ActionSide.BUY_YES
        elif str(getattr(leg, "outcome")) == "NO":
            side = ActionSide.BUY_NO
        else:
            return None
        observation_window = release_dates[index]
        key = SettlementObservationKey(
            OBSERVATION_SCHEMA_V1,
            sources[index],
            condition_id,
            observation_window,
            observation_window,
            "UTC",
            rules_hash,
        )
        action_id = f"polymarket:{condition_id}"
        contracts.append(condition_id)
        actions.append(CandidateAction(
            action_id,
            venue_id="polymarket",
            account_id="catalog-v2",
            chain_id="polymarket",
            market_contract_id=condition_id,
            settlement_observation_key=key,
            side=side,
            lot_step_units=1,
            quantity_scale=1,
            min_quantity_lots=1,
            max_quantity_lots=1,
            settlement_asset_id="USD",
            valuation_unit_id="USD",
            asset_valuation_rule_id="usd-1:1-v1",
            cost_slices=(ExecutableCostSlice(1, 1, 0),),
        ))
        yes_payout = 1 if side == ActionSide.BUY_YES else 0
        no_payout = 0 if side == ActionSide.BUY_YES else 1
        payouts[condition_id] = {
            "NORMAL_YES": yes_payout,
            "NORMAL_NO": no_payout,
            "VOID": 0,
        }
        release_at = release_dates[index]
        states.append(TerminalStateSet(
            condition_id,
            key,
            rules_hash,
            (
                TerminalAtom(
                    f"{condition_id}:NORMAL_YES", TerminalKind.NORMAL_YES, rules_hash,
                    (ActionPayout(action_id, yes_payout),), release_at,
                ),
                TerminalAtom(
                    f"{condition_id}:NORMAL_NO", TerminalKind.NORMAL_NO, rules_hash,
                    (ActionPayout(action_id, no_payout),), release_at,
                ),
                TerminalAtom(
                    f"{condition_id}:VOID", TerminalKind.VOID, rules_hash,
                    (ActionPayout(action_id, 0),), release_at,
                ),
            ),
        ))
    rule_digest = _digest({
        "direction": direction,
        "rules_hash_a": rules_hashes[0],
        "rules_hash_b": rules_hashes[1],
    })
    problem = ArbitrageProblem(
        PROBLEM_SCHEMA_V1,
        f"threshold:{rule_digest}",
        min(release_dates),
        "USD",
        tuple(actions),
        tuple(states),
        ConstraintModel(
            (
                RelationConstraint(
                    f"imply:{contracts[0]}->{contracts[1]}",
                    RelationKind.IMPLIES,
                    tuple(contracts),
                    rule_digest,
                ),
            ),
            (),
        ),
        (),
    )
    capital_release = max(release_dates)
    return {
        "completeness": "COMPLETE",
        "terminal_states": ["NORMAL_YES", "NORMAL_NO", "VOID"],
        "payouts": payouts,
        "capital_release": capital_release.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "problem": canonical_payload(problem),
    }


def _threshold_discovery_payload(
    relation: object, model: dict[str, object]
) -> dict[str, object]:
    """One deterministic Polymarket threshold relation as a v1 discovery payload."""
    def market(value: object) -> dict[str, object]:
        end_date = _string(getattr(value, "end_date"), "threshold end_date")
        return {
            "venue": "Polymarket", "contract_id": _string(getattr(value, "condition_id"), "condition_id"),
            "title": _string(getattr(value, "question"), "question"), "market_date": end_date,
            "expires_at": end_date, "event_identity_basis": _string(getattr(value, "event_id"), "event_id"),
            "settlement_observation_key": _string(getattr(value, "resolution_source") or getattr(value, "condition_id"), "resolution_source"),
            "settlement_rules": _string(getattr(value, "rules"), "rules"), "cancellation_rules": "not supplied by threshold discovery",
        }
    relation_direction = str(getattr(relation, "relation"))
    endpoints = [market(getattr(relation, "market_a")), market(getattr(relation, "market_b"))]
    if relation_direction in {"B_IMPLIES_A", "B_TO_A"}:
        endpoints = list(reversed(endpoints))
    semantics: dict[str, object] = {
        "statement": relation_direction, "direction": relation_direction,
    }
    if relation_direction in _DIRECTION_CODES:
        # After the reversal above endpoints are antecedent-first; persist the
        # antecedent/consequent contract ids because _normalise_discovery sorts
        # markets by (venue, contract_id) and would otherwise drop that order.
        semantics["antecedent_contract_id"] = str(endpoints[0]["contract_id"])
        semantics["consequent_contract_id"] = str(endpoints[1]["contract_id"])
    return {
        "discovery_source": "deterministic_rule", "discovered_at": _now(),
        "relation_type": "IMPLIES", "semantics": semantics,
        "source_evidence": [{"event_id": getattr(relation, "event_id"), "rules_hash_a": getattr(relation, "rules_hash_a"), "rules_hash_b": getattr(relation, "rules_hash_b")}],
        "model": model, "markets": endpoints,
    }


def default_catalog_path(data_dir: Path) -> Path:
    """The conventional v2 catalog SQLite path under one data directory."""
    return Path(data_dir) / "prediction_arbitrage" / "prediction_arbitrage.sqlite3"


class RelationCatalog:
    """V2-backed public domain API; readers consume only ``current_generation``."""

    def __init__(self, data_dir: Path, *, group_budget: int = _GROUP_BUDGET) -> None:
        if type(group_budget) is not int or group_budget < 2:
            raise ValueError("group_budget must be at least two")
        self.path = default_catalog_path(data_dir)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.group_budget = group_budget
        self._store = SqliteCatalogStore(self.path)
        self._catalog = RelationCatalogV2(store=self._store)

    def _converted(self, discovery: Mapping[str, object]) -> dict[str, object]:
        """Map a v1 discovery payload to the clean v2 canonical payload shape."""
        payload = _normalise_discovery(discovery)
        endpoints = [
            {
                "venue": str(market["venue"]).casefold(),
                "contract_id": str(market["contract_id"]),
                "title": market["title"],
                "market_date": market["market_date"],
                "expires_at": market["expires_at"],
                "settlement_observation_key": market["settlement_observation_key"],
                "settlement_rules": market["settlement_rules"],
                "cancellation_rules": market["cancellation_rules"],
            }
            for market in payload["markets"]
        ]
        direction = str(payload["semantics"].get("direction", ""))
        if direction in {"B_IMPLIES_A", "B_TO_A"}:
            endpoints = list(reversed(endpoints))
        roles = _semantics_endpoint_roles(payload["semantics"], endpoints, direction)
        for endpoint in endpoints:
            letter = roles.get(str(endpoint["contract_id"]))
            if letter is not None:
                endpoint["role"] = letter
        converted: dict[str, object] = {
            "relation_type": payload["relation_type"],
            "endpoints": endpoints,
            "discovery_source": payload["discovery_source"],
            "discovered_at": payload["discovered_at"],
            "statement": str(payload["semantics"].get("statement", "")),
        }
        model = payload.get("model", {})
        if isinstance(model, Mapping) and model.get("completeness") == "COMPLETE":
            converted["terminal_states"] = model.get("terminal_states", [])
            converted["payouts"] = model.get("payouts", {})
            converted["capital_release"] = model.get("capital_release")
            if model.get("problem") is not None:
                converted["problem"] = model["problem"]
        return converted


    def ingest(self, discovery: Mapping[str, object], *, git_sha: str = "") -> dict[str, object]:
        payload = self._converted(discovery)
        result = self._catalog.ingest(payload)
        return {
            "created": int(result["occurrence_count"]) == 1,
            "version_id": str(result["version_id"]),
            "identity": str(result["identity"]),
            "status": str(result["status"]),
            "occurrence_count": int(result["occurrence_count"]),
        }

    def ingest_controlled(self, discovery: Mapping[str, object], *, git_sha: str = "") -> dict[str, object]:
        """Admit only same-venue, same-event, COMPLETE N>=3 relation discoveries."""
        normalized = _normalise_discovery(discovery)
        model = normalized["model"]
        if str(model.get("completeness")) != "COMPLETE":
            raise ValueError("ingest_controlled requires model.completeness=COMPLETE")
        markets = normalized["markets"]
        if len(markets) < 3:
            raise ValueError("ingest_controlled requires at least three market endpoints")
        venues = {str(item["venue"]).casefold() for item in markets}
        event_bases = {str(item["event_identity_basis"]) for item in markets}
        if len(venues) != 1:
            raise ValueError("ingest_controlled requires all endpoints to share one venue")
        if len(event_bases) != 1:
            raise ValueError("ingest_controlled requires all endpoints to share one event_identity_basis")
        problem = model.get("problem")
        if not isinstance(problem, Mapping) or not problem:
            raise ValueError("ingest_controlled requires a compiled model.problem")
        try:
            decoded = problem_from_payload(problem)
        except Exception as exc:
            raise ValueError(f"ingest_controlled requires a valid compiled model.problem: {exc}") from exc
        if validate_problem(decoded):
            raise ValueError("ingest_controlled requires a valid compiled model.problem")
        return self.ingest(discovery, git_sha=git_sha)

    def ingest_threshold_relation(self, relation: object, *, git_sha: str = "") -> dict[str, object]:
        """Adapt the existing deterministic Polymarket discovery codec once."""
        enriched = _threshold_complete_model(relation)
        model: dict[str, object] = (
            enriched if enriched is not None else {"completeness": "INCOMPLETE"}
        )
        return self.ingest(
            _threshold_discovery_payload(relation, model), git_sha=git_sha
        )

    def threshold_relation_identity(self, relation: object) -> str:
        """Catalog identity for one threshold relation, via the ingest codec path."""
        payload = _threshold_discovery_payload(
            relation, {"completeness": "INCOMPLETE"}
        )
        return str(_canonicalize(self._converted(payload))[0])

    def prepared_relation_identities(self) -> set[str]:
        """Identities that already hold PENDING or APPROVED versions."""
        prepared = getattr(self._store, "prepared_identities", None)
        if callable(prepared):
            return set(prepared())
        return {
            str(record["identity"])
            for record in self._versions().values()
            if record.get("status") in {"PENDING", "APPROVED"}
        }

    def _versions(self) -> dict[str, dict[str, object]]:
        return self._store.get("versions", {})

    def _row(
        self,
        version_id: str,
        occurrences: int = 0,
        *,
        include_problem: bool = True,
    ) -> dict[str, object]:
        record = self._versions()[version_id]
        payload = record["payload"]
        model: dict[str, object] = {
            "terminal_states": payload.get("terminal_states", []),
            "payouts": payload.get("payouts", {}),
            "capital_release": payload.get("capital_release"),
        }
        if include_problem:
            model["problem"] = payload.get("problem")
        endpoints, roles = _row_endpoints_and_roles(payload)
        statement, direction_code = _derive_statement(
            str(payload.get("statement", "")),
            endpoints,
            roles=roles,
        )
        return {
            "version_id": version_id,
            "identity": str(record["identity"]),
            "fingerprint": str(record["version_fp"]),
            "status": record.get("status", "PENDING"),
            "activation": record.get("activation_status", "PENDING"),
            "occurrence_count": occurrences,
            "created_at": record.get("created_at", ""),
            "updated_at": record.get("updated_at", ""),
            "discovery_source": payload["discovery_source"],
            "discovered_at": payload["discovered_at"],
            "relation_type": payload["relation_type"],
            "endpoints": endpoints,
            "statement": statement,
            "direction_code": direction_code,
            "model": model,
        }

    def _current_generation(self) -> dict[str, dict[str, str]]:
        return self._catalog.current_generation()

    def _store_write(self, updates: dict[str, dict[str, object]]) -> None:
        begin = getattr(self._store, "begin_write", None)
        if begin is None:
            self._store.setdefault("versions", {}).update(updates)
            return
        begin()
        try:
            self._store.setdefault("versions", {}).update(updates)
            self._store.commit_write()
        except BaseException:
            self._store.rollback_write()
            raise

    def list(self, view: str) -> list[dict[str, object]]:
        """List version rows for one review view.

        Accepts the six review-state keys plus the legacy aliases ``pending``,
        ``approved_active``, and ``history``. List rows and ``detail()`` return
        the same full statements.
        """

        state = REVIEW_STATE_VIEWS.get(view) or _LEGACY_VIEW_ALIASES.get(view)
        generation = self._current_generation()
        versions = self._versions()
        if state is not None:
            result = [
                self._row(
                    version_id,
                    int(record.get("occurrence_count", 1)),
                    include_problem=False,
                )
                for version_id, record in versions.items()
                if review_state(record) == state
            ]
        elif view in {"approved_active", "history"}:
            result = [
                self._row(
                    version_id,
                    int(record.get("occurrence_count", 1)),
                    include_problem=False,
                )
                for version_id, record in versions.items()
                if self._in_view(view, version_id, record, generation)
            ]
        else:
            raise ValueError("relation catalog view is invalid")
        if state == "PENDING_APPROVAL":
            active_endpoints = {
                str(endpoint["contract_id"])
                for identity in generation
                for endpoint in versions[generation[identity]["version_id"]]["payload"]["endpoints"]
            }
            result.sort(key=lambda item: str(item["discovered_at"]), reverse=True)
            result.sort(key=lambda item: not bool(item["model"]["terminal_states"]))
            result.sort(key=lambda item: not bool(active_endpoints & {str(endpoint["contract_id"]) for endpoint in item["endpoints"]}))
        return result

    def review_counts(self) -> dict[str, object]:
        """Six-state counts plus the pending total for the read model payload."""

        counts: dict[str, int] = {state: 0 for state in REVIEW_STATES}
        for record in self._versions().values():
            state = review_state(record)
            if state is not None:
                counts[state] += 1
        return {"counts": counts, "pending_count": counts["PENDING_APPROVAL"]}

    def review_rows(self) -> list[dict[str, object]]:
        """All catalog versions as raw rows (full statements, compiled problem)."""

        return [
            self._row(version_id, int(record.get("occurrence_count", 1)))
            for version_id, record in self._versions().items()
        ]

    def _in_view(
        self,
        view: str,
        version_id: str,
        record: dict[str, object],
        generation: dict[str, dict[str, str]],
    ) -> bool:
        status = str(record.get("status", "PENDING"))
        activation = str(record.get("activation_status", "PENDING"))
        active = any(
            entry["version_id"] == version_id for entry in generation.values()
        )
        if view == "approved_active":
            return status == "APPROVED" and active
        return status in {"REJECTED", "REVOKED"} or activation == "SUPERSEDED"

    def pending_count(self) -> int:
        return sum(
            1
            for record in self._versions().values()
            if record.get("status") == "PENDING"
        )

    def cleanup_incomplete_pending(self, *, actor: str, git_sha: str, dry_run: bool = True):
        """Reject PENDING versions whose stored model is missing or incomplete."""
        matches: list[dict[str, object]] = []
        for version_id, record in self._versions().items():
            if record.get("status") != "PENDING":
                continue
            if _stored_payload_complete(record["payload"]):
                continue
            matches.append({
                "version_id": version_id,
                "identity": str(record["identity"]),
                "fingerprint": str(record["version_fp"]),
            })
        if dry_run:
            return matches
        rejected: list[dict[str, object]] = []
        for match in matches:
            version_id = str(match["version_id"])
            self._catalog.reject(
                version_id,
                reason="model_incomplete_or_wrong",
                note="issue-89 catalog cleanup",
                actor=actor,
                git_sha=git_sha,
            )
            record = self._versions()[version_id]
            self._store_write({
                version_id: {
                    **record,
                    "activation_status": "REJECTED",
                    "activation_diagnostic": "MODEL_INCOMPLETE",
                }
            })
            rejected.append({
                "version_id": version_id,
                "identity": str(match["identity"]),
                "status": "REJECTED",
            })
        return {"applied": len(rejected), "rejected": rejected}

    def dedup_complete_pending(
        self,
        *,
        actor: str,
        git_sha: str,
        dry_run: bool = True,
        limit: int = 200,
    ):
        """Reject duplicate COMPLETE PENDING versions, keeping the latest per identity.

        Version rows are never deleted; rejected duplicates stay in history.
        Apply is bounded by ``limit`` rejected versions per run and can be
        rerun until the dry-run report is empty.
        """
        if type(limit) is not int or limit < 1:
            raise ValueError("limit must be a positive integer")
        pending_by_identity: dict[str, list[str]] = {}
        for version_id, record in self._versions().items():
            if record.get("status") != "PENDING":
                continue
            if not _stored_payload_complete(record["payload"]):
                continue
            pending_by_identity.setdefault(str(record["identity"]), []).append(version_id)
        latest = self._store.get("latest", {})
        matches: list[dict[str, object]] = []
        for identity in sorted(pending_by_identity):
            version_ids = pending_by_identity[identity]
            if len(version_ids) < 2:
                continue
            keep = latest.get(identity)
            if keep not in version_ids:
                keep = sorted(version_ids)[-1]
            matches.append({
                "identity": identity,
                "kept_version_id": keep,
                "reject_version_ids": sorted(
                    version_id for version_id in version_ids if version_id != keep
                ),
            })
        if dry_run:
            return matches
        targets = [
            (str(match["identity"]), version_id, str(match["kept_version_id"]))
            for match in matches
            for version_id in match["reject_version_ids"]  # type: ignore[union-attr]
        ][:limit]
        if not targets:
            return {"applied": 0, "rejected": [], "remaining": 0}
        self._catalog.reject_many(
            [version_id for _, version_id, _ in targets],
            reason="other",
            note="issue-92 duplicate complete pending dedup",
            actor=actor,
            git_sha=git_sha,
        )
        updates: dict[str, dict[str, object]] = {}
        for _, version_id, kept_id in targets:
            record = self._versions()[version_id]
            updates[version_id] = {
                **record,
                "activation_status": "REJECTED",
                "activation_diagnostic": "DUPLICATE_COMPLETE_PENDING",
                "reject_note": f"issue-92 duplicate complete pending dedup; kept {kept_id}",
                "dedup_actor": actor,
                "dedup_git_sha": git_sha,
            }
        self._store_write(updates)
        remaining = sum(len(match["reject_version_ids"]) for match in matches) - len(targets)  # type: ignore[arg-type]
        return {
            "applied": len(targets),
            "rejected": [
                {"version_id": version_id, "identity": identity, "status": "REJECTED"}
                for identity, version_id, _ in targets
            ],
            "remaining": remaining,
        }

    def detail(self, relation_version_id: str) -> dict[str, object]:
        versions = self._versions()
        if relation_version_id not in versions:
            raise ValueError("relation version not found")
        record = versions[relation_version_id]
        result = self._row(
            relation_version_id, int(record.get("occurrence_count", 1))
        )
        result["evidence"] = []
        result["audit"] = []
        return result

    def _require_expected(self, version_id: str, expected: Mapping[str, object]) -> None:
        if set(expected) != {"version_id"} or str(expected["version_id"]) != version_id:
            raise RelationConflictError("relation version changed; refresh before deciding")

    def approve(self, relation_version_id: str, expected: Mapping[str, object], *, actor: str, git_sha: str) -> dict[str, object]:
        versions = self._versions()
        if relation_version_id not in versions:
            raise ValueError("relation version not found")
        self._require_expected(relation_version_id, expected)
        if versions[relation_version_id].get("status") != "PENDING":
            raise RelationConflictError("relation version is no longer pending")
        record = versions[relation_version_id]
        identity = str(record["identity"])
        latest = self._store.get("latest", {})
        if latest.get(identity) != relation_version_id:
            raise RelationConflictError("relation version changed; refresh before deciding")
        if "terminal_states" not in record["payload"]:
            self._store_write({
                relation_version_id: {
                    **record,
                    "status": "APPROVED",
                    "activation_status": "INCOMPLETE",
                    "activation_diagnostic": "INCOMPLETE_MODEL",
                }
            })
            return {
                "version_id": relation_version_id,
                "identity": identity,
                "status": "APPROVED",
                "activation": "INCOMPLETE",
            }
        if any(
            gen_identity == identity
            for gen_identity in self._current_generation()
        ):
            self._store_write({
                relation_version_id: {
                    **record,
                    "status": "APPROVED",
                    "activation_status": "ACTIVATION_BLOCKED_INCONSISTENT",
                    "activation_diagnostic": "ACTIVATION_BLOCKED_INCONSISTENT",
                }
            })
            return {
                "version_id": relation_version_id,
                "identity": identity,
                "status": "APPROVED",
                "activation": "ACTIVATION_BLOCKED_INCONSISTENT",
            }
        self._catalog.approve(relation_version_id, actor=actor, git_sha=git_sha)
        activation = self._activate(relation_version_id)
        return {
            "version_id": relation_version_id,
            "identity": identity,
            "status": "APPROVED",
            "activation": activation,
        }

    def _activate(self, relation_version_id: str) -> str:
        """Publish the v2 generation for one approved version, or record why not."""
        record = self._versions()[relation_version_id]
        payload = record["payload"]
        if "terminal_states" not in payload:
            return "INCOMPLETE"
        identity = str(record["identity"])
        previous_generation = self._current_generation()
        change_set = self._generation_change_set(relation_version_id)
        result = self._catalog.replace(change_set, actor="system", git_sha="")
        activation = "ACTIVE"
        if result["status"] != "ACTIVE":
            blocked = {
                str(item["identity"]): str(item["reason"])
                for item in result["blocked"]
            }
            reason = blocked.get(str(record["identity"]), "ACTIVATION_BLOCKED_INCONSISTENT")
            activation = "UNSUPPORTED_SIZE" if reason == "UNSUPPORTED_SIZE" else "ACTIVATION_BLOCKED_INCONSISTENT"
        updated = dict(self._versions()[relation_version_id])
        updated["activation_status"] = activation
        if activation != "ACTIVE":
            updated["activation_diagnostic"] = activation
        self._store_write({relation_version_id: updated})
        if activation == "ACTIVE":
            superseded_id = previous_generation.get(identity, {}).get("version_id")
            if superseded_id and superseded_id != relation_version_id:
                old_record = self._versions()[superseded_id]
                self._store_write({
                    superseded_id: {
                        **old_record,
                        "activation_status": "SUPERSEDED",
                    }
                })
        return activation

    def _generation_change_set(self, include_version_id: str) -> list[dict[str, object]]:
        """Payloads of the current generation members plus one approved version."""
        versions = self._versions()
        generation = self._current_generation()
        payloads = [
            versions[entry["version_id"]]["payload"]
            for entry in generation.values()
            if entry["version_id"] != include_version_id
        ]
        payloads.append(versions[include_version_id]["payload"])
        return payloads

    def reject(self, relation_version_id: str, expected: Mapping[str, object], *, reason: str, note: str = "", actor: str, git_sha: str) -> dict[str, object]:
        if reason not in _REASONS or len(note) > 1000:
            raise ValueError("relation decision reason or note is invalid")
        versions = self._versions()
        if relation_version_id not in versions:
            raise ValueError("relation version not found")
        self._require_expected(relation_version_id, expected)
        if versions[relation_version_id].get("status") != "PENDING":
            raise RelationConflictError("relation version is no longer pending")
        identity = str(versions[relation_version_id]["identity"])
        latest = self._store.get("latest", {})
        if latest.get(identity) != relation_version_id:
            raise RelationConflictError("relation version changed; refresh before deciding")
        self._catalog.reject(
            relation_version_id,
            reason=reason,
            note=note,
            actor=actor,
            git_sha=git_sha,
        )
        record = self._versions()[relation_version_id]
        self._store_write({
            relation_version_id: {
                **record,
                "activation_status": "REJECTED",
            }
        })
        return {"version_id": relation_version_id, "identity": identity, "status": "REJECTED"}

    def revoke(self, relation_version_id: str, expected: Mapping[str, object], *, reason: str, note: str = "", actor: str, git_sha: str) -> dict[str, object]:
        if reason not in _REASONS or len(note) > 1000:
            raise ValueError("relation decision reason or note is invalid")
        versions = self._versions()
        if relation_version_id not in versions:
            raise ValueError("relation version not found")
        self._require_expected(relation_version_id, expected)
        generation = self._current_generation()
        identity = str(versions[relation_version_id]["identity"])
        if generation.get(identity, {}).get("version_id") != relation_version_id:
            raise RelationConflictError("relation version is not active")
        self._catalog.revoke(relation_version_id, actor=actor, git_sha=git_sha)
        record = self._versions()[relation_version_id]
        self._store_write({
            relation_version_id: {
                **record,
                "status": "REVOKED",
                "activation_status": "REVOKED",
            }
        })
        return {
            "version_id": relation_version_id,
            "identity": str(record["identity"]),
            "status": "REVOKED",
        }

    def replace(self, active_expected: Mapping[str, object], candidate_expected: Mapping[str, object], *, reason: str, note: str = "", actor: str, git_sha: str) -> dict[str, object]:
        """Atomically revoke one current fact while publishing its replacement."""
        if reason not in _REASONS or len(note) > 1000:
            raise ValueError("relation decision reason or note is invalid")
        active_id = _string(active_expected.get("version_id"), "active version_id")
        candidate_id = _string(candidate_expected.get("version_id"), "candidate version_id")
        versions = self._versions()
        if active_id not in versions or candidate_id not in versions:
            raise ValueError("relation version not found")
        self._require_expected(active_id, active_expected)
        self._require_expected(candidate_id, candidate_expected)
        generation = self._current_generation()
        if (
            generation.get(str(versions[active_id]["identity"]), {}).get("version_id") != active_id
            or versions[candidate_id].get("status") != "APPROVED"
        ):
            raise RelationConflictError("change set versions are no longer eligible")
        if "terminal_states" not in versions[candidate_id]["payload"]:
            raise ValueError("replacement candidate is not activatable")
        change_set = [
            versions[entry["version_id"]]["payload"]
            for ident, entry in generation.items()
            if ident != versions[active_id]["identity"]
        ]
        change_set.append(versions[candidate_id]["payload"])
        self._catalog.approve(candidate_id, actor=actor, git_sha=git_sha)
        result = self._catalog.replace(change_set, actor=actor, git_sha=git_sha)
        if result["status"] != "ACTIVE":
            raise ValueError("replacement candidate is not activatable")
        updates: dict[str, dict[str, object]] = {
            active_id: {
                **versions[active_id],
                "status": "REVOKED",
                "activation_status": "SUPERSEDED",
            },
            candidate_id: {
                **versions[candidate_id],
                "activation_status": "ACTIVE",
            },
        }
        self._store_write(updates)
        return {
            "revoked_version_id": active_id,
            "activated_version_id": candidate_id,
        }

    def current_generation(self) -> dict[str, object]:
        generation = self._current_generation()
        rows: dict[str, object] = {}
        for identity, entry in generation.items():
            row = self._row(entry["version_id"])
            row["activation"] = entry["status"]
            rows[identity] = row
        return rows

    def generation_meta(self) -> dict[str, object]:
        """Monotonic catalog generation number and whole-generation fingerprint."""
        generation_number = int(self._store.get("generation_number", 0))
        fingerprint = hashlib.sha256(
            json.dumps(
                sorted(
                    (str(identity), str(entry["version_id"]))
                    for identity, entry in self._current_generation().items()
                ),
                sort_keys=True,
                default=str,
            ).encode()
        ).hexdigest()
        return {"generation": generation_number, "fingerprint": fingerprint}
