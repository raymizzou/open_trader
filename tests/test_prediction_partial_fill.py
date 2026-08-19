"""Issue #74: fill-adversary proof — fixtures, differential and timing tests.

The production proof never enumerates the prod(q_i + 1) fill vectors and
never samples; the exhaustive Oracle only runs here, inside the budget, to
(1) block false-safe SAFE proofs and (2) reproduce UNSAFE counterexamples.
"""

from __future__ import annotations

import json
import statistics
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import pytest

from open_trader.prediction_n_leg import (
    ActionQuantity,
    ActionPayout,
    ActionSide,
    ArbitrageProblem,
    CandidateAction,
    Comparison,
    ConstraintModel,
    ExecutableCostSlice,
    ForbiddenAtomCombination,
    OracleBudget,
    QualificationConstraint,
    QualificationMetric,
    RelationConstraint,
    RelationKind,
    SettlementObservationKey,
    SettlementScenario,
    SelectedAtom,
    TerminalAtom,
    TerminalKind,
    TerminalStateSet,
    problem_from_payload,
    validate_problem,
)
from open_trader.prediction_market_solution import ExecutionSolution
from open_trader.prediction_n_leg_oracle import evaluate_fill_adversary
from open_trader.prediction_partial_fill import (
    PARTIAL_FILL_PROOF_SCHEMA_V1,
    PARTIAL_FILL_SAFE,
    PARTIAL_FILL_UNKNOWN,
    PARTIAL_FILL_UNSAFE,
    TERMINATION_CLOSED,
    VERIFIER_NOT_APPLICABLE,
    VERIFIER_QUALIFIED,
    DEFAULT_ORDER_TYPES_V1,
    FILL_ADVERSARY_SCHEMA_V1,
    ORDER_SEMANTICS_SCHEMA_V1,
    FillAdversaryOracleResult,
    FillSemantics,
    ORDER_SEMANTICS_TABLE_V1,
    counterexample_loss_units,
    fill_adversary_problem_from_market_solution,
    fill_adversary_problem_from_payload,
    order_semantics_fingerprint_for,
    order_semantics_lookup,
    prove_partial_fill,
    solve_fill_adversary,
    verify_fill_adversary,
)
from open_trader.prediction_solver import (
    BackendResult,
    BenchmarkLimits,
    NativeSolveStatus,
)
from open_trader.prediction_solver_verified import fingerprint

AS_OF = datetime(2026, 8, 16, 2, 0, tzinfo=UTC)
RELEASE_AT = datetime(2026, 8, 16, 6, 0, tzinfo=UTC)

FIXTURE = Path(__file__).parent / "fixtures" / "prediction_n_leg_validation_frozen_n3.json"

_PAYOUT_YES = 1_000_000


def _observation() -> SettlementObservationKey:
    return SettlementObservationKey(
        schema_version="open_trader.prediction_n_leg.observation.v1",
        oracle_id="oracle-a",
        indicator_id="indicator-a",
        observation_start=AS_OF,
        observation_end=AS_OF,
        timezone="UTC",
        rule_version="v1",
    )


def _action(
    action_id: str,
    contract_id: str,
    venue_id: str,
    quantity_lots: int,
    unit_cost_units: int,
    *,
    lot_step_units: int = 1,
) -> CandidateAction:
    return CandidateAction(
        action_id=action_id,
        venue_id=venue_id,
        account_id="test-account",
        chain_id="test-chain",
        market_contract_id=contract_id,
        settlement_observation_key=_observation(),
        side=ActionSide.BUY_YES,
        lot_step_units=lot_step_units,
        quantity_scale=1,
        min_quantity_lots=1,
        max_quantity_lots=quantity_lots,
        settlement_asset_id="usd-cents",
        valuation_unit_id="usd-cents",
        asset_valuation_rule_id="usd-cents-v1",
        cost_slices=(
            ExecutableCostSlice(1, quantity_lots, unit_cost_units),
        ),
    )


def make_problem(
    actions: tuple[CandidateAction, ...],
    *,
    exactly_one: bool = False,
    contract_ids: tuple[str, ...] = (),
) -> ArbitrageProblem:
    contracts = sorted({action.market_contract_id for action in actions})
    state_sets = tuple(
        TerminalStateSet(
            market_contract_id=contract_id,
            settlement_observation_key=_observation(),
            rule_version="v1",
            atoms=(
                TerminalAtom(
                    atom_id=f"{contract_id}:yes",
                    kind=TerminalKind.NORMAL_YES,
                    rule_version="v1",
                    payouts=tuple(
                        ActionPayout(
                            action.action_id,
                            _PAYOUT_YES if action.market_contract_id == contract_id else 0,
                        )
                        for action in actions
                        if action.market_contract_id == contract_id
                    ),
                    capital_release_at=RELEASE_AT,
                ),
                TerminalAtom(
                    atom_id=f"{contract_id}:no",
                    kind=TerminalKind.NORMAL_NO,
                    rule_version="v1",
                    payouts=tuple(
                        ActionPayout(action.action_id, 0)
                        for action in actions
                        if action.market_contract_id == contract_id
                    ),
                    capital_release_at=RELEASE_AT,
                ),
            ),
        )
        for contract_id in contracts
    )
    relations = (
        (
            RelationConstraint(
                "exactly-one",
                RelationKind.EXACTLY_ONE,
                contract_ids or tuple(contracts),
                "v1",
            ),
        )
        if exactly_one and len(contracts) >= 2
        else ()
    )
    problem = ArbitrageProblem(
        schema_version="open_trader.prediction_n_leg.problem.v1",
        problem_id="problem-74-test",
        as_of=AS_OF,
        valuation_unit_id="usd-cents",
        actions=actions,
        terminal_state_sets=state_sets,
        constraint_model=ConstraintModel(relations, ()),
        qualification_constraints=(),
    )
    issues = validate_problem(problem)
    assert not issues, [issue.code for issue in issues]
    return problem


def make_execution(
    problem: ArbitrageProblem,
    quantities: tuple[ActionQuantity, ...],
) -> ExecutionSolution:
    return ExecutionSolution(
        market_solution_fingerprint=fingerprint({"market": "74-test"}),
        quantities=quantities,
        capital_use_units=0,
        reason="PARTIAL_FILL_PROOF_REQUIRED",
        order_ready=False,
        partial_fill_proof="UNKNOWN",
    )


def make_adversary(
    problem: ArbitrageProblem,
    execution: ExecutionSolution,
    *,
    cap: int,
) -> object:
    return fill_adversary_problem_from_market_solution(
        execution,
        problem,
        cap_config_version="caps-v1",
        max_partial_fill_loss=cap,
        max_auto_repair_loss=cap,
    )


def budget() -> OracleBudget:
    return OracleBudget(1_000_000, 1_000_000, 1_000_000)


# ---------------------------------------------------------------- semantics


def test_order_semantics_table_v1_contract() -> None:
    assert ORDER_SEMANTICS_SCHEMA_V1 == "open_trader.prediction_partial_fill.order_semantics.v1"
    assert ORDER_SEMANTICS_TABLE_V1 == (
        ("polymarket", "FOK", FillSemantics.ATOMIC),
        ("predict.fun", "LIMIT", FillSemantics.PARTIAL),
    )
    assert DEFAULT_ORDER_TYPES_V1 == (
        ("polymarket", "FOK"),
        ("predict.fun", "LIMIT"),
    )
    assert order_semantics_lookup("polymarket", "FOK") == FillSemantics.ATOMIC
    assert order_semantics_lookup("predict.fun", "LIMIT") == FillSemantics.PARTIAL
    assert order_semantics_lookup("polymarket", "LIMIT") == FillSemantics.UNKNOWN
    assert order_semantics_lookup("some-exchange", "FOK") == FillSemantics.UNKNOWN
    assert order_semantics_lookup("polymarket", "MARKET") == FillSemantics.UNKNOWN


def test_order_semantics_fingerprint_tracks_table_version() -> None:
    base = order_semantics_fingerprint_for(
        (("a", "polymarket", "FOK", "ATOMIC"),)
    )
    other = order_semantics_fingerprint_for(
        (("a", "polymarket", "FOK", "ATOMIC"), ("b", "predict.fun", "LIMIT", "PARTIAL"))
    )
    assert base != other
    assert order_semantics_fingerprint_for(()) != base


# ----------------------------------------------------------------- fixtures


def test_single_fok_atomic_leg_fills_zero_or_full() -> None:
    problem = make_problem(
        (_action("buy-yes-a", "a", "polymarket", 1, 500_000),)
    )
    execution = make_execution(problem, (ActionQuantity("buy-yes-a", 1),))
    adversary = make_adversary(problem, execution, cap=500_000)
    (leg,) = adversary.legs
    assert leg.semantics == "ATOMIC"
    assert leg.max_cost_units == 500_000 and leg.max_fee_units == 0
    # Worst: full fill, atom "a:no" pays nothing -> loss equals cost.
    record, counterexample = prove_partial_fill(adversary, time_limit_ms=30_000)
    assert record.status == PARTIAL_FILL_SAFE  # worst == cap boundary
    assert record.solver_upper_bound == 500_000
    assert record.solver_termination == TERMINATION_CLOSED
    assert record.verifier_status == VERIFIER_QUALIFIED
    assert counterexample is None
    # One lot below the cap the same proof is UNSAFE with the same worst loss.
    adversary_unsafe = make_adversary(problem, execution, cap=499_999)
    record, counterexample = prove_partial_fill(adversary_unsafe, time_limit_ms=30_000)
    assert record.status == PARTIAL_FILL_UNSAFE
    assert record.solver_upper_bound == 500_000
    assert counterexample is not None
    assert counterexample["loss_units"] == 500_000
    assert counterexample["fill_quantities"] == [
        {"action_id": "buy-yes-a", "quantity_lots": 1}
    ]
    assert [atom["atom_id"] for atom in counterexample["scenario"]] == ["a:no"]


def test_partial_leg_fills_in_lot_steps() -> None:
    problem = make_problem(
        (_action("buy-yes-a", "a", "predict.fun", 4, 100_000),),
    )
    execution = make_execution(problem, (ActionQuantity("buy-yes-a", 4),))
    adversary = make_adversary(problem, execution, cap=400_000)
    (leg,) = adversary.legs
    assert leg.semantics == "PARTIAL"
    assert leg.quantity_lots == 4
    record, counterexample = prove_partial_fill(adversary, time_limit_ms=30_000)
    assert record.status == PARTIAL_FILL_SAFE
    assert record.solver_upper_bound == 400_000  # all four lots, atom "a:no"
    # lot_step_units is a units-per-lot conversion factor (size *
    # quantity_scale / lot_step_units -> lots); the fill domain in lot space
    # steps by one lot, so the factor neither restricts the domain nor
    # requires divisibility.
    problem_lot2 = make_problem(
        (_action("buy-yes-a", "a", "predict.fun", 4, 100_000, lot_step_units=2),),
    )
    execution_lot2 = make_execution(problem_lot2, (ActionQuantity("buy-yes-a", 4),))
    adversary_lot2 = make_adversary(problem_lot2, execution_lot2, cap=400_000)
    (leg2,) = adversary_lot2.legs
    assert leg2.semantics == "PARTIAL" and leg2.quantity_lots == 4
    oracle = evaluate_fill_adversary(adversary_lot2, budget())
    assert oracle.closed is True and oracle.worst_loss_units == 400_000
    assert oracle.worst_fill_quantities == (ActionQuantity("buy-yes-a", 4),)
    record_lot2, _ = prove_partial_fill(adversary_lot2, time_limit_ms=30_000)
    assert record_lot2.solver_upper_bound == 400_000
    # A factor that does not divide the quantity is still only a conversion
    # factor: the problem compiles (no fail-closed divisibility error) and
    # the domain stays 0..4.
    problem_lot3 = make_problem(
        (_action("buy-yes-a", "a", "predict.fun", 4, 100_000, lot_step_units=3),),
    )
    execution_lot3 = make_execution(problem_lot3, (ActionQuantity("buy-yes-a", 4),))
    adversary_lot3 = make_adversary(problem_lot3, execution_lot3, cap=400_000)
    oracle_lot3 = evaluate_fill_adversary(adversary_lot3, budget())
    assert oracle_lot3.closed is True and oracle_lot3.worst_loss_units == 400_000
    assert oracle_lot3.worst_fill_quantities == (
        ActionQuantity("buy-yes-a", 4),
    )
    record_lot3, _ = prove_partial_fill(adversary_lot3, time_limit_ms=30_000)
    assert record_lot3.solver_upper_bound == 400_000


def test_zero_fill_vector_loss_is_zero() -> None:
    problem = make_problem(
        (
            _action("buy-yes-a", "a", "polymarket", 1, 500_000),
            _action("buy-yes-b", "b", "polymarket", 1, 500_000),
        ),
        exactly_one=True,
    )
    adversary = make_adversary(
        problem,
        make_execution(
            problem,
            (ActionQuantity("buy-yes-a", 1), ActionQuantity("buy-yes-b", 1)),
        ),
        cap=0,
    )
    zero = tuple(
        ActionQuantity(leg.action_id, 0) for leg in adversary.legs
    )
    scenario = SettlementScenario(
        tuple(
            SelectedAtom(contract, f"{contract}:no")
            for contract in ("a", "b")
        )
    )
    assert counterexample_loss_units(adversary, zero, scenario) == 0
    # The all-zero fill is feasible for the adversary (objective floor is 0);
    # the closed solver bounds both equal the optimum, never below 0.
    record, counterexample = prove_partial_fill(adversary, time_limit_ms=30_000)
    assert record.solver_lower_bound == 500_000
    assert record.solver_lower_bound == record.solver_upper_bound
    assert record.solver_lower_bound >= 0


def test_cross_leg_independence_no_joint_atomicity() -> None:
    # Two FOK atomic legs; if they were forced to fill jointly the worst loss
    # would collapse to 0, so the true optimum only appears with independence.
    problem = make_problem(
        (
            _action("buy-yes-a", "a", "polymarket", 1, 500_000),
            _action("buy-yes-b", "b", "polymarket", 1, 500_000),
        ),
        exactly_one=True,
    )
    execution = make_execution(
        problem,
        (ActionQuantity("buy-yes-a", 1), ActionQuantity("buy-yes-b", 1)),
    )
    adversary = make_adversary(problem, execution, cap=500_000)
    record, counterexample = prove_partial_fill(adversary, time_limit_ms=30_000)
    assert record.status == PARTIAL_FILL_SAFE
    assert record.solver_upper_bound == 500_000  # one leg filled, other not
    # Oracle agrees exactly (differential, no false-safe).
    oracle = evaluate_fill_adversary(adversary, budget())
    assert oracle.closed is True and oracle.worst_loss_units == 500_000


def test_multi_leg_mixed_atomic_partial() -> None:
    # predict.fun LIMIT partial leg (q=4, unit 300k) + polymarket FOK atomic
    # leg (q=1, 500k); worst = 4 lots of the partial leg, atomic unfilled,
    # scenario a:no/b:yes (exactly one YES).
    problem = make_problem(
        (
            _action("buy-yes-a", "a", "predict.fun", 4, 300_000),
            _action("buy-yes-b", "b", "polymarket", 1, 500_000),
        ),
        exactly_one=True,
    )
    execution = make_execution(
        problem,
        (ActionQuantity("buy-yes-a", 4), ActionQuantity("buy-yes-b", 1)),
    )
    adversary = make_adversary(problem, execution, cap=1_200_000)
    record, counterexample = prove_partial_fill(adversary, time_limit_ms=30_000)
    assert record.status == PARTIAL_FILL_SAFE  # worst == cap boundary
    assert record.solver_upper_bound == 1_200_000
    oracle = evaluate_fill_adversary(adversary, budget())
    assert oracle.closed is True and oracle.worst_loss_units == 1_200_000
    # Below the cap: UNSAFE and the counterexample reproduces exactly.
    adversary_unsafe = make_adversary(problem, execution, cap=1_199_999)
    record, counterexample = prove_partial_fill(
        adversary_unsafe, time_limit_ms=30_000
    )
    assert record.status == PARTIAL_FILL_UNSAFE
    assert record.solver_upper_bound == 1_200_000
    assert counterexample is not None
    assert counterexample["loss_units"] == 1_200_000
    # Zero fills are omitted from the counterexample payload by design.
    fills = {item["action_id"]: item["quantity_lots"] for item in counterexample["fill_quantities"]}
    assert fills == {"buy-yes-a": 4}
    atom_ids = {atom["atom_id"] for atom in counterexample["scenario"]}
    assert atom_ids == {"a:no", "b:yes"}
    # Differential: Oracle reproduces the same worst loss (same counterexample).
    oracle_unsafe = evaluate_fill_adversary(adversary_unsafe, budget())
    assert oracle_unsafe.closed is True
    assert oracle_unsafe.worst_loss_units == record.solver_upper_bound
    assert oracle_unsafe.worst_fill_quantities == (
        ActionQuantity("buy-yes-a", 4),
        ActionQuantity("buy-yes-b", 0),
    )


def test_unknown_semantics_proof_is_unknown() -> None:
    problem = make_problem(
        (_action("buy-yes-a", "a", "some-exchange", 1, 500_000),)
    )
    execution = make_execution(problem, (ActionQuantity("buy-yes-a", 1),))
    adversary = make_adversary(problem, execution, cap=500_000)
    (leg,) = adversary.legs
    assert leg.semantics == "UNKNOWN"
    record, counterexample = prove_partial_fill(adversary, time_limit_ms=30_000)
    assert record.status == PARTIAL_FILL_UNKNOWN
    assert record.solver_termination == "UNKNOWN:UNKNOWN_ORDER_SEMANTICS"
    assert record.verifier_status == VERIFIER_NOT_APPLICABLE
    assert record.solver_lower_bound == 0 and record.solver_upper_bound == 0
    assert counterexample is None
    # Oracle also fails closed for unknown semantics.
    oracle = evaluate_fill_adversary(adversary, budget())
    assert isinstance(oracle, FillAdversaryOracleResult)
    assert oracle.closed is False
    assert oracle.unknown_reason == "UNKNOWN_ORDER_SEMANTICS"


def test_oracle_over_budget_fails_closed() -> None:
    problem = make_problem(
        (_action("buy-yes-a", "a", "predict.fun", 200, 10_000),)
    )
    execution = make_execution(problem, (ActionQuantity("buy-yes-a", 200),))
    adversary = make_adversary(problem, execution, cap=2_000_000)
    small = OracleBudget(64, 1_000_000, 1_000_000)
    oracle = evaluate_fill_adversary(adversary, small)
    assert oracle.closed is False
    assert oracle.unknown_reason == "ORACLE_FILL_VECTOR_LIMIT_EXCEEDED"


class _TimedOutBackend:
    name = "fake"
    version = "test-timeout"

    def solve(self, model: object, *, time_limit_ms: int) -> BackendResult:
        return BackendResult(
            status=NativeSolveStatus.UNKNOWN,
            values=(),
            objective_value=None,
            objective_bound=None,
            native_status="TIME_LIMIT",
            solve_ns=0,
        )


def test_timeout_is_unknown_never_safe() -> None:
    problem = make_problem(
        (_action("buy-yes-a", "a", "polymarket", 1, 500_000),)
    )
    execution = make_execution(problem, (ActionQuantity("buy-yes-a", 1),))
    adversary = make_adversary(problem, execution, cap=1_000_000)
    record, counterexample = prove_partial_fill(
        adversary, backend=_TimedOutBackend(), time_limit_ms=30_000
    )
    assert record.status == PARTIAL_FILL_UNKNOWN
    assert record.solver_termination == "UNKNOWN:TIMEOUT"
    assert record.solver_upper_bound == 0
    assert record.verifier_status == VERIFIER_NOT_APPLICABLE
    assert counterexample is None


def test_verifier_disagreement_fails_closed() -> None:
    problem = make_problem(
        (_action("buy-yes-a", "a", "polymarket", 1, 500_000),)
    )
    execution = make_execution(problem, (ActionQuantity("buy-yes-a", 1),))
    adversary = make_adversary(problem, execution, cap=1_000_000)
    evidence_payload = solve_fill_adversary(adversary.to_payload(), time_limit_ms=30_000)
    tampered = dict(evidence_payload)
    tampered["worst_loss_units"] = tampered["worst_loss_units"] - 1
    verification = verify_fill_adversary(tampered, time_limit_ms=30_000)
    assert verification["status"] == "MISMATCH"


def test_proof_record_contract_and_roundtrip() -> None:
    problem = make_problem(
        (_action("buy-yes-a", "a", "polymarket", 1, 500_000),)
    )
    execution = make_execution(problem, (ActionQuantity("buy-yes-a", 1),))
    adversary = make_adversary(problem, execution, cap=499_999)
    record, counterexample = prove_partial_fill(adversary, time_limit_ms=30_000)
    assert record.schema_version == PARTIAL_FILL_PROOF_SCHEMA_V1
    assert record.fingerprint == fingerprint(
        {
            key: value
            for key, value in asdict(record).items()
            if key != "fingerprint"
        }
    )
    # The six binding fields are the stable execution facts.
    assert len(
        {
            record.execution_solution_fingerprint,
            record.execution_solution_payload_fingerprint,
            record.model_fingerprint,
            record.quote_fingerprint,
            record.cost_fingerprint,
            record.order_semantics_fingerprint,
        }
    ) == 6
    # Canonical payload roundtrip.
    payload = adversary.to_payload()
    adversary2 = fill_adversary_problem_from_payload(payload)
    assert adversary2 == adversary
    assert adversary2.fingerprint == adversary.fingerprint
    assert payload["schema_version"] == FILL_ADVERSARY_SCHEMA_V1
    # Same fixed solution + same cap -> same proof fingerprint (cache key).
    record2, counterexample2 = prove_partial_fill(adversary, time_limit_ms=30_000)
    assert record2.fingerprint == record.fingerprint
    assert counterexample2 == counterexample
    # Different cap -> different proof.
    adversary_other_cap = make_adversary(problem, execution, cap=500_000)
    record3, _ = prove_partial_fill(adversary_other_cap, time_limit_ms=30_000)
    assert record3.fingerprint != record.fingerprint


def test_frozen_n3_fixture_worst_loss_650k() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    problem = problem_from_payload(fixture["problem"])
    quantities = tuple(
        ActionQuantity(action.action_id, 1) for action in problem.actions
    )
    execution = make_execution(problem, quantities)
    adversary = make_adversary(problem, execution, cap=0)
    assert [leg.semantics for leg in adversary.legs] == ["ATOMIC"] * 3
    record, counterexample = prove_partial_fill(adversary, time_limit_ms=30_000)
    assert record.status == PARTIAL_FILL_UNSAFE
    assert record.solver_upper_bound == 650_000
    assert counterexample is not None
    assert counterexample["loss_units"] == 650_000
    oracle = evaluate_fill_adversary(adversary, budget())
    assert oracle.closed is True and oracle.worst_loss_units == 650_000


# ---------------------------------------------------------------- timing


def test_p50_p95_prove_latency_within_limits() -> None:
    """Representative 2-4 leg problems with q into the hundreds must close
    within the BenchmarkLimits hard budget; report p50/p95 in ms."""
    limits = BenchmarkLimits(
        soft_time_limit_ms=1_000,
        hard_time_limit_ms=60_000,
        memory_limit_bytes=256 * 1024 * 1024,
        max_constraint_generation_rounds=4,
    )
    problems = [
        make_problem(
            (_action("buy-yes-a", "a", "predict.fun", 200, 10_000),),
        ),
        make_problem(
            (
                _action("buy-yes-a", "a", "predict.fun", 150, 20_000),
                _action("buy-yes-b", "b", "predict.fun", 150, 20_000),
            ),
            exactly_one=True,
        ),
        make_problem(
            (
                _action("buy-yes-a", "a", "polymarket", 100, 50_000),
                _action("buy-yes-b", "b", "polymarket", 100, 50_000),
                _action("buy-yes-c", "c", "predict.fun", 120, 40_000),
            ),
            exactly_one=True,
        ),
    ]
    adversaries = [
        make_adversary(
            problem,
            make_execution(
                problem,
                tuple(
                    ActionQuantity(action.action_id, action.max_quantity_lots)
                    for action in problem.actions
                ),
            ),
            cap=0,
        )
        for problem in problems
    ]
    samples_ms: list[float] = []
    for adversary in adversaries:
        for _ in range(3):
            started = time.perf_counter()
            record, _ = prove_partial_fill(
                adversary, time_limit_ms=limits.hard_time_limit_ms
            )
            elapsed_ms = (time.perf_counter() - started) * 1_000
            samples_ms.append(elapsed_ms)
            assert record.status in {PARTIAL_FILL_SAFE, PARTIAL_FILL_UNSAFE}
            assert record.solver_termination == TERMINATION_CLOSED
            assert record.verifier_status == VERIFIER_QUALIFIED
            assert elapsed_ms < limits.hard_time_limit_ms
    samples_ms.sort()
    p50 = statistics.median(samples_ms)
    p95 = samples_ms[int(len(samples_ms) * 0.95) - 1]
    print(
        f"prove_partial_fill p50={p50:.1f}ms p95={p95:.1f}ms "
        f"samples={len(samples_ms)} (hard budget {limits.hard_time_limit_ms}ms)"
    )
    assert p95 < limits.hard_time_limit_ms
    assert p50 < limits.soft_time_limit_ms
