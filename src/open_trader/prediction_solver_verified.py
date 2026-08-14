"""Canonical request codec for the solver-verified proof engine."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping

from open_trader.prediction_n_leg import ModelDecodeError, ObjectiveBounds, OracleRequest, PayoutProof, PortfolioSolution, UnknownReason, canonical_payload, fingerprint, payout_proof_from_payload, portfolio_solution_from_payload, request_from_payload
from open_trader.prediction_n_leg_oracle import build_portfolio_solution, derive_selected_support_graph, evaluate_fixed_portfolio, find_qualified
from open_trader.prediction_solver import BenchmarkLimits
from open_trader.prediction_solver import SolverBackend, SolverEvidence, solver_evidence_from_payload, solve_with_constraint_generation
from open_trader.prediction_solver_worker import WorkerHarness, WorkerRequest


PROOF_REQUEST_SCHEMA_V1 = "open_trader.prediction_solver_verified.request.v1"
CANDIDATE_EVIDENCE_SCHEMA_V1 = "open_trader.prediction_solver_verified.candidate_evidence.v1"
VERIFICATION_RESULT_SCHEMA_V1 = "open_trader.prediction_solver_verified.result.v1"


class VerificationStatus(StrEnum):
    QUALIFIED_VERIFIED = "QUALIFIED_VERIFIED"
    NOT_QUALIFIED = "NOT_QUALIFIED"
    UNKNOWN = "UNKNOWN"
    NO_QUALIFIED_OPPORTUNITY = "NO_QUALIFIED_OPPORTUNITY"


class ProofLevel(StrEnum):
    SOLVER_VERIFIED = "SOLVER_VERIFIED"
    FORMALLY_VERIFIED = "FORMALLY_VERIFIED"
    NONE = "NONE"


def quote_fingerprint(problem: object) -> str:
    """Bind the proof to the canonical executable-cost input, not a caller SHA."""
    payload = canonical_payload(problem)
    actions = payload.get("actions")
    if not isinstance(actions, list):
        raise ValueError("problem actions must be canonical")
    if not all(isinstance(action, Mapping) and set(action) >= {"action_id", "cost_slices"} for action in actions):
        raise ValueError("problem actions must embed canonical cost input")
    return fingerprint({
        "actions": tuple(
            {"action_id": action["action_id"], "cost_slices": action["cost_slices"]}
            for action in actions
        )
    })


@dataclass(frozen=True, slots=True)
class ProofInput:
    schema_version: str
    request: OracleRequest
    limits: BenchmarkLimits
    quote_fingerprint: str
    current_generation: int
    code_version: str

    def __post_init__(self) -> None:
        if self.schema_version != PROOF_REQUEST_SCHEMA_V1 or not isinstance(self.request, OracleRequest) or not isinstance(self.limits, BenchmarkLimits):
            raise ValueError("invalid proof input")
        _fingerprint(self.quote_fingerprint, "quote_fingerprint")
        if self.quote_fingerprint != quote_fingerprint(self.request.problem):
            raise ValueError("proof input quote fingerprint mismatch")
        _nonnegative_int(self.current_generation, "current_generation")
        _string(self.code_version, "code_version")


def proof_input_from_payload(payload: object) -> ProofInput:
    value = _object(payload, "proof input", {"schema_version", "request", "limits", "quote_fingerprint", "current_generation", "code_version"})
    try:
        return ProofInput(
            _string(value["schema_version"], "schema_version"),
            request_from_payload(_mapping(value["request"], "request")),
            _limits_from_payload(value["limits"]),
            _fingerprint(value["quote_fingerprint"], "quote_fingerprint"),
            _nonnegative_int(value["current_generation"], "current_generation"),
            _string(value["code_version"], "code_version"),
        )
    except (ModelDecodeError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid proof input: {exc}") from exc


@dataclass(frozen=True, slots=True)
class CandidateEvidence:
    schema_version: str
    proof_input: ProofInput
    solver_name: str
    solver_version: str
    model_fingerprint: str
    portfolio_fingerprint: str | None
    solver_evidence: SolverEvidence

    def __post_init__(self) -> None:
        if self.schema_version != CANDIDATE_EVIDENCE_SCHEMA_V1 or not isinstance(self.proof_input, ProofInput) or not isinstance(self.solver_evidence, SolverEvidence):
            raise ValueError("invalid candidate evidence")
        _string(self.solver_name, "solver_name")
        _string(self.solver_version, "solver_version")
        _fingerprint(self.model_fingerprint, "model_fingerprint")
        if self.model_fingerprint != fingerprint(self.proof_input.request.problem):
            raise ValueError("candidate evidence model fingerprint mismatch")
        candidate = self.solver_evidence.candidate
        if candidate is None:
            if self.portfolio_fingerprint is not None:
                raise ValueError("candidate evidence without candidate cannot have portfolio fingerprint")
        elif self.portfolio_fingerprint != fingerprint({"quantities": candidate.quantities}):
            raise ValueError("candidate evidence portfolio fingerprint mismatch")

    @property
    def candidate(self):
        return self.solver_evidence.candidate


def candidate_evidence_from_payload(payload: object) -> CandidateEvidence:
    value = _object(payload, "candidate evidence", {"schema_version", "proof_input", "solver_name", "solver_version", "model_fingerprint", "portfolio_fingerprint", "solver_evidence"})
    try:
        return CandidateEvidence(
            _string(value["schema_version"], "schema_version"),
            proof_input_from_payload(value["proof_input"]),
            _string(value["solver_name"], "solver_name"),
            _string(value["solver_version"], "solver_version"),
            _fingerprint(value["model_fingerprint"], "model_fingerprint"),
            None if value["portfolio_fingerprint"] is None else _fingerprint(value["portfolio_fingerprint"], "portfolio_fingerprint"),
            solver_evidence_from_payload(value["solver_evidence"]),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid candidate evidence: {exc}") from exc


def solve(payload: object, *, backend: SolverBackend | None = None) -> dict[str, object]:
    """Consume only a canonical proof input and persist CP-SAT candidate evidence."""
    proof_input = proof_input_from_payload(payload)
    if backend is None:
        from open_trader.prediction_solver_backends import CpSatBackend

        backend = CpSatBackend()
    try:
        solver_evidence = solve_with_constraint_generation(proof_input.request, backend, proof_input.limits)
    except ModuleNotFoundError:
        solver_evidence = _empty_solver_evidence("BACKEND_UNAVAILABLE")
    return canonical_payload(_candidate_evidence(proof_input, backend.name, backend.version, solver_evidence))


def solve_via_worker(payload: object, harness: WorkerHarness, *, request_id: str) -> dict[str, object]:
    """Run the CP-SAT step through the existing serialized worker harness."""
    proof_input = proof_input_from_payload(payload)
    outcome = harness.submit(WorkerRequest(request_id, "cp_sat", proof_input.request, proof_input.limits))
    if outcome.status != "OK" or outcome.response is None or outcome.response.evidence is None:
        return canonical_payload(_candidate_evidence(proof_input, "cp_sat", "worker", _empty_solver_evidence(outcome.termination)))
    try:
        solver_evidence = solver_evidence_from_payload(outcome.response.evidence)
    except ValueError:
        solver_evidence = _empty_solver_evidence("INVALID_WORKER_EVIDENCE")
    return canonical_payload(_candidate_evidence(proof_input, "cp_sat", "worker", solver_evidence))


def _candidate_evidence(proof_input: ProofInput, solver_name: str, solver_version: str, solver_evidence: SolverEvidence) -> CandidateEvidence:
    return CandidateEvidence(
        CANDIDATE_EVIDENCE_SCHEMA_V1,
        proof_input,
        solver_name,
        solver_version,
        fingerprint(proof_input.request.problem),
        None if solver_evidence.candidate is None else fingerprint({"quantities": solver_evidence.candidate.quantities}),
        solver_evidence,
    )


def _empty_solver_evidence(reason: str) -> SolverEvidence:
    return SolverEvidence(
        reason, None, ObjectiveBounds(None, None, None, False), None, None, None, None,
        None, False, False, 0, 0, (), None,
    )


@dataclass(frozen=True, slots=True)
class VerificationResult:
    schema_version: str
    status: VerificationStatus
    proof_level: ProofLevel
    model_fingerprint: str
    portfolio_fingerprint: str | None
    quote_fingerprint: str
    current_generation: int
    solution: PortfolioSolution | None
    negative_proof: PayoutProof | None
    unknown_reason: str | None

    def __post_init__(self) -> None:
        if self.schema_version != VERIFICATION_RESULT_SCHEMA_V1 or not isinstance(self.status, VerificationStatus) or not isinstance(self.proof_level, ProofLevel):
            raise ValueError("invalid verification result")
        _fingerprint(self.model_fingerprint, "model_fingerprint")
        if self.portfolio_fingerprint is not None:
            _fingerprint(self.portfolio_fingerprint, "portfolio_fingerprint")
        _fingerprint(self.quote_fingerprint, "quote_fingerprint")
        _nonnegative_int(self.current_generation, "current_generation")
        if self.status == VerificationStatus.QUALIFIED_VERIFIED:
            if self.proof_level != ProofLevel.SOLVER_VERIFIED or self.solution is None or self.negative_proof is not None or self.unknown_reason is not None:
                raise ValueError("qualified verification requires a solver-verified solution")
        elif self.status == VerificationStatus.NOT_QUALIFIED:
            if self.proof_level != ProofLevel.NONE or self.solution is None or self.negative_proof is not None or self.unknown_reason is not None:
                raise ValueError("not-qualified verification is candidate-scoped")
        elif self.status == VerificationStatus.NO_QUALIFIED_OPPORTUNITY:
            if self.proof_level != ProofLevel.NONE or self.solution is not None or self.negative_proof is None or self.unknown_reason is not None:
                raise ValueError("component negative requires an exact negative proof")
        elif self.proof_level != ProofLevel.NONE or self.solution is not None or self.negative_proof is not None or self.unknown_reason is None:
            raise ValueError("unknown verification must fail closed")
        if self.solution is not None:
            proof = self.solution.payout_proof
            if proof.problem_fingerprint != self.model_fingerprint or proof.portfolio_fingerprint != self.portfolio_fingerprint or self.portfolio_fingerprint != fingerprint({"quantities": self.solution.quantities}):
                raise ValueError("verification result solution fingerprint mismatch")


def verify(payload: object) -> dict[str, object]:
    """Rebuild fixed-portfolio payout and qualification facts without solver objects."""
    evidence = candidate_evidence_from_payload(payload)
    candidate = evidence.candidate
    if candidate is None:
        return canonical_payload(_unknown(evidence, "NO_CANDIDATE"))
    try:
        evaluation = evaluate_fixed_portfolio(
            evidence.proof_input.request.problem,
            candidate.quantities,
            evidence.proof_input.request.budget,
        )
        support = derive_selected_support_graph(
            evidence.proof_input.request.problem,
            evaluation,
            evidence.proof_input.request.budget,
        )
        if isinstance(support, UnknownReason):
            return canonical_payload(_unknown(evidence, support.value))
        solution = build_portfolio_solution(evidence.proof_input.request.problem, evaluation, support)
    except (OverflowError, ValueError) as exc:
        return canonical_payload(_unknown(evidence, str(exc)))
    if evaluation.failed_qualification_ids:
        return canonical_payload(VerificationResult(
            VERIFICATION_RESULT_SCHEMA_V1, VerificationStatus.NOT_QUALIFIED, ProofLevel.NONE,
            evidence.model_fingerprint, evidence.portfolio_fingerprint, evidence.proof_input.quote_fingerprint,
            evidence.proof_input.current_generation, solution, None, None,
        ))
    return canonical_payload(VerificationResult(
        VERIFICATION_RESULT_SCHEMA_V1, VerificationStatus.QUALIFIED_VERIFIED, ProofLevel.SOLVER_VERIFIED,
        evidence.model_fingerprint, evidence.portfolio_fingerprint, evidence.proof_input.quote_fingerprint,
        evidence.proof_input.current_generation, solution, None, None,
    ))


def verify_component(payload: object) -> dict[str, object]:
    """The sole component-negative path: bounded exact Oracle only."""
    proof_input = proof_input_from_payload(payload)
    result = find_qualified(proof_input.request)
    if result.negative_proof is not None:
        return canonical_payload(VerificationResult(
            VERIFICATION_RESULT_SCHEMA_V1, VerificationStatus.NO_QUALIFIED_OPPORTUNITY, ProofLevel.NONE,
            fingerprint(proof_input.request.problem), None, proof_input.quote_fingerprint,
            proof_input.current_generation, None, result.negative_proof, None,
        ))
    return canonical_payload(VerificationResult(
        VERIFICATION_RESULT_SCHEMA_V1, VerificationStatus.UNKNOWN, ProofLevel.NONE,
        fingerprint(proof_input.request.problem), None, proof_input.quote_fingerprint,
        proof_input.current_generation, None, None, result.unknown_reason.value if result.unknown_reason else "QUALIFIED_CANDIDATE_EXISTS",
    ))


def _unknown(evidence: CandidateEvidence, reason: str) -> VerificationResult:
    return VerificationResult(
        VERIFICATION_RESULT_SCHEMA_V1, VerificationStatus.UNKNOWN, ProofLevel.NONE,
        evidence.model_fingerprint, evidence.portfolio_fingerprint, evidence.proof_input.quote_fingerprint,
        evidence.proof_input.current_generation, None, None, reason or "UNKNOWN",
    )


def verification_result_from_payload(payload: object, *, source: ProofInput | CandidateEvidence) -> VerificationResult:
    value = _object(payload, "verification result", {"schema_version", "status", "proof_level", "model_fingerprint", "portfolio_fingerprint", "quote_fingerprint", "current_generation", "solution", "negative_proof", "unknown_reason"})
    try:
        result = VerificationResult(
            _string(value["schema_version"], "schema_version"),
            VerificationStatus(_string(value["status"], "status")),
            ProofLevel(_string(value["proof_level"], "proof_level")),
            _fingerprint(value["model_fingerprint"], "model_fingerprint"),
            None if value["portfolio_fingerprint"] is None else _fingerprint(value["portfolio_fingerprint"], "portfolio_fingerprint"),
            _fingerprint(value["quote_fingerprint"], "quote_fingerprint"),
            _nonnegative_int(value["current_generation"], "current_generation"),
            None if value["solution"] is None else portfolio_solution_from_payload(value["solution"]),
            None if value["negative_proof"] is None else payout_proof_from_payload(value["negative_proof"]),
            None if value["unknown_reason"] is None else _string(value["unknown_reason"], "unknown_reason"),
        )
        proof_input = source.proof_input if isinstance(source, CandidateEvidence) else source
        if not isinstance(proof_input, ProofInput) or (
            result.model_fingerprint != fingerprint(proof_input.request.problem)
            or result.quote_fingerprint != proof_input.quote_fingerprint
            or result.current_generation != proof_input.current_generation
            or isinstance(source, CandidateEvidence) and result.portfolio_fingerprint != source.portfolio_fingerprint
            or result.negative_proof is not None and (
                result.negative_proof.problem_fingerprint != result.model_fingerprint
                or result.negative_proof.request_fingerprint != fingerprint(proof_input.request)
            )
        ):
            raise ValueError("verification result source binding mismatch")
        return result
    except (ModelDecodeError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid verification result: {exc}") from exc


def _limits_from_payload(payload: object) -> BenchmarkLimits:
    value = _object(payload, "limits", {"soft_time_limit_ms", "hard_time_limit_ms", "memory_limit_bytes", "max_constraint_generation_rounds"})
    return BenchmarkLimits(*(_positive_int(value[name], name) for name in ("soft_time_limit_ms", "hard_time_limit_ms", "memory_limit_bytes", "max_constraint_generation_rounds")))


def _object(payload: object, name: str, keys: set[str]) -> Mapping[str, object]:
    if not isinstance(payload, Mapping) or set(payload) != keys or not all(isinstance(key, str) for key in payload):
        raise ValueError(f"{name} must contain exactly {sorted(keys)}")
    return payload


def _mapping(payload: object, name: str) -> Mapping[str, object]:
    if not isinstance(payload, Mapping):
        raise ValueError(f"{name} must be an object")
    return payload


def _string(payload: object, name: str) -> str:
    if not isinstance(payload, str) or not payload.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return payload


def _fingerprint(payload: object, name: str) -> str:
    value = _string(payload, name)
    if not value.startswith("sha256:") or len(value) != 71 or any(character not in "0123456789abcdef" for character in value[7:]):
        raise ValueError(f"{name} must be a sha256 fingerprint")
    return value


def _nonnegative_int(payload: object, name: str) -> int:
    if isinstance(payload, bool) or not isinstance(payload, int) or payload < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return payload


def _positive_int(payload: object, name: str) -> int:
    value = _nonnegative_int(payload, name)
    if value == 0:
        raise ValueError(f"{name} must be positive")
    return value
