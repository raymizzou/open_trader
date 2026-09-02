"""Issue #110: the stale-capital gate is scoped from catalog-wide to component level.

The 2026-09-01 incident: the activation gate compared every candidate against
whole-ACTIVE-set aggregates (max as_of / min terminal release), so one market
settling at 2027-01-01T05:00Z blocked every candidate settling earlier even
when they shared no contract and no observation key. The gate must judge each
canonical component (contract/observation-key connected markets that enter one
arbitrage portfolio) on that component's own timeline.

Fixtures under ``tests/fixtures/issue_110_incident_payloads.json`` are the
verbatim production payloads of the incident: the one ACTIVE poison relation
(settling 2027-01-01T05:00Z) and the 51 blocked payloads (45 IMPLIES + 6
NATIVE_COMPLEMENT). The IMPLIES payloads come in four contract-connected
families whose members used to assign conflicting direction-less action ids
to shared markets, so the full set failed the fail-closed merge
(``_merge_one``). Since issue #111 the compile seam normalizes every IMPLIES
problem to canonical ``{venue}:{contract}:{side}`` actions, so the families
merge conflict-free and all 51 payloads activate; the conflict-free subset
used for the heterogeneous-compile case is derived in fixture order with the
merge seam's own per-key equality semantics over the canonicalized problems.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from open_trader.prediction_monitor_selection import (
    problem_for_component,
    relation_generation_problem,
)
from open_trader.prediction_n_leg import (
    canonical_json,
    canonicalize_directional_actions,
    problem_from_payload,
    validate_problem,
)
from open_trader.relation_catalog_v2 import RelationCatalogV2, _canonicalize
from test_relation_catalog import compiled_problem, compiled_relation_discovery

FIXTURE = Path(__file__).parent / "fixtures" / "issue_110_incident_payloads.json"
POISON_AS_OF = datetime(2027, 1, 1, 5, 0, tzinfo=UTC)
IMPLIES_AS_OF = datetime(2026, 12, 31, 23, 59, tzinfo=UTC)
NC_AS_OF = datetime(2026, 12, 31, 21, 0, tzinfo=UTC)


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text())


_MODEL_FIELDS = ("terminal_states", "payouts", "capital_release", "problem")


def _row(payload: dict) -> dict:
    """Compile seam row shape for one payload (facade ``current_generation``).

    Accepts both carrier shapes: the v2 core payload (model fields at the top
    level, as in the incident fixture) and the facade discovery payload (model
    fields nested under ``model``).
    """
    model = payload.get("model")
    if not isinstance(model, dict):
        model = {name: payload.get(name) for name in _MODEL_FIELDS}
    return {
        "activation": "ACTIVE",
        "model": {name: model.get(name) for name in _MODEL_FIELDS},
    }


def _implies(data: dict) -> list[dict]:
    return [p for p in data["blocked"] if p["relation_type"] == "IMPLIES"]


def _natives(data: dict) -> list[dict]:
    return [p for p in data["blocked"] if p["relation_type"] == "NATIVE_COMPLEMENT"]


def _conflict_free_implies(impls: list[dict]) -> list[int]:
    """Fixture-order IMPLIES indexes whose merged models never conflict.

    Applies the compile seam's own semantics exactly: each stored problem is
    decoded and passed through ``canonicalize_directional_actions`` (the
    issue-#111 read-hook normalization) before ``_merge_one``-style per-key
    canonical equality over actions, terminal state sets and relations. With
    direction-aware action identities every fixture IMPLIES payload
    co-exists in one compiled generation (all 45).
    """
    actions: dict[str, str] = {}
    states: dict[str, str] = {}
    relations: dict[str, str] = {}
    chosen: list[int] = []
    for index, payload in enumerate(impls):
        problem = canonicalize_directional_actions(
            problem_from_payload(payload["problem"])
        )
        candidate_actions = {
            action.action_id: canonical_json(action)
            for action in problem.actions
        }
        candidate_states = {
            state.market_contract_id: canonical_json(state)
            for state in problem.terminal_state_sets
        }
        candidate_relations = {
            relation.constraint_id: canonical_json(relation)
            for relation in problem.constraint_model.relations
        }
        if (
            all(actions.get(key, raw) == raw for key, raw in candidate_actions.items())
            and all(states.get(key, raw) == raw for key, raw in candidate_states.items())
            and all(
                relations.get(key, raw) == raw
                for key, raw in candidate_relations.items()
            )
        ):
            for key, raw in candidate_actions.items():
                actions.setdefault(key, raw)
            for key, raw in candidate_states.items():
                states.setdefault(key, raw)
            for key, raw in candidate_relations.items():
                relations.setdefault(key, raw)
            chosen.append(index)
    return chosen


def _endpoint_contract_ids(payload: dict) -> set[str]:
    return {endpoint["contract_id"] for endpoint in payload["endpoints"]}


def _implies_families(implies: list[dict]) -> dict[int, int]:
    """Fixture-order IMPLIES index -> family number (contract connectivity)."""
    parent: dict[str, str] = {}

    def find(contract: str) -> str:
        while parent[contract] != contract:
            parent[contract] = parent[parent[contract]]
            contract = parent[contract]
        return contract

    def join(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for payload in implies:
        contracts = sorted(_endpoint_contract_ids(payload))
        for contract in contracts:
            parent.setdefault(contract, contract)
        for contract in contracts[1:]:
            join(contracts[0], contract)
    family_of_root: dict[str, int] = {}
    family_by_index: dict[int, int] = {}
    for index, payload in enumerate(implies):
        root = find(next(iter(_endpoint_contract_ids(payload))))
        family = family_of_root.setdefault(root, len(family_of_root))
        family_by_index[index] = family
    return family_by_index


def _tagged(payload: dict, basis: str) -> dict:
    """Copy of one payload with one ``event_identity_basis`` on every endpoint.

    The activation gate's #102 event gate requires a basis per component
    contract; the production discovery pipeline supplies it. Identity never
    includes the basis, so the tagged payload keeps its production identity.
    """
    tagged = copy.deepcopy(payload)
    for endpoint in tagged["endpoints"]:
        endpoint["event_identity_basis"] = basis
    return tagged


def _activation_payloads(data: dict) -> list[dict]:
    """The 51 blocked payloads with the approved per-component bases.

    One basis per IMPLIES family (families differ), one basis per
    NATIVE_COMPLEMENT payload, and the poison payload untouched (it carries
    its own production basis).
    """
    implies = _implies(data)
    families = _implies_families(implies)
    payloads: list[dict] = []
    for index, payload in enumerate(implies):
        payloads.append(_tagged(payload, f"event-implies-{families[index]}"))
    for number, payload in enumerate(_natives(data)):
        payloads.append(_tagged(payload, f"event-native-{number}"))
    return payloads


# -- C1: component timelines come from the component, not the catalog --------


def test_c1_component_as_of_scopes_to_incident_families() -> None:
    data = _fixture()
    implies = _implies(data)
    rows = {
        "poison": _row(data["poison"]),
        "impl-0": _row(implies[0]),
    }

    problem, components = relation_generation_problem(rows)

    assert problem is not None
    assert len(components) == 2
    poison_contracts = _endpoint_contract_ids(data["poison"])
    implies_contracts = _endpoint_contract_ids(implies[0])
    for component in components:
        contracts = set(component.contract_ids)
        if contracts & poison_contracts:
            assert contracts == poison_contracts
            assert component.as_of == POISON_AS_OF
        else:
            assert contracts & implies_contracts
            assert component.as_of == IMPLIES_AS_OF


# -- C2: component slices validate on their own timeline ---------------------


def test_c2_component_slices_validate_problem_clean() -> None:
    data = _fixture()
    implies = _implies(data)
    merged, components = relation_generation_problem(
        {
            "poison": _row(data["poison"]),
            "impl-0": _row(implies[0]),
        }
    )
    assert merged is not None

    assert len(components) == 2
    poison_contracts = _endpoint_contract_ids(data["poison"])
    for component in components:
        sub = problem_for_component(merged, component)
        if set(component.contract_ids) & poison_contracts:
            assert sub.as_of == POISON_AS_OF
        else:
            assert sub.as_of == IMPLIES_AS_OF
        assert validate_problem(sub) == ()


# -- B2: intra-component staleness still blocks (protection) -----------------


def test_b2_single_stale_relation_still_raises_through_compile_seam() -> None:
    payload = compiled_relation_discovery(
        ["b2-contract-a", "b2-contract-b"],
        {"b2-contract-a": "BUY_YES", "b2-contract-b": "BUY_YES"},
        rule="rules-b2-stale",
        as_of="2027-01-01T05:00:00Z",
        release="2026-12-31T23:59:00Z",
    )

    with pytest.raises(ValueError, match="STALE_CAPITAL_RELEASE_AT"):
        relation_generation_problem({"b2": _row(payload)})


# -- B1: heterogeneous compile over the incident payload set -----------------


def test_b1_heterogeneous_generation_compiles_into_disjoint_components() -> None:
    data = _fixture()
    implies = _implies(data)
    conflict_free = _conflict_free_implies(implies)
    # Issue #111: direction-aware action identities dissolve the old
    # cross-relation action conflicts, so all 45 IMPLIES payloads merge.
    assert len(conflict_free) == 45
    rows = {"poison": _row(data["poison"])}
    for index in conflict_free:
        rows[f"impl-{index}"] = _row(implies[index])

    problem, components = relation_generation_problem(rows)

    assert problem is not None
    assert len(components) >= 2
    poison_contracts = _endpoint_contract_ids(data["poison"])
    poison_component = next(
        component
        for component in components
        if set(component.contract_ids) & poison_contracts
    )
    assert poison_component.as_of == POISON_AS_OF
    for component in components:
        if component is poison_component:
            continue
        assert component.as_of == IMPLIES_AS_OF


# -- A1: the incident unlock through the activation gate ---------------------


def test_a1_incident_implies_activates_after_poison_is_active() -> None:
    data = _fixture()
    catalog = RelationCatalogV2(store={})
    poison_id = _canonicalize(data["poison"])[0]
    first = catalog.activate_many([data["poison"]], actor="op", git_sha="sha")
    assert first["results"][poison_id]["status"] == "APPROVED"
    assert first["status"] == "ACTIVE"

    candidate = _activation_payloads(data)[0]
    identity = _canonicalize(candidate)[0]
    result = catalog.activate_many([candidate], actor="op", git_sha="sha")

    assert result["results"][identity]["status"] == "APPROVED"
    assert result["status"] == "ACTIVE"


# -- A2: deterministic batch replay of the exact 51 blocked payloads ---------


def test_a2_batch_replay_activates_direction_families() -> None:
    """Issue #111: canonical {venue}:{contract}:{side} action identities let
    the full 45-payload IMPLIES families merge conflict-free, so the verbatim
    incident replay activates all 51 blocked payloads on top of the poison."""
    data = _fixture()
    catalog = RelationCatalogV2(store={})
    catalog.activate_many([data["poison"]], actor="op", git_sha="sha")

    payloads = _activation_payloads(data)
    assert len(payloads) == 51
    identities = [_canonicalize(payload)[0] for payload in payloads]
    result = catalog.activate_many(payloads, actor="op", git_sha="sha")

    statuses = [result["results"][identity]["status"] for identity in identities]
    assert statuses.count("APPROVED") == 51
    blocked = [
        result["results"][identity]
        for identity in identities
        if result["results"][identity]["status"] == "BLOCKED"
    ]
    assert blocked == []
    assert result["status"] == "ACTIVE"
    assert len(catalog.store["generation"]) == 52


# -- A3: shared-contract cross-timeline candidates stay blocked --------------


def test_a3_shared_contract_cross_timeline_candidate_still_blocked() -> None:
    data = _fixture()
    poison = data["poison"]
    problem = poison["problem"]
    keep = copy.deepcopy(problem["terminal_state_sets"][0])
    keep_contract = keep["market_contract_id"]
    donor = problem["terminal_state_sets"][1]
    keep_action = copy.deepcopy(
        next(
            action
            for action in problem["actions"]
            if action["market_contract_id"] == keep_contract
        )
    )
    donor_action = next(
        action
        for action in problem["actions"]
        if action["market_contract_id"] == donor["market_contract_id"]
    )

    # Constructed topology (production date literals): one fresh market bound
    # to the poison market by an IMPLIES relation. Alone it is fresh (its atom
    # release equals its own as_of); inside the poison's component the shared
    # timeline (2027-01-01T05:00Z) makes its release stale.
    new_contract = "110-construction-a3"
    as_of = "2026-12-31T23:59:00Z"
    new_action = copy.deepcopy(donor_action)
    new_action["market_contract_id"] = new_contract
    new_action["action_id"] = f"polymarket:{new_contract}:BUY_YES"
    new_state = copy.deepcopy(donor)
    new_state["market_contract_id"] = new_contract
    new_state["rule_version"] = "rules-110-construction"
    for atom in new_state["atoms"]:
        atom["atom_id"] = f"{new_contract}:{atom['atom_id'].rsplit(':', 1)[1]}"
        atom["rule_version"] = "rules-110-construction"
        atom["capital_release_at"] = as_of
        for payout in atom["payouts"]:
            payout["action_id"] = f"polymarket:{new_contract}:BUY_YES"
    observation_key = new_state["settlement_observation_key"]
    observation_key["indicator_id"] = new_contract
    observation_key["observation_start"] = as_of
    observation_key["observation_end"] = as_of
    observation_key["rule_version"] = "rules-110-construction"
    keep_endpoint = next(
        endpoint
        for endpoint in poison["endpoints"]
        if endpoint["contract_id"] == keep_contract
    )
    candidate = {
        "relation_type": "IMPLIES",
        "endpoints": [
            copy.deepcopy(keep_endpoint),
            {
                "venue": keep_endpoint["venue"],
                "contract_id": new_contract,
                "event_identity_basis": keep_endpoint.get("event_identity_basis"),
            },
        ],
        "terminal_states": ["NORMAL_YES", "NORMAL_NO", "VOID"],
        "payouts": {new_contract: {"NORMAL_YES": 1, "NORMAL_NO": 0, "VOID": 0}},
        "capital_release": "2026-12-31T23:59:00.000000Z",
        "problem": {
            "schema_version": problem["schema_version"],
            "problem_id": "110-construction-a3",
            "as_of": as_of,
            "valuation_unit_id": problem["valuation_unit_id"],
            "actions": [keep_action, new_action],
            "terminal_state_sets": [keep, new_state],
            "constraint_model": {
                "relations": [
                    {
                        "constraint_id": "imply:110-construction-a3",
                        "kind": "IMPLIES",
                        "contract_ids": [keep_contract, new_contract],
                        "rule_version": "rules-110-construction",
                    }
                ],
                "forbidden_atom_combinations": [],
            },
            "qualification_constraints": [],
        },
        "statement": "construction implies the poison market",
        "discovery_source": "deterministic_rule",
        "discovered_at": "2026-09-01T00:00:00Z",
    }

    catalog = RelationCatalogV2(store={})
    poison_id = _canonicalize(poison)[0]
    assert (
        catalog.activate_many([poison], actor="op", git_sha="sha")["results"][
            poison_id
        ]["status"]
        == "APPROVED"
    )
    identity = _canonicalize(candidate)[0]
    result = catalog.activate_many([candidate], actor="op", git_sha="sha")

    assert result["results"][identity]["status"] == "BLOCKED"
    assert result["results"][identity]["reason"] == "ACTIVATION_BLOCKED_INCONSISTENT"


# -- T4a: non-direction conflicts still fail closed (issue #111 red line) ----


def _t4a_payload(shared_rule: str, other_contract: str, other_rule: str) -> dict:
    """One v2 payload over a fixed shared contract plus its own market."""
    contracts = ["t4a-shared", other_contract]
    sides = {contract: "BUY_YES" for contract in contracts}
    return {
        "relation_type": "EXACTLY_ONE",
        "endpoints": [
            {
                "venue": "polymarket",
                "contract_id": contract,
                "event_identity_basis": "event-t4a",
            }
            for contract in contracts
        ],
        "terminal_states": ["NORMAL_YES", "NORMAL_NO", "VOID"],
        "payouts": {
            contract: {"NORMAL_YES": 1, "NORMAL_NO": 0, "VOID": 0}
            for contract in contracts
        },
        "capital_release": "2026-12-31T17:00:00.000000Z",
        "problem": compiled_problem(
            contracts,
            sides,
            rule={"t4a-shared": shared_rule, other_contract: other_rule},
        ),
        "statement": "t4a: rules change on a shared contract",
        "discovery_source": "exchange_metadata",
        "discovered_at": "2026-09-01T00:00:00Z",
    }


def test_shared_contract_rules_change_still_blocked_as_inconsistent() -> None:
    """Two relations sharing one contract with different rules_hash (sides and
    every other compile field equal) still conflict after the issue-#111
    direction normalization: the settlement observation keys differ, so the
    canonical action payloads can never agree and the gate blocks."""
    first = _t4a_payload("rules-t4a-1", "t4a-a", "rules-t4a-1")
    second = _t4a_payload("rules-t4a-2", "t4a-b", "rules-t4a-2")

    catalog = RelationCatalogV2(store={})
    first_identity = _canonicalize(first)[0]
    assert (
        catalog.activate_many([first], actor="op", git_sha="sha")["results"][
            first_identity
        ]["status"]
        == "APPROVED"
    )
    second_identity = _canonicalize(second)[0]
    result = catalog.activate_many([second], actor="op", git_sha="sha")

    assert result["results"][second_identity]["status"] == "BLOCKED"
    assert (
        result["results"][second_identity]["reason"]
        == "ACTIVATION_BLOCKED_INCONSISTENT"
    )
    assert len(catalog.store["generation"]) == 1
