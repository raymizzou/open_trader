from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

import pytest

from open_trader.prediction_solver import (
    BackendResult,
    BenchmarkClassification,
    BenchmarkLimits,
    CertificateEvidence,
    CompiledAdversary,
    INT64_MAX,
    INT64_MIN,
    IntVariable,
    LinearConstraint,
    LinearModel,
    LinearObjective,
    NativeSolveStatus,
    ReleaseProfile,
    SolverEvidence,
    SolverRun,
    TerminationReason,
    UnsafeSolverResult,
    compile_adversary,
    compile_master,
    compile_terminal_model,
    linear_model_fingerprint,
    solver_evidence_from_payload,
    solve_with_constraint_generation,
    validate_backend_result,
    validate_linear_model,
)
from open_trader.prediction_n_leg import (
    ActionPayout,
    ActionQuantity,
    ActionSide,
    ArbitrageProblem,
    BusinessStatus,
    CandidateAction,
    Comparison,
    ConstraintModel,
    ExecutableCostSlice,
    ForbiddenAtomCombination,
    ObjectiveBounds,
    OBSERVATION_SCHEMA_V1,
    OracleBudget,
    OracleRequest,
    PortfolioCandidate,
    OptimalityStatus,
    OracleResult,
    PROBLEM_SCHEMA_V1,
    ProofStatus,
    QualificationConstraint,
    QualificationMetric,
    REQUEST_SCHEMA_V1,
    RelationConstraint,
    RelationKind,
    SettlementObservationKey,
    SettlementScenario,
    SelectedAtom,
    SearchMode,
    SolveStatus,
    TerminalAtom,
    TerminalKind,
    TerminalStateSet,
    UnknownReason,
    canonical_payload,
    fingerprint,
    request_from_payload,
    result_from_payload,
)
from open_trader.prediction_n_leg_oracle import (
    build_relation_components,
    cost_upper_bound,
    cut_from_scenario,
    derive_selected_support_graph,
    evaluate_fixed_portfolio,
    find_qualified,
    solve_optimal,
    split_disconnected_solution,
)


AS_OF = datetime(2026, 8, 12, tzinfo=UTC)
CORPUS_PATH = Path(__file__).with_name("fixtures") / "prediction_n_leg_v1.json"
CORPUS_REPLAYS = tuple(
    (case["case_id"], replay)
    for case in json.loads(CORPUS_PATH.read_text(encoding="utf-8"))["cases"]
    for replay in (case, *case.get("additional_replays", ()))
)


class BruteForceBackend:
    name = "brute-force-test"
    version = "1"

    def solve(self, model: LinearModel, *, time_limit_ms: int) -> BackendResult:
        del time_limit_ms
        validate_linear_model(model)
        variables = {variable.name: variable for variable in model.variables}
        best_values: dict[str, int] | None = None
        best_objective: int | None = None

        def possible(values: dict[str, int]) -> bool:
            for constraint in model.constraints:
                minimum = maximum = 0
                for name, coefficient in constraint.coefficients:
                    if name in values:
                        minimum += coefficient * values[name]
                        maximum += coefficient * values[name]
                    else:
                        variable = variables[name]
                        minimum += coefficient * (variable.lower if coefficient >= 0 else variable.upper)
                        maximum += coefficient * (variable.upper if coefficient >= 0 else variable.lower)
                if constraint.lower is not None and maximum < constraint.lower:
                    return False
                if constraint.upper is not None and minimum > constraint.upper:
                    return False
            return True

        def forced_value(values: dict[str, int]) -> tuple[str, int] | None:
            for constraint in model.constraints:
                if constraint.lower is None or constraint.lower != constraint.upper:
                    continue
                missing = tuple((name, coefficient) for name, coefficient in constraint.coefficients if name not in values)
                if len(missing) != 1:
                    continue
                name, coefficient = missing[0]
                assigned = sum(coefficient_ * values[name_] for name_, coefficient_ in constraint.coefficients if name_ in values)
                dividend = constraint.lower - assigned
                if coefficient and dividend % coefficient == 0:
                    return name, dividend // coefficient
            return None

        def visit(values: dict[str, int]) -> None:
            nonlocal best_objective, best_values
            if not possible(values):
                return
            if len(values) == len(variables):
                objective = 0 if model.objective is None else sum(
                    coefficient * values[name] for name, coefficient in model.objective.coefficients
                )
                if best_values is None or (
                    model.objective is not None
                    and (
                        objective > best_objective
                        if model.objective.sense == "MAX"
                        else objective < best_objective
                    )
                ):
                    best_values = dict(values)
                    best_objective = objective
                return
            forced = forced_value(values)
            if forced is not None:
                name, value = forced
                variable = variables[name]
                if variable.lower <= value <= variable.upper:
                    values[name] = value
                    visit(values)
                    del values[name]
                return
            variable = min(
                (item for item in model.variables if item.name not in values),
                key=lambda item: (item.upper - item.lower, item.name),
            )
            for value in range(variable.lower, variable.upper + 1):
                values[variable.name] = value
                visit(values)
            del values[variable.name]

        visit({})
        if best_values is None:
            return BackendResult(NativeSolveStatus.INFEASIBLE, (), None, None, "infeasible", 0)
        assert best_objective is not None
        return BackendResult(
            NativeSolveStatus.OPTIMAL,
            tuple(sorted(best_values.items())),
            best_objective if model.objective is not None else None,
            best_objective if model.objective is not None else None,
            "optimal",
            0,
        )


class RecordingBackend(BruteForceBackend):
    def __init__(self, *, feasible_adversary: bool = False) -> None:
        self.feasible_adversary = feasible_adversary
        self.calls: list[tuple[LinearModel, BackendResult]] = []

    def solve(self, model: LinearModel, *, time_limit_ms: int) -> BackendResult:
        result = super().solve(model, time_limit_ms=time_limit_ms)
        if (
            self.feasible_adversary
            and model.objective is not None
            and model.objective.sense == "MIN"
            and all(variable.name.startswith("z:") for variable in model.variables)
            and result.status == NativeSolveStatus.OPTIMAL
        ):
            result = replace(result, status=NativeSolveStatus.FEASIBLE, native_status="time limit")
        self.calls.append((model, result))
        return result


class UnknownReachabilityBackend(BruteForceBackend):
    def __init__(self, atom_id: str) -> None:
        self.atom_id = atom_id

    def solve(self, model: LinearModel, *, time_limit_ms: int) -> BackendResult:
        if any(constraint.name == f"reachability:{self.atom_id}" for constraint in model.constraints):
            return BackendResult(NativeSolveStatus.UNKNOWN, (), None, None, "time limit", 0)
        return super().solve(model, time_limit_ms=time_limit_ms)


class UnclosedLexicographicBackend(BruteForceBackend):
    def __init__(self) -> None:
        self.master_objective_calls = 0

    def solve(self, model: LinearModel, *, time_limit_ms: int) -> BackendResult:
        is_master = any(variable.name == "profit" for variable in model.variables) and model.objective is not None
        if is_master:
            self.master_objective_calls += 1
            if self.master_objective_calls == 2:
                return BackendResult(NativeSolveStatus.INFEASIBLE, (), None, None, "inconsistent re-solve", 0)
        return super().solve(model, time_limit_ms=time_limit_ms)


class UnclosedSupportBackend(BruteForceBackend):
    def solve(self, model: LinearModel, *, time_limit_ms: int) -> BackendResult:
        result = super().solve(model, time_limit_ms=time_limit_ms)
        if (
            model.objective is not None
            and model.objective.sense == "MIN"
            and not any(constraint.name == "relation:redundant" for constraint in model.constraints)
            and result.status == NativeSolveStatus.OPTIMAL
        ):
            return replace(result, status=NativeSolveStatus.FEASIBLE, native_status="time limit")
        return result


class FeasibleFirstMasterBackend(BruteForceBackend):
    def __init__(self) -> None:
        self.returned_feasible = False

    def solve(self, model: LinearModel, *, time_limit_ms: int) -> BackendResult:
        result = super().solve(model, time_limit_ms=time_limit_ms)
        if (
            not self.returned_feasible
            and model.objective == LinearObjective("MAX", (("profit", 1),))
            and result.status == NativeSolveStatus.OPTIMAL
        ):
            self.returned_feasible = True
            return replace(result, status=NativeSolveStatus.FEASIBLE, native_status="time limit")
        return result


class FeasibleMasterBackend(BruteForceBackend):
    def solve(self, model: LinearModel, *, time_limit_ms: int) -> BackendResult:
        result = super().solve(model, time_limit_ms=time_limit_ms)
        if model.objective == LinearObjective("MAX", (("profit", 1),)) and result.status == NativeSolveStatus.OPTIMAL:
            return replace(result, status=NativeSolveStatus.FEASIBLE, native_status="time limit")
        return result


class MalformedAssignmentBackend(BruteForceBackend):
    def solve(self, model: LinearModel, *, time_limit_ms: int) -> BackendResult:
        result = super().solve(model, time_limit_ms=time_limit_ms)
        if result.status in {NativeSolveStatus.OPTIMAL, NativeSolveStatus.FEASIBLE}:
            return replace(result, values=())
        return result


class MalformedObjectiveBackend(BruteForceBackend):
    def solve(self, model: LinearModel, *, time_limit_ms: int) -> BackendResult:
        result = super().solve(model, time_limit_ms=time_limit_ms)
        return replace(result, objective_value=INT64_MAX + 1)


class RaisingBackend:
    name = "raising-test"
    version = "1"

    def __init__(self, error: Exception) -> None:
        self.error = error

    def solve(self, model: LinearModel, *, time_limit_ms: int) -> BackendResult:
        del model, time_limit_ms
        raise self.error


def solver_problem(action: CandidateAction) -> ArbitrageProblem:
    key = action.settlement_observation_key
    return ArbitrageProblem(
        PROBLEM_SCHEMA_V1,
        "solver-test",
        AS_OF,
        "usd-cents",
        (action,),
        (
            TerminalStateSet(
                action.market_contract_id,
                key,
                "v1",
                (
                    TerminalAtom(
                        "yes",
                        TerminalKind.NORMAL_YES,
                        "v1",
                        (ActionPayout(action.action_id, 100),),
                        AS_OF + timedelta(days=1),
                    ),
                ),
            ),
        ),
        ConstraintModel((), ()),
        (),
    )


def piecewise_action() -> CandidateAction:
    key = SettlementObservationKey(OBSERVATION_SCHEMA_V1, "oracle", "indicator", AS_OF, AS_OF, "UTC", "v1")
    return CandidateAction(
        "action-a",
        "venue",
        "account",
        "chain",
        "contract-a",
        key,
        ActionSide.BUY_YES,
        1,
        1,
        2,
        7,
        "usd-cents",
        "usd-cents",
        "usd-cents-v1",
        (
            ExecutableCostSlice(1, 3, 2),
            ExecutableCostSlice(4, 5, 4),
            ExecutableCostSlice(6, 7, 9),
        ),
    )


def piecewise_problem() -> ArbitrageProblem:
    action = piecewise_action()
    anchor = replace(
        action,
        action_id="anchor",
        min_quantity_lots=1,
        max_quantity_lots=1,
        cost_slices=(ExecutableCostSlice(1, 1, 0),),
    )
    return replace(
        solver_problem(action),
        actions=(action, anchor),
        terminal_state_sets=(
            replace(
                solver_problem(action).terminal_state_sets[0],
                atoms=(
                    replace(
                        solver_problem(action).terminal_state_sets[0].atoms[0],
                        payouts=(ActionPayout("action-a", 100), ActionPayout("anchor", 100)),
                    ),
                ),
            ),
        ),
    )


def qualification_problem(
    metric: QualificationMetric,
    comparison: Comparison,
    threshold: int,
    *,
    denominator: int = 1,
    payout: int = 10,
) -> ArbitrageProblem:
    action = replace(
        piecewise_action(),
        min_quantity_lots=1,
        max_quantity_lots=1,
        cost_slices=(ExecutableCostSlice(1, 1, 2),),
    )
    base = solver_problem(action)
    atom = replace(
        base.terminal_state_sets[0].atoms[0],
        payouts=(ActionPayout(action.action_id, payout),),
        capital_release_at=AS_OF + timedelta(days=2),
    )
    return replace(
        base,
        terminal_state_sets=(replace(base.terminal_state_sets[0], atoms=(atom,)),),
        qualification_constraints=(QualificationConstraint("qualification", "v1", metric, comparison, threshold, denominator),),
    )


def constraint_generation_problem() -> ArbitrageProblem:
    action = replace(
        piecewise_action(),
        min_quantity_lots=1,
        max_quantity_lots=1,
        cost_slices=(ExecutableCostSlice(1, 1, 1),),
    )
    base = solver_problem(action)
    return replace(
        base,
        terminal_state_sets=(
            replace(
                base.terminal_state_sets[0],
                atoms=(
                    TerminalAtom("a-high", TerminalKind.NORMAL_YES, "v1", (ActionPayout("action-a", 10),), AS_OF + timedelta(days=1)),
                    TerminalAtom("a-low", TerminalKind.NORMAL_NO, "v1", (ActionPayout("action-a", 2),), AS_OF + timedelta(days=1)),
                ),
            ),
        ),
        qualification_constraints=(
            QualificationConstraint(
                "minimum-profit",
                "v1",
                QualificationMetric.GUARANTEED_PROFIT_UNITS,
                Comparison.GREATER_THAN_OR_EQUAL,
                1,
                1,
            ),
        ),
    )


def signed_overflow_problem() -> ArbitrageProblem:
    base = constraint_generation_problem()
    action = replace(base.actions[0], cost_slices=(ExecutableCostSlice(1, 1, -1),))
    state = replace(
        base.terminal_state_sets[0],
        atoms=tuple(
            replace(atom, payouts=(ActionPayout("action-a", INT64_MAX),))
            for atom in base.terminal_state_sets[0].atoms
        ),
    )
    return replace(base, actions=(action,), terminal_state_sets=(state,))


def release_reachability_problem() -> ArbitrageProblem:
    base = constraint_generation_problem()
    state = base.terminal_state_sets[0]
    return replace(
        base,
        terminal_state_sets=(
            replace(
                state,
                atoms=(
                    TerminalAtom("early", TerminalKind.NORMAL_YES, "v1", (ActionPayout("action-a", 2),), AS_OF + timedelta(days=1)),
                    TerminalAtom("late", TerminalKind.NORMAL_NO, "v1", (ActionPayout("action-a", 2),), AS_OF + timedelta(days=10)),
                ),
            ),
        ),
        constraint_model=ConstraintModel((), (ForbiddenAtomCombination("late-unreachable", ("late",), "v1"),)),
        qualification_constraints=(
            QualificationConstraint(
                "release",
                "v1",
                QualificationMetric.MAX_CAPITAL_RELEASE_DELAY_SECONDS,
                Comparison.LESS_THAN_OR_EQUAL,
                86_400,
                1,
            ),
        ),
    )


def redundant_connection_problem() -> ArbitrageProblem:
    base = terminal_problem(
        ("a", "b"),
        relations=(RelationConstraint("redundant", RelationKind.IMPLIES, ("a", "b"), "v1"),),
    )
    actions = tuple(
        replace(action, cost_slices=(ExecutableCostSlice(1, 1, 1),))
        for action in base.actions
    )
    states = tuple(
        replace(
            state,
            atoms=(
                replace(
                    state.atoms[0],
                    payouts=(ActionPayout(f"action-{state.market_contract_id}", 2),),
                ),
            ),
        )
        for state in base.terminal_state_sets
    )
    return replace(
        base,
        actions=actions,
        terminal_state_sets=states,
        qualification_constraints=(
            QualificationConstraint(
                "minimum-profit",
                "v1",
                QualificationMetric.GUARANTEED_PROFIT_UNITS,
                Comparison.GREATER_THAN_OR_EQUAL,
                1,
                1,
            ),
        ),
    )


def lexicographic_tie_problem() -> ArbitrageProblem:
    action = replace(
        piecewise_action(),
        min_quantity_lots=1,
        max_quantity_lots=1,
        cost_slices=(ExecutableCostSlice(1, 1, 1),),
    )
    action_b = replace(action, action_id="action-b")
    base = solver_problem(action)
    state = base.terminal_state_sets[0]
    return replace(
        base,
        actions=(action, action_b),
        terminal_state_sets=(
            replace(
                state,
                atoms=(
                    replace(
                        state.atoms[0],
                        payouts=(ActionPayout("action-a", 1), ActionPayout("action-b", 1)),
                    ),
                ),
            ),
        ),
        qualification_constraints=(
            QualificationConstraint(
                "non-negative",
                "v1",
                QualificationMetric.GUARANTEED_PROFIT_UNITS,
                Comparison.GREATER_THAN_OR_EQUAL,
                0,
                1,
            ),
        ),
    )


def two_component_optimization_problem() -> ArbitrageProblem:
    base = terminal_problem(("a", "b"))
    states = tuple(
        replace(
            state,
            atoms=(
                replace(
                    state.atoms[0],
                    payouts=(
                        ActionPayout(
                            f"action-{state.market_contract_id}",
                            2 if state.market_contract_id == "a" else 4,
                        ),
                    ),
                ),
            ),
        )
        for state in base.terminal_state_sets
    )
    return replace(
        base,
        terminal_state_sets=states,
        qualification_constraints=(
            QualificationConstraint(
                "non-negative",
                "v1",
                QualificationMetric.GUARANTEED_PROFIT_UNITS,
                Comparison.GREATER_THAN_OR_EQUAL,
                0,
                1,
            ),
        ),
    )


def two_profile_optimization_problem() -> ArbitrageProblem:
    base = terminal_problem(
        ("a", "b"),
        relations=(RelationConstraint("connected", RelationKind.IMPLIES, ("a", "b"), "v1"),),
    )
    states = tuple(
        replace(
            state,
            atoms=(
                TerminalAtom(
                    f"{state.market_contract_id}-only",
                    TerminalKind.NORMAL_YES,
                    "v1",
                    (
                        ActionPayout(
                            f"action-{state.market_contract_id}",
                            2 if state.market_contract_id == "a" else 4,
                        ),
                    ),
                    AS_OF + timedelta(days=1 if state.market_contract_id == "a" else 2),
                ),
            ),
        )
        for state in base.terminal_state_sets
    )
    return replace(
        base,
        terminal_state_sets=states,
        qualification_constraints=(
            QualificationConstraint(
                "non-negative",
                "v1",
                QualificationMetric.GUARANTEED_PROFIT_UNITS,
                Comparison.GREATER_THAN_OR_EQUAL,
                0,
                1,
            ),
        ),
    )


def interleaved_quantity_tie_problem() -> ArbitrageProblem:
    action_a = replace(
        piecewise_action(),
        min_quantity_lots=1,
        max_quantity_lots=2,
        cost_slices=(ExecutableCostSlice(1, 2, 1),),
    )
    action_b = replace(
        action_a,
        action_id="action-b",
        max_quantity_lots=1,
        cost_slices=(ExecutableCostSlice(1, 1, 1),),
    )
    action_c = replace(
        action_b,
        action_id="action-c",
        cost_slices=(ExecutableCostSlice(1, 1, 2),),
    )
    base = solver_problem(action_a)
    state = base.terminal_state_sets[0]
    return replace(
        base,
        actions=(action_a, action_b, action_c),
        terminal_state_sets=(
            replace(
                state,
                atoms=(
                    replace(
                        state.atoms[0],
                        payouts=(
                            ActionPayout("action-a", 3),
                            ActionPayout("action-b", 2),
                            ActionPayout("action-c", 5),
                        ),
                    ),
                ),
            ),
        ),
        qualification_constraints=(
            QualificationConstraint(
                "maximum-profit",
                "v1",
                QualificationMetric.GUARANTEED_PROFIT_UNITS,
                Comparison.LESS_THAN_OR_EQUAL,
                5,
                1,
            ),
        ),
    )


def benchmark_limits(rounds: int = 20) -> BenchmarkLimits:
    return BenchmarkLimits(1_000, 2_000, 1_000_000, rounds)


def with_fixed_value(model: LinearModel, variable_name: str, value: int) -> LinearModel:
    return replace(
        model,
        constraints=model.constraints + (LinearConstraint(f"test:fix:{variable_name}", ((variable_name, 1),), value, value),),
    )


def terminal_problem(
    contract_ids: tuple[str, ...],
    *,
    relations: tuple[RelationConstraint, ...] = (),
    forbidden: tuple[ForbiddenAtomCombination, ...] = (),
) -> ArbitrageProblem:
    actions = []
    states = []
    for contract_id in contract_ids:
        key = SettlementObservationKey(
            OBSERVATION_SCHEMA_V1,
            f"oracle-{contract_id}",
            f"indicator-{contract_id}",
            AS_OF,
            AS_OF,
            "UTC",
            "v1",
        )
        action_id = f"action-{contract_id}"
        actions.append(
            CandidateAction(
                action_id,
                "venue",
                "account",
                "chain",
                contract_id,
                key,
                ActionSide.BUY_YES,
                1,
                1,
                1,
                1,
                "usd-cents",
                "usd-cents",
                "usd-cents-v1",
                (ExecutableCostSlice(1, 1, 1),),
            )
        )
        states.append(
            TerminalStateSet(
                contract_id,
                key,
                "v1",
                tuple(
                    TerminalAtom(
                        f"{contract_id}-{suffix}",
                        kind,
                        "v1",
                        (ActionPayout(action_id, payout),),
                        AS_OF + timedelta(days=1),
                    )
                    for suffix, kind, payout in (
                        ("yes", TerminalKind.NORMAL_YES, 100),
                        ("no", TerminalKind.NORMAL_NO, 0),
                        ("void", TerminalKind.VOID, 50),
                    )
                ),
            )
        )
    return ArbitrageProblem(
        PROBLEM_SCHEMA_V1,
        "terminal-test",
        AS_OF,
        "usd-cents",
        tuple(actions),
        tuple(states),
        ConstraintModel(relations, forbidden),
        (),
    )


def with_fixed_atoms(model: LinearModel, atom_values: dict[str, int]) -> LinearModel:
    return replace(
        model,
        constraints=model.constraints
        + tuple(
            LinearConstraint(f"test:fix-atom:{atom_id}", ((f"z:{atom_id}", 1),), value, value)
            for atom_id, value in sorted(atom_values.items())
        ),
    )


def valid_linear_model() -> LinearModel:
    return LinearModel(
        variables=(IntVariable("lots", 0, 4), IntVariable("reserve", -2, 3)),
        constraints=(LinearConstraint("budget", (("lots", 3), ("reserve", -2)), -6, 12),),
        objective=LinearObjective("MAX", (("lots", 5), ("reserve", -1))),
    )


@pytest.mark.parametrize(
    ("quantity", "expected_cost"),
    ((0, 0), (2, 4), (3, 6), (5, 14), (7, 32)),
)
def test_master_piecewise_cost_matches_canonical_quantity_boundaries(quantity: int, expected_cost: int) -> None:
    problem = piecewise_problem()
    component = build_relation_components(problem)[0]
    scenario = SettlementScenario((SelectedAtom("contract-a", "yes"),))
    compiled = compile_master(
        problem,
        component,
        ReleaseProfile(
            86_400,
            1,
            AS_OF + timedelta(days=1),
            ("action-a", "anchor"),
            ("action-a", "anchor"),
        ),
        (cut_from_scenario(problem, scenario),),
        (),
    )
    quantity_variable = dict(compiled.quantity_variables)["action-a"]
    result = BruteForceBackend().solve(with_fixed_value(compiled.model, quantity_variable, quantity), time_limit_ms=1)

    assert cost_upper_bound(problem, (ActionQuantity("action-a", quantity),)) == expected_cost
    assert result.status == NativeSolveStatus.OPTIMAL
    assert dict(result.values)[compiled.cost_variable] == expected_cost


@pytest.mark.parametrize("quantity", (1, 8))
def test_master_rejects_quantities_below_minimum_or_above_maximum(quantity: int) -> None:
    problem = piecewise_problem()
    component = build_relation_components(problem)[0]
    scenario = SettlementScenario((SelectedAtom("contract-a", "yes"),))
    compiled = compile_master(
        problem,
        component,
        ReleaseProfile(
            86_400,
            1,
            AS_OF + timedelta(days=1),
            ("action-a", "anchor"),
            ("action-a", "anchor"),
        ),
        (cut_from_scenario(problem, scenario),),
        (),
    )
    quantity_variable = dict(compiled.quantity_variables)["action-a"]

    result = BruteForceBackend().solve(with_fixed_value(compiled.model, quantity_variable, quantity), time_limit_ms=1)

    assert result.status == NativeSolveStatus.INFEASIBLE


@pytest.mark.parametrize(
    "atom_values",
    (
        {"a-yes": 0, "a-no": 0, "a-void": 0},
        {"a-yes": 1, "a-no": 1},
    ),
)
def test_terminal_model_selects_exactly_one_atom_per_contract(atom_values: dict[str, int]) -> None:
    model = compile_terminal_model(terminal_problem(("a",)))

    result = BruteForceBackend().solve(with_fixed_atoms(model, atom_values), time_limit_ms=1)

    assert result.status == NativeSolveStatus.INFEASIBLE


@pytest.mark.parametrize(
    ("relation", "normal_violation", "exceptional_bypass"),
    (
        (
            RelationConstraint("implies", RelationKind.IMPLIES, ("a", "b"), "v1"),
            ("a-yes", "b-no"),
            ("a-void", "b-no"),
        ),
        (
            RelationConstraint("exclusive", RelationKind.MUTUALLY_EXCLUSIVE, ("a", "b", "c"), "v1"),
            ("a-yes", "b-yes", "c-no"),
            ("a-void", "b-yes", "c-yes"),
        ),
        (
            RelationConstraint("one", RelationKind.EXACTLY_ONE, ("a", "b", "c"), "v1"),
            ("a-yes", "b-yes", "c-no"),
            ("a-void", "b-yes", "c-yes"),
        ),
    ),
)
def test_terminal_relations_apply_only_when_every_selected_atom_is_normal(
    relation: RelationConstraint,
    normal_violation: tuple[str, ...],
    exceptional_bypass: tuple[str, ...],
) -> None:
    problem = terminal_problem(tuple(relation.contract_ids), relations=(relation,))
    model = compile_terminal_model(problem)

    violation = BruteForceBackend().solve(
        with_fixed_atoms(model, {atom_id: 1 for atom_id in normal_violation}),
        time_limit_ms=1,
    )
    bypass = BruteForceBackend().solve(
        with_fixed_atoms(model, {atom_id: 1 for atom_id in exceptional_bypass}),
        time_limit_ms=1,
    )

    assert violation.status == NativeSolveStatus.INFEASIBLE
    assert bypass.status == NativeSolveStatus.OPTIMAL


def test_terminal_forbidden_atom_combination_is_infeasible_even_with_exceptional_atom() -> None:
    problem = terminal_problem(
        ("a", "b"),
        forbidden=(ForbiddenAtomCombination("forbidden", ("a-void", "b-yes"), "v1"),),
    )
    model = compile_terminal_model(problem)

    result = BruteForceBackend().solve(
        with_fixed_atoms(model, {"a-void": 1, "b-yes": 1}),
        time_limit_ms=1,
    )

    assert result.status == NativeSolveStatus.INFEASIBLE


def test_adversary_canonicalizes_fixed_quantities_and_respects_active_constraint_ids() -> None:
    problem = terminal_problem(
        ("b", "a"),
        forbidden=(ForbiddenAtomCombination("not-both-no", ("a-no", "b-no"), "v1"),),
    )
    quantities = (
        ActionQuantity("action-b", 1),
        ActionQuantity("action-a", 1),
    )

    constrained = compile_adversary(problem, quantities)
    relaxed = compile_adversary(problem, quantities, active_constraint_ids=frozenset())
    constrained_result = BruteForceBackend().solve(constrained.model, time_limit_ms=1)
    relaxed_result = BruteForceBackend().solve(relaxed.model, time_limit_ms=1)

    assert isinstance(constrained, CompiledAdversary)
    assert constrained.quantities == (
        ActionQuantity("action-a", 1),
        ActionQuantity("action-b", 1),
    )
    assert constrained_result.status == NativeSolveStatus.OPTIMAL
    assert constrained_result.objective_value == 50
    assert relaxed_result.status == NativeSolveStatus.OPTIMAL
    assert relaxed_result.objective_value == 0


@pytest.mark.parametrize(
    ("metric", "comparison", "denominator", "passing_threshold", "failing_threshold"),
    (
        (QualificationMetric.GUARANTEED_PROFIT_UNITS, Comparison.GREATER_THAN_OR_EQUAL, 3, 24, 25),
        (QualificationMetric.GUARANTEED_PROFIT_UNITS, Comparison.LESS_THAN_OR_EQUAL, 3, 24, 23),
        (QualificationMetric.NET_MARGIN_PPM, Comparison.GREATER_THAN_OR_EQUAL, 3, 2_400_000, 2_400_001),
        (QualificationMetric.NET_MARGIN_PPM, Comparison.LESS_THAN_OR_EQUAL, 3, 2_400_000, 2_399_999),
        (QualificationMetric.ANNUALIZED_RETURN_PPM, Comparison.GREATER_THAN_OR_EQUAL, 2, 1_460_000_000, 1_460_000_001),
        (QualificationMetric.ANNUALIZED_RETURN_PPM, Comparison.LESS_THAN_OR_EQUAL, 2, 1_460_000_000, 1_459_999_999),
        (QualificationMetric.MAX_CAPITAL_RELEASE_DELAY_SECONDS, Comparison.GREATER_THAN_OR_EQUAL, 3, 518_400, 518_401),
        (QualificationMetric.MAX_CAPITAL_RELEASE_DELAY_SECONDS, Comparison.LESS_THAN_OR_EQUAL, 3, 518_400, 518_399),
    ),
)
def test_master_qualification_rows_match_exact_canonical_boundaries(
    metric: QualificationMetric,
    comparison: Comparison,
    denominator: int,
    passing_threshold: int,
    failing_threshold: int,
) -> None:
    results = []
    for threshold in (passing_threshold, failing_threshold):
        problem = qualification_problem(metric, comparison, threshold, denominator=denominator)
        oracle_evaluation = evaluate_fixed_portfolio(
            problem,
            (ActionQuantity("action-a", 1),),
            OracleBudget(2, 1, 1),
        )
        scenario = SettlementScenario((SelectedAtom("contract-a", "yes"),))
        compiled = compile_master(
            problem,
            build_relation_components(problem)[0],
            ReleaseProfile(172_800, 2, AS_OF + timedelta(days=2), ("action-a",), ("action-a",)),
            (cut_from_scenario(problem, scenario),),
            (),
        )
        result = BruteForceBackend().solve(compiled.model, time_limit_ms=1).status
        results.append(result)
        assert (result == NativeSolveStatus.OPTIMAL) == (oracle_evaluation.failed_qualification_ids == ())

    assert results == [NativeSolveStatus.OPTIMAL, NativeSolveStatus.INFEASIBLE]


@pytest.mark.parametrize(
    ("comparison", "threshold", "expected_status"),
    (
        (Comparison.GREATER_THAN_OR_EQUAL, INT64_MIN, NativeSolveStatus.OPTIMAL),
        (Comparison.GREATER_THAN_OR_EQUAL, INT64_MAX, NativeSolveStatus.INFEASIBLE),
        (Comparison.LESS_THAN_OR_EQUAL, INT64_MAX, NativeSolveStatus.OPTIMAL),
        (Comparison.LESS_THAN_OR_EQUAL, INT64_MIN, NativeSolveStatus.INFEASIBLE),
    ),
)
def test_constant_release_qualification_uses_signed_int64_thresholds_without_rewriting(
    comparison: Comparison,
    threshold: int,
    expected_status: NativeSolveStatus,
) -> None:
    problem = qualification_problem(
        QualificationMetric.MAX_CAPITAL_RELEASE_DELAY_SECONDS,
        comparison,
        threshold,
    )
    oracle_evaluation = evaluate_fixed_portfolio(
        problem,
        (ActionQuantity("action-a", 1),),
        OracleBudget(2, 1, 1),
    )
    scenario = SettlementScenario((SelectedAtom("contract-a", "yes"),))
    compiled = compile_master(
        problem,
        build_relation_components(problem)[0],
        ReleaseProfile(172_800, 2, AS_OF + timedelta(days=2), ("action-a",), ("action-a",)),
        (cut_from_scenario(problem, scenario),),
        (),
    )

    result = BruteForceBackend().solve(compiled.model, time_limit_ms=1)

    assert result.status == expected_status
    assert (result.status == NativeSolveStatus.OPTIMAL) == (oracle_evaluation.failed_qualification_ids == ())


@pytest.mark.parametrize("comparison", tuple(Comparison))
def test_positive_margin_threshold_rejects_non_positive_payout(comparison: Comparison) -> None:
    problem = qualification_problem(QualificationMetric.NET_MARGIN_PPM, comparison, 1, payout=0)
    scenario = SettlementScenario((SelectedAtom("contract-a", "yes"),))
    compiled = compile_master(
        problem,
        build_relation_components(problem)[0],
        ReleaseProfile(172_800, 2, AS_OF + timedelta(days=2), ("action-a",), ("action-a",)),
        (cut_from_scenario(problem, scenario),),
        (),
    )

    result = BruteForceBackend().solve(compiled.model, time_limit_ms=1)

    assert result.status == NativeSolveStatus.INFEASIBLE


def test_constraint_generation_adds_canonical_worse_cut_then_closes_fixed_portfolio() -> None:
    problem = constraint_generation_problem()
    request = OracleRequest(REQUEST_SCHEMA_V1, SearchMode.ADMISSION, problem, OracleBudget(2, 2, 2))
    backend = RecordingBackend()

    evidence = solve_with_constraint_generation(request, backend, benchmark_limits())

    master_results = [
        result
        for model, result in backend.calls
        if model.objective == LinearObjective("MAX", (("profit", 1),))
    ]
    adversary_results = [
        result
        for model, result in backend.calls
        if model.objective is not None and model.objective.sense == "MIN" and all(variable.name.startswith("z:") for variable in model.variables)
    ]
    low_scenario = SettlementScenario((SelectedAtom("contract-a", "a-low"),))

    assert [dict(result.values)["payout"] for result in master_results] == [10, 2]
    assert [result.objective_value for result in adversary_results] == [2, 2]
    assert evidence.candidate == PortfolioCandidate((ActionQuantity("action-a", 1),), 1)
    assert evidence.fixed_portfolio_closed is True
    assert evidence.global_search_closed is False
    assert evidence.payout_lower_bound_units == 2
    assert evidence.cost_upper_bound_units == 1
    assert evidence.guaranteed_profit_units == 1
    assert evidence.worst_scenario == low_scenario
    assert evidence.cuts[-1].cut_id == f"cut:{fingerprint(low_scenario)}"
    assert evidence.master_rounds == evidence.adversary_rounds == 2


def test_phase_observer_records_only_achieved_qualified_and_optimal_events() -> None:
    problem = constraint_generation_problem()
    admission_events: list[str] = []
    optimal_events: list[str] = []
    failed_events: list[str] = []

    admission = solve_with_constraint_generation(
        OracleRequest(REQUEST_SCHEMA_V1, SearchMode.ADMISSION, problem, OracleBudget(2, 2, 2)),
        BruteForceBackend(),
        benchmark_limits(),
        phase_observer=admission_events.append,
    )
    optimal = solve_with_constraint_generation(
        OracleRequest(REQUEST_SCHEMA_V1, SearchMode.OPTIMIZATION, problem, OracleBudget(2, 2, 2)),
        BruteForceBackend(),
        benchmark_limits(),
        phase_observer=optimal_events.append,
    )
    failed = solve_with_constraint_generation(
        OracleRequest(REQUEST_SCHEMA_V1, SearchMode.ADMISSION, problem, OracleBudget(2, 2, 2)),
        RecordingBackend(feasible_adversary=True),
        benchmark_limits(),
        phase_observer=failed_events.append,
    )

    assert admission.candidate is not None
    assert admission_events == ["first_qualified"]
    assert optimal.global_search_closed is True
    assert optimal_events == ["first_qualified", "optimal"]
    assert failed.native_status == TerminationReason.PROOF_UNCLOSED
    assert failed_events == []


@pytest.mark.parametrize("mode", (SearchMode.ADMISSION, SearchMode.OPTIMIZATION))
def test_constraint_generation_retains_the_final_fixed_portfolio_cut(mode: SearchMode) -> None:
    corpus = json.loads(
        (Path(__file__).parents[1] / "benchmarks/prediction_solver/corpus/synthetic_v1.json").read_text()
    )
    payload = next(case for case in corpus["cases"] if case["case_id"] == "implies_exceptional")
    request = replace(request_from_payload(payload["request"]), mode=mode)

    evidence = solve_with_constraint_generation(request, BruteForceBackend(), benchmark_limits(100))

    assert evidence.candidate is not None
    assert evidence.worst_scenario is not None
    assert cut_from_scenario(request.problem, evidence.worst_scenario) in evidence.cuts


def test_partial_seed_cut_cannot_close_a_false_no_qualified_result() -> None:
    base = constraint_generation_problem()
    problem = replace(
        base,
        qualification_constraints=(
            QualificationConstraint(
                "maximum-profit",
                "v1",
                QualificationMetric.GUARANTEED_PROFIT_UNITS,
                Comparison.LESS_THAN_OR_EQUAL,
                1,
                1,
            ),
        ),
    )
    request = OracleRequest(REQUEST_SCHEMA_V1, SearchMode.ADMISSION, problem, OracleBudget(2, 2, 2))
    backend = RecordingBackend()

    oracle_result = find_qualified(request)
    evidence = solve_with_constraint_generation(request, backend, benchmark_limits())
    master_results = [
        result
        for model, result in backend.calls
        if model.objective == LinearObjective("MAX", (("profit", 1),))
    ]

    assert oracle_result.business_status == BusinessStatus.QUALIFIED_FEASIBLE
    assert [result.status for result in master_results] == [NativeSolveStatus.OPTIMAL, NativeSolveStatus.OPTIMAL]
    assert [dict(result.values)["payout"] for result in master_results] == [10, 2]
    assert evidence.candidate == PortfolioCandidate((ActionQuantity("action-a", 1),), 1)
    assert evidence.payout_lower_bound_units == 2
    assert evidence.fixed_portfolio_closed is True


def test_master_rejects_repeated_canonical_cuts() -> None:
    problem = constraint_generation_problem()
    scenario = SettlementScenario((SelectedAtom("contract-a", "a-high"),))
    cut = cut_from_scenario(problem, scenario)

    with pytest.raises(ValueError, match="repeated settlement cut"):
        compile_master(
            problem,
            build_relation_components(problem)[0],
            ReleaseProfile(86_400, 1, AS_OF + timedelta(days=1), ("action-a",), ("action-a",)),
            (cut, cut),
            (),
        )


def test_master_rejects_a_cut_that_does_not_match_its_canonical_scenario() -> None:
    problem = constraint_generation_problem()
    scenario = SettlementScenario((SelectedAtom("contract-a", "a-high"),))
    cut = cut_from_scenario(problem, scenario)
    forged_payout = replace(
        cut,
        payout_per_lot=(ActionPayout("action-a", cut.payout_per_lot[0].payout_lower_bound_per_lot_units - 1),),
    )

    for forged_cut in (replace(cut, cut_id="cut:forged"), forged_payout):
        with pytest.raises(ValueError, match="canonical settlement cut"):
            compile_master(
                problem,
                build_relation_components(problem)[0],
                ReleaseProfile(86_400, 1, AS_OF + timedelta(days=1), ("action-a",), ("action-a",)),
                (forged_cut,),
                (),
            )


def test_feasible_adversary_result_never_closes_fixed_portfolio_proof() -> None:
    problem = constraint_generation_problem()
    request = OracleRequest(REQUEST_SCHEMA_V1, SearchMode.ADMISSION, problem, OracleBudget(2, 2, 2))

    evidence = solve_with_constraint_generation(request, RecordingBackend(feasible_adversary=True), benchmark_limits())

    assert evidence.native_status == TerminationReason.PROOF_UNCLOSED
    assert evidence.candidate is None
    assert evidence.fixed_portfolio_closed is False
    assert evidence.global_search_closed is False


def test_contradictory_terminal_constraints_return_unknown_evidence() -> None:
    base = terminal_problem(
        ("a", "b"),
        relations=(RelationConstraint("exclusive", RelationKind.MUTUALLY_EXCLUSIVE, ("a", "b"), "v1"),),
    )
    contradictory = replace(
        base,
        terminal_state_sets=tuple(replace(state, atoms=(state.atoms[0],)) for state in base.terminal_state_sets),
    )
    request = OracleRequest(REQUEST_SCHEMA_V1, SearchMode.ADMISSION, contradictory, OracleBudget(4, 1, 1))

    evidence = solve_with_constraint_generation(request, BruteForceBackend(), benchmark_limits())

    assert evidence.native_status == UnknownReason.CONTRADICTORY_CONSTRAINT_MODEL
    assert evidence.candidate is None
    assert evidence.fixed_portfolio_closed is False
    assert evidence.global_search_closed is False


def test_unreachable_late_atom_does_not_delay_release_profile() -> None:
    problem = release_reachability_problem()
    request = OracleRequest(REQUEST_SCHEMA_V1, SearchMode.ADMISSION, problem, OracleBudget(2, 2, 2))

    evidence = solve_with_constraint_generation(request, BruteForceBackend(), benchmark_limits())

    assert evidence.candidate == PortfolioCandidate((ActionQuantity("action-a", 1),), 1)
    assert evidence.conservative_capital_release_at == AS_OF + timedelta(days=1)


def test_public_master_uses_proved_release_sets_when_a_late_atom_is_unreachable() -> None:
    problem = release_reachability_problem()
    scenario = SettlementScenario((SelectedAtom("contract-a", "early"),))
    profile = ReleaseProfile(
        86_400,
        1,
        AS_OF + timedelta(days=1),
        eligible_action_ids=("action-a",),
        exact_action_ids=("action-a",),
    )

    compiled = compile_master(
        problem,
        build_relation_components(problem)[0],
        profile,
        (cut_from_scenario(problem, scenario),),
        (),
    )
    result = BruteForceBackend().solve(compiled.model, time_limit_ms=1)

    assert result.status == NativeSolveStatus.OPTIMAL
    assert dict(result.values)["q:action-a"] == 1


def test_release_profile_canonicalizes_proved_action_sets() -> None:
    profile = ReleaseProfile(
        86_400,
        1,
        AS_OF + timedelta(days=1),
        eligible_action_ids=("action-b", "action-a"),
        exact_action_ids=("action-b",),
    )

    assert profile.eligible_action_ids == ("action-a", "action-b")
    assert profile.exact_action_ids == ("action-b",)


def test_public_master_rejects_release_timing_that_disagrees_with_release_at() -> None:
    problem = qualification_problem(
        QualificationMetric.MAX_CAPITAL_RELEASE_DELAY_SECONDS,
        Comparison.LESS_THAN_OR_EQUAL,
        86_400,
    )
    scenario = SettlementScenario((SelectedAtom("contract-a", "yes"),))
    inconsistent_profile = ReleaseProfile(
        1,
        1,
        AS_OF + timedelta(days=2),
        ("action-a",),
        ("action-a",),
    )

    with pytest.raises(ValueError, match="release profile timing"):
        compile_master(
            problem,
            build_relation_components(problem)[0],
            inconsistent_profile,
            (cut_from_scenario(problem, scenario),),
            (),
        )


def test_unknown_late_atom_reachability_that_can_change_release_is_proof_unclosed() -> None:
    problem = release_reachability_problem()
    request = OracleRequest(REQUEST_SCHEMA_V1, SearchMode.ADMISSION, problem, OracleBudget(2, 2, 2))

    evidence = solve_with_constraint_generation(request, UnknownReachabilityBackend("late"), benchmark_limits())

    assert evidence.native_status == TerminationReason.PROOF_UNCLOSED
    assert evidence.candidate is None
    assert evidence.fixed_portfolio_closed is False


def test_disconnected_parent_vector_is_excluded_and_only_a_child_is_admitted() -> None:
    problem = redundant_connection_problem()
    request = OracleRequest(REQUEST_SCHEMA_V1, SearchMode.ADMISSION, problem, OracleBudget(4, 1, 4))

    evidence = solve_with_constraint_generation(request, BruteForceBackend(), benchmark_limits())

    assert evidence.candidate is not None
    assert len(evidence.candidate.quantities) == 1
    assert evidence.candidate.claimed_guaranteed_profit_units == 1
    assert evidence.fixed_portfolio_closed is True


def test_unclosed_support_recheck_makes_the_request_unknown() -> None:
    problem = redundant_connection_problem()
    request = OracleRequest(REQUEST_SCHEMA_V1, SearchMode.ADMISSION, problem, OracleBudget(4, 1, 4))

    evidence = solve_with_constraint_generation(request, UnclosedSupportBackend(), benchmark_limits())

    assert evidence.native_status == TerminationReason.PROOF_UNCLOSED
    assert evidence.candidate is None
    assert evidence.fixed_portfolio_closed is False


def test_optimization_uses_lexicographically_earliest_selected_action_ids() -> None:
    problem = lexicographic_tie_problem()
    request = OracleRequest(REQUEST_SCHEMA_V1, SearchMode.OPTIMIZATION, problem, OracleBudget(4, 1, 1))

    evidence = solve_with_constraint_generation(request, BruteForceBackend(), benchmark_limits())

    assert evidence.candidate == PortfolioCandidate((ActionQuantity("action-a", 1),), 0)
    assert evidence.objective_bounds == ObjectiveBounds(0, 0, 0, True)
    assert evidence.global_search_closed is True


def test_optimization_interleaves_each_selected_action_quantity_in_the_canonical_tuple_order() -> None:
    problem = interleaved_quantity_tie_problem()
    request = OracleRequest(REQUEST_SCHEMA_V1, SearchMode.OPTIMIZATION, problem, OracleBudget(12, 1, 1))
    expected_quantities = (
        ActionQuantity("action-a", 1),
        ActionQuantity("action-c", 1),
    )

    oracle_result = solve_optimal(request)
    evidence = solve_with_constraint_generation(request, BruteForceBackend(), benchmark_limits())

    assert oracle_result.solution is not None
    assert oracle_result.solution.quantities == expected_quantities
    assert evidence.candidate == PortfolioCandidate(expected_quantities, 5)
    assert evidence.global_search_closed is True


def test_optimization_continues_until_every_component_search_closes() -> None:
    problem = two_component_optimization_problem()
    request = OracleRequest(REQUEST_SCHEMA_V1, SearchMode.OPTIMIZATION, problem, OracleBudget(4, 1, 1))

    evidence = solve_with_constraint_generation(request, BruteForceBackend(), benchmark_limits())

    assert evidence.candidate == PortfolioCandidate((ActionQuantity("action-b", 1),), 3)
    assert evidence.objective_bounds == ObjectiveBounds(3, 3, 0, True)
    assert evidence.global_search_closed is True


def test_optimization_continues_until_every_release_profile_search_closes() -> None:
    problem = two_profile_optimization_problem()
    request = OracleRequest(REQUEST_SCHEMA_V1, SearchMode.OPTIMIZATION, problem, OracleBudget(4, 1, 1))

    evidence = solve_with_constraint_generation(request, BruteForceBackend(), benchmark_limits())

    assert evidence.candidate == PortfolioCandidate((ActionQuantity("action-b", 1),), 3)
    assert evidence.conservative_capital_release_at == AS_OF + timedelta(days=2)
    assert evidence.objective_bounds == ObjectiveBounds(3, 3, 0, True)
    assert evidence.global_search_closed is True


def test_unclosed_later_lexicographic_objective_cannot_become_a_negative_result() -> None:
    problem = lexicographic_tie_problem()
    request = OracleRequest(REQUEST_SCHEMA_V1, SearchMode.OPTIMIZATION, problem, OracleBudget(4, 1, 1))

    evidence = solve_with_constraint_generation(request, UnclosedLexicographicBackend(), benchmark_limits())

    assert evidence.native_status == TerminationReason.PROOF_UNCLOSED
    assert evidence.candidate is None
    assert evidence.global_search_closed is False


def test_unclosed_raw_first_objective_never_becomes_closed_no_arbitrage() -> None:
    problem = lexicographic_tie_problem()
    request = OracleRequest(REQUEST_SCHEMA_V1, SearchMode.RAW_ARBITRAGE_DIAGNOSTIC, problem, OracleBudget(4, 1, 1))

    evidence = solve_with_constraint_generation(request, FeasibleFirstMasterBackend(), benchmark_limits())

    assert evidence.native_status == TerminationReason.PROOF_UNCLOSED
    assert evidence.global_search_closed is False
    assert evidence.objective_bounds.closed is False


def test_admission_preserves_a_feasible_master_native_status() -> None:
    problem = constraint_generation_problem()
    request = OracleRequest(REQUEST_SCHEMA_V1, SearchMode.ADMISSION, problem, OracleBudget(2, 2, 2))

    evidence = solve_with_constraint_generation(request, FeasibleMasterBackend(), benchmark_limits())

    assert evidence.candidate is not None
    assert evidence.native_status == NativeSolveStatus.FEASIBLE
    assert evidence.objective_bounds.closed is False


def test_signed_int64_overflow_in_compiled_profit_is_numeric_unsafe() -> None:
    problem = signed_overflow_problem()
    request = OracleRequest(REQUEST_SCHEMA_V1, SearchMode.ADMISSION, problem, OracleBudget(2, 2, 2))

    evidence = solve_with_constraint_generation(request, BruteForceBackend(), benchmark_limits())

    assert evidence.native_status == TerminationReason.NUMERIC_UNSAFE
    assert evidence.candidate is None


def test_public_compiler_raises_actual_overflow_for_unsafe_signed_int64_arithmetic() -> None:
    problem = signed_overflow_problem()
    scenario = SettlementScenario((SelectedAtom("contract-a", "a-high"),))

    with pytest.raises(OverflowError):
        compile_master(
            problem,
            build_relation_components(problem)[0],
            ReleaseProfile(86_400, 1, AS_OF + timedelta(days=1), ("action-a",), ("action-a",)),
            (cut_from_scenario(problem, scenario),),
            (),
        )


def test_malformed_backend_assignment_is_invalid_output_not_numeric_unsafe() -> None:
    problem = constraint_generation_problem()
    request = OracleRequest(REQUEST_SCHEMA_V1, SearchMode.ADMISSION, problem, OracleBudget(2, 2, 2))

    evidence = solve_with_constraint_generation(request, MalformedAssignmentBackend(), benchmark_limits())

    assert evidence.native_status == TerminationReason.INVALID_OUTPUT
    assert evidence.candidate is None
    assert evidence.fixed_portfolio_closed is False


def test_malformed_backend_objective_is_invalid_output_not_numeric_unsafe() -> None:
    problem = constraint_generation_problem()
    request = OracleRequest(REQUEST_SCHEMA_V1, SearchMode.ADMISSION, problem, OracleBudget(2, 2, 2))

    evidence = solve_with_constraint_generation(request, MalformedObjectiveBackend(), benchmark_limits())

    assert evidence.native_status == TerminationReason.INVALID_OUTPUT
    assert evidence.candidate is None


@pytest.mark.parametrize(
    "error",
    (ValueError("internal value defect"), KeyError("internal key defect"), TypeError("internal type defect")),
    ids=("value-error", "key-error", "type-error"),
)
def test_unexpected_internal_defects_propagate_to_the_worker_boundary(error: Exception) -> None:
    problem = constraint_generation_problem()
    request = OracleRequest(REQUEST_SCHEMA_V1, SearchMode.ADMISSION, problem, OracleBudget(2, 2, 2))

    with pytest.raises(type(error)):
        solve_with_constraint_generation(request, RaisingBackend(error), benchmark_limits())


def test_backend_originated_overflow_propagates_to_the_worker_boundary() -> None:
    problem = constraint_generation_problem()
    request = OracleRequest(REQUEST_SCHEMA_V1, SearchMode.ADMISSION, problem, OracleBudget(2, 2, 2))

    with pytest.raises(OverflowError, match="adapter overflow defect"):
        solve_with_constraint_generation(
            request,
            RaisingBackend(OverflowError("adapter overflow defect")),
            benchmark_limits(),
        )


@pytest.mark.parametrize(("case_id", "replay"), CORPUS_REPLAYS, ids=[f"{case_id}:{replay['request']['mode']}" for case_id, replay in CORPUS_REPLAYS])
def test_common_engine_matches_frozen_oracle_business_and_proof_safety(case_id: str, replay: dict[str, object]) -> None:
    del case_id
    request = request_from_payload(replay["request"])  # type: ignore[arg-type]
    expected = result_from_payload(replay["expected_result"])  # type: ignore[arg-type]

    evidence = solve_with_constraint_generation(request, BruteForceBackend(), benchmark_limits(100))

    if expected.business_status == BusinessStatus.QUALIFIED_FEASIBLE:
        assert evidence.candidate is not None
        assert evidence.fixed_portfolio_closed is True
        checked_problem = (
            replace(
                request.problem,
                problem_id=f"{request.problem.problem_id}:raw-arbitrage-diagnostic",
                qualification_constraints=(),
            )
            if request.mode == SearchMode.RAW_ARBITRAGE_DIAGNOSTIC
            else request.problem
        )
        evaluation = evaluate_fixed_portfolio(checked_problem, evidence.candidate.quantities, request.budget)
        support = derive_selected_support_graph(checked_problem, evaluation, request.budget)
        assert not isinstance(support, UnknownReason)
        assert evaluation.failed_qualification_ids == ()
        assert len(split_disconnected_solution(checked_problem, evaluation, support)) == 1
        assert evidence.payout_lower_bound_units == evaluation.payout_lower_bound_units
        assert evidence.cost_upper_bound_units == evaluation.cost_upper_bound_units
        assert evidence.guaranteed_profit_units == evaluation.guaranteed_profit_units
        assert evidence.conservative_capital_release_at == evaluation.conservative_capital_release_at
        if request.mode != SearchMode.ADMISSION:
            assert expected.solution is not None
            assert evidence.candidate.quantities == expected.solution.quantities
            assert evidence.objective_bounds == expected.objective_bounds
    elif expected.business_status == BusinessStatus.NO_QUALIFIED_OPPORTUNITY:
        assert evidence.native_status == BusinessStatus.NO_QUALIFIED_OPPORTUNITY
        assert evidence.candidate is None
        assert evidence.global_search_closed is True
    elif expected.business_status == BusinessStatus.NO_ARBITRAGE:
        assert evidence.native_status == BusinessStatus.NO_ARBITRAGE
        assert evidence.candidate is None
        assert evidence.global_search_closed is True
        assert evidence.objective_bounds == expected.objective_bounds
    else:
        assert expected.unknown_reason is not None
        if expected.unknown_reason in {
            UnknownReason.ORACLE_DECISION_LIMIT_EXCEEDED,
            UnknownReason.ORACLE_STATE_LIMIT_EXCEEDED,
        }:
            assert evidence.native_status not in {
                BusinessStatus.NO_QUALIFIED_OPPORTUNITY,
                BusinessStatus.NO_ARBITRAGE,
            }
        else:
            assert evidence.candidate is None
            assert evidence.fixed_portfolio_closed is False
            assert evidence.native_status == expected.unknown_reason


@pytest.mark.parametrize(
    ("model", "expected_exception"),
    (
        (
            replace(valid_linear_model(), variables=(IntVariable("lots", 0, 1), IntVariable("lots", 0, 1))),
            ValueError,
        ),
        (
            replace(valid_linear_model(), constraints=(LinearConstraint("budget", (("missing", 1),), None, None),)),
            ValueError,
        ),
        (
            replace(valid_linear_model(), variables=(IntVariable("lots", True, 1), IntVariable("reserve", -2, 3))),
            ValueError,
        ),
        (
            replace(valid_linear_model(), constraints=(LinearConstraint("budget", (("lots", 1),), 2, 1),)),
            ValueError,
        ),
        (
            replace(valid_linear_model(), constraints=(LinearConstraint("budget", (("lots", INT64_MAX + 1),), None, None),)),
            OverflowError,
        ),
        (
            replace(valid_linear_model(), variables=(IntVariable("lots", 0, INT64_MAX + 1), IntVariable("reserve", -2, 3))),
            OverflowError,
        ),
        (
            LinearModel((IntVariable("lots", 2, INT64_MAX),), (LinearConstraint("budget", (("lots", 2),), None, None),), None),
            OverflowError,
        ),
    ),
)
def test_linear_model_rejects_unsafe_integer_ir(
    model: LinearModel,
    expected_exception: type[Exception],
) -> None:
    with pytest.raises(expected_exception):
        validate_linear_model(model)


def test_linear_model_rejects_possible_objective_activity_outside_int64() -> None:
    model = LinearModel(
        variables=(IntVariable("lots", 2, INT64_MAX),),
        constraints=(),
        objective=LinearObjective("MAX", (("lots", 2),)),
    )

    with pytest.raises(OverflowError, match="possible objective activity exceeds signed int64"):
        validate_linear_model(model)


def test_linear_model_rejects_duplicate_constraint_names() -> None:
    model = replace(
        valid_linear_model(),
        constraints=(
            LinearConstraint("budget", (("lots", 1),), None, 4),
            LinearConstraint("budget", (("reserve", 1),), -2, 3),
        ),
    )

    with pytest.raises(ValueError, match="duplicate constraint name"):
        validate_linear_model(model)


def test_linear_model_canonicalizes_coefficient_order_for_fingerprint() -> None:
    model = valid_linear_model()
    shuffled = replace(
        model,
        constraints=(LinearConstraint("budget", (("reserve", -2), ("lots", 3)), -6, 12),),
        objective=LinearObjective("MAX", (("reserve", -1), ("lots", 5))),
    )

    validate_linear_model(model)
    validate_linear_model(shuffled)

    assert shuffled.constraints[0].coefficients == model.constraints[0].coefficients
    assert shuffled.objective == model.objective
    assert linear_model_fingerprint(shuffled) == linear_model_fingerprint(model)


def test_backend_result_rejects_non_integer_incomplete_extra_and_infeasible_values() -> None:
    model = valid_linear_model()
    unsafe_results = (
        BackendResult(NativeSolveStatus.FEASIBLE, (("lots", True), ("reserve", 0)), 0, None, "feasible", 1),
        BackendResult(NativeSolveStatus.FEASIBLE, (("lots", 1),), 0, None, "feasible", 1),
        BackendResult(NativeSolveStatus.FEASIBLE, (("lots", 1), ("reserve", 0), ("extra", 0)), 0, None, "feasible", 1),
        BackendResult(NativeSolveStatus.FEASIBLE, (("lots", 5), ("reserve", 0)), 0, None, "feasible", 1),
        BackendResult(NativeSolveStatus.FEASIBLE, (("lots", 4), ("reserve", -2)), 0, None, "feasible", 1),
        BackendResult(NativeSolveStatus.FEASIBLE, (("lots", 2), ("reserve", -2)), 13, None, "feasible", 1),
    )

    for result in unsafe_results:
        with pytest.raises(UnsafeSolverResult):
            validate_backend_result(model, result)


def test_backend_result_accepts_exact_integer_values_that_satisfy_every_row() -> None:
    model = valid_linear_model()
    result = BackendResult(NativeSolveStatus.FEASIBLE, (("reserve", -2), ("lots", 2)), 12, None, "feasible", 1)

    validate_backend_result(model, result)


def valid_certificate() -> CertificateEvidence:
    return CertificateEvidence(
        certificate_sha256="sha256:" + "a" * 64,
        certificate_size_bytes=10,
        completed_certificate_sha256="sha256:" + "b" * 64,
        completed_certificate_size_bytes=12,
        checker_name="viprchk",
        checker_version="1.0",
        checker_exit_code=0,
        checker_succeeded=True,
        generation_ns=1,
        completion_ns=2,
        check_ns=3,
    )


def valid_solver_evidence() -> SolverEvidence:
    return SolverEvidence(
        native_status="optimal",
        candidate=None,
        objective_bounds=ObjectiveBounds(None, None, None, False),
        worst_scenario=None,
        payout_lower_bound_units=None,
        cost_upper_bound_units=None,
        guaranteed_profit_units=None,
        conservative_capital_release_at=None,
        fixed_portfolio_closed=False,
        global_search_closed=False,
        master_rounds=0,
        adversary_rounds=0,
        cuts=(),
        certificate=None,
    )


def nested_solver_evidence() -> SolverEvidence:
    problem = constraint_generation_problem()
    scenario = SettlementScenario((SelectedAtom("contract-a", "a-low"),))
    return SolverEvidence(
        native_status="OPTIMAL",
        candidate=PortfolioCandidate((ActionQuantity("action-a", 1),), 1),
        objective_bounds=ObjectiveBounds(1, 1, 0, True),
        worst_scenario=scenario,
        payout_lower_bound_units=2,
        cost_upper_bound_units=1,
        guaranteed_profit_units=1,
        conservative_capital_release_at=AS_OF + timedelta(days=1),
        fixed_portfolio_closed=True,
        global_search_closed=True,
        master_rounds=2,
        adversary_rounds=3,
        cuts=(cut_from_scenario(problem, scenario),),
        certificate=valid_certificate(),
    )


def test_solver_evidence_payload_round_trips_every_nested_canonical_field() -> None:
    evidence = nested_solver_evidence()
    payload = canonical_payload(evidence)

    decoded = solver_evidence_from_payload(payload)

    assert decoded == evidence
    assert canonical_payload(decoded) == payload


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        (lambda payload: payload.pop("candidate"), "solver evidence"),
        (lambda payload: payload.__setitem__("extra", 1), "solver evidence"),
        (lambda payload: payload["candidate"]["quantities"][0].__setitem__("quantity_lots", True), "quantity_lots"),
        (lambda payload: payload["worst_scenario"].__setitem__("extra", 1), "scenario"),
        (lambda payload: payload["objective_bounds"].__setitem__("upper_bound_units", 2**63), "upper_bound_units"),
        (lambda payload: payload.__setitem__("conservative_capital_release_at", "2026-08-13T00:00:00+01:00"), "UTC"),
        (lambda payload: payload["candidate"].__setitem__("claimed_guaranteed_profit_units", 2), "candidate"),
    ),
)
def test_solver_evidence_payload_rejects_malformed_or_inconsistent_nested_claims(
    mutation,
    message: str,
) -> None:
    payload = canonical_payload(nested_solver_evidence())
    mutation(payload)

    with pytest.raises(ValueError, match=message):
        solver_evidence_from_payload(payload)


def valid_solver_run(**changes: object) -> SolverRun:
    values: dict[str, object] = {
        "schema_version": "open_trader.prediction_solver.protocol.v1",
        "request_id": "request-1",
        "solver_name": "test-solver",
        "solver_version": "1.0",
        "adapter_version": "1.0",
        "worker_id": "worker-1",
        "environment_id": "test-env",
        "solve_status": SolveStatus.UNKNOWN,
        "proof_status": ProofStatus.UNKNOWN,
        "business_status": BusinessStatus.UNKNOWN,
        "optimality_status": OptimalityStatus.NOT_APPLICABLE,
        "objective_bounds": ObjectiveBounds(None, None, None, False),
        "classification": BenchmarkClassification.UNKNOWN,
        "termination_reason": TerminationReason.CRASH,
        "evidence": None,
        "canonical_result": None,
        "phase_timings_ns": (("decode", 0),),
        "peak_rss_bytes": 0,
        "diagnostics": (("error", "worker crashed"),),
    }
    values.update(changes)
    return SolverRun(**values)  # type: ignore[arg-type]


def unknown_oracle_result() -> OracleResult:
    return OracleResult(
        SolveStatus.UNKNOWN,
        ProofStatus.UNKNOWN,
        BusinessStatus.UNKNOWN,
        OptimalityStatus.NOT_APPLICABLE,
        ObjectiveBounds(None, None, None, False),
        None,
        None,
        UnknownReason.INVALID_MODEL,
    )


def test_benchmark_limits_and_certificate_evidence_reject_unsafe_values() -> None:
    assert BenchmarkLimits(1, 2, 3, 4).hard_time_limit_ms == 2
    assert valid_certificate().checker_succeeded is True

    with pytest.raises(ValueError):
        BenchmarkLimits(True, 2, 3, 4)
    with pytest.raises(ValueError):
        BenchmarkLimits(2, 1, 3, 4)
    with pytest.raises(ValueError):
        replace(valid_certificate(), certificate_sha256="sha256:" + "A" * 64)
    with pytest.raises(ValueError):
        replace(valid_certificate(), completed_certificate_size_bytes=None)
    with pytest.raises(ValueError):
        replace(valid_certificate(), checker_succeeded=True, checker_exit_code=1)


def test_solver_run_requires_checked_canonical_result_and_matching_axes() -> None:
    with pytest.raises(AssertionError):
        valid_solver_run(
            classification=BenchmarkClassification.CHECKED,
            canonical_result=None,
        )
    with pytest.raises(AssertionError):
        valid_solver_run(
            classification=BenchmarkClassification.MEASUREMENT_ONLY,
            canonical_result=unknown_oracle_result(),
        )
    checked = valid_solver_run(
        classification=BenchmarkClassification.CHECKED,
        canonical_result=unknown_oracle_result(),
    )
    assert checked.canonical_result == unknown_oracle_result()
    with pytest.raises(AssertionError):
        valid_solver_run(
            classification=BenchmarkClassification.CHECKED,
            canonical_result=unknown_oracle_result(),
            solve_status=SolveStatus.FEASIBLE,
        )


def test_solver_run_certificate_and_unknown_classifications_stay_fail_closed() -> None:
    certificate_checked = valid_solver_run(
        classification=BenchmarkClassification.CERTIFICATE_CHECKED,
        termination_reason=TerminationReason.COMPLETED,
        evidence=replace(valid_solver_evidence(), certificate=valid_certificate()),
    )
    assert certificate_checked.evidence is not None
    with pytest.raises(AssertionError):
        valid_solver_run(
            classification=BenchmarkClassification.CERTIFICATE_CHECKED,
            evidence=valid_solver_evidence(),
        )
    with pytest.raises(AssertionError):
        valid_solver_run(
            classification=BenchmarkClassification.UNKNOWN,
            solve_status=SolveStatus.FEASIBLE,
        )
    with pytest.raises(ValueError):
        valid_solver_run(
            classification=BenchmarkClassification.MEASUREMENT_ONLY,
            proof_status=ProofStatus.PROVEN,
        )
