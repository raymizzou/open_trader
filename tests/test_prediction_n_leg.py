from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

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
    ExhaustiveSearchProof,
    ForbiddenAtomCombination,
    ObjectiveBounds,
    OptimalityStatus,
    OracleBudget,
    OracleRequest,
    OracleResult,
    PayoutProof,
    PortfolioCandidate,
    PortfolioSolution,
    ProofStatus,
    QualificationConstraint,
    QualificationMetric,
    RelationConstraint,
    RelationKind,
    SearchMode,
    SelectedAtom,
    SelectedSupportGraph,
    SettlementObservationKey,
    SettlementScenario,
    SolveStatus,
    TerminalAtom,
    TerminalKind,
    TerminalStateSet,
    UnknownReason,
    WorstStateCut,
    ModelDecodeError,
    canonical_json,
    canonical_payload,
    fingerprint,
    problem_from_payload,
    request_from_payload,
    result_from_payload,
    validate_problem,
)


AS_OF = datetime(2026, 8, 11, tzinfo=UTC)


def sample_problem() -> ArbitrageProblem:
    observation = SettlementObservationKey(
        schema_version="open_trader.prediction_n_leg.observation.v1",
        oracle_id="oracle-a",
        indicator_id="indicator-a",
        observation_start=AS_OF,
        observation_end=AS_OF,
        timezone="UTC",
        rule_version="v1",
    )
    action = CandidateAction(
        action_id="buy-no-a",
        market_contract_id="contract-a",
        settlement_observation_key=observation,
        side=ActionSide.BUY_NO,
        lot_step_units=1,
        quantity_scale=1,
        settlement_asset_id="usd",
        valuation_unit_id="usd-cents",
        asset_valuation_rule_id="usd-cents-v1",
        cost_slices=(ExecutableCostSlice(1, 1, 90),),
    )
    terminal_state_set = TerminalStateSet(
        market_contract_id="contract-a",
        settlement_observation_key=observation,
        rule_version="v1",
        atoms=(
            TerminalAtom(
                atom_id="a-no",
                kind=TerminalKind.NORMAL_NO,
                rule_version="v1",
                payouts=(ActionPayout("buy-no-a", 100),),
                capital_release_at=AS_OF,
            ),
        ),
    )
    return ArbitrageProblem(
        schema_version="open_trader.prediction_n_leg.problem.v1",
        problem_id="problem-1",
        as_of=AS_OF,
        valuation_unit_id="usd-cents",
        actions=(action,),
        terminal_state_sets=(terminal_state_set,),
        constraint_model=ConstraintModel(
            relations=(),
            forbidden_atom_combinations=(
                ForbiddenAtomCombination("forbidden-1", ("a-no",), "v1"),
            ),
        ),
        qualification_constraints=(
            QualificationConstraint(
                constraint_id="qualified-profit",
                rule_version="v1",
                metric=QualificationMetric.GUARANTEED_PROFIT_UNITS,
                comparison=Comparison.GREATER_THAN_OR_EQUAL,
                threshold_numerator=10,
                threshold_denominator=1,
            ),
        ),
    )


def test_canonical_contract_separates_master_adversary_and_result_statuses() -> None:
    problem = sample_problem()
    candidate = PortfolioCandidate(
        quantities=(ActionQuantity("buy-no-a", 1), ActionQuantity("buy-yes-b", 1)),
        claimed_guaranteed_profit_units=10,
    )
    scenario = SettlementScenario(
        atoms=(SelectedAtom("contract-a", "a-no"), SelectedAtom("contract-b", "b-no")),
    )
    cut = WorstStateCut(
        cut_id="cut:a-no:b-no",
        scenario=scenario,
        payout_per_lot=(ActionPayout("buy-no-a", 100), ActionPayout("buy-yes-b", 0)),
    )
    support_graph = SelectedSupportGraph(
        action_ids=("buy-no-a",),
        contract_ids=("contract-a",),
        constraint_ids=("relation-1",),
        hyperedges=(("relation-1", ("contract-a",)),),
    )
    proof = PayoutProof(
        problem_fingerprint="problem-fingerprint",
        portfolio_fingerprint="portfolio-fingerprint",
        worst_scenario=scenario,
        worst_state_cut=cut,
        payout_lower_bound_units=100,
        cost_upper_bound_units=90,
        guaranteed_profit_units=10,
        conservative_capital_release_at=AS_OF,
        selected_support_graph=support_graph,
    )
    solution = PortfolioSolution(quantities=candidate.quantities, payout_proof=proof)
    request = OracleRequest(
        schema_version="open_trader.prediction_n_leg.request.v1",
        mode=SearchMode.ADMISSION,
        problem=problem,
        budget=OracleBudget(1, 1, 1),
    )
    negative_proof = ExhaustiveSearchProof(
        proof_method="exhaustive-v1",
        conclusion=BusinessStatus.NO_QUALIFIED_OPPORTUNITY,
        request_fingerprint="request-fingerprint",
        problem_fingerprint="problem-fingerprint",
        source_problem_fingerprint=None,
        qualification_fingerprint="qualification-fingerprint",
        quantity_vectors_total=1,
        quantity_vectors_examined=1,
        joint_states_per_vector=1,
        rejection_counts=(("qualified-profit", 1),),
    )
    result = OracleResult(
        solve_status=SolveStatus.FEASIBLE,
        proof_status=ProofStatus.PROVEN,
        business_status=BusinessStatus.QUALIFIED_FEASIBLE,
        optimality_status=OptimalityStatus.NOT_PROVEN,
        objective_bounds=ObjectiveBounds(
            lower_bound_units=10,
            upper_bound_units=None,
            gap_units=None,
            closed=False,
        ),
        solution=solution,
        negative_proof=negative_proof,
        unknown_reason=UnknownReason.ORACLE_STATE_LIMIT_EXCEEDED,
    )

    assert problem.schema_version == "open_trader.prediction_n_leg.problem.v1"
    assert candidate.claimed_guaranteed_profit_units == 10
    assert cut.scenario == scenario
    assert request.problem.qualification_constraints == problem.qualification_constraints
    assert result.business_status == BusinessStatus.QUALIFIED_FEASIBLE
    assert result.optimality_status == OptimalityStatus.NOT_PROVEN
    assert result.objective_bounds.closed is False


def sample_problem_with_two_actions() -> ArbitrageProblem:
    problem = sample_problem()
    observation = problem.actions[0].settlement_observation_key
    action_b = replace(
        problem.actions[0],
        action_id="buy-yes-b",
        market_contract_id="contract-b",
        side=ActionSide.BUY_YES,
        cost_slices=(ExecutableCostSlice(1, 1, 80),),
    )
    state_b = TerminalStateSet(
        market_contract_id="contract-b",
        settlement_observation_key=observation,
        rule_version="v1",
        atoms=(
            TerminalAtom(
                atom_id="b-yes",
                kind=TerminalKind.NORMAL_YES,
                rule_version="v1",
                payouts=(ActionPayout("buy-yes-b", 100),),
                capital_release_at=AS_OF,
            ),
        ),
    )
    return replace(
        problem,
        actions=(action_b, problem.actions[0]),
        terminal_state_sets=(state_b, problem.terminal_state_sets[0]),
        constraint_model=ConstraintModel(
            relations=(
                RelationConstraint("relation-z", RelationKind.IMPLIES, ("contract-b", "contract-a"), "v1"),
                RelationConstraint("relation-a", RelationKind.MUTUALLY_EXCLUSIVE, ("contract-a", "contract-b"), "v1"),
            ),
            forbidden_atom_combinations=(
                ForbiddenAtomCombination("forbidden-z", ("b-yes", "a-no"), "v1"),
                ForbiddenAtomCombination("forbidden-a", ("a-no",), "v1"),
            ),
        ),
        qualification_constraints=(
            QualificationConstraint("qualified-z", "v1", QualificationMetric.GUARANTEED_PROFIT_UNITS, Comparison.GREATER_THAN_OR_EQUAL, 10, 1),
            QualificationConstraint("qualified-a", "v1", QualificationMetric.NET_MARGIN_PPM, Comparison.GREATER_THAN_OR_EQUAL, 1, 1),
        ),
    )


def test_valid_problem_round_trips_through_canonical_json() -> None:
    problem = sample_problem_with_two_actions()

    decoded = problem_from_payload(json.loads(canonical_json(problem)))

    assert canonical_json(decoded) == canonical_json(problem)
    assert fingerprint(decoded) == fingerprint(problem)


def test_canonical_json_sorts_unordered_problem_collections_but_preserves_implies_order() -> None:
    problem = sample_problem_with_two_actions()
    reordered = replace(
        problem,
        actions=tuple(reversed(problem.actions)),
        terminal_state_sets=tuple(reversed(problem.terminal_state_sets)),
        constraint_model=replace(
            problem.constraint_model,
            relations=tuple(reversed(problem.constraint_model.relations)),
            forbidden_atom_combinations=tuple(reversed(problem.constraint_model.forbidden_atom_combinations)),
        ),
        qualification_constraints=tuple(reversed(problem.qualification_constraints)),
    )

    payload = canonical_payload(problem)
    relation = next(item for item in payload["constraint_model"]["relations"] if item["constraint_id"] == "relation-z")

    assert canonical_json(reordered) == canonical_json(problem)
    assert fingerprint(reordered) == fingerprint(problem)
    assert relation["contract_ids"] == ["contract-b", "contract-a"]


@pytest.mark.parametrize(
    ("problem", "code"),
    [
        (replace(sample_problem(), actions=(replace(sample_problem().actions[0], lot_step_units=True),)), "INVALID_INTEGER"),
        (replace(sample_problem(), actions=(replace(sample_problem().actions[0], lot_step_units=1.5),)), "INVALID_INTEGER"),
        (replace(sample_problem(), actions=(replace(sample_problem().actions[0], lot_step_units=0),)), "NON_POSITIVE_LOT_STEP"),
        (replace(sample_problem(), actions=(replace(sample_problem().actions[0], lot_step_units=-1),)), "NON_POSITIVE_LOT_STEP"),
        (replace(sample_problem(), actions=(replace(sample_problem().actions[0], cost_slices=(ExecutableCostSlice(1, 1, 90), ExecutableCostSlice(3, 3, 90))),)), "NON_CONTIGUOUS_COST_SLICES"),
        (replace(sample_problem(), actions=(sample_problem().actions[0], sample_problem().actions[0])), "DUPLICATE_ID"),
        (replace(sample_problem(), terminal_state_sets=(replace(sample_problem().terminal_state_sets[0], atoms=(replace(sample_problem().terminal_state_sets[0].atoms[0], payouts=()),)),)), "MISSING_ACTION_PAYOUT"),
        (replace(sample_problem(), terminal_state_sets=(replace(sample_problem().terminal_state_sets[0], atoms=(replace(sample_problem().terminal_state_sets[0].atoms[0], capital_release_at=AS_OF - timedelta(seconds=1)),)),)), "STALE_CAPITAL_RELEASE_AT"),
        (replace(sample_problem(), terminal_state_sets=(replace(sample_problem().terminal_state_sets[0], atoms=(replace(sample_problem().terminal_state_sets[0].atoms[0], capital_release_at=datetime(2026, 8, 12)),)),)), "NAIVE_DATETIME"),
        (replace(sample_problem(), constraint_model=ConstraintModel((RelationConstraint("relation-x", RelationKind.IMPLIES, ("contract-a", "missing"), "v1"),), ())), "UNKNOWN_CONTRACT_REFERENCE"),
        (replace(sample_problem(), actions=(replace(sample_problem().actions[0], valuation_unit_id="eur-cents"),)), "VALUATION_UNIT_MISMATCH"),
        (replace(sample_problem(), actions=(replace(sample_problem().actions[0], quantity_scale=2**63),)), "INTEGER_OUT_OF_RANGE"),
        (replace(sample_problem(), actions=(replace(sample_problem().actions[0], lot_step_units=2**62, quantity_scale=4),)), "DERIVED_INTEGER_OUT_OF_RANGE"),
        (replace(sample_problem(), actions=(replace(sample_problem().actions[0], settlement_asset_id="eur", asset_valuation_rule_id=""),)), "MISSING_ASSET_VALUATION_RULE"),
    ],
)
def test_validate_problem_rejects_every_required_invalid_shape(problem: ArbitrageProblem, code: str) -> None:
    assert code in {issue.code for issue in validate_problem(problem)}


def test_asset_valuation_rule_id_changes_problem_fingerprint() -> None:
    problem = sample_problem()
    changed = replace(problem, actions=(replace(problem.actions[0], asset_valuation_rule_id="usd-cents-v2"),))

    assert fingerprint(changed) != fingerprint(problem)


def test_fingerprints_change_for_problem_portfolio_and_request_math() -> None:
    problem = sample_problem()
    portfolio = PortfolioCandidate((ActionQuantity("buy-no-a", 1),), 10)
    request = OracleRequest("open_trader.prediction_n_leg.request.v1", SearchMode.ADMISSION, problem, OracleBudget(1, 1, 1))

    assert fingerprint(replace(problem, actions=(replace(problem.actions[0], cost_slices=(ExecutableCostSlice(1, 1, 91),)),))) != fingerprint(problem)
    assert fingerprint(replace(portfolio, claimed_guaranteed_profit_units=11)) != fingerprint(portfolio)
    assert fingerprint(replace(request, budget=OracleBudget(2, 1, 1))) != fingerprint(request)


def test_fingerprint_uses_canonical_json_and_schema_version() -> None:
    payload = {"schema_version": "v1", "value": 7}

    assert fingerprint(payload) == "sha256:8488f77c4baf5203eec22972f9cc9e17963c379295e34913b33334e0f4424812"


def valid_result() -> OracleResult:
    problem = sample_problem()
    scenario = SettlementScenario((SelectedAtom("contract-a", "a-no"),))
    proof = PayoutProof(
        problem_fingerprint=fingerprint(problem),
        portfolio_fingerprint="sha256:portfolio",
        worst_scenario=scenario,
        worst_state_cut=WorstStateCut("cut:a-no", scenario, (ActionPayout("buy-no-a", 100),)),
        payout_lower_bound_units=100,
        cost_upper_bound_units=90,
        guaranteed_profit_units=10,
        conservative_capital_release_at=AS_OF + timedelta(days=1),
        selected_support_graph=SelectedSupportGraph(("buy-no-a",), ("contract-a",), (), ()),
    )
    return OracleResult(
        solve_status=SolveStatus.FEASIBLE,
        proof_status=ProofStatus.PROVEN,
        business_status=BusinessStatus.QUALIFIED_FEASIBLE,
        optimality_status=OptimalityStatus.NOT_PROVEN,
        objective_bounds=ObjectiveBounds(10, None, None, False),
        solution=PortfolioSolution((ActionQuantity("buy-no-a", 1),), proof),
        negative_proof=None,
        unknown_reason=None,
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update(solution=None),
        lambda payload: payload.update(optimality_status="OPTIMAL"),
        lambda payload: payload.update(
            solve_status="INFEASIBLE",
            proof_status="PROVEN",
            business_status="NO_QUALIFIED_OPPORTUNITY",
            optimality_status="NOT_APPLICABLE",
            solution=None,
            negative_proof=None,
            unknown_reason=None,
        ),
        lambda payload: payload.update(
            solve_status="INFEASIBLE",
            proof_status="PROVEN",
            business_status="NO_ARBITRAGE",
            optimality_status="NOT_APPLICABLE",
            solution=None,
            negative_proof=None,
            unknown_reason=None,
        ),
    ],
)
def test_result_from_payload_rejects_impossible_status_combinations(mutate: object) -> None:
    payload = canonical_payload(valid_result())
    mutate(payload)  # type: ignore[operator]

    with pytest.raises(ModelDecodeError):
        result_from_payload(payload)


def test_request_from_payload_rejects_non_integer_budget() -> None:
    request = OracleRequest("open_trader.prediction_n_leg.request.v1", SearchMode.ADMISSION, sample_problem(), OracleBudget(1, 1, 1))
    payload = canonical_payload(request)
    payload["budget"]["max_joint_states"] = True

    with pytest.raises(ModelDecodeError):
        request_from_payload(payload)


def test_validate_problem_rejects_direct_non_buy_action_side() -> None:
    problem = sample_problem()
    malformed = replace(problem, actions=(replace(problem.actions[0], side="SELL"),))

    assert "INVALID_ACTION_SIDE" in {issue.code for issue in validate_problem(malformed)}


def test_validate_problem_reports_naive_as_of_without_comparison_error() -> None:
    malformed = replace(sample_problem(), as_of=datetime(2026, 8, 11))

    assert "NAIVE_DATETIME" in {issue.code for issue in validate_problem(malformed)}


def negative_result_payload() -> dict[str, object]:
    proof = ExhaustiveSearchProof(
        proof_method="EXHAUSTIVE_ORACLE_V1",
        conclusion=BusinessStatus.NO_QUALIFIED_OPPORTUNITY,
        request_fingerprint="sha256:request",
        problem_fingerprint="sha256:problem",
        source_problem_fingerprint=None,
        qualification_fingerprint="sha256:qualification",
        quantity_vectors_total=1,
        quantity_vectors_examined=1,
        joint_states_per_vector=1,
        rejection_counts=(("qualified-profit", 1),),
    )
    return canonical_payload(
        OracleResult(
            solve_status=SolveStatus.INFEASIBLE,
            proof_status=ProofStatus.PROVEN,
            business_status=BusinessStatus.NO_QUALIFIED_OPPORTUNITY,
            optimality_status=OptimalityStatus.NOT_APPLICABLE,
            objective_bounds=ObjectiveBounds(None, None, None, False),
            solution=None,
            negative_proof=proof,
            unknown_reason=None,
        )
    )


def test_result_from_payload_rejects_negative_quantity_lots() -> None:
    payload = canonical_payload(valid_result())
    payload["solution"]["quantities"][0]["quantity_lots"] = -1

    with pytest.raises(ModelDecodeError):
        result_from_payload(payload)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda proof: proof.update(quantity_vectors_total=-1, quantity_vectors_examined=-1),
        lambda proof: proof.update(quantity_vectors_total=1, quantity_vectors_examined=0),
        lambda proof: proof.update(joint_states_per_vector=-1),
        lambda proof: proof["rejection_counts"][0].__setitem__(1, -1),
    ],
)
def test_result_from_payload_rejects_non_exhaustive_or_negative_proof_counts(mutate: object) -> None:
    payload = negative_result_payload()
    mutate(payload["negative_proof"])  # type: ignore[operator]

    with pytest.raises(ModelDecodeError):
        result_from_payload(payload)


def test_validate_problem_reports_mixed_observation_datetimes_without_comparison_error() -> None:
    problem = sample_problem()
    key = replace(problem.actions[0].settlement_observation_key, observation_start=datetime(2026, 8, 11))
    malformed = replace(
        problem,
        actions=(replace(problem.actions[0], settlement_observation_key=key),),
        terminal_state_sets=(replace(problem.terminal_state_sets[0], settlement_observation_key=key),),
    )

    assert "NAIVE_DATETIME" in {issue.code for issue in validate_problem(malformed)}


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda problem: replace(problem, terminal_state_sets=(replace(problem.terminal_state_sets[0], atoms=(replace(problem.terminal_state_sets[0].atoms[0], kind="INVALID"),)),)), "INVALID_TERMINAL_KIND"),
        (lambda problem: replace(problem, constraint_model=ConstraintModel((RelationConstraint("relation-x", "INVALID", ("contract-a", "contract-a"), "v1"),), problem.constraint_model.forbidden_atom_combinations)), "INVALID_RELATION_KIND"),
        (lambda problem: replace(problem, qualification_constraints=(replace(problem.qualification_constraints[0], metric="INVALID"),)), "INVALID_QUALIFICATION_METRIC"),
        (lambda problem: replace(problem, qualification_constraints=(replace(problem.qualification_constraints[0], comparison="INVALID"),)), "INVALID_COMPARISON"),
    ],
)
def test_validate_problem_rejects_direct_invalid_enum_values(mutate: object, code: str) -> None:
    assert code in {issue.code for issue in validate_problem(mutate(sample_problem()))}  # type: ignore[operator]


def test_validate_problem_reports_invalid_direct_graph_nodes_without_raising() -> None:
    problem = sample_problem()
    malformed = replace(
        problem,
        actions=(object(),),
        terminal_state_sets=(object(),),
        constraint_model=ConstraintModel((object(),), (object(),)),
        qualification_constraints=(object(),),
    )

    codes = {issue.code for issue in validate_problem(malformed)}

    assert {"INVALID_ACTION", "INVALID_TERMINAL_STATE_SET", "INVALID_RELATION", "INVALID_FORBIDDEN_ATOM_COMBINATION", "INVALID_QUALIFICATION_CONSTRAINT"} <= codes


def test_validate_problem_reports_invalid_observation_and_payout_nodes_without_raising() -> None:
    problem = sample_problem()
    malformed = replace(
        problem,
        actions=(replace(problem.actions[0], settlement_observation_key=None),),
        terminal_state_sets=(replace(problem.terminal_state_sets[0], settlement_observation_key=None, atoms=(replace(problem.terminal_state_sets[0].atoms[0], payouts=(object(),)),)),),
    )

    codes = {issue.code for issue in validate_problem(malformed)}

    assert {"INVALID_SETTLEMENT_OBSERVATION_KEY", "INVALID_ACTION_PAYOUT"} <= codes


def test_validate_problem_reports_invalid_terminal_atom_node_without_raising() -> None:
    problem = sample_problem()
    malformed = replace(problem, terminal_state_sets=(replace(problem.terminal_state_sets[0], atoms=(object(),)),))

    assert "INVALID_TERMINAL_ATOM" in {issue.code for issue in validate_problem(malformed)}


def test_validate_problem_reports_unhashable_action_id_without_raising() -> None:
    problem = sample_problem()
    malformed = replace(problem, actions=(replace(problem.actions[0], action_id=[]),))

    assert "INVALID_IDENTIFIER" in {issue.code for issue in validate_problem(malformed)}


def test_validate_problem_reports_unhashable_relation_references_without_raising() -> None:
    problem = sample_problem()
    malformed = replace(
        problem,
        constraint_model=ConstraintModel((RelationConstraint("relation-x", RelationKind.IMPLIES, ([], []), "v1"),), ()),
    )

    assert "INVALID_IDENTIFIER" in {issue.code for issue in validate_problem(malformed)}
