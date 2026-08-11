from __future__ import annotations

from datetime import UTC, datetime

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
            relations=(RelationConstraint("relation-1", RelationKind.IMPLIES, ("contract-a",), "v1"),),
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
