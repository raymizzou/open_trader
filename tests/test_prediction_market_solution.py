"""Issue #84 regression matrix for MarketSolution / ExecutionSolution.

Covers the locked decisions: book -> cost slices (asks/bids, depth
truncation), structure-fingerprint dedup, qualified verification ->
MarketSolution, non-qualified -> none, ExecutionSolution depth + capital
checks, over-cap non-executable reasons, exact component-level negative-proof
matching, and solver timeout -> UNKNOWN.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from open_trader.prediction_arbitrage import BookLevel
from open_trader.prediction_n_leg import (
    OBSERVATION_SCHEMA_V1,
    PAYOUT_PROOF_SCHEMA_V1,
    PROBLEM_SCHEMA_V1,
    REQUEST_SCHEMA_V1,
    ActionPayout,
    ActionQuantity,
    ActionSide,
    ArbitrageProblem,
    CandidateAction,
    ConstraintModel,
    ExecutableCostSlice,
    OracleBudget,
    OracleRequest,
    PayoutProof,
    PortfolioCandidate,
    ProofResultKind,
    RelationConstraint,
    RelationKind,
    SearchMode,
    SettlementObservationKey,
    TerminalAtom,
    TerminalKind,
    TerminalStateSet,
    canonical_payload,
    fingerprint,
)
from open_trader.prediction_n_leg_oracle import (
    build_portfolio_solution,
    derive_selected_support_graph,
    evaluate_fixed_portfolio,
)
from open_trader.prediction_snapshot_scheduler import ComponentSnapshot, LegBook, SnapshotLeg
from open_trader.prediction_solver import BenchmarkLimits, ObjectiveBounds, SolverEvidence
from open_trader.prediction_solver_verified import (
    CANDIDATE_EVIDENCE_SCHEMA_V1,
    PROOF_REQUEST_SCHEMA_V1,
    VERIFICATION_RESULT_SCHEMA_V1,
    CandidateEvidence,
    ProofInput,
    ProofLevel,
    VerificationResult,
    VerificationStatus,
    model_fingerprint,
    quote_fingerprint,
)

import open_trader.prediction_market_solution as pms


AS_OF = datetime(2026, 8, 1, tzinfo=UTC)
BUDGET = OracleBudget(max_quantity_vectors=9, max_joint_states=2, max_support_rechecks=1)
LIMITS = BenchmarkLimits(
    soft_time_limit_ms=1_000,
    hard_time_limit_ms=2_000,
    memory_limit_bytes=1 << 30,
    max_constraint_generation_rounds=3,
)


def observation(suffix: str = "a") -> SettlementObservationKey:
    return SettlementObservationKey(
        OBSERVATION_SCHEMA_V1,
        f"oracle-{suffix}",
        f"indicator-{suffix}",
        AS_OF,
        AS_OF.replace(hour=AS_OF.hour + 1),
        "UTC",
        "v1",
    )


def action(
    action_id: str,
    contract_id: str,
    side: ActionSide,
    cost_slices: tuple[ExecutableCostSlice, ...],
    *,
    min_quantity_lots: int = 1,
) -> CandidateAction:
    return CandidateAction(
        action_id=action_id,
        venue_id="test-venue",
        account_id="test-account",
        chain_id="test-chain",
        market_contract_id=contract_id,
        settlement_observation_key=observation(contract_id),
        side=side,
        lot_step_units=100,
        quantity_scale=100,
        min_quantity_lots=min_quantity_lots,
        max_quantity_lots=cost_slices[-1].last_lot,
        settlement_asset_id="usd-cents",
        valuation_unit_id="usd-cents",
        asset_valuation_rule_id="usd-cents-v1",
        cost_slices=cost_slices,
    )


def yes_no_state(contract_id: str, yes_payouts, no_payouts) -> TerminalStateSet:
    return TerminalStateSet(
        contract_id,
        observation(contract_id),
        "v1",
        (
            TerminalAtom(
                "yes",
                TerminalKind.NORMAL_YES,
                "v1",
                tuple(ActionPayout(action_id, amount) for action_id, amount in yes_payouts),
                AS_OF,
            ),
            TerminalAtom(
                "no",
                TerminalKind.NORMAL_NO,
                "v1",
                tuple(ActionPayout(action_id, amount) for action_id, amount in no_payouts),
                AS_OF,
            ),
        ),
    )


def problem(
    actions: tuple[CandidateAction, ...],
    states: tuple[TerminalStateSet, ...],
    relations: tuple[RelationConstraint, ...] = (),
) -> ArbitrageProblem:
    return ArbitrageProblem(
        PROBLEM_SCHEMA_V1,
        "issue-84-test",
        AS_OF,
        "usd-cents",
        actions,
        states,
        ConstraintModel(relations, ()),
        (),
    )


def book_level(price: str, size: str) -> BookLevel:
    return BookLevel(Decimal(price), Decimal(size))


@pytest.fixture
def book_cls():
    class _Book:
        def __init__(self, asks=(), bids=()):
            self.asks = asks
            self.bids = bids

    return _Book


def qualified_problem(*, yes_cost: int = 60, no_cost: int = 30) -> ArbitrageProblem:
    yes = action("a-yes", "a", ActionSide.BUY_YES, (ExecutableCostSlice(1, 1, yes_cost),))
    no = action("a-no", "a", ActionSide.BUY_NO, (ExecutableCostSlice(1, 1, no_cost),))
    state = yes_no_state(
        "a",
        (("a-yes", 100), ("a-no", 0)),
        (("a-yes", 0), ("a-no", 100)),
    )
    return problem((yes, no), (state,))


def quantities() -> tuple[ActionQuantity, ...]:
    return (ActionQuantity("a-no", 1), ActionQuantity("a-yes", 1))


def verified_solution(problem_: ArbitrageProblem) -> object:
    evaluation = evaluate_fixed_portfolio(problem_, quantities(), BUDGET)
    support = derive_selected_support_graph(problem_, evaluation, BUDGET)
    return build_portfolio_solution(problem_, evaluation, support)


def evidence_with(
    problem_: ArbitrageProblem,
    selected: tuple[ActionQuantity, ...],
    *,
    generation: int = 0,
    code_version: str = "issue-84",
    global_search_closed: bool = False,
) -> CandidateEvidence:
    proof_input = ProofInput(
        PROOF_REQUEST_SCHEMA_V1,
        OracleRequest(REQUEST_SCHEMA_V1, SearchMode.ADMISSION, problem_, BUDGET),
        LIMITS,
        quote_fingerprint(problem_),
        generation,
        code_version,
    )
    solver_evidence = SolverEvidence(
        native_status="FEASIBLE",
        candidate=PortfolioCandidate(selected, 0),
        objective_bounds=ObjectiveBounds(None, None, None, False),
        worst_scenario=None,
        payout_lower_bound_units=None,
        cost_upper_bound_units=None,
        guaranteed_profit_units=None,
        conservative_capital_release_at=None,
        fixed_portfolio_closed=True,
        global_search_closed=global_search_closed,
        master_rounds=0,
        adversary_rounds=0,
        cuts=(),
        certificate=None,
    )
    return CandidateEvidence(
        CANDIDATE_EVIDENCE_SCHEMA_V1,
        proof_input,
        "test",
        "1.0",
        model_fingerprint(problem_),
        fingerprint({"quantities": selected}),
        solver_evidence,
    )


def qualified_verification(problem_: ArbitrageProblem, solution: object) -> VerificationResult:
    return VerificationResult(
        VERIFICATION_RESULT_SCHEMA_V1,
        VerificationStatus.QUALIFIED_VERIFIED,
        ProofLevel.SOLVER_VERIFIED,
        model_fingerprint(problem_),
        fingerprint({"quantities": solution.quantities}),
        quote_fingerprint(problem_),
        0,
        solution,
        None,
        None,
    )


def market(problem_: ArbitrageProblem) -> pms.MarketSolution:
    solution = verified_solution(problem_)
    evidence = evidence_with(problem_, solution.quantities)
    verification = qualified_verification(problem_, solution)
    resolution = pms.resolution_from_verification(
        "c1", problem_, evidence, verification, code_version="issue-84"
    )
    assert resolution.market_solution is not None
    return resolution.market_solution


def qualification_fingerprint(problem_: ArbitrageProblem) -> str:
    constraints = tuple(
        sorted(problem_.qualification_constraints, key=lambda item: item.constraint_id)
    )
    return fingerprint(
        {"schema_version": PROBLEM_SCHEMA_V1, "qualification_constraints": constraints}
    )


def test_cost_slices_from_book_asks_bids_and_depth_truncation(book_cls) -> None:
    yes = action("a-yes", "a", ActionSide.BUY_YES, (ExecutableCostSlice(1, 5, 60),))
    no = action("a-no", "a", ActionSide.BUY_NO, (ExecutableCostSlice(1, 5, 30),))

    assert pms.cost_slices_from_book(
        yes,
        book_cls(
            asks=(
                book_level("0.6", "1"),
                book_level("0.6", "1"),
                book_level("0.7", "2"),
            )
        ),
        price_units_per_quote_unit=100,
    ) == (
        ExecutableCostSlice(1, 2, 60),
        ExecutableCostSlice(3, 4, 70),
    )
    assert pms.cost_slices_from_book(
        yes,
        book_cls(asks=(book_level("0.6", "2"), book_level("0.7", "3"))),
        price_units_per_quote_unit=100,
    ) == (
        ExecutableCostSlice(1, 2, 60),
        ExecutableCostSlice(3, 5, 70),
    )
    assert pms.cost_slices_from_book(
        no,
        book_cls(bids=(book_level("0.3", "2"), book_level("0.2", "1"))),
        price_units_per_quote_unit=100,
    ) == (
        ExecutableCostSlice(1, 2, 30),
        ExecutableCostSlice(3, 3, 20),
    )
    assert pms.cost_slices_from_book(yes, book_cls()) == ()


def test_structure_fingerprint_ignores_prices_but_binds_structure() -> None:
    base = qualified_problem(yes_cost=60, no_cost=30)
    repriced = qualified_problem(yes_cost=55, no_cost=25)
    assert pms.structure_fingerprint(base, quantities()) == pms.structure_fingerprint(
        repriced, quantities()
    )

    changed_quantities = (ActionQuantity("a-no", 0), ActionQuantity("a-yes", 1))
    assert pms.structure_fingerprint(base, quantities()) != pms.structure_fingerprint(
        base, changed_quantities
    )

    key = observation("a")
    relation = RelationConstraint("r1", RelationKind.MUTUALLY_EXCLUSIVE, ("a", "b"), "v1")
    related = problem(base.actions, base.terminal_state_sets, (relation,))
    assert pms.structure_fingerprint(base, quantities()) != pms.structure_fingerprint(
        related, quantities()
    )


def test_qualified_verification_produces_market_solution() -> None:
    problem_ = qualified_problem()
    solution = verified_solution(problem_)
    evidence = evidence_with(problem_, solution.quantities, global_search_closed=False)
    verification = qualified_verification(problem_, solution)

    resolution = pms.resolution_from_verification(
        "c1", problem_, evidence, verification, code_version="issue-84"
    )
    assert resolution.status == VerificationStatus.QUALIFIED_VERIFIED
    assert resolution.market_solution is not None
    market_solution = resolution.market_solution
    assert market_solution.component_id == "c1"
    assert market_solution.quantities == solution.quantities
    assert market_solution.guaranteed_profit_units == 10
    assert market_solution.bounded_cost_units == 90
    assert market_solution.bounded_payout_units == 100
    assert market_solution.global_search_closed is False
    assert market_solution.structure_fingerprint == pms.structure_fingerprint(
        problem_, solution.quantities
    )
    assert market_solution.quote_fingerprint == quote_fingerprint(problem_)
    assert market_solution.verification_fingerprint == fingerprint(
        canonical_payload(solution.payout_proof)
    )


def test_non_qualified_verification_produces_no_market_solution() -> None:
    problem_ = qualified_problem()
    solution = verified_solution(problem_)
    evidence = evidence_with(problem_, solution.quantities)

    not_qualified = replace(
        qualified_verification(problem_, solution),
        status=VerificationStatus.NOT_QUALIFIED,
        proof_level=ProofLevel.NONE,
    )
    resolution = pms.resolution_from_verification(
        "c1", problem_, evidence, not_qualified, code_version="issue-84"
    )
    assert resolution.status == VerificationStatus.NOT_QUALIFIED
    assert resolution.market_solution is None

    unknown = VerificationResult(
        VERIFICATION_RESULT_SCHEMA_V1,
        VerificationStatus.UNKNOWN,
        ProofLevel.NONE,
        model_fingerprint(problem_),
        None,
        quote_fingerprint(problem_),
        0,
        None,
        None,
        "ORACLE_DECISION_LIMIT_EXCEEDED",
    )
    resolution = pms.resolution_from_verification(
        "c1", problem_, evidence, unknown, code_version="issue-84"
    )
    assert resolution.status == VerificationStatus.UNKNOWN
    assert resolution.market_solution is None


def test_execution_solution_respects_depth_and_capital() -> None:
    problem_ = qualified_problem()
    market_solution = market(problem_)
    account = pms.AccountView(available_units=100, allowance_units=100, unsettled_capital_units=0)

    execution = pms.execution_solution_from_market(
        market_solution, problem_, account, max_total_unsettled_capital=100
    )
    assert execution.reason == pms.EXECUTABLE_REASON
    assert execution.capital_use_units == 90
    assert execution.quantities == market_solution.quantities
    assert execution.order_ready is False
    assert execution.partial_fill_proof == "UNKNOWN"
    assert execution.market_solution_fingerprint == fingerprint(market_solution)


def test_execution_solution_over_capital_has_non_executable_reason() -> None:
    problem_ = qualified_problem()
    market_solution = market(problem_)

    underfunded = pms.AccountView(available_units=50, allowance_units=50, unsettled_capital_units=0)
    execution = pms.execution_solution_from_market(
        market_solution, problem_, underfunded, max_total_unsettled_capital=100
    )
    assert execution.reason == pms.INSUFFICIENT_FUNDS_REASON
    assert execution.order_ready is False

    capped = pms.AccountView(available_units=100, allowance_units=100, unsettled_capital_units=60)
    execution = pms.execution_solution_from_market(
        market_solution, problem_, capped, max_total_unsettled_capital=100
    )
    assert execution.reason == pms.UNSETTLED_CAP_EXCEEDED_REASON

    over_depth = replace(market_solution, quantities=(ActionQuantity("a-yes", 2),))
    execution = pms.execution_solution_from_market(
        over_depth, problem_, pms.AccountView(100, 100, 0), max_total_unsettled_capital=100
    )
    assert execution.reason == pms.INSUFFICIENT_DEPTH_REASON

    assert market_solution.guaranteed_profit_units == 10


def test_negative_proof_exact_match_and_mismatch() -> None:
    problem_ = qualified_problem()
    proof = PayoutProof(
        schema_version=PAYOUT_PROOF_SCHEMA_V1,
        result_kind=ProofResultKind.NO_QUALIFIED_OPPORTUNITY,
        problem_fingerprint=fingerprint(problem_),
        portfolio_fingerprint=None,
        worst_scenario=None,
        worst_state_cut=None,
        payout_lower_bound_units=None,
        cost_upper_bound_units=None,
        guaranteed_profit_units=None,
        conservative_capital_release_at=None,
        selected_support_graph=None,
        proof_method="EXHAUSTIVE_ORACLE_V1",
        request_fingerprint=fingerprint(
            OracleRequest(REQUEST_SCHEMA_V1, SearchMode.ADMISSION, problem_, BUDGET)
        ),
        source_problem_fingerprint=None,
        qualification_fingerprint=qualification_fingerprint(problem_),
        quantity_vectors_total=9,
        quantity_vectors_examined=9,
        joint_states_per_vector=2,
        rejection_counts=(),
    )
    evidence = evidence_with(problem_, ())
    verification = VerificationResult(
        VERIFICATION_RESULT_SCHEMA_V1,
        VerificationStatus.NO_QUALIFIED_OPPORTUNITY,
        ProofLevel.NONE,
        model_fingerprint(problem_),
        None,
        quote_fingerprint(problem_),
        0,
        None,
        proof,
        None,
    )

    resolution = pms.resolution_from_verification(
        "c1", problem_, evidence, verification, code_version="issue-84"
    )
    assert resolution.status == VerificationStatus.NO_QUALIFIED_OPPORTUNITY
    assert resolution.market_solution is None

    repriced = qualified_problem(yes_cost=55, no_cost=25)
    tampered_quote = replace(verification, quote_fingerprint=quote_fingerprint(repriced))
    resolution = pms.resolution_from_verification(
        "c1", problem_, evidence, tampered_quote, code_version="issue-84"
    )
    assert resolution.status == VerificationStatus.UNKNOWN
    assert resolution.reason == "NEGATIVE_PROOF_MISMATCH"

    tampered_generation = replace(verification, current_generation=1)
    resolution = pms.resolution_from_verification(
        "c1", problem_, evidence, tampered_generation, code_version="issue-84"
    )
    assert resolution.status == VerificationStatus.UNKNOWN
    assert resolution.reason == "NEGATIVE_PROOF_MISMATCH"


def test_solver_timeout_maps_to_unknown(monkeypatch) -> None:
    problem_ = qualified_problem()

    def boom(*args, **kwargs):
        raise TimeoutError("solver timed out")

    monkeypatch.setattr(pms, "solve", boom)
    resolution = pms.resolve_market_solution(
        "c1", problem_, budget=BUDGET, limits=LIMITS, code_version="issue-84"
    )
    assert resolution.status == VerificationStatus.UNKNOWN
    assert resolution.market_solution is None


def test_unchanged_structure_reuses_fixed_portfolio_without_new_solve(monkeypatch) -> None:
    prior = market(qualified_problem(yes_cost=60, no_cost=30))
    repriced = qualified_problem(yes_cost=55, no_cost=25)

    def fail_solve(*args, **kwargs):
        raise AssertionError("solve must not run on unchanged structure")

    monkeypatch.setattr(pms, "solve", fail_solve)
    resolution = pms.resolve_market_solution(
        "c1",
        repriced,
        budget=BUDGET,
        limits=LIMITS,
        code_version="issue-84",
        prior=prior,
    )
    assert resolution.status == VerificationStatus.QUALIFIED_VERIFIED
    assert resolution.market_solution is not None
    assert resolution.market_solution.guaranteed_profit_units == 20
    assert resolution.market_solution.bounded_cost_units == 80


def test_changed_structure_runs_new_solve(monkeypatch) -> None:
    prior = market(qualified_problem(yes_cost=60, no_cost=30))
    changed = qualified_problem(yes_cost=60, no_cost=30)
    changed = replace(
        changed,
        actions=(
            action(
                "a-yes",
                "a",
                ActionSide.BUY_YES,
                (ExecutableCostSlice(1, 2, 60),),
            ),
            changed.actions[1],
        ),
    )
    captured: dict[str, bool] = {}

    def fake_solve(payload, **kwargs):
        captured["called"] = True
        return canonical_payload(
            evidence_with(changed, (ActionQuantity("a-no", 1), ActionQuantity("a-yes", 2)))
        )

    monkeypatch.setattr(pms, "solve", fake_solve)
    pms.resolve_market_solution(
        "c1",
        changed,
        budget=BUDGET,
        limits=LIMITS,
        code_version="issue-84",
        prior=prior,
    )
    assert captured.get("called") is True


def test_build_solve_request_rebuilds_cost_slices_from_snapshot(book_cls) -> None:
    problem_ = qualified_problem()
    snapshot = ComponentSnapshot(
        "c1",
        (
            SnapshotLeg(
                "a-yes",
                LegBook(bids=(), asks=(book_level("0.6", "1"),), taker_fee_bps=Decimal("0"), available=True),
                None,
                None,
                None,
            ),
            SnapshotLeg(
                "a-no",
                LegBook(bids=(book_level("0.3", "1"),), asks=(), taker_fee_bps=Decimal("0"), available=True),
                None,
                None,
                None,
            ),
        ),
    )
    request = pms.build_solve_request(
        problem_, snapshot, budget=BUDGET, limits=LIMITS, price_units_per_quote_unit=100
    )
    assert request.backend == "cp_sat"
    assert request.request.schema_version == REQUEST_SCHEMA_V1
    assert request.request.problem.actions[0].cost_slices == (ExecutableCostSlice(1, 1, 60),)
    assert request.request.problem.actions[1].cost_slices == (ExecutableCostSlice(1, 1, 30),)
