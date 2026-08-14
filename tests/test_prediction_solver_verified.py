from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from open_trader.prediction_n_leg import (
    ActionPayout,
    ActionQuantity,
    ActionSide,
    ArbitrageProblem,
    CandidateAction,
    Comparison,
    ConstraintModel,
    ExecutableCostSlice,
    OBSERVATION_SCHEMA_V1,
    OracleBudget,
    OracleRequest,
    ObjectiveBounds,
    PortfolioCandidate,
    PROBLEM_SCHEMA_V1,
    QualificationConstraint,
    QualificationMetric,
    REQUEST_SCHEMA_V1,
    SearchMode,
    SettlementObservationKey,
    TerminalAtom,
    TerminalKind,
    TerminalStateSet,
    canonical_payload,
    fingerprint,
)
from open_trader.prediction_n_leg_oracle import cut_from_scenario, evaluate_fixed_portfolio, find_qualified
from open_trader.prediction_solver import BenchmarkLimits, SolverEvidence
from open_trader.prediction_solver_worker import WorkerOutcome, WorkerResponse
from open_trader.prediction_solver_verified import (
    PROOF_REQUEST_SCHEMA_V1,
    CANDIDATE_EVIDENCE_SCHEMA_V1,
    CandidateEvidence,
    ProofInput,
    candidate_evidence_from_payload,
    proof_input_from_payload,
    solve,
    solve_via_worker,
    verify,
    verification_result_from_payload,
)


AS_OF = datetime(2026, 8, 14, tzinfo=UTC)


def proof_input() -> ProofInput:
    key = SettlementObservationKey(OBSERVATION_SCHEMA_V1, "oracle", "indicator", AS_OF, AS_OF, "UTC", "rules-v1")
    action = CandidateAction(
        "action-a", "venue", "account", "chain", "contract-a", key, ActionSide.BUY_YES,
        1, 1, 1, 1, "usd-cents", "usd-cents", "usd-cents-v1", (ExecutableCostSlice(1, 1, 2),),
    )
    problem = ArbitrageProblem(
        PROBLEM_SCHEMA_V1, "verified-test", AS_OF, "usd-cents", (action,),
        (TerminalStateSet("contract-a", key, "rules-v1", (
            TerminalAtom("yes", TerminalKind.NORMAL_YES, "rules-v1", (ActionPayout("action-a", 5),), AS_OF + timedelta(days=1)),
        )),),
        ConstraintModel((), ()),
        (QualificationConstraint("profit", "rules-v1", QualificationMetric.GUARANTEED_PROFIT_UNITS, Comparison.GREATER_THAN_OR_EQUAL, 3, 1),),
    )
    return ProofInput(
        PROOF_REQUEST_SCHEMA_V1,
        OracleRequest(REQUEST_SCHEMA_V1, SearchMode.ADMISSION, problem, OracleBudget(2, 2, 2)),
        BenchmarkLimits(100, 200, 1_000_000, 4),
        "sha256:" + "a" * 64,
        7,
        "diagnostic-code-version",
    )


def test_proof_input_codec_requires_exact_canonical_shape() -> None:
    payload = canonical_payload(proof_input())

    assert proof_input_from_payload(payload) == proof_input()
    payload["extra"] = True
    with pytest.raises(ValueError, match="proof input"):
        proof_input_from_payload(payload)


def test_candidate_evidence_codec_persists_canonical_candidate_and_fingerprints() -> None:
    evidence = candidate_evidence(proof_input())
    payload = canonical_payload(evidence)

    assert candidate_evidence_from_payload(payload) == evidence
    payload["portfolio_fingerprint"] = "sha256:" + "b" * 64
    with pytest.raises(ValueError, match="portfolio fingerprint"):
        candidate_evidence_from_payload(payload)


def test_solve_persists_a_feasible_candidate_without_global_optimality(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = candidate_evidence(proof_input())
    monkeypatch.setattr(
        "open_trader.prediction_solver_verified.solve_with_constraint_generation",
        lambda request, backend, limits: expected.solver_evidence,
    )

    payload = solve(canonical_payload(proof_input()), backend=type("Backend", (), {"name": "cp_sat", "version": "test"})())

    decoded = candidate_evidence_from_payload(payload)
    assert decoded.candidate is not None
    assert decoded.solver_evidence.objective_bounds.closed is False


def test_verify_independently_recomputes_fixed_candidate_as_solver_verified() -> None:
    payload = verify(canonical_payload(candidate_evidence(proof_input())))

    assert payload["status"] == "QUALIFIED_VERIFIED"
    assert payload["proof_level"] == "SOLVER_VERIFIED"
    assert payload["solution"]["payout_proof"]["guaranteed_profit_units"] == 3
    assert verification_result_from_payload(payload).status.value == "QUALIFIED_VERIFIED"


def test_not_qualified_candidate_cannot_become_component_negative_proof() -> None:
    input_ = proof_input()
    not_qualified = replace(
        input_,
        request=replace(
            input_.request,
            problem=replace(
                input_.request.problem,
                qualification_constraints=(
                    QualificationConstraint("profit", "rules-v1", QualificationMetric.GUARANTEED_PROFIT_UNITS, Comparison.GREATER_THAN_OR_EQUAL, 4, 1),
                ),
            ),
        ),
    )

    payload = verify(canonical_payload(candidate_evidence(not_qualified)))

    assert payload["status"] == "NOT_QUALIFIED"
    assert payload["negative_proof"] is None


def test_solve_via_worker_accepts_only_serialized_worker_evidence() -> None:
    expected = candidate_evidence(proof_input())

    class Harness:
        def submit(self, request):
            assert request.to_payload()["request"] == canonical_payload(proof_input().request)
            return WorkerOutcome(
                request.request_id, "OK", "COMPLETED", 1, 1, 0, False, True,
                WorkerResponse("open_trader.prediction_solver.protocol.v1", "cp_sat", request.request_id, "OK", canonical_payload(expected.solver_evidence), {name: 0 for name in ("backend", "first_qualified", "optimal", "certificate_generation", "certificate_completion", "certificate_check", "serialization")}, ()),
            )

    payload = solve_via_worker(canonical_payload(proof_input()), Harness(), request_id="proof-1")

    assert candidate_evidence_from_payload(payload).candidate == expected.candidate


def test_default_cp_sat_import_failure_is_persisted_as_unknown_evidence() -> None:
    payload = solve(canonical_payload(proof_input()))

    assert candidate_evidence_from_payload(payload).candidate is None


def test_incomplete_exact_fixed_adversary_fails_closed_as_unknown() -> None:
    original = proof_input()
    state = original.request.problem.terminal_state_sets[0]
    expanded = replace(
        original,
        request=replace(
            original.request,
            problem=replace(
                original.request.problem,
                terminal_state_sets=(replace(
                    state,
                    atoms=state.atoms + (
                        TerminalAtom("no", TerminalKind.NORMAL_NO, "rules-v1", (ActionPayout("action-a", 4),), AS_OF + timedelta(days=1)),
                    ),
                ),),
            ),
        ),
    )
    limited = replace(expanded, request=replace(expanded.request, budget=OracleBudget(2, 1, 2)))
    evidence = replace(
        candidate_evidence(expanded),
        proof_input=limited,
        model_fingerprint=fingerprint(limited.request.problem),
    )

    payload = verify(canonical_payload(evidence))

    assert payload["status"] == "UNKNOWN"
    assert payload["negative_proof"] is None


def test_verify_differential_ignores_solver_claims_and_matches_exact_oracle() -> None:
    input_ = proof_input()
    original = candidate_evidence(input_)
    forged_solver_evidence = replace(
        original.solver_evidence,
        candidate=PortfolioCandidate(original.candidate.quantities, 99),
        objective_bounds=ObjectiveBounds(99, None, None, False),
        payout_lower_bound_units=100,
        cost_upper_bound_units=1,
        guaranteed_profit_units=99,
    )
    forged = replace(original, solver_evidence=forged_solver_evidence)

    result = verification_result_from_payload(verify(canonical_payload(forged)))
    oracle = find_qualified(input_.request)

    assert result.solution is not None and oracle.solution is not None
    assert result.solution.payout_proof.guaranteed_profit_units == oracle.solution.payout_proof.guaranteed_profit_units == 3


@pytest.mark.parametrize(
    "mutate",
    (
        lambda payload: payload.__setitem__("model_fingerprint", "sha256:" + "b" * 64),
        lambda payload: payload["proof_input"]["request"]["problem"]["terminal_state_sets"][0]["atoms"][0].__setitem__("rule_version", "rules-v2"),
    ),
)
def test_fingerprint_or_terminal_rule_drift_is_rejected_before_verification(mutate) -> None:
    payload = canonical_payload(candidate_evidence(proof_input()))
    mutate(payload)

    with pytest.raises(ValueError):
        verify(payload)


def candidate_evidence(input_: ProofInput) -> CandidateEvidence:
    quantities = (ActionQuantity("action-a", 1),)
    evaluation = evaluate_fixed_portfolio(input_.request.problem, quantities, input_.request.budget)
    solver_evidence = SolverEvidence(
        "FEASIBLE", PortfolioCandidate(quantities, evaluation.guaranteed_profit_units),
        ObjectiveBounds(evaluation.guaranteed_profit_units, None, None, False),
        evaluation.worst_scenario, evaluation.payout_lower_bound_units, evaluation.cost_upper_bound_units,
        evaluation.guaranteed_profit_units, evaluation.conservative_capital_release_at,
        True, False, 1, 1, (cut_from_scenario(input_.request.problem, evaluation.worst_scenario),), None,
    )
    return CandidateEvidence(
        CANDIDATE_EVIDENCE_SCHEMA_V1, input_, "cp_sat", "test", fingerprint(input_.request.problem),
        fingerprint({"quantities": quantities}), solver_evidence,
    )
