from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol

from open_trader.prediction_n_leg import (
    BusinessStatus,
    ObjectiveBounds,
    OptimalityStatus,
    OracleResult,
    PortfolioCandidate,
    ProofStatus,
    SettlementScenario,
    SolveStatus,
    WorstStateCut,
    fingerprint,
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


class UnsafeSolverResult(ValueError):
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
            raise ValueError("possible objective activity exceeds signed int64")


def validate_backend_result(model: LinearModel, result: BackendResult) -> None:
    validate_linear_model(model)
    if not isinstance(result, BackendResult) or not isinstance(result.status, NativeSolveStatus):
        raise UnsafeSolverResult("invalid backend result status")
    _string(result.native_status, "native_status")
    _nonnegative_int(result.solve_ns, "solve_ns")
    _optional_int64(result.objective_value, "objective_value")
    _optional_int64(result.objective_bound, "objective_bound")
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
        raise ValueError(f"possible row activity exceeds signed int64: {name}")


def _expression_activity_fits_int64(terms: tuple[tuple[str, int], ...], variables: dict[str, IntVariable]) -> bool:
    minimum = 0
    maximum = 0
    for variable_name, coefficient in terms:
        variable = variables[variable_name]
        minimum += coefficient * (variable.lower if coefficient >= 0 else variable.upper)
        maximum += coefficient * (variable.upper if coefficient >= 0 else variable.lower)
    return INT64_MIN <= minimum <= INT64_MAX and INT64_MIN <= maximum <= INT64_MAX


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
        raise ValueError(f"{name} must fit signed int64")
    return value


def _result_int64(value: object, name: str) -> int:
    try:
        return _int64(value, name)
    except ValueError as exc:
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
