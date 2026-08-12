from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol

from open_trader.prediction_n_leg import (
    ActionQuantity,
    ArbitrageProblem,
    BusinessStatus,
    Comparison,
    ObjectiveBounds,
    OptimalityStatus,
    OracleRequest,
    OracleResult,
    PortfolioCandidate,
    ProofStatus,
    QualificationConstraint,
    QualificationMetric,
    REQUEST_SCHEMA_V1,
    RelationKind,
    SearchMode,
    SelectedAtom,
    SelectedSupportGraph,
    SettlementScenario,
    SolveStatus,
    TerminalKind,
    UnknownReason,
    WorstStateCut,
    fingerprint,
    validate_problem,
)
from open_trader.prediction_n_leg_oracle import (
    PortfolioEvaluation,
    RelationComponent,
    build_relation_components,
    cut_from_scenario,
    split_disconnected_solution,
)


BENCHMARK_PROTOCOL_V1 = "open_trader.prediction_solver.protocol.v1"
INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1


class NativeSolveStatus(StrEnum):
    OPTIMAL = "OPTIMAL"
    FEASIBLE = "FEASIBLE"
    INFEASIBLE = "INFEASIBLE"
    UNKNOWN = "UNKNOWN"


class BenchmarkClassification(StrEnum):
    CHECKED = "CHECKED"
    CERTIFICATE_CHECKED = "CERTIFICATE_CHECKED"
    MEASUREMENT_ONLY = "MEASUREMENT_ONLY"
    UNKNOWN = "UNKNOWN"


class TerminationReason(StrEnum):
    COMPLETED = "COMPLETED"
    SOFT_TIMEOUT = "SOFT_TIMEOUT"
    HARD_TIMEOUT = "HARD_TIMEOUT"
    MEMORY_LIMIT = "MEMORY_LIMIT"
    CRASH = "CRASH"
    INVALID_OUTPUT = "INVALID_OUTPUT"
    PROTOCOL_MISMATCH = "PROTOCOL_MISMATCH"
    NUMERIC_UNSAFE = "NUMERIC_UNSAFE"
    PROOF_UNCLOSED = "PROOF_UNCLOSED"


_SolveFailure = UnknownReason | TerminationReason


class UnsafeSolverResult(ValueError):
    pass


class _NumericUnsafeError(OverflowError, ValueError):
    pass


@dataclass(frozen=True, slots=True)
class IntVariable:
    name: str
    lower: int
    upper: int
    integer: bool = True


@dataclass(frozen=True, slots=True)
class LinearConstraint:
    name: str
    coefficients: tuple[tuple[str, int], ...]
    lower: int | None
    upper: int | None

    def __post_init__(self) -> None:
        if isinstance(self.coefficients, tuple) and all(
            isinstance(term, tuple) and len(term) == 2 and isinstance(term[0], str)
            for term in self.coefficients
        ):
            object.__setattr__(self, "coefficients", tuple(sorted(self.coefficients, key=lambda term: term[0])))


@dataclass(frozen=True, slots=True)
class LinearObjective:
    sense: Literal["MAX", "MIN"]
    coefficients: tuple[tuple[str, int], ...]

    def __post_init__(self) -> None:
        if isinstance(self.coefficients, tuple) and all(
            isinstance(term, tuple) and len(term) == 2 and isinstance(term[0], str)
            for term in self.coefficients
        ):
            object.__setattr__(self, "coefficients", tuple(sorted(self.coefficients, key=lambda term: term[0])))


@dataclass(frozen=True, slots=True)
class LinearModel:
    variables: tuple[IntVariable, ...]
    constraints: tuple[LinearConstraint, ...]
    objective: LinearObjective | None

    def __post_init__(self) -> None:
        if isinstance(self.variables, tuple) and all(isinstance(item, IntVariable) and isinstance(item.name, str) for item in self.variables):
            object.__setattr__(self, "variables", tuple(sorted(self.variables, key=lambda item: item.name)))
        if isinstance(self.constraints, tuple) and all(isinstance(item, LinearConstraint) and isinstance(item.name, str) for item in self.constraints):
            object.__setattr__(self, "constraints", tuple(sorted(self.constraints, key=lambda item: item.name)))


@dataclass(frozen=True, slots=True)
class BackendResult:
    status: NativeSolveStatus
    values: tuple[tuple[str, int], ...]
    objective_value: int | None
    objective_bound: int | None
    native_status: str
    solve_ns: int


class SolverBackend(Protocol):
    name: str
    version: str

    def solve(self, model: LinearModel, *, time_limit_ms: int) -> BackendResult:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class BenchmarkLimits:
    soft_time_limit_ms: int
    hard_time_limit_ms: int
    memory_limit_bytes: int
    max_constraint_generation_rounds: int

    def __post_init__(self) -> None:
        for name in ("soft_time_limit_ms", "hard_time_limit_ms", "memory_limit_bytes", "max_constraint_generation_rounds"):
            _positive_int(getattr(self, name), name)
        if self.hard_time_limit_ms < self.soft_time_limit_ms:
            raise ValueError("hard_time_limit_ms must be at least soft_time_limit_ms")


@dataclass(frozen=True, slots=True)
class ReleaseProfile:
    delay_seconds: int
    occupied_days: int
    release_at: datetime
    eligible_action_ids: tuple[str, ...]
    exact_action_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _nonnegative_int(self.delay_seconds, "delay_seconds")
        _positive_int(self.occupied_days, "occupied_days")
        if not isinstance(self.release_at, datetime):
            raise ValueError("release_at must be a datetime")
        eligible_action_ids = _canonical_action_ids(self.eligible_action_ids, "eligible_action_ids")
        exact_action_ids = _canonical_action_ids(self.exact_action_ids, "exact_action_ids")
        if not exact_action_ids:
            raise ValueError("exact_action_ids must not be empty")
        if not set(exact_action_ids) <= set(eligible_action_ids):
            raise ValueError("exact_action_ids must be a subset of eligible_action_ids")
        object.__setattr__(self, "eligible_action_ids", eligible_action_ids)
        object.__setattr__(self, "exact_action_ids", exact_action_ids)


@dataclass(frozen=True, slots=True)
class CompiledMaster:
    model: LinearModel
    component: RelationComponent
    release_profile: ReleaseProfile
    quantity_variables: tuple[tuple[str, str], ...]
    selected_variables: tuple[tuple[str, str], ...]
    payout_variable: str
    cost_variable: str
    profit_variable: str


@dataclass(frozen=True, slots=True)
class CompiledAdversary:
    model: LinearModel
    quantities: tuple[ActionQuantity, ...]
    atom_variables: tuple[tuple[SelectedAtom, str], ...]


@dataclass(frozen=True, slots=True)
class _QualificationFormula:
    left_coefficients: tuple[tuple[Literal["profit", "payout", "cost"], int], ...]
    right_coefficients: tuple[tuple[Literal["profit", "payout", "cost"], int], ...]
    left_constant: int
    right_constant: int
    requires_positive_payout: bool = False


@dataclass(frozen=True, slots=True)
class CertificateEvidence:
    certificate_sha256: str
    certificate_size_bytes: int
    completed_certificate_sha256: str | None
    completed_certificate_size_bytes: int | None
    checker_name: str
    checker_version: str
    checker_exit_code: int
    checker_succeeded: bool
    generation_ns: int
    completion_ns: int
    check_ns: int

    def __post_init__(self) -> None:
        _sha256(self.certificate_sha256, "certificate_sha256")
        _nonnegative_int(self.certificate_size_bytes, "certificate_size_bytes")
        if (self.completed_certificate_sha256 is None) != (self.completed_certificate_size_bytes is None):
            raise ValueError("completed certificate hash and size must both be present or absent")
        if self.completed_certificate_sha256 is not None:
            _sha256(self.completed_certificate_sha256, "completed_certificate_sha256")
            _nonnegative_int(self.completed_certificate_size_bytes, "completed_certificate_size_bytes")
        _string(self.checker_name, "checker_name", allow_empty=True)
        _string(self.checker_version, "checker_version", allow_empty=True)
        _strict_int(self.checker_exit_code, "checker_exit_code")
        if not isinstance(self.checker_succeeded, bool):
            raise ValueError("checker_succeeded must be a bool")
        for name in ("generation_ns", "completion_ns", "check_ns"):
            _nonnegative_int(getattr(self, name), name)
        if self.checker_succeeded and (
            self.checker_exit_code != 0
            or not self.checker_name.strip()
            or not self.checker_version.strip()
        ):
            raise ValueError("successful certificate checks require exit code zero and checker identity")


@dataclass(frozen=True, slots=True)
class SolverEvidence:
    native_status: str
    candidate: PortfolioCandidate | None
    objective_bounds: ObjectiveBounds
    worst_scenario: SettlementScenario | None
    payout_lower_bound_units: int | None
    cost_upper_bound_units: int | None
    guaranteed_profit_units: int | None
    conservative_capital_release_at: datetime | None
    fixed_portfolio_closed: bool
    global_search_closed: bool
    master_rounds: int
    adversary_rounds: int
    cuts: tuple[WorstStateCut, ...]
    certificate: CertificateEvidence | None

    def __post_init__(self) -> None:
        _string(self.native_status, "native_status")
        if self.candidate is not None and not isinstance(self.candidate, PortfolioCandidate):
            raise ValueError("candidate must be a PortfolioCandidate or None")
        _objective_bounds(self.objective_bounds)
        if self.worst_scenario is not None and not isinstance(self.worst_scenario, SettlementScenario):
            raise ValueError("worst_scenario must be a SettlementScenario or None")
        for name in ("payout_lower_bound_units", "cost_upper_bound_units", "guaranteed_profit_units"):
            _optional_int64(getattr(self, name), name)
        if self.conservative_capital_release_at is not None and not isinstance(self.conservative_capital_release_at, datetime):
            raise ValueError("conservative_capital_release_at must be a datetime or None")
        for name in ("fixed_portfolio_closed", "global_search_closed"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be a bool")
        for name in ("master_rounds", "adversary_rounds"):
            _nonnegative_int(getattr(self, name), name)
        if not isinstance(self.cuts, tuple) or not all(isinstance(cut, WorstStateCut) for cut in self.cuts):
            raise ValueError("cuts must be a tuple of WorstStateCut")
        if self.certificate is not None and not isinstance(self.certificate, CertificateEvidence):
            raise ValueError("certificate must be CertificateEvidence or None")


@dataclass(frozen=True, slots=True)
class SolverRun:
    schema_version: str
    request_id: str
    solver_name: str
    solver_version: str
    adapter_version: str
    worker_id: str
    environment_id: str
    solve_status: SolveStatus
    proof_status: ProofStatus
    business_status: BusinessStatus
    optimality_status: OptimalityStatus
    objective_bounds: ObjectiveBounds
    classification: BenchmarkClassification
    termination_reason: TerminationReason
    evidence: SolverEvidence | None
    canonical_result: OracleResult | None
    phase_timings_ns: tuple[tuple[str, int], ...]
    peak_rss_bytes: int
    diagnostics: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if self.schema_version != BENCHMARK_PROTOCOL_V1:
            raise ValueError("unsupported solver benchmark protocol")
        for name in ("request_id", "solver_name", "solver_version", "adapter_version", "worker_id", "environment_id"):
            _string(getattr(self, name), name)
        if not isinstance(self.solve_status, SolveStatus) or not isinstance(self.proof_status, ProofStatus) or not isinstance(self.business_status, BusinessStatus) or not isinstance(self.optimality_status, OptimalityStatus):
            raise ValueError("solver run statuses must use canonical enums")
        _objective_bounds(self.objective_bounds)
        if not isinstance(self.classification, BenchmarkClassification) or not isinstance(self.termination_reason, TerminationReason):
            raise ValueError("invalid solver run classification or termination reason")
        if self.evidence is not None and not isinstance(self.evidence, SolverEvidence):
            raise ValueError("evidence must be SolverEvidence or None")
        if self.canonical_result is not None and not isinstance(self.canonical_result, OracleResult):
            raise ValueError("canonical_result must be OracleResult or None")
        _named_nonnegative_ints(self.phase_timings_ns, "phase_timings_ns")
        _nonnegative_int(self.peak_rss_bytes, "peak_rss_bytes")
        _named_strings(self.diagnostics, "diagnostics")
        if self.classification == BenchmarkClassification.CHECKED:
            assert self.canonical_result is not None
            assert (
                self.solve_status,
                self.proof_status,
                self.business_status,
                self.optimality_status,
                self.objective_bounds,
            ) == (
                self.canonical_result.solve_status,
                self.canonical_result.proof_status,
                self.canonical_result.business_status,
                self.canonical_result.optimality_status,
                self.canonical_result.objective_bounds,
            )
        else:
            assert self.canonical_result is None
        if self.classification == BenchmarkClassification.CERTIFICATE_CHECKED:
            assert self.evidence is not None and self.evidence.certificate is not None
            assert self.evidence.certificate.checker_succeeded
        if self.classification in {BenchmarkClassification.MEASUREMENT_ONLY, BenchmarkClassification.UNKNOWN} and self.proof_status != ProofStatus.UNKNOWN:
            raise ValueError("measurement-only and unknown runs must retain UNKNOWN proof status")
        if self.classification == BenchmarkClassification.UNKNOWN:
            assert self.solve_status == SolveStatus.UNKNOWN
            assert self.business_status == BusinessStatus.UNKNOWN


def compile_terminal_model(problem: ArbitrageProblem) -> LinearModel:
    return _compile_terminal_model(problem, None)


def _compile_terminal_model(
    problem: ArbitrageProblem,
    active_constraint_ids: frozenset[str] | None,
) -> LinearModel:
    issues = validate_problem(problem)
    if issues:
        raise ValueError("invalid problem: " + "; ".join(issue.code for issue in issues))
    variables = tuple(
        IntVariable(f"z:{atom.atom_id}", 0, 1)
        for state in problem.terminal_state_sets
        for atom in state.atoms
    )
    constraints: list[LinearConstraint] = [
        LinearConstraint(
            f"terminal:exactly-one:{state.market_contract_id}",
            tuple((f"z:{atom.atom_id}", 1) for atom in state.atoms),
            1,
            1,
        )
        for state in problem.terminal_state_sets
    ]
    states_by_contract = {state.market_contract_id: state for state in problem.terminal_state_sets}
    normal_kinds = {TerminalKind.NORMAL_YES, TerminalKind.NORMAL_NO}
    for relation in problem.constraint_model.relations:
        if active_constraint_ids is not None and relation.constraint_id not in active_constraint_ids:
            continue
        states = tuple(states_by_contract[contract_id] for contract_id in relation.contract_ids)
        yes_terms = tuple(
            (f"z:{atom.atom_id}", 1)
            for state in states
            for atom in state.atoms
            if atom.kind == TerminalKind.NORMAL_YES
        )
        exceptional_terms = tuple(
            f"z:{atom.atom_id}"
            for state in states
            for atom in state.atoms
            if atom.kind not in normal_kinds
        )
        if relation.kind == RelationKind.IMPLIES:
            antecedent_yes = tuple(
                (f"z:{atom.atom_id}", 1)
                for atom in states[0].atoms
                if atom.kind == TerminalKind.NORMAL_YES
            )
            consequent_no = tuple(
                (f"z:{atom.atom_id}", 1)
                for atom in states[1].atoms
                if atom.kind == TerminalKind.NORMAL_NO
            )
            constraints.append(
                LinearConstraint(
                    f"relation:{relation.constraint_id}",
                    (*antecedent_yes, *consequent_no, *((name, -1) for name in exceptional_terms)),
                    None,
                    1,
                )
            )
        elif relation.kind == RelationKind.MUTUALLY_EXCLUSIVE:
            big_m = len(relation.contract_ids) - 1
            constraints.append(
                LinearConstraint(
                    f"relation:{relation.constraint_id}",
                    (*yes_terms, *((name, -big_m) for name in exceptional_terms)),
                    None,
                    1,
                )
            )
        else:
            constraints.extend(
                (
                    LinearConstraint(
                        f"relation:{relation.constraint_id}:lower",
                        (*yes_terms, *((name, 1) for name in exceptional_terms)),
                        1,
                        None,
                    ),
                    LinearConstraint(
                        f"relation:{relation.constraint_id}:upper",
                        (*yes_terms, *((name, -(len(relation.contract_ids) - 1)) for name in exceptional_terms)),
                        None,
                        1,
                    ),
                )
            )
    for forbidden in problem.constraint_model.forbidden_atom_combinations:
        if active_constraint_ids is not None and forbidden.constraint_id not in active_constraint_ids:
            continue
        constraints.append(
            LinearConstraint(
                f"forbidden:{forbidden.constraint_id}",
                tuple((f"z:{atom_id}", 1) for atom_id in forbidden.atom_ids),
                None,
                len(forbidden.atom_ids) - 1,
            )
        )
    model = LinearModel(variables, tuple(constraints), None)
    validate_linear_model(model)
    return model


def compile_adversary(
    problem: ArbitrageProblem,
    quantities: tuple[ActionQuantity, ...],
    *,
    active_constraint_ids: frozenset[str] | None = None,
) -> CompiledAdversary:
    selected = _canonical_quantities(problem, quantities)
    if not selected:
        raise ValueError("adversary requires at least one selected action")
    if active_constraint_ids is not None:
        known_ids = {
            constraint.constraint_id
            for constraint in (
                *problem.constraint_model.relations,
                *problem.constraint_model.forbidden_atom_combinations,
            )
        }
        if not active_constraint_ids <= known_ids:
            raise ValueError("active_constraint_ids contains an unknown constraint")
    terminal_model = _compile_terminal_model(problem, active_constraint_ids)
    quantities_by_action = {item.action_id: item.quantity_lots for item in selected}
    actions_by_contract: dict[str, tuple[str, ...]] = {}
    for action in problem.actions:
        if action.action_id in quantities_by_action:
            actions_by_contract[action.market_contract_id] = (
                *actions_by_contract.get(action.market_contract_id, ()),
                action.action_id,
            )
    objective_terms = []
    atom_variables = []
    for state in sorted(problem.terminal_state_sets, key=lambda item: item.market_contract_id):
        selected_action_ids = actions_by_contract.get(state.market_contract_id, ())
        for atom in sorted(state.atoms, key=lambda item: item.atom_id):
            payouts = {item.action_id: item.payout_lower_bound_per_lot_units for item in atom.payouts}
            coefficient = 0
            for action_id in selected_action_ids:
                coefficient = _int64(
                    coefficient + _int64(payouts[action_id] * quantities_by_action[action_id], "adversary payout product"),
                    "adversary payout coefficient",
                )
            variable_name = f"z:{atom.atom_id}"
            objective_terms.append((variable_name, coefficient))
            atom_variables.append((SelectedAtom(state.market_contract_id, atom.atom_id), variable_name))
    model = replace(terminal_model, objective=LinearObjective("MIN", tuple(objective_terms)))
    validate_linear_model(model)
    return CompiledAdversary(model, selected, tuple(atom_variables))


def compile_master(
    problem: ArbitrageProblem,
    component: RelationComponent,
    release_profile: ReleaseProfile,
    cuts: tuple[WorstStateCut, ...],
    excluded_vectors: tuple[tuple[ActionQuantity, ...], ...],
) -> CompiledMaster:
    actions_by_id = {action.action_id: action for action in problem.actions}
    if (release_profile.delay_seconds, release_profile.occupied_days) != _release_timing(
        problem,
        release_profile.release_at,
    ):
        raise ValueError("release profile timing does not match release_at")
    if tuple(cut_from_scenario(problem, cut.scenario) for cut in cuts) != cuts:
        raise ValueError("master requires canonical settlement cuts")
    if len({cut.cut_id for cut in cuts}) != len(cuts):
        raise ValueError("repeated settlement cut")
    actions = tuple(actions_by_id[action_id] for action_id in component.action_ids)
    component_action_ids = set(component.action_ids)
    if not set(release_profile.eligible_action_ids) <= component_action_ids:
        raise ValueError("release profile contains an eligible action outside the component")
    if not set(release_profile.exact_action_ids) <= component_action_ids:
        raise ValueError("release profile contains an exact action outside the component")
    variables: list[IntVariable] = []
    constraints: list[LinearConstraint] = []
    quantity_variables: list[tuple[str, str]] = []
    selected_variables: list[tuple[str, str]] = []
    cost_terms: list[tuple[str, int]] = []

    for action in actions:
        quantity_name = f"q:{action.action_id}"
        selected_name = f"b:{action.action_id}"
        quantity_variables.append((action.action_id, quantity_name))
        selected_variables.append((action.action_id, selected_name))
        variables.extend((IntVariable(quantity_name, 0, action.max_quantity_lots), IntVariable(selected_name, 0, 1)))
        constraints.extend(
            (
                LinearConstraint(
                    f"quantity:min:{action.action_id}",
                    ((quantity_name, 1), (selected_name, -action.min_quantity_lots)),
                    0,
                    None,
                ),
                LinearConstraint(
                    f"quantity:max:{action.action_id}",
                    ((quantity_name, 1), (selected_name, -action.max_quantity_lots)),
                    None,
                    0,
                ),
            )
        )
        fill_names: list[str] = []
        open_names: list[str] = []
        widths: list[int] = []
        for index, cost_slice in enumerate(action.cost_slices):
            width = cost_slice.last_lot - cost_slice.first_lot + 1
            fill_name = f"x:{action.action_id}:{index}"
            open_name = f"y:{action.action_id}:{index}"
            fill_names.append(fill_name)
            open_names.append(open_name)
            widths.append(width)
            variables.extend((IntVariable(fill_name, 0, width), IntVariable(open_name, 0, 1)))
            cost_terms.append((fill_name, cost_slice.incremental_cost_upper_bound_units))
            constraints.append(
                LinearConstraint(
                    f"slice:capacity:{action.action_id}:{index}",
                    ((fill_name, 1), (open_name, -width)),
                    None,
                    0,
                )
            )
            if index:
                constraints.extend(
                    (
                        LinearConstraint(
                            f"slice:fill-order:{action.action_id}:{index}",
                            ((fill_names[index - 1], 1), (open_name, -widths[index - 1])),
                            0,
                            None,
                        ),
                        LinearConstraint(
                            f"slice:open-order:{action.action_id}:{index}",
                            ((open_name, 1), (open_names[index - 1], -1)),
                            None,
                            0,
                        ),
                    )
                )
        constraints.append(
            LinearConstraint(
                f"quantity:slices:{action.action_id}",
                ((quantity_name, 1), *( (fill_name, -1) for fill_name in fill_names)),
                0,
                0,
            )
        )

    quantity_names = dict(quantity_variables)
    component_action_ids = set(quantity_names)
    for vector_index, vector in enumerate(excluded_vectors):
        canonical = _canonical_quantities(problem, vector)
        if any(item.action_id not in component_action_ids for item in canonical):
            raise ValueError("excluded vector contains an action outside the component")
        targets = {item.action_id: item.quantity_lots for item in canonical}
        witnesses = []
        for action in actions:
            target = targets.get(action.action_id, 0)
            quantity_name = quantity_names[action.action_id]
            if target > 0:
                witness = f"nogood:{vector_index}:lower:{action.action_id}"
                big_m = action.max_quantity_lots - target + 1
                variables.append(IntVariable(witness, 0, 1))
                constraints.append(
                    LinearConstraint(
                        f"{witness}:implies",
                        ((quantity_name, 1), (witness, big_m)),
                        None,
                        target - 1 + big_m,
                    )
                )
                witnesses.append(witness)
            if target < action.max_quantity_lots:
                witness = f"nogood:{vector_index}:higher:{action.action_id}"
                big_m = target + 1
                variables.append(IntVariable(witness, 0, 1))
                constraints.append(
                    LinearConstraint(
                        f"{witness}:implies",
                        ((quantity_name, 1), (witness, -big_m)),
                        0,
                        None,
                    )
                )
                witnesses.append(witness)
        constraints.append(
            LinearConstraint(
                f"nogood:{vector_index}:different",
                tuple((name, 1) for name in witnesses),
                1,
                None,
            )
        )

    cost_minimum, cost_maximum = _terms_bounds(tuple(cost_terms), {item.name: item for item in variables})
    cost_name = "cost"
    variables.append(IntVariable(cost_name, cost_minimum, cost_maximum))
    constraints.append(LinearConstraint("cost:def", ((cost_name, 1), *((name, -coefficient) for name, coefficient in cost_terms)), 0, 0))

    cut_terms = []
    for cut in cuts:
        payouts = {payout.action_id: payout.payout_lower_bound_per_lot_units for payout in cut.payout_per_lot}
        cut_terms.append(tuple((quantity_name, payouts[action_id]) for action_id, quantity_name in quantity_variables))
    if not cut_terms:
        raise ValueError("master requires at least one settlement cut")
    variable_map = {item.name: item for item in variables}
    cut_bounds = tuple(_terms_bounds(terms, variable_map) for terms in cut_terms)
    payout_name = "payout"
    payout_minimum = min(lower for lower, _ in cut_bounds)
    payout_maximum = min(upper for _, upper in cut_bounds)
    variables.append(IntVariable(payout_name, payout_minimum, payout_maximum))
    cut_choice_names = tuple(f"cut-choice:{index}" for index in range(len(cut_terms)))
    variables.extend(IntVariable(name, 0, 1) for name in cut_choice_names)
    constraints.append(LinearConstraint("cut-choice:one", tuple((name, 1) for name in cut_choice_names), 1, 1))
    for index, (terms, (_, expression_maximum)) in enumerate(zip(cut_terms, cut_bounds, strict=True)):
        constraints.append(
            LinearConstraint(
                f"cut:{index}:upper",
                ((payout_name, 1), *((name, -coefficient) for name, coefficient in terms)),
                None,
                0,
            )
        )
        big_m = _int64(expression_maximum - payout_minimum, f"cut:{index}.big_m")
        constraints.append(
            LinearConstraint(
                f"cut:{index}:selected-lower",
                (
                    (payout_name, 1),
                    *((name, -coefficient) for name, coefficient in terms),
                    (cut_choice_names[index], -big_m),
                ),
                -big_m,
                None,
            )
        )

    profit_name = "profit"
    profit_minimum = _int64(payout_minimum - cost_maximum, "profit.lower")
    profit_maximum = _int64(payout_maximum - cost_minimum, "profit.upper")
    variables.append(IntVariable(profit_name, profit_minimum, profit_maximum))
    constraints.append(LinearConstraint("profit:def", ((profit_name, 1), (payout_name, -1), (cost_name, 1)), 0, 0))

    qualification_payout_name = payout_name
    qualification_profit_name = profit_name
    if problem.qualification_constraints:
        minimum_payouts = _minimum_action_payouts(problem)
        payout_floor_terms = tuple(
            (quantity_name, minimum_payouts[action_id])
            for action_id, quantity_name in quantity_variables
        )
        payout_floor_minimum, _ = _terms_bounds(payout_floor_terms, variable_map)
        qualification_payout_name = "qualification-payout"
        variables.append(IntVariable(qualification_payout_name, payout_floor_minimum, payout_maximum))
        constraints.extend(
            (
                LinearConstraint(
                    "qualification-payout:known-cut-upper",
                    ((qualification_payout_name, 1), (payout_name, -1)),
                    None,
                    0,
                ),
                LinearConstraint(
                    "qualification-payout:terminal-floor",
                    ((qualification_payout_name, 1), *((name, -coefficient) for name, coefficient in payout_floor_terms)),
                    0,
                    None,
                ),
            )
        )
        qualification_profit_name = "qualification-profit"
        qualification_profit_minimum = _int64(payout_floor_minimum - cost_maximum, "qualification-profit.lower")
        qualification_profit_maximum = _int64(payout_maximum - cost_minimum, "qualification-profit.upper")
        variables.append(IntVariable(qualification_profit_name, qualification_profit_minimum, qualification_profit_maximum))
        constraints.append(
            LinearConstraint(
                "qualification-profit:def",
                ((qualification_profit_name, 1), (qualification_payout_name, -1), (cost_name, 1)),
                0,
                0,
            )
        )

    qualification_variables = {
        "profit": qualification_profit_name,
        "payout": qualification_payout_name,
        "cost": cost_name,
    }
    for qualification in problem.qualification_constraints:
        name = f"qualification:{qualification.constraint_id}"
        formula = _qualification_formula(
            qualification,
            delay_seconds=release_profile.delay_seconds,
            occupied_days=release_profile.occupied_days,
        )
        if formula.requires_positive_payout:
            constraints.append(LinearConstraint(f"{name}:positive-payout", ((qualification_payout_name, 1),), 1, None))
        terms = tuple(
            (qualification_variables[semantic_name], coefficient)
            for semantic_name, coefficient in formula.left_coefficients
        ) + tuple(
            (qualification_variables[semantic_name], _negate64(coefficient, f"{name}.{semantic_name}"))
            for semantic_name, coefficient in formula.right_coefficients
        )
        if terms:
            if formula.left_constant:
                raise ValueError("variable qualification formula cannot have a left constant")
            constraints.append(_comparison_row(name, terms, formula.right_constant, qualification.comparison))
        else:
            passed = _comparison_passes(
                formula.left_constant,
                formula.right_constant,
                qualification.comparison,
            )
            constraints.append(LinearConstraint(name, (), 0 if passed else 1, None))

    eligible_action_ids = set(release_profile.eligible_action_ids)
    exact_action_ids = set(release_profile.exact_action_ids)
    exact_release_variables = tuple(
        selected_name
        for action_id, selected_name in selected_variables
        if action_id in exact_action_ids
    )
    constraints.append(LinearConstraint("release:exact", tuple((name, 1) for name in exact_release_variables), 1, None))
    for action_id, selected_name in selected_variables:
        if action_id not in eligible_action_ids:
            constraints.append(LinearConstraint(f"release:ineligible:{action_id}", ((selected_name, 1),), 0, 0))

    model = LinearModel(tuple(variables), tuple(constraints), LinearObjective("MAX", ((profit_name, 1),)))
    validate_linear_model(model)
    return CompiledMaster(
        model,
        component,
        release_profile,
        tuple(quantity_variables),
        tuple(selected_variables),
        payout_name,
        cost_name,
        profit_name,
    )


def _minimum_action_payouts(problem: ArbitrageProblem) -> dict[str, int]:
    states_by_contract = {
        state.market_contract_id: state
        for state in problem.terminal_state_sets
    }
    minimums = {}
    for action in problem.actions:
        state = states_by_contract[action.market_contract_id]
        minimums[action.action_id] = min(
            next(
                payout.payout_lower_bound_per_lot_units
                for payout in atom.payouts
                if payout.action_id == action.action_id
            )
            for atom in state.atoms
        )
    return minimums


def _canonical_quantities(
    problem: ArbitrageProblem,
    quantities: tuple[ActionQuantity, ...],
) -> tuple[ActionQuantity, ...]:
    actions_by_id = {action.action_id: action for action in problem.actions}
    seen: set[str] = set()
    selected = []
    for item in quantities:
        if not isinstance(item, ActionQuantity) or item.action_id not in actions_by_id or item.action_id in seen:
            raise ValueError("quantities contain an invalid or duplicate action")
        seen.add(item.action_id)
        quantity = _strict_int(item.quantity_lots, f"quantity:{item.action_id}")
        action = actions_by_id[item.action_id]
        if quantity < 0 or (quantity and not action.min_quantity_lots <= quantity <= action.max_quantity_lots):
            raise ValueError(f"quantity outside action domain: {item.action_id}")
        if quantity:
            selected.append(ActionQuantity(item.action_id, quantity))
    return tuple(sorted(selected, key=lambda item: item.action_id))


def _canonical_action_ids(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, tuple) or not all(isinstance(action_id, str) and action_id for action_id in value):
        raise ValueError(f"{name} must be a tuple of nonempty action IDs")
    if len(set(value)) != len(value):
        raise ValueError(f"{name} must not contain duplicate action IDs")
    return tuple(sorted(value))


def _comparison_row(
    name: str,
    terms: tuple[tuple[str, int], ...],
    right: int,
    comparison: Comparison,
) -> LinearConstraint:
    return (
        LinearConstraint(name, terms, right, None)
        if comparison == Comparison.GREATER_THAN_OR_EQUAL
        else LinearConstraint(name, terms, None, right)
    )


def _comparison_passes(left: int, right: int, comparison: Comparison) -> bool:
    return left >= right if comparison == Comparison.GREATER_THAN_OR_EQUAL else left <= right


def _qualification_formula(
    constraint: QualificationConstraint,
    *,
    delay_seconds: int,
    occupied_days: int,
) -> _QualificationFormula:
    if constraint.metric == QualificationMetric.GUARANTEED_PROFIT_UNITS:
        return _QualificationFormula(
            (("profit", constraint.threshold_denominator),),
            (),
            0,
            constraint.threshold_numerator,
        )
    if constraint.metric == QualificationMetric.NET_MARGIN_PPM:
        return _QualificationFormula(
            (("profit", _product64(1_000_000, constraint.threshold_denominator)),),
            (("payout", constraint.threshold_numerator),),
            0,
            0,
            constraint.threshold_numerator > 0,
        )
    if constraint.metric == QualificationMetric.ANNUALIZED_RETURN_PPM:
        return _QualificationFormula(
            (("profit", _product64(365, 1_000_000, constraint.threshold_denominator)),),
            (("cost", _product64(constraint.threshold_numerator, occupied_days)),),
            0,
            0,
        )
    if constraint.metric == QualificationMetric.MAX_CAPITAL_RELEASE_DELAY_SECONDS:
        return _QualificationFormula(
            (),
            (),
            _product64(delay_seconds, constraint.threshold_denominator),
            constraint.threshold_numerator,
        )
    raise AssertionError(constraint.metric)


def _product64(*values: int) -> int:
    result = 1
    for value in values:
        result = _int64(result * value, "integer product")
    return result


def _negate64(value: int, name: str) -> int:
    return _int64(-value, name)


def solve_with_constraint_generation(
    request: OracleRequest,
    backend: SolverBackend,
    limits: BenchmarkLimits,
) -> SolverEvidence:
    if (
        not isinstance(request, OracleRequest)
        or request.schema_version != REQUEST_SCHEMA_V1
        or request.mode not in set(SearchMode)
        or not isinstance(request.problem, ArbitrageProblem)
        or not isinstance(limits, BenchmarkLimits)
    ):
        return _empty_evidence(UnknownReason.INVALID_MODEL.value)
    issues = validate_problem(request.problem)
    if any(action.settlement_asset_id != request.problem.valuation_unit_id for action in request.problem.actions):
        return _empty_evidence(UnknownReason.UNKNOWN_VALUATION.value)
    if issues:
        codes = {issue.code for issue in issues}
        reason = (
            UnknownReason.UNKNOWN_TERMINAL_DATA
            if codes & {"MISSING_ACTION_PAYOUT", "MISSING_TERMINAL_RULE_IDENTITY", "MISSING_CAPITAL_RELEASE_AT"}
            else UnknownReason.UNKNOWN_VALUATION
            if "MISSING_ASSET_VALUATION_RULE" in codes
            else UnknownReason.INVALID_MODEL
        )
        return _empty_evidence(reason.value)
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in (
            request.budget.max_quantity_vectors,
            request.budget.max_joint_states,
            request.budget.max_support_rechecks,
        )
    ):
        return _empty_evidence(UnknownReason.INVALID_MODEL.value)

    problem = (
        replace(
            request.problem,
            problem_id=f"{request.problem.problem_id}:raw-arbitrage-diagnostic",
            qualification_constraints=(),
        )
        if request.mode == SearchMode.RAW_ARBITRAGE_DIAGNOSTIC
        else request.problem
    )
    try:
        releases = _reachable_action_releases(problem, backend, limits.soft_time_limit_ms)
        if isinstance(releases, (UnknownReason, TerminationReason)):
            return _empty_evidence(releases.value)
        seed = _deterministic_allowed_scenario(problem, backend, limits.soft_time_limit_ms)
        if isinstance(seed, (UnknownReason, TerminationReason)):
            return _empty_evidence(seed.value)
        cuts = [cut_from_scenario(problem, seed)]
        best: SolverEvidence | None = None
        master_rounds = adversary_rounds = 0
        all_searches_closed = True

        for component in build_relation_components(problem):
            profiles = _release_profiles(problem, component, releases)
            for profile in profiles:
                excluded: tuple[tuple[ActionQuantity, ...], ...] = ()
                while True:
                    if master_rounds >= limits.max_constraint_generation_rounds:
                        return _empty_evidence(
                            TerminationReason.PROOF_UNCLOSED.value,
                            master_rounds=master_rounds,
                            adversary_rounds=adversary_rounds,
                            cuts=tuple(cuts),
                        )
                    compiled = compile_master(problem, component, profile, tuple(cuts), excluded)
                    master_result, master_closed = _solve_master_lexicographically(
                        compiled,
                        backend,
                        limits.soft_time_limit_ms,
                    )
                    master_rounds += 1
                    if master_result.status == NativeSolveStatus.INFEASIBLE:
                        break
                    if master_result.status not in {NativeSolveStatus.OPTIMAL, NativeSolveStatus.FEASIBLE}:
                        return _empty_evidence(
                            TerminationReason.PROOF_UNCLOSED.value,
                            master_rounds=master_rounds,
                            adversary_rounds=adversary_rounds,
                            cuts=tuple(cuts),
                        )
                    all_searches_closed &= master_closed
                    master_values = dict(master_result.values)
                    quantities = tuple(
                        ActionQuantity(action_id, master_values[variable_name])
                        for action_id, variable_name in compiled.quantity_variables
                        if master_values[variable_name]
                    )
                    adversary = compile_adversary(problem, quantities)
                    adversary_result = _checked_backend_solve(adversary.model, backend, limits.soft_time_limit_ms)
                    adversary_rounds += 1
                    if adversary_result.status != NativeSolveStatus.OPTIMAL:
                        return _empty_evidence(
                            TerminationReason.PROOF_UNCLOSED.value,
                            master_rounds=master_rounds,
                            adversary_rounds=adversary_rounds,
                            cuts=tuple(cuts),
                        )
                    scenario = _scenario_from_result(adversary, adversary_result)
                    payout = _objective_activity(adversary.model, adversary_result)
                    master_payout = master_values[compiled.payout_variable]
                    if payout < master_payout:
                        cut = cut_from_scenario(problem, scenario)
                        if any(existing.cut_id == cut.cut_id for existing in cuts):
                            return _empty_evidence(
                                TerminationReason.PROOF_UNCLOSED.value,
                                master_rounds=master_rounds,
                                adversary_rounds=adversary_rounds,
                                cuts=tuple(cuts),
                            )
                        cuts.append(cut)
                        continue
                    if payout != master_payout:
                        return _empty_evidence(
                            TerminationReason.PROOF_UNCLOSED.value,
                            master_rounds=master_rounds,
                            adversary_rounds=adversary_rounds,
                            cuts=tuple(cuts),
                        )
                    cost = master_values[compiled.cost_variable]
                    profit = _int64(payout - cost, "guaranteed profit")
                    if _failed_qualification_ids(problem, payout, cost, profile.release_at):
                        excluded = (*excluded, quantities)
                        continue
                    support = _minimize_support(
                        problem,
                        quantities,
                        payout,
                        cost,
                        profile.release_at,
                        backend,
                        limits.soft_time_limit_ms,
                        request.budget.max_support_rechecks,
                    )
                    if isinstance(support, TerminationReason):
                        return _empty_evidence(
                            support.value,
                            master_rounds=master_rounds,
                            adversary_rounds=adversary_rounds,
                            cuts=tuple(cuts),
                        )
                    support_graph, support_rechecks = support
                    adversary_rounds += support_rechecks
                    evaluation = PortfolioEvaluation(
                        quantities,
                        payout,
                        cost,
                        profit,
                        scenario,
                        cut_from_scenario(problem, scenario),
                        profile.release_at,
                        (),
                    )
                    if len(split_disconnected_solution(problem, evaluation, support_graph)) > 1:
                        excluded = (*excluded, quantities)
                        continue
                    evidence = SolverEvidence(
                        native_status=NativeSolveStatus.OPTIMAL.value,
                        candidate=PortfolioCandidate(quantities, profit),
                        objective_bounds=ObjectiveBounds(
                            profit,
                            None if request.mode == SearchMode.ADMISSION else profit,
                            None if request.mode == SearchMode.ADMISSION else 0,
                            request.mode != SearchMode.ADMISSION,
                        ),
                        worst_scenario=scenario,
                        payout_lower_bound_units=payout,
                        cost_upper_bound_units=cost,
                        guaranteed_profit_units=profit,
                        conservative_capital_release_at=profile.release_at,
                        fixed_portfolio_closed=True,
                        global_search_closed=False,
                        master_rounds=master_rounds,
                        adversary_rounds=adversary_rounds,
                        cuts=tuple(cuts),
                        certificate=None,
                    )
                    if request.mode == SearchMode.ADMISSION:
                        return evidence
                    if best is None or _evidence_key(evidence) < _evidence_key(best):
                        best = evidence
                    break

        if best is None:
            return _empty_evidence(
                BusinessStatus.NO_QUALIFIED_OPPORTUNITY.value,
                global_search_closed=True,
                master_rounds=master_rounds,
                adversary_rounds=adversary_rounds,
                cuts=tuple(cuts),
            )
        assert best.guaranteed_profit_units is not None
        profit = best.guaranteed_profit_units
        if not all_searches_closed:
            return replace(
                best,
                native_status=TerminationReason.PROOF_UNCLOSED.value,
                objective_bounds=ObjectiveBounds(profit, None, None, False),
                global_search_closed=False,
                master_rounds=master_rounds,
                adversary_rounds=adversary_rounds,
                cuts=tuple(cuts),
            )
        if request.mode == SearchMode.RAW_ARBITRAGE_DIAGNOSTIC and profit <= 0:
            return replace(
                best,
                native_status=BusinessStatus.NO_ARBITRAGE.value,
                candidate=None,
                fixed_portfolio_closed=False,
                global_search_closed=True,
            )
        return replace(
            best,
            objective_bounds=ObjectiveBounds(profit, profit, 0, True),
            global_search_closed=True,
            master_rounds=master_rounds,
            adversary_rounds=adversary_rounds,
            cuts=tuple(cuts),
        )
    except UnsafeSolverResult:
        return _empty_evidence(TerminationReason.INVALID_OUTPUT.value)
    except _NumericUnsafeError:
        return _empty_evidence(TerminationReason.NUMERIC_UNSAFE.value)


def _minimize_support(
    problem: ArbitrageProblem,
    quantities: tuple[ActionQuantity, ...],
    payout: int,
    cost: int,
    release_at: datetime,
    backend: SolverBackend,
    time_limit_ms: int,
    max_rechecks: int,
) -> tuple[SelectedSupportGraph, int] | TerminationReason:
    actions_by_id = {action.action_id: action for action in problem.actions}
    selected_contract_ids = {
        actions_by_id[item.action_id].market_contract_id for item in quantities
    }
    all_constraint_ids = {
        constraint.constraint_id
        for constraint in (
            *problem.constraint_model.relations,
            *problem.constraint_model.forbidden_atom_combinations,
        )
    }
    relevant: set[str] = set()
    connected = set(selected_contract_ids)
    while True:
        newly_relevant = {
            constraint_id
            for constraint_id in all_constraint_ids
            if set(_constraint_contract_ids(problem, constraint_id)) & connected
        }
        expanded = connected | {
            contract_id
            for constraint_id in newly_relevant
            for contract_id in _constraint_contract_ids(problem, constraint_id)
        }
        if newly_relevant == relevant and expanded == connected:
            break
        relevant = newly_relevant
        connected = expanded

    active = set(relevant)
    non_relevant = all_constraint_ids - relevant
    rechecks = 0
    base_failures = _failed_qualification_ids(problem, payout, cost, release_at)
    for constraint_id in sorted(relevant, reverse=True):
        if rechecks >= max_rechecks:
            return TerminationReason.PROOF_UNCLOSED
        rechecks += 1
        trial = active - {constraint_id}
        active_global = frozenset(non_relevant | trial)
        adversary = compile_adversary(problem, quantities, active_constraint_ids=active_global)
        result = _checked_backend_solve(adversary.model, backend, time_limit_ms)
        if result.status != NativeSolveStatus.OPTIMAL:
            return TerminationReason.PROOF_UNCLOSED
        trial_payout = _objective_activity(adversary.model, result)
        trial_releases = _reachable_action_releases(
            problem,
            backend,
            time_limit_ms,
            active_constraint_ids=active_global,
            required_contract_ids=selected_contract_ids,
        )
        if isinstance(trial_releases, (UnknownReason, TerminationReason)):
            return TerminationReason.PROOF_UNCLOSED
        trial_release = max(trial_releases[item.action_id] for item in quantities)
        if (
            trial_payout == payout
            and trial_release == release_at
            and _failed_qualification_ids(problem, trial_payout, cost, trial_release) == base_failures
        ):
            active = trial

    constraint_ids = tuple(sorted(active))
    contract_ids = tuple(
        sorted(
            selected_contract_ids
            | {
                contract_id
                for constraint_id in constraint_ids
                for contract_id in _constraint_contract_ids(problem, constraint_id)
            }
        )
    )
    return (
        SelectedSupportGraph(
            tuple(item.action_id for item in quantities),
            contract_ids,
            constraint_ids,
            tuple((constraint_id, _constraint_contract_ids(problem, constraint_id)) for constraint_id in constraint_ids),
        ),
        rechecks,
    )


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


def _failed_qualification_ids(
    problem: ArbitrageProblem,
    payout: int,
    cost: int,
    release_at: datetime,
) -> tuple[str, ...]:
    profit = _int64(payout - cost, "qualification profit")
    delay_seconds, occupied_days = _release_timing(problem, release_at)
    values = {"profit": profit, "payout": payout, "cost": cost}
    failures = []
    for constraint in sorted(problem.qualification_constraints, key=lambda item: item.constraint_id):
        formula = _qualification_formula(
            constraint,
            delay_seconds=delay_seconds,
            occupied_days=occupied_days,
        )
        if formula.requires_positive_payout and payout <= 0:
            failures.append(constraint.constraint_id)
            continue
        left = _qualification_activity(formula.left_coefficients, formula.left_constant, values)
        right = _qualification_activity(formula.right_coefficients, formula.right_constant, values)
        if not _comparison_passes(left, right, constraint.comparison):
            failures.append(constraint.constraint_id)
    return tuple(failures)


def _qualification_activity(
    coefficients: tuple[tuple[Literal["profit", "payout", "cost"], int], ...],
    constant: int,
    values: dict[str, int],
) -> int:
    result = constant
    for semantic_name, coefficient in coefficients:
        result = _int64(
            result + _product64(coefficient, values[semantic_name]),
            f"qualification {semantic_name} activity",
        )
    return result


def _checked_backend_solve(model: LinearModel, backend: SolverBackend, time_limit_ms: int) -> BackendResult:
    result = backend.solve(model, time_limit_ms=time_limit_ms)
    validate_backend_result(model, result)
    return result


def _solve_master_lexicographically(
    compiled: CompiledMaster,
    backend: SolverBackend,
    time_limit_ms: int,
) -> tuple[BackendResult, bool]:
    selected = dict(compiled.selected_variables)
    quantities = dict(compiled.quantity_variables)
    leading_objectives = (
        LinearObjective("MAX", ((compiled.profit_variable, 1),)),
        LinearObjective("MIN", ((compiled.cost_variable, 1),)),
        LinearObjective("MIN", tuple((name, 1) for name in selected.values())),
    )
    model = compiled.model
    result: BackendResult | None = None
    objective_index = 0

    def solve_and_lock(objective: LinearObjective) -> BackendResult:
        nonlocal model, objective_index
        solved_model = replace(model, objective=objective)
        solved = _checked_backend_solve(solved_model, backend, time_limit_ms)
        if solved.status != NativeSolveStatus.OPTIMAL:
            return solved
        value = _objective_activity(solved_model, solved)
        model = replace(
            model,
            constraints=model.constraints
            + (
                LinearConstraint(
                    f"objective-lock:{objective_index}",
                    objective.coefficients,
                    value,
                    value,
                ),
            ),
        )
        objective_index += 1
        return solved

    for objective in leading_objectives:
        result = solve_and_lock(objective)
        if result.status != NativeSolveStatus.OPTIMAL:
            if objective_index:
                return replace(result, status=NativeSolveStatus.UNKNOWN), False
            return result, False

    for action_id in sorted(selected):
        result = solve_and_lock(LinearObjective("MAX", ((selected[action_id], 1),)))
        if result.status != NativeSolveStatus.OPTIMAL:
            return replace(result, status=NativeSolveStatus.UNKNOWN), False
        if dict(result.values)[selected[action_id]]:
            result = solve_and_lock(LinearObjective("MIN", ((quantities[action_id], 1),)))
            if result.status != NativeSolveStatus.OPTIMAL:
                return replace(result, status=NativeSolveStatus.UNKNOWN), False
    assert result is not None
    return result, True


def _reachable_action_releases(
    problem: ArbitrageProblem,
    backend: SolverBackend,
    time_limit_ms: int,
    *,
    active_constraint_ids: frozenset[str] | None = None,
    required_contract_ids: set[str] | None = None,
) -> dict[str, datetime] | _SolveFailure:
    terminal = _compile_terminal_model(problem, active_constraint_ids)
    reachable_by_contract: dict[str, list[datetime]] = {}
    unknown_by_contract: dict[str, list[datetime]] = {}
    for state in sorted(problem.terminal_state_sets, key=lambda item: item.market_contract_id):
        if required_contract_ids is not None and state.market_contract_id not in required_contract_ids:
            continue
        for atom in sorted(state.atoms, key=lambda item: item.atom_id):
            assert atom.capital_release_at is not None
            model = replace(
                terminal,
                constraints=terminal.constraints
                + (
                    LinearConstraint(
                        f"reachability:{atom.atom_id}",
                        ((f"z:{atom.atom_id}", 1),),
                        1,
                        1,
                    ),
                ),
            )
            result = _checked_backend_solve(model, backend, time_limit_ms)
            if result.status in {NativeSolveStatus.OPTIMAL, NativeSolveStatus.FEASIBLE}:
                reachable_by_contract.setdefault(state.market_contract_id, []).append(atom.capital_release_at)
            elif result.status == NativeSolveStatus.UNKNOWN:
                unknown_by_contract.setdefault(state.market_contract_id, []).append(atom.capital_release_at)
    releases_by_contract: dict[str, datetime] = {}
    for state in problem.terminal_state_sets:
        if required_contract_ids is not None and state.market_contract_id not in required_contract_ids:
            continue
        reachable = reachable_by_contract.get(state.market_contract_id, [])
        unknown = unknown_by_contract.get(state.market_contract_id, [])
        if not reachable:
            return (
                TerminationReason.PROOF_UNCLOSED
                if unknown
                else UnknownReason.CONTRADICTORY_CONSTRAINT_MODEL
            )
        release_at = max(reachable)
        if any(candidate > release_at for candidate in unknown):
            return TerminationReason.PROOF_UNCLOSED
        releases_by_contract[state.market_contract_id] = release_at
    return {
        action.action_id: releases_by_contract[action.market_contract_id]
        for action in problem.actions
        if action.market_contract_id in releases_by_contract
    }


def _deterministic_allowed_scenario(
    problem: ArbitrageProblem,
    backend: SolverBackend,
    time_limit_ms: int,
) -> SettlementScenario | _SolveFailure:
    model = compile_terminal_model(problem)
    selected: list[SelectedAtom] = []
    for state in sorted(problem.terminal_state_sets, key=lambda item: item.market_contract_id):
        chosen = None
        for atom in sorted(state.atoms, key=lambda item: item.atom_id):
            trial = replace(
                model,
                constraints=model.constraints
                + (
                    LinearConstraint(f"seed:{state.market_contract_id}:{atom.atom_id}", ((f"z:{atom.atom_id}", 1),), 1, 1),
                ),
            )
            result = _checked_backend_solve(trial, backend, time_limit_ms)
            if result.status in {NativeSolveStatus.OPTIMAL, NativeSolveStatus.FEASIBLE}:
                chosen = atom
                model = trial
                break
            if result.status == NativeSolveStatus.UNKNOWN:
                return TerminationReason.PROOF_UNCLOSED
        if chosen is None:
            return UnknownReason.CONTRADICTORY_CONSTRAINT_MODEL
        selected.append(SelectedAtom(state.market_contract_id, chosen.atom_id))
    return SettlementScenario(tuple(selected))


def _release_profiles(
    problem: ArbitrageProblem,
    component: RelationComponent,
    releases: dict[str, datetime],
) -> tuple[ReleaseProfile, ...]:
    action_ids = tuple(sorted(component.action_ids))
    return tuple(
        _release_profile(
            problem,
            release_at,
            tuple(action_id for action_id in action_ids if releases[action_id] <= release_at),
            tuple(action_id for action_id in action_ids if releases[action_id] == release_at),
        )
        for release_at in sorted({releases[action_id] for action_id in component.action_ids})
    )


def _release_profile(
    problem: ArbitrageProblem,
    release_at: datetime,
    eligible_action_ids: tuple[str, ...],
    exact_action_ids: tuple[str, ...],
) -> ReleaseProfile:
    seconds, occupied_days = _release_timing(problem, release_at)
    return ReleaseProfile(seconds, occupied_days, release_at, eligible_action_ids, exact_action_ids)


def _release_timing(problem: ArbitrageProblem, release_at: datetime) -> tuple[int, int]:
    delta = release_at - problem.as_of
    seconds = _int64(delta.days * 86_400 + delta.seconds + int(bool(delta.microseconds)), "release delay")
    occupied_days = max(1, _int64(seconds + 86_399, "occupied day numerator") // 86_400)
    return seconds, occupied_days


def _scenario_from_result(compiled: CompiledAdversary, result: BackendResult) -> SettlementScenario:
    values = dict(result.values)
    return SettlementScenario(tuple(atom for atom, variable_name in compiled.atom_variables if values[variable_name] == 1))


def _objective_activity(model: LinearModel, result: BackendResult) -> int:
    assert model.objective is not None
    values = dict(result.values)
    return _int64(sum(coefficient * values[name] for name, coefficient in model.objective.coefficients), "objective activity")


def _evidence_key(evidence: SolverEvidence) -> tuple[object, ...]:
    assert evidence.candidate is not None
    assert evidence.guaranteed_profit_units is not None
    assert evidence.cost_upper_bound_units is not None
    return (
        -evidence.guaranteed_profit_units,
        evidence.cost_upper_bound_units,
        len(evidence.candidate.quantities),
        tuple((item.action_id, item.quantity_lots) for item in evidence.candidate.quantities),
    )


def _empty_evidence(
    native_status: str,
    *,
    global_search_closed: bool = False,
    master_rounds: int = 0,
    adversary_rounds: int = 0,
    cuts: tuple[WorstStateCut, ...] = (),
) -> SolverEvidence:
    return SolverEvidence(
        native_status=native_status,
        candidate=None,
        objective_bounds=ObjectiveBounds(None, None, None, False),
        worst_scenario=None,
        payout_lower_bound_units=None,
        cost_upper_bound_units=None,
        guaranteed_profit_units=None,
        conservative_capital_release_at=None,
        fixed_portfolio_closed=False,
        global_search_closed=global_search_closed,
        master_rounds=master_rounds,
        adversary_rounds=adversary_rounds,
        cuts=cuts,
        certificate=None,
    )


def validate_linear_model(model: LinearModel) -> None:
    if not isinstance(model, LinearModel):
        raise ValueError("model must be a LinearModel")
    if not isinstance(model.variables, tuple) or not isinstance(model.constraints, tuple):
        raise ValueError("model variables and constraints must be tuples")
    variable_names: set[str] = set()
    variables: dict[str, IntVariable] = {}
    for variable in model.variables:
        if not isinstance(variable, IntVariable):
            raise ValueError("variables must be IntVariable")
        _string(variable.name, "variable name")
        if variable.name in variable_names:
            raise ValueError(f"duplicate variable name: {variable.name}")
        variable_names.add(variable.name)
        _int64(variable.lower, f"{variable.name}.lower")
        _int64(variable.upper, f"{variable.name}.upper")
        if variable.lower > variable.upper:
            raise ValueError(f"variable lower bound exceeds upper bound: {variable.name}")
        if variable.integer is not True:
            raise ValueError(f"variable must be integer: {variable.name}")
        variables[variable.name] = variable
    constraint_names: set[str] = set()
    for constraint in model.constraints:
        if not isinstance(constraint, LinearConstraint):
            raise ValueError("constraints must be LinearConstraint")
        _string(constraint.name, "constraint name")
        if constraint.name in constraint_names:
            raise ValueError(f"duplicate constraint name: {constraint.name}")
        constraint_names.add(constraint.name)
        _validate_row(constraint.name, constraint.coefficients, constraint.lower, constraint.upper, variables)
    if model.objective is not None:
        if not isinstance(model.objective, LinearObjective) or model.objective.sense not in {"MAX", "MIN"}:
            raise ValueError("objective must use MAX or MIN")
        terms = _validate_terms("objective", model.objective.coefficients, variables)
        if not _expression_activity_fits_int64(terms, variables):
            raise _NumericUnsafeError("possible objective activity exceeds signed int64")


def validate_backend_result(model: LinearModel, result: BackendResult) -> None:
    validate_linear_model(model)
    if not isinstance(result, BackendResult) or not isinstance(result.status, NativeSolveStatus):
        raise UnsafeSolverResult("invalid backend result status")
    try:
        _string(result.native_status, "native_status")
        _nonnegative_int(result.solve_ns, "solve_ns")
        _optional_int64(result.objective_value, "objective_value")
        _optional_int64(result.objective_bound, "objective_bound")
    except (OverflowError, ValueError) as exc:
        raise UnsafeSolverResult(str(exc)) from exc
    if not isinstance(result.values, tuple):
        raise UnsafeSolverResult("backend values must be a tuple")
    values: dict[str, int] = {}
    for item in result.values:
        if not isinstance(item, tuple) or len(item) != 2:
            raise UnsafeSolverResult("backend values must contain name/value pairs")
        name, value = item
        if not isinstance(name, str) or not name or name in values:
            raise UnsafeSolverResult("backend values have duplicate or invalid names")
        values[name] = _result_int64(value, name)
    variable_names = {variable.name for variable in model.variables}
    if values and set(values) != variable_names:
        raise UnsafeSolverResult("backend result has missing or extra variables")
    if result.status in {NativeSolveStatus.OPTIMAL, NativeSolveStatus.FEASIBLE} and set(values) != variable_names:
        raise UnsafeSolverResult("feasible backend result requires every variable")
    if not values:
        return
    for variable in model.variables:
        value = values[variable.name]
        if not variable.lower <= value <= variable.upper:
            raise UnsafeSolverResult(f"variable outside bounds: {variable.name}")
    for constraint in model.constraints:
        activity = sum(values[name] * coefficient for name, coefficient in constraint.coefficients)
        if (constraint.lower is not None and activity < constraint.lower) or (constraint.upper is not None and activity > constraint.upper):
            raise UnsafeSolverResult(f"constraint violated: {constraint.name}")
    if result.objective_value is not None and model.objective is not None:
        activity = sum(values[name] * coefficient for name, coefficient in model.objective.coefficients)
        if activity != result.objective_value:
            raise UnsafeSolverResult("objective value does not match exact integer activity")


def linear_model_fingerprint(model: LinearModel) -> str:
    validate_linear_model(model)
    return fingerprint(model)


def _validate_row(name: str, coefficients: object, lower: int | None, upper: int | None, variables: dict[str, IntVariable]) -> None:
    terms = _validate_terms(name, coefficients, variables)
    _optional_int64(lower, f"{name}.lower")
    _optional_int64(upper, f"{name}.upper")
    if lower is not None and upper is not None and lower > upper:
        raise ValueError(f"constraint lower bound exceeds upper bound: {name}")
    if not _expression_activity_fits_int64(terms, variables):
        raise _NumericUnsafeError(f"possible row activity exceeds signed int64: {name}")


def _expression_activity_fits_int64(terms: tuple[tuple[str, int], ...], variables: dict[str, IntVariable]) -> bool:
    minimum = 0
    maximum = 0
    for variable_name, coefficient in terms:
        variable = variables[variable_name]
        minimum += coefficient * (variable.lower if coefficient >= 0 else variable.upper)
        maximum += coefficient * (variable.upper if coefficient >= 0 else variable.lower)
    return INT64_MIN <= minimum <= INT64_MAX and INT64_MIN <= maximum <= INT64_MAX


def _terms_bounds(terms: tuple[tuple[str, int], ...], variables: dict[str, IntVariable]) -> tuple[int, int]:
    minimum = 0
    maximum = 0
    for variable_name, coefficient in terms:
        variable = variables[variable_name]
        minimum += coefficient * (variable.lower if coefficient >= 0 else variable.upper)
        maximum += coefficient * (variable.upper if coefficient >= 0 else variable.lower)
    return _int64(minimum, "expression minimum"), _int64(maximum, "expression maximum")


def _validate_terms(owner: str, coefficients: object, variables: dict[str, IntVariable]) -> tuple[tuple[str, int], ...]:
    if not isinstance(coefficients, tuple):
        raise ValueError(f"{owner} coefficients must be a tuple")
    names: set[str] = set()
    terms: list[tuple[str, int]] = []
    for term in coefficients:
        if not isinstance(term, tuple) or len(term) != 2:
            raise ValueError(f"{owner} coefficients must contain name/value pairs")
        name, coefficient = term
        _string(name, f"{owner} coefficient name")
        if name not in variables:
            raise ValueError(f"unknown variable reference: {name}")
        if name in names:
            raise ValueError(f"duplicate coefficient variable: {name}")
        names.add(name)
        terms.append((name, _int64(coefficient, f"{owner}.{name}")))
    return tuple(terms)


def _strict_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


def _int64(value: object, name: str) -> int:
    value = _strict_int(value, name)
    if not INT64_MIN <= value <= INT64_MAX:
        raise _NumericUnsafeError(f"{name} must fit signed int64")
    return value


def _result_int64(value: object, name: str) -> int:
    try:
        return _int64(value, name)
    except (OverflowError, ValueError) as exc:
        raise UnsafeSolverResult(str(exc)) from exc


def _optional_int64(value: object, name: str) -> None:
    if value is not None:
        _int64(value, name)


def _positive_int(value: object, name: str) -> None:
    if _strict_int(value, name) <= 0:
        raise ValueError(f"{name} must be positive")


def _nonnegative_int(value: object, name: str) -> None:
    if _strict_int(value, name) < 0:
        raise ValueError(f"{name} must be nonnegative")


def _string(value: object, name: str, *, allow_empty: bool = False) -> None:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ValueError(f"{name} must be a nonempty string")


def _sha256(value: object, name: str) -> None:
    if not isinstance(value, str) or len(value) != 71 or not value.startswith("sha256:") or any(character not in "0123456789abcdef" for character in value[7:]):
        raise ValueError(f"{name} must be a sha256 fingerprint")


def _objective_bounds(value: object) -> None:
    if not isinstance(value, ObjectiveBounds):
        raise ValueError("objective_bounds must be ObjectiveBounds")
    _optional_int64(value.lower_bound_units, "lower_bound_units")
    _optional_int64(value.upper_bound_units, "upper_bound_units")
    _optional_int64(value.gap_units, "gap_units")
    if not isinstance(value.closed, bool):
        raise ValueError("objective bounds closed must be a bool")


def _named_nonnegative_ints(value: object, name: str) -> None:
    if not isinstance(value, tuple):
        raise ValueError(f"{name} must be a tuple")
    keys: set[str] = set()
    for item in value:
        if not isinstance(item, tuple) or len(item) != 2:
            raise ValueError(f"{name} must contain name/value pairs")
        key, number = item
        _string(key, name)
        if key in keys:
            raise ValueError(f"{name} keys must be unique")
        keys.add(key)
        _nonnegative_int(number, f"{name}.{key}")


def _named_strings(value: object, name: str) -> None:
    if not isinstance(value, tuple):
        raise ValueError(f"{name} must be a tuple")
    keys: set[str] = set()
    for item in value:
        if not isinstance(item, tuple) or len(item) != 2:
            raise ValueError(f"{name} must contain name/value pairs")
        key, text = item
        _string(key, name)
        if key in keys:
            raise ValueError(f"{name} keys must be unique")
        keys.add(key)
        if not isinstance(text, str):
            raise ValueError(f"{name} values must be strings")
