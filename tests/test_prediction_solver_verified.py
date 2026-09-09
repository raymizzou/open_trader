from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

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
    RelationConstraint,
    RelationKind,
    SearchMode,
    SettlementObservationKey,
    TerminalAtom,
    TerminalKind,
    TerminalStateSet,
    canonical_payload,
    canonicalize_directional_actions,
    fingerprint,
    problem_from_payload,
    request_from_payload,
)
from open_trader.prediction_n_leg_oracle import (
    cut_from_scenario,
    evaluate_fixed_portfolio,
    find_qualified,
    quantity_vector_count,
)
from open_trader.prediction_live_resolver import LIVE_BUDGET, LIVE_LIMITS
from open_trader.polymarket_relation_discovery import NativeComplementMarket, NativeComplementRelation
from open_trader.relation_catalog import RelationCatalog
from open_trader.prediction_solver import BenchmarkLimits, SolverEvidence
from open_trader.prediction_solver_worker import WorkerHarness, WorkerOutcome, WorkerResponse
from open_trader.prediction_solver_verified import (
    PROOF_REQUEST_SCHEMA_V1,
    CANDIDATE_EVIDENCE_SCHEMA_V1,
    CandidateEvidence,
    ProofInput,
    candidate_evidence_from_payload,
    model_fingerprint,
    proof_input_from_payload,
    quote_fingerprint,
    solve,
    verify,
    verification_result_from_payload,
)


AS_OF = datetime(2026, 8, 14, tzinfo=UTC)
ORACLE_CORPUS_PATH = Path(__file__).with_name("fixtures") / "prediction_n_leg_v1.json"


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
        quote_fingerprint(problem),
        7,
        "diagnostic-code-version",
    )


def proof_input_for_request(request: OracleRequest) -> ProofInput:
    return ProofInput(
        PROOF_REQUEST_SCHEMA_V1,
        request,
        BenchmarkLimits(100, 200, 1_000_000, 4),
        quote_fingerprint(request.problem),
        7,
        "diagnostic-code-version",
    )


def corpus_request(case_id: str) -> OracleRequest:
    corpus = json.loads(ORACLE_CORPUS_PATH.read_text(encoding="utf-8"))
    case = next(case for case in corpus["cases"] if case["case_id"] == case_id)
    return request_from_payload(case["request"])


def live_proof_input(request: OracleRequest) -> ProofInput:
    request = replace(request, budget=LIVE_BUDGET)
    return ProofInput(
        PROOF_REQUEST_SCHEMA_V1,
        request,
        LIVE_LIMITS,
        quote_fingerprint(request.problem),
        0,
        "issue115-live-budget",
    )


def live_worker() -> WorkerHarness:
    return WorkerHarness(
        [sys.executable, "-m", "open_trader.prediction_solver_worker", "--backend", "cp_sat"],
        request_timeout_ms=LIVE_LIMITS.hard_time_limit_ms,
        startup_timeout_ms=5_000,
        env=dict(os.environ),
    )


def dual_action_negative_request(contract_count: int = 2) -> OracleRequest:
    as_of = datetime(2026, 9, 8, tzinfo=UTC)
    key = SettlementObservationKey(
        OBSERVATION_SCHEMA_V1,
        "oracle",
        "indicator",
        as_of,
        as_of,
        "UTC",
        "rules-v1",
    )
    actions = []
    states = []
    contract_ids = tuple(f"contract-{index}" for index in range(contract_count))
    for index, contract_id in enumerate(contract_ids):
        action_id = f"polymarket:{contract_id}"
        actions.append(CandidateAction(
            action_id,
            "polymarket",
            "account",
            "chain",
            contract_id,
            key,
            ActionSide.BUY_YES,
            1,
            1,
            1,
            1,
            "usd",
            "usd",
            "usd-v1",
            (ExecutableCostSlice(1, 1, 1),),
        ))
        states.append(TerminalStateSet(
            contract_id,
            key,
            "rules-v1",
            tuple(
                TerminalAtom(
                    f"{contract_id}:{kind.value}",
                    kind,
                    "rules-v1",
                    (ActionPayout(action_id, payout),),
                    as_of + timedelta(days=1),
                )
                for kind, payout in (
                    (TerminalKind.NORMAL_YES, 4),
                    (TerminalKind.NORMAL_NO, 0),
                    (TerminalKind.VOID, 0),
                )
            ),
        ))
    problem = canonicalize_directional_actions(ArbitrageProblem(
        PROBLEM_SCHEMA_V1,
        "dual-action-negative",
        as_of,
        "usd",
        tuple(actions),
        tuple(states),
        ConstraintModel(
            tuple(
                RelationConstraint(
                    f"{antecedent}-implies-{consequent}",
                    RelationKind.IMPLIES,
                    (antecedent, consequent),
                    "rules-v1",
                )
                for antecedent, consequent in zip(contract_ids, contract_ids[1:])
            ),
            (),
        ),
        (
            QualificationConstraint(
                "minimum-profit",
                "rules-v1",
                QualificationMetric.GUARANTEED_PROFIT_UNITS,
                Comparison.GREATER_THAN_OR_EQUAL,
                1,
                1,
            ),
        ),
    ))
    return OracleRequest(REQUEST_SCHEMA_V1, SearchMode.ADMISSION, problem, LIVE_BUDGET)


def native_complement_proof_input(tmp_path: Path) -> ProofInput:
    relation = NativeComplementRelation(
        event_id="event-1",
        market=NativeComplementMarket(
            event_id="event-1",
            market_id="market-1",
            condition_id="condition-1",
            question="Will the candidate win?",
            rules="official index",
            resolution_source="Binance",
            end_date="2026-12-31T17:00:00Z",
            yes_token_id="yes-1",
            no_token_id="no-1",
            rules_hash="rules-1",
        ),
    )
    catalog = RelationCatalog(tmp_path)
    assert catalog.ingest_mechanical_relation(relation)["status"] == "PENDING"
    (row,) = catalog.review_rows()
    compiled = problem_from_payload(row["model"]["problem"])
    problem = replace(
        compiled,
        actions=tuple(
            replace(action, cost_slices=(ExecutableCostSlice(1, 1, 1),))
            for action in compiled.actions
        ),
        qualification_constraints=(
            QualificationConstraint(
                "minimum-profit",
                "rules-1",
                QualificationMetric.GUARANTEED_PROFIT_UNITS,
                Comparison.GREATER_THAN_OR_EQUAL,
                1,
                1,
            ),
        ),
    )
    return live_proof_input(OracleRequest(REQUEST_SCHEMA_V1, SearchMode.ADMISSION, problem, LIVE_BUDGET))


def test_proof_input_codec_requires_exact_canonical_shape() -> None:
    payload = canonical_payload(proof_input())

    assert proof_input_from_payload(payload) == proof_input()
    payload["extra"] = True
    with pytest.raises(ValueError, match="proof input"):
        proof_input_from_payload(payload)


def test_quote_fingerprint_rejects_actions_without_embedded_cost_input() -> None:
    with pytest.raises(ValueError, match="cost"):
        quote_fingerprint({"actions": [{"action_id": "action-a"}]})


def test_quote_only_cost_change_keeps_model_identity_but_changes_quote_identity() -> None:
    original = proof_input()
    action = original.request.problem.actions[0]
    repriced_problem = replace(
        original.request.problem,
        actions=(replace(action, cost_slices=(ExecutableCostSlice(1, 1, 3),)),),
    )
    repriced = replace(
        original,
        request=replace(original.request, problem=repriced_problem),
        quote_fingerprint=quote_fingerprint(repriced_problem),
    )

    assert candidate_evidence(original).model_fingerprint == candidate_evidence(repriced).model_fingerprint
    assert original.quote_fingerprint != repriced.quote_fingerprint


def test_structural_change_changes_model_identity() -> None:
    original = proof_input()
    changed_problem = replace(
        original.request.problem,
        qualification_constraints=(QualificationConstraint("profit", "rules-v1", QualificationMetric.GUARANTEED_PROFIT_UNITS, Comparison.GREATER_THAN_OR_EQUAL, 2, 1),),
    )
    changed = replace(
        original,
        request=replace(original.request, problem=changed_problem),
        quote_fingerprint=quote_fingerprint(changed_problem),
    )

    assert candidate_evidence(original).model_fingerprint != candidate_evidence(changed).model_fingerprint


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
    assert verification_result_from_payload(payload, source=candidate_evidence(proof_input())).status.value == "QUALIFIED_VERIFIED"


def test_live_budget_verifies_fixed_implies_candidate() -> None:
    input_ = live_proof_input(corpus_request("implication-two-contract"))

    with live_worker() as harness:
        evidence = solve(
            canonical_payload(input_),
            harness=harness,
            request_id="issue115-case1",
        )
    payload = verify(evidence)

    assert payload["status"] == "QUALIFIED_VERIFIED"
    assert payload["proof_level"] == "SOLVER_VERIFIED"
    assert payload["solution"]["payout_proof"]["payout_lower_bound_units"] == 100
    assert payload["solution"]["payout_proof"]["cost_upper_bound_units"] == 90
    assert payload["solution"]["payout_proof"]["guaranteed_profit_units"] == 10


def test_live_budget_closes_dual_action_negative_proof() -> None:
    input_ = live_proof_input(dual_action_negative_request())

    with live_worker() as harness:
        evidence = solve(
            canonical_payload(input_),
            harness=harness,
            request_id="issue115-case2",
        )
    assert candidate_evidence_from_payload(evidence).candidate is None

    payload = verify(canonical_payload(input_))

    assert payload["status"] == "NO_QUALIFIED_OPPORTUNITY"


def test_live_budget_verifies_native_complement_25_states(tmp_path: Path) -> None:
    input_ = native_complement_proof_input(tmp_path)
    problem = input_.request.problem

    assert {atom.kind for state in problem.terminal_state_sets for atom in state.atoms} == {
        TerminalKind.NORMAL_YES,
        TerminalKind.NORMAL_NO,
        TerminalKind.VOID,
        TerminalKind.REFUND,
        TerminalKind.SPLIT,
    }
    assert quantity_vector_count(problem) == 4
    assert len(problem.terminal_state_sets) == 2
    assert len(problem.terminal_state_sets[0].atoms) * len(problem.terminal_state_sets[1].atoms) == 25

    payload = verify(canonical_payload(input_))

    assert payload["status"] == "NO_QUALIFIED_OPPORTUNITY"
    assert payload["negative_proof"]["proof_method"] == "EXHAUSTIVE_ORACLE_V1"


@pytest.mark.parametrize(("contract_count", "expected_vectors"), ((5, 1_024), (6, 4_096)))
def test_live_budget_keeps_large_components_unknown(contract_count: int, expected_vectors: int) -> None:
    input_ = live_proof_input(dual_action_negative_request(contract_count))

    assert quantity_vector_count(input_.request.problem) == expected_vectors
    payload = verify(canonical_payload(input_))

    assert payload["status"] == "UNKNOWN"
    assert payload["unknown_reason"] == "ORACLE_DECISION_LIMIT_EXCEEDED"


def test_positive_result_cannot_be_decoded_against_only_a_proof_input() -> None:
    payload = verify(canonical_payload(candidate_evidence(proof_input())))

    with pytest.raises(ValueError, match="source binding"):
        verification_result_from_payload(payload, source=proof_input())


def test_no_candidate_unknown_cannot_be_decoded_against_only_a_proof_input() -> None:
    original = candidate_evidence(proof_input())
    evidence = replace(
        original,
        portfolio_fingerprint=None,
        solver_evidence=SolverEvidence(
            "INFEASIBLE", None, ObjectiveBounds(None, None, None, False), None, None, None, None,
            None, False, False, 0, 0, (), None,
        ),
    )
    payload = verify(canonical_payload(evidence))

    with pytest.raises(ValueError, match="source binding"):
        verification_result_from_payload(payload, source=evidence.proof_input)


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


def test_component_negative_requires_completed_exact_oracle() -> None:
    input_ = proof_input()
    no_qualification = replace(
        input_,
        request=replace(input_.request, problem=replace(
            input_.request.problem,
            qualification_constraints=(QualificationConstraint("profit", "rules-v1", QualificationMetric.GUARANTEED_PROFIT_UNITS, Comparison.GREATER_THAN_OR_EQUAL, 4, 1),),
        )),
    )

    payload = verify(canonical_payload(no_qualification))

    assert payload["status"] == "NO_QUALIFIED_OPPORTUNITY"
    assert payload["negative_proof"]["proof_method"] == "EXHAUSTIVE_ORACLE_V1"


def test_component_negative_proof_tampering_fails_source_binding() -> None:
    input_ = proof_input()
    no_qualification = replace(
        input_,
        request=replace(input_.request, problem=replace(
            input_.request.problem,
            qualification_constraints=(QualificationConstraint("profit", "rules-v1", QualificationMetric.GUARANTEED_PROFIT_UNITS, Comparison.GREATER_THAN_OR_EQUAL, 4, 1),),
        )),
    )
    payload = verify(canonical_payload(no_qualification))
    payload["negative_proof"]["problem_fingerprint"] = "sha256:" + "b" * 64

    with pytest.raises(ValueError, match="source binding"):
        verification_result_from_payload(payload, source=no_qualification)


@pytest.mark.parametrize(
    "field,value",
    (("model_fingerprint", "sha256:" + "b" * 64), ("portfolio_fingerprint", "sha256:" + "b" * 64), ("quote_fingerprint", "sha256:" + "b" * 64), ("current_generation", 8)),
)
def test_result_tampering_fails_source_and_solution_binding(field, value) -> None:
    evidence = candidate_evidence(proof_input())
    payload = verify(canonical_payload(evidence))
    payload[field] = value

    with pytest.raises(ValueError):
        verification_result_from_payload(payload, source=evidence)


def test_solve_accepts_only_serialized_worker_evidence() -> None:
    expected = candidate_evidence(proof_input())

    class Harness:
        def submit(self, request):
            assert request.to_payload()["request"] == canonical_payload(proof_input().request)
            return WorkerOutcome(
                request.request_id, "OK", "COMPLETED", 1, 1, 0, False, True,
                WorkerResponse("open_trader.prediction_solver.protocol.v1", "cp_sat", request.request_id, "OK", canonical_payload(expected.solver_evidence), {name: 0 for name in ("backend", "first_qualified", "optimal", "certificate_generation", "certificate_completion", "certificate_check", "serialization")}, ()),
            )

    payload = solve(canonical_payload(proof_input()), harness=Harness(), request_id="proof-1")

    assert candidate_evidence_from_payload(payload).candidate == expected.candidate


def test_solve_preserves_returned_worker_solver_version() -> None:
    expected = candidate_evidence(proof_input())

    class Harness:
        def submit(self, request):
            return SimpleNamespace(
                status="OK",
                termination="COMPLETED",
                solver_version="9.15.6755",
                response=WorkerResponse(
                    "open_trader.prediction_solver.protocol.v1", "cp_sat", request.request_id, "OK",
                    canonical_payload(expected.solver_evidence),
                    {name: 0 for name in ("backend", "first_qualified", "optimal", "certificate_generation", "certificate_completion", "certificate_check", "serialization")},
                    (),
                ),
            )

    payload = solve(canonical_payload(proof_input()), harness=Harness(), request_id="proof-version")

    assert payload["solver_version"] == "9.15.6755"


def test_direct_solve_rejects_a_worker_request_id() -> None:
    with pytest.raises(ValueError, match="request_id"):
        solve(
            canonical_payload(proof_input()),
            backend=type("Backend", (), {"name": "cp_sat", "version": "test"})(),
            request_id="worker-only",
        )


@pytest.mark.parametrize(
    "kwargs",
    (
        {"harness": object()},
        {"harness": object(), "request_id": ""},
        {"backend": type("Backend", (), {"name": "cp_sat", "version": "test"})(), "harness": object(), "request_id": "worker-id"},
    ),
)
def test_worker_solve_requires_a_nonempty_request_id_and_no_direct_backend(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="worker solve"):
        solve(canonical_payload(proof_input()), **kwargs)


def test_default_cp_sat_import_failure_is_persisted_as_unknown_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(*args, **kwargs):
        raise ModuleNotFoundError("ortools")

    monkeypatch.setattr("open_trader.prediction_solver_verified.solve_with_constraint_generation", unavailable)
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
        model_fingerprint=model_fingerprint(limited.request.problem),
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

    result = verification_result_from_payload(verify(canonical_payload(forged)), source=forged)
    oracle = find_qualified(input_.request)

    assert result.solution is not None and oracle.solution is not None
    assert result.solution.payout_proof.guaranteed_profit_units == oracle.solution.payout_proof.guaranteed_profit_units == 3


@pytest.mark.parametrize("case_id", ("exactly-one-n3", "explicit-exception-link", "disconnected-double-arbitrage"))
def test_verified_candidate_replays_multiple_exact_oracle_relation_cases(case_id: str) -> None:
    input_ = proof_input_for_request(corpus_request(case_id))
    oracle = find_qualified(input_.request)
    assert oracle.solution is not None
    evidence = candidate_evidence(input_, oracle.solution.quantities)

    result = verification_result_from_payload(verify(canonical_payload(evidence)), source=evidence)

    assert result.status.value == "QUALIFIED_VERIFIED"
    assert result.solution == oracle.solution


@pytest.mark.parametrize("case_id", ("void-refund-split", "rounded-false-edge", "no-qualified-positive-raw"))
def test_component_negative_replays_multiple_completed_exact_oracle_cases(case_id: str) -> None:
    input_ = proof_input_for_request(corpus_request(case_id))
    oracle = find_qualified(input_.request)
    assert oracle.negative_proof is not None

    result = verification_result_from_payload(verify(canonical_payload(input_)), source=input_)

    assert result.status.value == "NO_QUALIFIED_OPPORTUNITY"
    assert result.negative_proof == oracle.negative_proof


@pytest.mark.parametrize("case_id", ("unknown-state-budget", "unknown-decision-budget"))
def test_component_negative_budget_exhaustion_replays_as_unknown(case_id: str) -> None:
    input_ = proof_input_for_request(corpus_request(case_id))
    oracle = find_qualified(input_.request)
    assert oracle.negative_proof is None and oracle.unknown_reason is not None

    result = verification_result_from_payload(verify(canonical_payload(input_)), source=input_)

    assert result.status.value == "UNKNOWN"
    assert result.unknown_reason == oracle.unknown_reason.value


@pytest.mark.parametrize(
    "mutate",
    (
        lambda payload: payload.__setitem__("model_fingerprint", "sha256:" + "b" * 64),
        lambda payload: payload["proof_input"]["request"]["problem"]["terminal_state_sets"][0]["atoms"][0].__setitem__("rule_version", "rules-v2"),
    ),
)
def test_fingerprint_or_terminal_rule_drift_fails_closed_as_unknown(mutate) -> None:
    payload = canonical_payload(candidate_evidence(proof_input()))
    mutate(payload)

    result = verify(payload)

    assert result["status"] == "UNKNOWN"
    assert result["unknown_reason"] == "INVALID_CANONICAL_SEMANTICS"


def test_terminal_atom_rule_version_mismatch_fails_closed() -> None:
    payload = canonical_payload(candidate_evidence(proof_input()))
    payload["proof_input"]["request"]["problem"]["terminal_state_sets"][0]["atoms"][0]["rule_version"] = "other-rules"

    with pytest.raises(ValueError, match="TERMINAL_RULE_VERSION_MISMATCH"):
        candidate_evidence_from_payload(payload)


def test_verify_maps_recomputed_terminal_rule_mismatch_to_canonical_unknown() -> None:
    payload = canonical_payload(candidate_evidence(proof_input()))
    problem = payload["proof_input"]["request"]["problem"]
    problem["terminal_state_sets"][0]["atoms"][0]["rule_version"] = "rules-v2"
    payload["model_fingerprint"] = model_fingerprint(problem)

    result = verify(payload)

    assert result["status"] == "UNKNOWN"
    assert result["unknown_reason"] == "INVALID_CANONICAL_SEMANTICS"
    assert result["model_fingerprint"] == model_fingerprint(problem)


def candidate_evidence(input_: ProofInput, quantities: tuple[ActionQuantity, ...] = (ActionQuantity("action-a", 1),)) -> CandidateEvidence:
    evaluation = evaluate_fixed_portfolio(input_.request.problem, quantities, input_.request.budget)
    solver_evidence = SolverEvidence(
        "FEASIBLE", PortfolioCandidate(quantities, evaluation.guaranteed_profit_units),
        ObjectiveBounds(evaluation.guaranteed_profit_units, None, None, False),
        evaluation.worst_scenario, evaluation.payout_lower_bound_units, evaluation.cost_upper_bound_units,
        evaluation.guaranteed_profit_units, evaluation.conservative_capital_release_at,
        True, False, 1, 1, (cut_from_scenario(input_.request.problem, evaluation.worst_scenario),), None,
    )
    return CandidateEvidence(
        CANDIDATE_EVIDENCE_SCHEMA_V1, input_, "cp_sat", "test", model_fingerprint(input_.request.problem),
        fingerprint({"quantities": quantities}), solver_evidence,
    )
