from __future__ import annotations

from dataclasses import dataclass
from itertools import product

from open_trader.prediction_n_leg import (
    ActionPayout,
    ArbitrageProblem,
    OracleBudget,
    RelationKind,
    SelectedAtom,
    SettlementScenario,
    TerminalAtom,
    TerminalKind,
    UnknownReason,
    WorstStateCut,
    fingerprint,
    validate_problem,
)


@dataclass(frozen=True, slots=True)
class RelationComponent:
    component_id: str
    action_ids: tuple[str, ...]
    contract_ids: tuple[str, ...]
    constraint_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ScenarioEnumeration:
    scenarios: tuple[SettlementScenario, ...] | None
    raw_joint_state_count: int
    unknown_reason: UnknownReason | None


def _require_valid(problem: ArbitrageProblem) -> None:
    issues = validate_problem(problem)
    if issues:
        raise ValueError("invalid problem: " + "; ".join(issue.code for issue in issues))


def build_relation_components(problem: ArbitrageProblem) -> tuple[RelationComponent, ...]:
    _require_valid(problem)
    contract_ids = tuple(sorted(state.market_contract_id for state in problem.terminal_state_sets))
    parent = {contract_id: contract_id for contract_id in contract_ids}

    def find(contract_id: str) -> str:
        while parent[contract_id] != contract_id:
            parent[contract_id] = parent[parent[contract_id]]
            contract_id = parent[contract_id]
        return contract_id

    def join(contract_ids: tuple[str, ...]) -> None:
        head = find(contract_ids[0])
        for contract_id in contract_ids[1:]:
            tail = find(contract_id)
            if head != tail:
                parent[tail] = head

    identity_contracts: dict[str, list[str]] = {}
    for state in problem.terminal_state_sets:
        identity_id = f"identity:{fingerprint(state.settlement_observation_key)}"
        identity_contracts.setdefault(identity_id, []).append(state.market_contract_id)
    for contracts in identity_contracts.values():
        if len(contracts) > 1:
            join(tuple(sorted(contracts)))
    for relation in problem.constraint_model.relations:
        join(relation.contract_ids)

    actions_by_contract: dict[str, list[str]] = {contract_id: [] for contract_id in contract_ids}
    for action in problem.actions:
        actions_by_contract[action.market_contract_id].append(action.action_id)
    contracts_by_root: dict[str, list[str]] = {}
    for contract_id in contract_ids:
        contracts_by_root.setdefault(find(contract_id), []).append(contract_id)
    components = []
    for contracts in contracts_by_root.values():
        component_contracts = tuple(sorted(contracts))
        component_actions = tuple(sorted(action_id for contract_id in component_contracts for action_id in actions_by_contract[contract_id]))
        component_constraints = tuple(
            sorted(
                relation.constraint_id
                for relation in problem.constraint_model.relations
                if set(relation.contract_ids).issubset(component_contracts)
            )
        )
        components.append(
            RelationComponent(
                f"component:{':'.join(component_contracts)}",
                component_actions,
                component_contracts,
                component_constraints,
            )
        )
    return tuple(sorted(components, key=lambda component: component.component_id))


def enumerate_allowed_scenarios(problem: ArbitrageProblem, budget: OracleBudget) -> ScenarioEnumeration:
    if validate_problem(problem):
        return ScenarioEnumeration(None, 0, UnknownReason.INVALID_MODEL)
    state_sets = tuple(sorted(problem.terminal_state_sets, key=lambda state: state.market_contract_id))
    raw_joint_state_count = 1
    for state in state_sets:
        raw_joint_state_count *= len(state.atoms)
    if raw_joint_state_count > budget.max_joint_states:
        return ScenarioEnumeration(None, raw_joint_state_count, UnknownReason.ORACLE_STATE_LIMIT_EXCEEDED)

    allowed = []
    for atoms in product(*(tuple(sorted(state.atoms, key=lambda atom: atom.atom_id)) for state in state_sets)):
        atoms_by_contract = dict(zip((state.market_contract_id for state in state_sets), atoms, strict=True))
        if _violates_normal_relation(problem, atoms_by_contract):
            continue
        selected_atom_ids = {atom.atom_id for atom in atoms}
        if any(set(forbidden.atom_ids).issubset(selected_atom_ids) for forbidden in problem.constraint_model.forbidden_atom_combinations):
            continue
        allowed.append(
            SettlementScenario(
                tuple(
                    SelectedAtom(state.market_contract_id, atom.atom_id)
                    for state, atom in zip(state_sets, atoms, strict=True)
                )
            )
        )
    if not allowed:
        return ScenarioEnumeration(None, raw_joint_state_count, UnknownReason.CONTRADICTORY_CONSTRAINT_MODEL)
    return ScenarioEnumeration(tuple(allowed), raw_joint_state_count, None)


def _violates_normal_relation(problem: ArbitrageProblem, atoms_by_contract: dict[str, TerminalAtom]) -> bool:
    normal_kinds = {TerminalKind.NORMAL_YES, TerminalKind.NORMAL_NO}
    for relation in problem.constraint_model.relations:
        atoms = tuple(atoms_by_contract[contract_id] for contract_id in relation.contract_ids)
        if any(atom.kind not in normal_kinds for atom in atoms):
            continue
        yes_count = sum(atom.kind == TerminalKind.NORMAL_YES for atom in atoms)
        if relation.kind == RelationKind.IMPLIES and atoms[0].kind == TerminalKind.NORMAL_YES and atoms[1].kind == TerminalKind.NORMAL_NO:
            return True
        if relation.kind == RelationKind.MUTUALLY_EXCLUSIVE and yes_count > 1:
            return True
        if relation.kind == RelationKind.EXACTLY_ONE and yes_count != 1:
            return True
    return False


def cut_from_scenario(problem: ArbitrageProblem, scenario: SettlementScenario) -> WorstStateCut:
    _require_valid(problem)
    selected_atom_ids = {selected.market_contract_id: selected.atom_id for selected in scenario.atoms}
    if len(selected_atom_ids) != len(scenario.atoms):
        raise ValueError("scenario selects a contract more than once")
    payouts = []
    for state in problem.terminal_state_sets:
        atom_id = selected_atom_ids.get(state.market_contract_id)
        if atom_id is None:
            raise ValueError(f"scenario is missing {state.market_contract_id}")
        atom = next((candidate for candidate in state.atoms if candidate.atom_id == atom_id), None)
        if atom is None:
            raise ValueError(f"scenario selects unknown atom {atom_id}")
        payouts.extend(atom.payouts)
    if len(selected_atom_ids) != len(problem.terminal_state_sets):
        raise ValueError("scenario selects an unknown contract")
    return WorstStateCut(
        f"cut:{fingerprint(scenario)}",
        scenario,
        tuple(sorted(payouts, key=lambda payout: payout.action_id)),
    )
