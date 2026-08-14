from datetime import UTC, datetime, timedelta
from decimal import Decimal

import open_trader.prediction_executable_cost as executable_cost
import pytest

from open_trader.prediction_executable_cost import (
    ResolutionStatus,
    AccountBalance,
    AccountSnapshot,
    execution_solution_from_payload,
    ImmutableBook,
    VerifiedComponent,
    market_solution_from_payload,
    resolve_component,
)
from open_trader.prediction_arbitrage import BookLevel, ThresholdOrderBook
from open_trader.prediction_n_leg_oracle import evaluate_fixed_portfolio
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
)
from open_trader.prediction_solver import BenchmarkLimits, ObjectiveBounds, SolverEvidence
from open_trader.prediction_solver_verified import (
    CANDIDATE_EVIDENCE_SCHEMA_V1,
    CandidateEvidence,
    model_fingerprint,
    proof_input_from_payload,
)


def test_unknown_status_is_explicit() -> None:
    assert ResolutionStatus.UNKNOWN.value == "UNKNOWN"


AS_OF = datetime(2026, 8, 14, tzinfo=UTC)


def component() -> VerifiedComponent:
    key = SettlementObservationKey(OBSERVATION_SCHEMA_V1, "oracle", "indicator", AS_OF, AS_OF, "UTC", "rules-v1")
    action = CandidateAction(
        "action-a", "venue-a", "account-a", "chain-a", "contract-a", key, ActionSide.BUY_YES,
        1, 1, 1, 1, "usd-cents", "usd-cents", "usd-cents-v1", (ExecutableCostSlice(1, 1, 1),),
    )
    problem = ArbitrageProblem(
        PROBLEM_SCHEMA_V1, "component-a", AS_OF, "usd-cents", (action,),
        (TerminalStateSet("contract-a", key, "rules-v1", (
            TerminalAtom("yes", TerminalKind.NORMAL_YES, "rules-v1", (ActionPayout("action-a", 200),), AS_OF + timedelta(days=1)),
        )),),
        ConstraintModel((), ()),
        (QualificationConstraint("profit", "rules-v1", QualificationMetric.GUARANTEED_PROFIT_UNITS, Comparison.GREATER_THAN_OR_EQUAL, 1, 1),),
    )
    return VerifiedComponent(problem, model_fingerprint(problem), 100)


def test_missing_book_fails_closed_before_solving() -> None:
    result = resolve_component(
        component(), (), None,
        BenchmarkLimits(100, 200, 1_000_000, 4),
        OracleBudget(2, 2, 2),
        now=AS_OF,
    )

    assert result.status is ResolutionStatus.UNKNOWN
    assert result.failure_reason == "BOOK_STATE_UNKNOWN"
    assert result.market_solution is None


def qualified_solver(monkeypatch):
    captured = {"calls": 0}

    def fake_solve(payload, **_kwargs):
        captured["calls"] += 1
        captured["input"] = proof_input_from_payload(payload)
        evaluation = evaluate_fixed_portfolio(
            captured["input"].request.problem,
            (ActionQuantity("action-a", 1),),
            captured["input"].request.budget,
        )
        evidence = CandidateEvidence(
            CANDIDATE_EVIDENCE_SCHEMA_V1,
            captured["input"], "test", "1",
            executable_cost.model_fingerprint(captured["input"].request.problem),
            executable_cost.fingerprint({"quantities": (ActionQuantity("action-a", 1),)}),
            SolverEvidence(
                "FEASIBLE", PortfolioCandidate((ActionQuantity("action-a", 1),), evaluation.guaranteed_profit_units),
                ObjectiveBounds(evaluation.guaranteed_profit_units, 200, 51, False),
                evaluation.worst_scenario, evaluation.payout_lower_bound_units,
                evaluation.cost_upper_bound_units, evaluation.guaranteed_profit_units,
                evaluation.conservative_capital_release_at, True, False, 1, 1,
                (evaluation.worst_state_cut,), None,
            ),
        )
        return executable_cost.canonical_payload(evidence)

    monkeypatch.setattr(executable_cost, "solve", fake_solve)
    return captured


def executable_book() -> ImmutableBook:
    return ImmutableBook(
        "action-a", "action-a",
        ThresholdOrderBook("action-a", (BookLevel(Decimal("0.40"), Decimal("1")),), (), AS_OF),
        fee_ppm=100_000, tick_units=1, haircut_ppm=100_000,
        price_units_per_quote_unit=100,
    )


def test_qualified_fixed_market_plan_becomes_non_order_ready_execution_solution(
    monkeypatch,
) -> None:
    book = executable_book()
    account = AccountSnapshot(
        AS_OF,
        (AccountBalance("venue-a", "account-a", "usd-cents", 1_000, 1_000),),
        1_000,
        0,
        1_000,
    )
    captured = qualified_solver(monkeypatch)
    result = resolve_component(
        component(), (book,), account,
        BenchmarkLimits(100, 200, 1_000_000, 4),
        OracleBudget(2, 2, 2),
        now=AS_OF,
    )

    assert captured["input"].request.problem.actions[0].cost_slices[0].incremental_cost_upper_bound_units == 51
    assert result.status is ResolutionStatus.EXECUTION_SOLUTION
    assert result.market_solution.global_search_closed is False
    assert result.execution_solution.order_ready is False
    assert result.execution_solution.reason == "PARTIAL_FILL_PROOF_REQUIRED"


def test_insufficient_fixed_funding_retains_market_solution_without_second_solve(monkeypatch) -> None:
    captured = qualified_solver(monkeypatch)
    account = AccountSnapshot(
        AS_OF,
        (AccountBalance("venue-a", "account-a", "usd-cents", 50, 50),),
        1_000, 0, 1_000,
    )

    result = resolve_component(
        component(), (executable_book(),), account,
        BenchmarkLimits(100, 200, 1_000_000, 4), OracleBudget(2, 2, 2), now=AS_OF,
    )

    assert result.status is ResolutionStatus.INSUFFICIENT_FUNDS
    assert result.market_solution is not None
    assert result.execution_solution is None
    assert result.market_solution.quantities == (ActionQuantity("action-a", 1),)
    assert captured["calls"] == 1


def test_stale_account_retains_market_solution_as_unknown(monkeypatch) -> None:
    qualified_solver(monkeypatch)
    account = AccountSnapshot(
        AS_OF - timedelta(seconds=61),
        (AccountBalance("venue-a", "account-a", "usd-cents", 1_000, 1_000),),
        1_000, 0, 1_000,
    )

    result = resolve_component(
        component(), (executable_book(),), account,
        BenchmarkLimits(100, 200, 1_000_000, 4), OracleBudget(2, 2, 2), now=AS_OF,
    )

    assert result.status is ResolutionStatus.ACCOUNT_STATE_UNKNOWN
    assert result.market_solution is not None
    assert result.execution_solution is None


def test_market_solution_decoder_recomputes_its_fingerprint(monkeypatch) -> None:
    qualified_solver(monkeypatch)
    account = AccountSnapshot(
        AS_OF,
        (AccountBalance("venue-a", "account-a", "usd-cents", 1_000, 1_000),),
        1_000, 0, 1_000,
    )
    result = resolve_component(
        component(), (executable_book(),), account,
        BenchmarkLimits(100, 200, 1_000_000, 4), OracleBudget(2, 2, 2), now=AS_OF,
    )
    payload = executable_cost.canonical_payload(result.market_solution)

    assert executable_cost.canonical_payload(market_solution_from_payload(payload)) == payload
    payload["fingerprint"] = "sha256:" + "0" * 64
    with pytest.raises(ValueError, match="fingerprint"):
        market_solution_from_payload(payload)


def test_execution_solution_decoder_recomputes_its_fingerprint(monkeypatch) -> None:
    qualified_solver(monkeypatch)
    account = AccountSnapshot(
        AS_OF,
        (AccountBalance("venue-a", "account-a", "usd-cents", 1_000, 1_000),),
        1_000, 0, 1_000,
    )
    result = resolve_component(
        component(), (executable_book(),), account,
        BenchmarkLimits(100, 200, 1_000_000, 4), OracleBudget(2, 2, 2), now=AS_OF,
    )
    payload = executable_cost.canonical_payload(result.execution_solution)

    assert executable_cost.canonical_payload(execution_solution_from_payload(payload)) == payload
    payload["order_ready"] = True
    with pytest.raises(ValueError, match="partial-fill"):
        execution_solution_from_payload(payload)
