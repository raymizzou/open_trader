from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime
from itertools import chain, product

from open_trader.prediction_n_leg import (
    ActionPayout,
    ActionQuantity,
    ArbitrageProblem,
    BusinessStatus,
    CandidateAction,
    Comparison,
    ConstraintModel,
    ObjectiveBounds,
    OptimalityStatus,
    OracleBudget,
    OracleRequest,
    OracleResult,
    PortfolioSolution,
    PayoutProof,
    ProofResultKind,
    ProofStatus,
    PAYOUT_PROOF_SCHEMA_V1,
    PROBLEM_SCHEMA_V1,
    QualificationConstraint,
    QualificationMetric,
    REQUEST_SCHEMA_V1,
    RelationKind,
    SearchMode,
    SelectedSupportGraph,
    SelectedAtom,
    SettlementScenario,
    SolveStatus,
    TerminalAtom,
    TerminalKind,
    UnknownReason,
    WorstStateCut,
    fingerprint,
    validate_problem,
)


_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1


def _checked(value: int) -> int:
    if not _INT64_MIN <= value <= _INT64_MAX:
        raise OverflowError
    return value


def _checked_add(left: int, right: int) -> int:
    return _checked(left + right)


def _checked_subtract(left: int, right: int) -> int:
    return _checked(left - right)


def _checked_multiply(left: int, right: int) -> int:
    return _checked(left * right)


def _checked_product(*values: int) -> int:
    result = 1
    for value in values:
        result = _checked_multiply(result, value)
    return result


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


@dataclass(frozen=True, slots=True)
class PortfolioEvaluation:
    quantities: tuple[ActionQuantity, ...]
    payout_lower_bound_units: int
    cost_upper_bound_units: int
    guaranteed_profit_units: int
    worst_scenario: SettlementScenario
    worst_state_cut: WorstStateCut
    conservative_capital_release_at: datetime
    failed_qualification_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SearchAudit:
    quantity_vectors_total: int
    quantity_vectors_examined: int
    joint_states_per_vector: int
    rejection_counts: tuple[tuple[str, int], ...]


def _require_valid(problem: ArbitrageProblem) -> None:
    issues = validate_problem(problem)
    if issues:
        raise ValueError("invalid problem: " + "; ".join(issue.code for issue in issues))


def _input_unknown_reason(problem: ArbitrageProblem) -> UnknownReason | None:
    if not isinstance(problem, ArbitrageProblem) or not isinstance(problem.actions, tuple) or any(
        not isinstance(action, CandidateAction) for action in problem.actions
    ):
        return UnknownReason.INVALID_MODEL
    issues = validate_problem(problem)
    if any(issue.code in {"MISSING_ACTION_PAYOUT", "MISSING_TERMINAL_RULE_IDENTITY", "MISSING_CAPITAL_RELEASE_AT"} for issue in issues):
        return UnknownReason.UNKNOWN_TERMINAL_DATA
    if any(issue.code == "MISSING_ASSET_VALUATION_RULE" for issue in issues) or any(
        action.settlement_asset_id != problem.valuation_unit_id for action in problem.actions
    ):
        return UnknownReason.UNKNOWN_VALUATION
    return UnknownReason.INVALID_MODEL if issues else None


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
    atoms_to_contract = {
        atom.atom_id: state.market_contract_id
        for state in problem.terminal_state_sets
        for atom in state.atoms
    }
    for forbidden in problem.constraint_model.forbidden_atom_combinations:
        join(tuple(sorted({atoms_to_contract[atom_id] for atom_id in forbidden.atom_ids})))
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
                [
                    relation.constraint_id
                    for relation in problem.constraint_model.relations
                    if set(relation.contract_ids).issubset(component_contracts)
                ]
                + [
                    forbidden.constraint_id
                    for forbidden in problem.constraint_model.forbidden_atom_combinations
                    if set(atoms_to_contract[atom_id] for atom_id in forbidden.atom_ids).issubset(component_contracts)
                ]
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
    try:
        for state in state_sets:
            raw_joint_state_count = _checked_multiply(raw_joint_state_count, len(state.atoms))
    except OverflowError:
        return ScenarioEnumeration(None, 0, UnknownReason.NUMERIC_OVERFLOW)
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


def _selected_quantities(problem: ArbitrageProblem, quantities: tuple[ActionQuantity, ...]) -> tuple[ActionQuantity, ...]:
    actions_by_id = {action.action_id: action for action in problem.actions}
    selected: dict[str, int] = {}
    seen_action_ids: set[str] = set()
    for quantity in quantities:
        if not isinstance(quantity, ActionQuantity):
            raise ValueError("quantities must contain ActionQuantity values")
        if quantity.action_id not in actions_by_id:
            raise ValueError(f"unknown action quantity: {quantity.action_id}")
        if (
            isinstance(quantity.quantity_lots, bool)
            or not isinstance(quantity.quantity_lots, int)
            or not 0 <= quantity.quantity_lots <= _INT64_MAX
        ):
            raise ValueError(f"invalid action quantity: {quantity.action_id}")
        if quantity.action_id in seen_action_ids:
            raise ValueError(f"duplicate action quantity: {quantity.action_id}")
        seen_action_ids.add(quantity.action_id)
        action = actions_by_id[quantity.action_id]
        if quantity.quantity_lots and not action.min_quantity_lots <= quantity.quantity_lots <= action.max_quantity_lots:
            raise ValueError(f"action quantity outside selected quantity range: {quantity.action_id}")
        if quantity.quantity_lots:
            selected[quantity.action_id] = quantity.quantity_lots
    return tuple(ActionQuantity(action_id, selected[action_id]) for action_id in sorted(selected))


def _cost_upper_bound_for_selected(problem: ArbitrageProblem, quantities: tuple[ActionQuantity, ...]) -> int:
    actions_by_id = {action.action_id: action for action in problem.actions}
    total = 0
    for quantity in quantities:
        action = actions_by_id[quantity.action_id]
        remaining = quantity.quantity_lots
        for cost_slice in action.cost_slices:
            if remaining < cost_slice.first_lot:
                break
            covered_last_lot = min(remaining, cost_slice.last_lot)
            covered_lots = _checked_add(_checked_subtract(covered_last_lot, cost_slice.first_lot), 1)
            total = _checked_add(
                total,
                _checked_multiply(covered_lots, cost_slice.incremental_cost_upper_bound_units),
            )
        if remaining > action.cost_slices[-1].last_lot:
            raise ValueError(f"quantity exceeds executable cost slices: {quantity.action_id}")
    return total


def cost_upper_bound(problem: ArbitrageProblem, quantities: tuple[ActionQuantity, ...]) -> int:
    _require_valid(problem)
    return _cost_upper_bound_for_selected(problem, _selected_quantities(problem, quantities))


def _qualification_passes(problem: ArbitrageProblem, constraint: QualificationConstraint, evaluation: PortfolioEvaluation) -> bool:
    if constraint.metric == QualificationMetric.GUARANTEED_PROFIT_UNITS:
        left = _checked_multiply(evaluation.guaranteed_profit_units, constraint.threshold_denominator)
        right = constraint.threshold_numerator
    elif constraint.metric == QualificationMetric.NET_MARGIN_PPM:
        if constraint.threshold_numerator > 0 and evaluation.payout_lower_bound_units <= 0:
            return False
        left = _checked_product(evaluation.guaranteed_profit_units, 1_000_000, constraint.threshold_denominator)
        right = _checked_multiply(constraint.threshold_numerator, evaluation.payout_lower_bound_units)
    elif constraint.metric == QualificationMetric.ANNUALIZED_RETURN_PPM:
        occupied_days = _occupied_days(problem, evaluation)
        left = _checked_product(
            evaluation.guaranteed_profit_units,
            365,
            1_000_000,
            constraint.threshold_denominator,
        )
        right = _checked_product(
            constraint.threshold_numerator,
            evaluation.cost_upper_bound_units,
            occupied_days,
        )
    elif constraint.metric == QualificationMetric.MAX_CAPITAL_RELEASE_DELAY_SECONDS:
        release_delay_seconds = _release_delay_seconds(problem, evaluation)
        left = _checked_multiply(release_delay_seconds, constraint.threshold_denominator)
        right = constraint.threshold_numerator
    else:
        raise AssertionError(constraint.metric)
    return left >= right if constraint.comparison == Comparison.GREATER_THAN_OR_EQUAL else left <= right


def _release_delay_seconds(problem: ArbitrageProblem, evaluation: PortfolioEvaluation) -> int:
    delay = evaluation.conservative_capital_release_at - problem.as_of
    whole_seconds = _checked_add(_checked_multiply(delay.days, 24 * 60 * 60), delay.seconds)
    return _checked_add(whole_seconds, int(bool(delay.microseconds)))


def _occupied_days(problem: ArbitrageProblem, evaluation: PortfolioEvaluation) -> int:
    seconds = _release_delay_seconds(problem, evaluation)
    return max(1, _checked_add(seconds, 86_399) // 86_400)


def evaluate_fixed_portfolio(
    problem: ArbitrageProblem,
    quantities: tuple[ActionQuantity, ...],
    budget: OracleBudget,
) -> PortfolioEvaluation:
    _require_valid(problem)
    selected = _selected_quantities(problem, quantities)
    if not selected:
        raise ValueError("fixed portfolio must select at least one action")
    enumeration = enumerate_allowed_scenarios(problem, budget)
    if enumeration.scenarios is None:
        raise ValueError(enumeration.unknown_reason.value if enumeration.unknown_reason is not None else "scenario enumeration failed")
    quantities_by_action = {quantity.action_id: quantity.quantity_lots for quantity in selected}

    def payout(scenario: SettlementScenario) -> int:
        total = 0
        for payout in cut_from_scenario(problem, scenario).payout_per_lot:
            total = _checked_add(
                total,
                _checked_multiply(
                    payout.payout_lower_bound_per_lot_units,
                    quantities_by_action.get(payout.action_id, 0),
                ),
            )
        return total

    payout_lower_bound_units, _, worst_scenario = min(
        (payout(scenario), fingerprint(scenario), scenario) for scenario in enumeration.scenarios
    )
    selected_contract_ids = {
        action.market_contract_id for action in problem.actions if action.action_id in quantities_by_action
    }
    atoms_by_contract = {
        state.market_contract_id: {atom.atom_id: atom for atom in state.atoms}
        for state in problem.terminal_state_sets
    }
    conservative_capital_release_at = max(
        atoms_by_contract[selected_atom.market_contract_id][selected_atom.atom_id].capital_release_at
        for scenario in enumeration.scenarios
        for selected_atom in scenario.atoms
        if selected_atom.market_contract_id in selected_contract_ids
    )
    cost_upper_bound_units = _cost_upper_bound_for_selected(problem, selected)
    provisional = PortfolioEvaluation(
        selected,
        payout_lower_bound_units,
        cost_upper_bound_units,
        _checked_subtract(payout_lower_bound_units, cost_upper_bound_units),
        worst_scenario,
        cut_from_scenario(problem, worst_scenario),
        conservative_capital_release_at,
        (),
    )
    failed_qualification_ids = tuple(
        constraint.constraint_id
        for constraint in sorted(problem.qualification_constraints, key=lambda constraint: constraint.constraint_id)
        if not _qualification_passes(problem, constraint, provisional)
    )
    return replace(provisional, failed_qualification_ids=failed_qualification_ids)


def _constraint_contract_ids(problem: ArbitrageProblem, constraint_id: str) -> tuple[str, ...]:
    for relation in problem.constraint_model.relations:
        if relation.constraint_id == constraint_id:
            return tuple(sorted(relation.contract_ids))
    atoms_to_contract = {
        atom.atom_id: state.market_contract_id
        for state in problem.terminal_state_sets
        for atom in state.atoms
    }
    for forbidden in problem.constraint_model.forbidden_atom_combinations:
        if forbidden.constraint_id == constraint_id:
            return tuple(sorted({atoms_to_contract[atom_id] for atom_id in forbidden.atom_ids}))
    raise ValueError(f"unknown constraint: {constraint_id}")


def _problem_with_active_support_constraints(
    problem: ArbitrageProblem,
    active_constraint_ids: set[str],
    relevant_constraint_ids: set[str],
) -> ArbitrageProblem:
    return replace(
        problem,
        constraint_model=ConstraintModel(
            tuple(
                relation
                for relation in problem.constraint_model.relations
                if relation.constraint_id not in relevant_constraint_ids or relation.constraint_id in active_constraint_ids
            ),
            tuple(
                forbidden
                for forbidden in problem.constraint_model.forbidden_atom_combinations
                if forbidden.constraint_id not in relevant_constraint_ids or forbidden.constraint_id in active_constraint_ids
            ),
        ),
    )


def derive_selected_support_graph(
    problem: ArbitrageProblem,
    evaluation: PortfolioEvaluation,
    budget: OracleBudget,
) -> SelectedSupportGraph | UnknownReason:
    _require_valid(problem)
    selected_contract_ids = {
        action.market_contract_id for action in problem.actions if action.action_id in {quantity.action_id for quantity in evaluation.quantities}
    }
    all_constraint_ids = tuple(
        relation.constraint_id for relation in problem.constraint_model.relations
    ) + tuple(forbidden.constraint_id for forbidden in problem.constraint_model.forbidden_atom_combinations)
    relevant_constraint_ids: set[str] = set()
    connected_contract_ids = set(selected_contract_ids)
    while True:
        newly_relevant = {
            constraint_id
            for constraint_id in all_constraint_ids
            if set(_constraint_contract_ids(problem, constraint_id)) & connected_contract_ids
        }
        expanded_contract_ids = connected_contract_ids | {
            contract_id
            for constraint_id in newly_relevant
            for contract_id in _constraint_contract_ids(problem, constraint_id)
        }
        if newly_relevant == relevant_constraint_ids and expanded_contract_ids == connected_contract_ids:
            break
        relevant_constraint_ids = newly_relevant
        connected_contract_ids = expanded_contract_ids
    active_constraint_ids = set(relevant_constraint_ids)
    rechecks = 0
    current_bound = evaluation.payout_lower_bound_units
    for constraint_id in sorted(relevant_constraint_ids, reverse=True):
        if rechecks >= budget.max_support_rechecks:
            return UnknownReason.ORACLE_SUPPORT_LIMIT_EXCEEDED
        rechecks += 1
        trial = active_constraint_ids - {constraint_id}
        trial_evaluation = evaluate_fixed_portfolio(
            _problem_with_active_support_constraints(problem, trial, relevant_constraint_ids),
            evaluation.quantities,
            budget,
        )
        if (
            trial_evaluation.payout_lower_bound_units == current_bound
            and trial_evaluation.conservative_capital_release_at == evaluation.conservative_capital_release_at
            and trial_evaluation.failed_qualification_ids == evaluation.failed_qualification_ids
        ):
            active_constraint_ids = trial
    constraint_ids = tuple(sorted(active_constraint_ids))
    contract_ids = tuple(
        sorted(
            selected_contract_ids
            | {contract_id for constraint_id in constraint_ids for contract_id in _constraint_contract_ids(problem, constraint_id)}
        )
    )
    return SelectedSupportGraph(
        tuple(quantity.action_id for quantity in evaluation.quantities),
        contract_ids,
        constraint_ids,
        tuple((constraint_id, _constraint_contract_ids(problem, constraint_id)) for constraint_id in constraint_ids),
    )


def build_portfolio_solution(
    problem: ArbitrageProblem,
    evaluation: PortfolioEvaluation,
    support_graph: SelectedSupportGraph,
) -> PortfolioSolution:
    _require_valid(problem)
    selected = _selected_quantities(problem, evaluation.quantities)
    if tuple(quantity.action_id for quantity in evaluation.quantities) != support_graph.action_ids:
        raise ValueError("support graph actions must match evaluated quantities")
    if len(split_disconnected_solution(problem, evaluation, support_graph)) > 1:
        raise ValueError("disconnected portfolio requires separate evaluation")
    if selected != evaluation.quantities or _cost_upper_bound_for_selected(problem, selected) != evaluation.cost_upper_bound_units:
        raise ValueError("evaluation does not match problem quantities or costs")
    current_cut = cut_from_scenario(problem, evaluation.worst_scenario)
    payout_by_action = {payout.action_id: payout.payout_lower_bound_per_lot_units for payout in current_cut.payout_per_lot}
    current_payout = 0
    for quantity in selected:
        current_payout = _checked_add(
            current_payout,
            _checked_multiply(payout_by_action[quantity.action_id], quantity.quantity_lots),
        )
    selected_contract_ids = {
        action.market_contract_id for action in problem.actions if action.action_id in {quantity.action_id for quantity in selected}
    }
    atoms_by_contract = {
        state_set.market_contract_id: {atom.atom_id: atom for atom in state_set.atoms}
        for state_set in problem.terminal_state_sets
    }
    worst_scenario_release_at = max(
        atoms_by_contract[selected_atom.market_contract_id][selected_atom.atom_id].capital_release_at
        for selected_atom in evaluation.worst_scenario.atoms
        if selected_atom.market_contract_id in selected_contract_ids
    )
    if (
        current_cut != evaluation.worst_state_cut
        or current_payout != evaluation.payout_lower_bound_units
        or evaluation.guaranteed_profit_units
        != _checked_subtract(evaluation.payout_lower_bound_units, evaluation.cost_upper_bound_units)
        or evaluation.conservative_capital_release_at < worst_scenario_release_at
        or evaluation.conservative_capital_release_at < problem.as_of
    ):
        raise ValueError("evaluation does not match problem payout proof")
    return PortfolioSolution(
        evaluation.quantities,
        PayoutProof(
            schema_version=PAYOUT_PROOF_SCHEMA_V1,
            result_kind=ProofResultKind.PORTFOLIO,
            problem_fingerprint=fingerprint(problem),
            portfolio_fingerprint=fingerprint({"quantities": evaluation.quantities}),
            worst_scenario=evaluation.worst_scenario,
            worst_state_cut=evaluation.worst_state_cut,
            payout_lower_bound_units=evaluation.payout_lower_bound_units,
            cost_upper_bound_units=evaluation.cost_upper_bound_units,
            guaranteed_profit_units=evaluation.guaranteed_profit_units,
            conservative_capital_release_at=evaluation.conservative_capital_release_at,
            selected_support_graph=support_graph,
            proof_method="BOUNDED_EXACT_ORACLE_V1",
            request_fingerprint=None,
            source_problem_fingerprint=None,
            qualification_fingerprint=_qualification_fingerprint(problem),
            quantity_vectors_total=None,
            quantity_vectors_examined=None,
            joint_states_per_vector=None,
            rejection_counts=(),
        ),
    )


def split_disconnected_solution(
    problem: ArbitrageProblem,
    evaluation: PortfolioEvaluation,
    support_graph: SelectedSupportGraph,
) -> tuple[tuple[ActionQuantity, ...], ...]:
    actions_by_id = {action.action_id: action for action in problem.actions}
    selected_contract_ids = {
        actions_by_id[quantity.action_id].market_contract_id for quantity in evaluation.quantities
    }
    parent = {contract_id: contract_id for contract_id in set(support_graph.contract_ids) | selected_contract_ids}

    def find(contract_id: str) -> str:
        while parent[contract_id] != contract_id:
            parent[contract_id] = parent[parent[contract_id]]
            contract_id = parent[contract_id]
        return contract_id

    def join(contract_ids: tuple[str, ...]) -> None:
        if not contract_ids:
            return
        head = find(contract_ids[0])
        for contract_id in contract_ids[1:]:
            tail = find(contract_id)
            if head != tail:
                parent[tail] = head

    for _, contract_ids in support_graph.hyperedges:
        connected = tuple(contract_id for contract_id in contract_ids if contract_id in parent)
        join(connected)
    groups: dict[str, list[ActionQuantity]] = {}
    for quantity in evaluation.quantities:
        groups.setdefault(find(actions_by_id[quantity.action_id].market_contract_id), []).append(quantity)
    identified_groups = []
    for quantities in groups.values():
        sorted_quantities = tuple(sorted(quantities, key=lambda quantity: quantity.action_id))
        component_root = find(actions_by_id[sorted_quantities[0].action_id].market_contract_id)
        component_contract_ids = tuple(sorted(contract_id for contract_id in parent if find(contract_id) == component_root))
        identified_groups.append((f"component:{':'.join(component_contract_ids)}", sorted_quantities))
    return tuple(quantities for _, quantities in sorted(identified_groups))


def quantity_vector_count(problem: ArbitrageProblem) -> int:
    _require_valid(problem)
    total = 1
    for action in problem.actions:
        domain_size = _checked_add(
            _checked_subtract(action.max_quantity_lots, action.min_quantity_lots),
            2,
        )
        total = _checked_multiply(total, domain_size)
    return total


def _quantity_ranges(actions: tuple[CandidateAction, ...]) -> tuple[Iterable[int], ...]:
    return tuple(
        chain((0,), range(action.min_quantity_lots, action.max_quantity_lots + 1))
        for action in actions
    )


def build_exhaustive_search_proof(
    request: OracleRequest,
    audit: SearchAudit,
    conclusion: BusinessStatus,
    source_problem_fingerprint: str | None = None,
) -> PayoutProof:
    if conclusion not in {BusinessStatus.NO_QUALIFIED_OPPORTUNITY, BusinessStatus.NO_ARBITRAGE}:
        raise ValueError("exhaustive proof requires a negative business conclusion")
    return PayoutProof(
        schema_version=PAYOUT_PROOF_SCHEMA_V1,
        result_kind=ProofResultKind(conclusion.value),
        problem_fingerprint=fingerprint(request.problem),
        portfolio_fingerprint=None,
        worst_scenario=None,
        worst_state_cut=None,
        payout_lower_bound_units=None,
        cost_upper_bound_units=None,
        guaranteed_profit_units=None,
        conservative_capital_release_at=None,
        selected_support_graph=None,
        proof_method="EXHAUSTIVE_ORACLE_V1",
        request_fingerprint=fingerprint(request),
        source_problem_fingerprint=source_problem_fingerprint,
        qualification_fingerprint=_qualification_fingerprint(request.problem),
        quantity_vectors_total=audit.quantity_vectors_total,
        quantity_vectors_examined=audit.quantity_vectors_examined,
        joint_states_per_vector=audit.joint_states_per_vector,
        rejection_counts=audit.rejection_counts,
    )


def _qualification_fingerprint(problem: ArbitrageProblem) -> str:
    constraints = tuple(
        sorted(problem.qualification_constraints, key=lambda item: item.constraint_id)
    )
    return fingerprint({
        "schema_version": PROBLEM_SCHEMA_V1,
        "qualification_constraints": constraints,
    })


def _unknown_result(reason: UnknownReason) -> OracleResult:
    return OracleResult(
        SolveStatus.UNKNOWN,
        ProofStatus.UNKNOWN,
        BusinessStatus.UNKNOWN,
        OptimalityStatus.NOT_APPLICABLE,
        ObjectiveBounds(None, None, None, False),
        None,
        None,
        reason,
    )


def _record_rejections(rejection_ids: set[str], evaluation: PortfolioEvaluation) -> None:
    for constraint_id in evaluation.failed_qualification_ids:
        rejection_ids.add(constraint_id)


def _merge_rejections(rejection_counts: dict[str, int], rejection_ids: set[str]) -> None:
    for rejection_id in rejection_ids:
        rejection_counts[rejection_id] = rejection_counts.get(rejection_id, 0) + 1


def _connected_qualified_solution(
    problem: ArbitrageProblem,
    evaluation: PortfolioEvaluation,
    budget: OracleBudget,
    rejection_ids: set[str],
) -> PortfolioSolution | UnknownReason | None:
    solutions = _connected_qualified_solutions(problem, evaluation, budget, rejection_ids)
    if isinstance(solutions, UnknownReason):
        return solutions
    return solutions[0] if solutions else None


def _connected_qualified_solutions(
    problem: ArbitrageProblem,
    evaluation: PortfolioEvaluation,
    budget: OracleBudget,
    rejection_ids: set[str],
) -> tuple[PortfolioSolution, ...] | UnknownReason:
    try:
        support_graph = derive_selected_support_graph(problem, evaluation, budget)
    except OverflowError:
        return UnknownReason.NUMERIC_OVERFLOW
    if isinstance(support_graph, UnknownReason):
        return support_graph
    groups = split_disconnected_solution(problem, evaluation, support_graph)
    if len(groups) == 1:
        return (build_portfolio_solution(problem, evaluation, support_graph),)

    children = tuple(evaluate_fixed_portfolio(problem, quantities, budget) for quantities in groups)
    solutions = []
    for child in children:
        if child.failed_qualification_ids:
            _record_rejections(rejection_ids, child)
        else:
            child_solutions = _connected_qualified_solutions(problem, child, budget, rejection_ids)
            if isinstance(child_solutions, UnknownReason):
                return child_solutions
            solutions.extend(child_solutions)
    return tuple(solutions)


def _search_request_unknown_reason(request: OracleRequest, mode: SearchMode) -> UnknownReason | None:
    if (
        not isinstance(request, OracleRequest)
        or request.schema_version != REQUEST_SCHEMA_V1
        or request.mode != mode
        or not isinstance(request.budget, OracleBudget)
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in (
                request.budget.max_quantity_vectors,
                request.budget.max_joint_states,
                request.budget.max_support_rechecks,
            )
        )
    ):
        return UnknownReason.INVALID_MODEL
    if reason := _input_unknown_reason(request.problem):
        return reason
    try:
        total_vectors = quantity_vector_count(request.problem)
    except OverflowError:
        return UnknownReason.NUMERIC_OVERFLOW
    if total_vectors > request.budget.max_quantity_vectors:
        return UnknownReason.ORACLE_DECISION_LIMIT_EXCEEDED
    enumeration = enumerate_allowed_scenarios(request.problem, request.budget)
    if enumeration.unknown_reason is not None:
        return enumeration.unknown_reason
    try:
        _numeric_domain_preflight(request.problem, request.budget)
    except OverflowError:
        return UnknownReason.NUMERIC_OVERFLOW
    return None


def _numeric_domain_preflight(problem: ArbitrageProblem, budget: OracleBudget) -> None:
    actions = tuple(sorted(problem.actions, key=lambda action: action.action_id))
    for lots in product(*_quantity_ranges(actions)):
        if any(lots):
            evaluate_fixed_portfolio(
                problem,
                tuple(
                    ActionQuantity(action.action_id, quantity)
                    for action, quantity in zip(actions, lots, strict=True)
                    if quantity
                ),
                budget,
            )


def _solution_objective(solution: PortfolioSolution) -> tuple[object, ...]:
    payout_proof = solution.payout_proof
    return (
        -payout_proof.guaranteed_profit_units,
        payout_proof.cost_upper_bound_units,
        len(solution.quantities),
        tuple((quantity.action_id, quantity.quantity_lots) for quantity in solution.quantities),
    )


def _solve_exhaustive(request: OracleRequest, mode: SearchMode) -> OracleResult:
    reason = _search_request_unknown_reason(request, mode)
    if reason is not None:
        return _unknown_result(reason)

    actions = tuple(sorted(request.problem.actions, key=lambda action: action.action_id))
    rejection_counts: dict[str, int] = {}
    best_solution: PortfolioSolution | None = None
    examined = 0
    for lots in product(*_quantity_ranges(actions)):
        examined += 1
        vector_rejection_ids: set[str] = set()
        if not any(lots):
            vector_rejection_ids.add("ALL_ZERO")
            _merge_rejections(rejection_counts, vector_rejection_ids)
            continue
        evaluation = evaluate_fixed_portfolio(
            request.problem,
            tuple(ActionQuantity(action.action_id, quantity) for action, quantity in zip(actions, lots, strict=True) if quantity),
            request.budget,
        )
        if evaluation.failed_qualification_ids:
            _record_rejections(vector_rejection_ids, evaluation)
            _merge_rejections(rejection_counts, vector_rejection_ids)
            continue
        solutions = _connected_qualified_solutions(request.problem, evaluation, request.budget, vector_rejection_ids)
        if isinstance(solutions, UnknownReason):
            return _unknown_result(solutions)
        for solution in solutions:
            if best_solution is None or _solution_objective(solution) < _solution_objective(best_solution):
                best_solution = solution
        _merge_rejections(rejection_counts, vector_rejection_ids)

    audit = SearchAudit(
        quantity_vector_count(request.problem),
        examined,
        _checked_product(*(len(state_set.atoms) for state_set in request.problem.terminal_state_sets)),
        tuple(sorted(rejection_counts.items())),
    )
    if best_solution is None:
        proof = build_exhaustive_search_proof(request, audit, BusinessStatus.NO_QUALIFIED_OPPORTUNITY)
        return OracleResult(
            SolveStatus.INFEASIBLE,
            ProofStatus.PROVEN,
            BusinessStatus.NO_QUALIFIED_OPPORTUNITY,
            OptimalityStatus.NOT_APPLICABLE,
            ObjectiveBounds(None, None, None, False),
            None,
            proof,
            None,
        )
    solution = best_solution
    profit = solution.payout_proof.guaranteed_profit_units
    return OracleResult(
        SolveStatus.FEASIBLE,
        ProofStatus.PROVEN,
        BusinessStatus.QUALIFIED_FEASIBLE,
        OptimalityStatus.OPTIMAL,
        ObjectiveBounds(profit, profit, 0, True),
        solution,
        None,
        None,
    )


def solve_optimal(request: OracleRequest) -> OracleResult:
    return _solve_exhaustive(request, SearchMode.OPTIMIZATION)


def diagnose_raw_arbitrage(problem: ArbitrageProblem, budget: OracleBudget) -> OracleResult:
    if not isinstance(problem, ArbitrageProblem) or not isinstance(budget, OracleBudget):
        return _unknown_result(UnknownReason.INVALID_MODEL)
    diagnostic_problem = replace(
        problem,
        problem_id=f"{problem.problem_id}:raw-arbitrage-diagnostic",
        qualification_constraints=(),
    )
    request = OracleRequest(
        REQUEST_SCHEMA_V1,
        SearchMode.RAW_ARBITRAGE_DIAGNOSTIC,
        diagnostic_problem,
        budget,
    )
    result = _solve_exhaustive(request, SearchMode.RAW_ARBITRAGE_DIAGNOSTIC)
    if result.business_status != BusinessStatus.QUALIFIED_FEASIBLE:
        return result
    assert result.solution is not None
    if result.solution.payout_proof.guaranteed_profit_units > 0:
        return result
    total_vectors = quantity_vector_count(diagnostic_problem)
    joint_states = _checked_product(*(len(state_set.atoms) for state_set in diagnostic_problem.terminal_state_sets))
    proof = build_exhaustive_search_proof(
        request,
        SearchAudit(total_vectors, total_vectors, joint_states, (("ALL_ZERO", 1),)),
        BusinessStatus.NO_ARBITRAGE,
        fingerprint(problem),
    )
    return OracleResult(
        SolveStatus.INFEASIBLE,
        ProofStatus.PROVEN,
        BusinessStatus.NO_ARBITRAGE,
        OptimalityStatus.NOT_APPLICABLE,
        result.objective_bounds,
        None,
        proof,
        None,
    )


def find_qualified(request: OracleRequest) -> OracleResult:
    if reason := _search_request_unknown_reason(request, SearchMode.ADMISSION):
        return _unknown_result(reason)

    total_vectors = quantity_vector_count(request.problem)
    joint_states_per_vector = 1
    for state_set in request.problem.terminal_state_sets:
        joint_states_per_vector *= len(state_set.atoms)
    actions = tuple(sorted(request.problem.actions, key=lambda action: action.action_id))
    rejection_counts: dict[str, int] = {}
    examined = 0
    for lots in product(*_quantity_ranges(actions)):
        examined += 1
        vector_rejection_ids: set[str] = set()
        if not any(lots):
            vector_rejection_ids.add("ALL_ZERO")
            _merge_rejections(rejection_counts, vector_rejection_ids)
            continue
        evaluation = evaluate_fixed_portfolio(
            request.problem,
            tuple(ActionQuantity(action.action_id, quantity) for action, quantity in zip(actions, lots, strict=True) if quantity),
            request.budget,
        )
        if evaluation.failed_qualification_ids:
            _record_rejections(vector_rejection_ids, evaluation)
            _merge_rejections(rejection_counts, vector_rejection_ids)
            continue
        solution = _connected_qualified_solution(request.problem, evaluation, request.budget, vector_rejection_ids)
        if isinstance(solution, UnknownReason):
            return _unknown_result(solution)
        if solution is not None:
            profit = solution.payout_proof.guaranteed_profit_units
            return OracleResult(
                SolveStatus.FEASIBLE,
                ProofStatus.PROVEN,
                BusinessStatus.QUALIFIED_FEASIBLE,
                OptimalityStatus.NOT_PROVEN,
                ObjectiveBounds(profit, None, None, False),
                solution,
                None,
                None,
            )
        _merge_rejections(rejection_counts, vector_rejection_ids)
    audit = SearchAudit(total_vectors, examined, joint_states_per_vector, tuple(sorted(rejection_counts.items())))
    proof = build_exhaustive_search_proof(request, audit, BusinessStatus.NO_QUALIFIED_OPPORTUNITY)
    return OracleResult(
        SolveStatus.INFEASIBLE,
        ProofStatus.PROVEN,
        BusinessStatus.NO_QUALIFIED_OPPORTUNITY,
        OptimalityStatus.NOT_APPLICABLE,
        ObjectiveBounds(None, None, None, False),
        None,
        proof,
        None,
    )
