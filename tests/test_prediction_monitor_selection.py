"""Issue #77: relation generation -> components -> background resolution."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from open_trader.prediction_monitor_selection import (
    BackgroundResolution,
    idle_capacity,
    relation_generation_components,
    resolve_background_candidate,
    run_discovery,
)
from open_trader.prediction_n_leg import (
    OBSERVATION_SCHEMA_V1,
    PROBLEM_SCHEMA_V1,
    ActionPayout,
    ActionQuantity,
    ActionSide,
    ArbitrageProblem,
    CandidateAction,
    Comparison,
    ConstraintModel,
    ExecutableCostSlice,
    ObjectiveBounds,
    OptimalityStatus,
    OracleBudget,
    QualificationConstraint,
    QualificationMetric,
    RelationConstraint,
    RelationKind,
    SettlementObservationKey,
    TerminalAtom,
    TerminalKind,
    TerminalStateSet,
    canonical_payload,
)
from open_trader.prediction_n_leg_oracle import (
    RelationComponent,
    build_relation_components,
    cut_from_scenario,
    evaluate_fixed_portfolio,
)
from open_trader.prediction_solver import BenchmarkLimits, PortfolioCandidate, SolverEvidence
from open_trader.prediction_solver_verified import VerificationStatus


AS_OF = datetime(2026, 8, 16, tzinfo=UTC)


def observation(suffix: str = "a") -> SettlementObservationKey:
    return SettlementObservationKey(
        OBSERVATION_SCHEMA_V1,
        f"oracle-{suffix}",
        f"indicator-{suffix}",
        AS_OF,
        AS_OF + timedelta(hours=1),
        "UTC",
        "v1",
    )


def action(contract_id: str, key: SettlementObservationKey) -> CandidateAction:
    return CandidateAction(
        action_id=contract_id,
        venue_id="polymarket",
        account_id="test-account",
        chain_id="test-chain",
        market_contract_id=contract_id,
        settlement_observation_key=key,
        side=ActionSide.BUY_YES,
        lot_step_units=1,
        quantity_scale=1,
        min_quantity_lots=1,
        max_quantity_lots=1,
        settlement_asset_id="usd-cents",
        valuation_unit_id="usd-cents",
        asset_valuation_rule_id="usd-cents-v1",
        cost_slices=(ExecutableCostSlice(1, 1, 1),),
    )


def state(contract_id: str, key: SettlementObservationKey) -> TerminalStateSet:
    return TerminalStateSet(
        contract_id,
        key,
        "v1",
        (
            TerminalAtom(
                f"{contract_id}:yes",
                TerminalKind.NORMAL_YES,
                "v1",
                (ActionPayout(contract_id, 2),),
                AS_OF,
            ),
            TerminalAtom(
                f"{contract_id}:no",
                TerminalKind.NORMAL_NO,
                "v1",
                (ActionPayout(contract_id, 0),),
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
        "compiled",
        AS_OF,
        "usd-cents",
        actions,
        states,
        ConstraintModel(relations, ()),
        (),
    )


def relation_problem(
    antecedent: str, consequent: str, key: SettlementObservationKey
) -> ArbitrageProblem:
    return problem(
        (action(antecedent, key), action(consequent, key)),
        (state(antecedent, key), state(consequent, key)),
        (
            RelationConstraint(
                f"r:{antecedent}->{consequent}",
                RelationKind.IMPLIES,
                (antecedent, consequent),
                "v1",
            ),
        ),
    )


def compiled(payload: ArbitrageProblem) -> dict[str, object]:
    return canonical_payload(payload)


def row(
    identity: str,
    *,
    activation: str = "ACTIVE",
    complete: bool = True,
    compiled_problem: dict[str, object] | None = None,
) -> dict[str, object]:
    model: dict[str, object] = {}
    if complete:
        model = {
            "terminal_states": ["NORMAL_YES", "NORMAL_NO", "VOID"],
            "payouts": {},
            "capital_release": "2026-08-31T00:00:00Z",
        }
    if compiled_problem is not None:
        model["problem"] = compiled_problem
    return {
        "identity": identity,
        "version_id": f"v-{identity}",
        "fingerprint": f"fp-{identity}",
        "activation": activation,
        "relation_type": "IMPLIES",
        "endpoints": [],
        "model": model,
    }


def test_empty_generation_yields_no_components() -> None:
    assert relation_generation_components({}) == ()


def test_incomplete_rows_never_enter_selection() -> None:
    generation = {
        "r:ab": row("r:ab", complete=False),
        "r:cd": row("r:cd", complete=False),
    }
    assert relation_generation_components(generation) == ()


def test_unknown_or_inactive_rows_never_enter_selection() -> None:
    key = observation()
    generation = {
        "r:ab": row("r:ab", activation="UNKNOWN", compiled_problem=compiled(relation_problem("contract-a", "contract-b", key))),
        "r:cd": row("r:cd", activation="PENDING", compiled_problem=compiled(relation_problem("contract-c", "contract-d", key))),
    }
    assert relation_generation_components(generation) == ()


def test_complete_row_without_compiled_problem_fails_closed() -> None:
    generation = {"r:ab": row("r:ab")}
    with pytest.raises(ValueError, match="model.problem"):
        relation_generation_components(generation)


def test_overlapping_relations_merge_into_one_component() -> None:
    key = observation()
    generation = {
        "r:ab": row("r:ab", compiled_problem=compiled(relation_problem("contract-a", "contract-b", key))),
        "r:bc": row("r:bc", compiled_problem=compiled(relation_problem("contract-b", "contract-c", key))),
    }
    assert relation_generation_components(generation) == (
        RelationComponent(
            "component:contract-a:contract-b:contract-c",
            ("contract-a", "contract-b", "contract-c"),
            ("contract-a", "contract-b", "contract-c"),
            ("r:contract-a->contract-b", "r:contract-b->contract-c"),
        ),
    )


def test_disjoint_relations_form_separate_components() -> None:
    key_a = observation("a")
    key_b = observation("b")
    generation = {
        "r:ab": row("r:ab", compiled_problem=compiled(relation_problem("contract-a", "contract-b", key_a))),
        "r:cd": row("r:cd", compiled_problem=compiled(relation_problem("contract-c", "contract-d", key_b))),
    }
    assert relation_generation_components(generation) == (
        RelationComponent("component:contract-a:contract-b", ("contract-a", "contract-b"), ("contract-a", "contract-b"), ("r:contract-a->contract-b",)),
        RelationComponent("component:contract-c:contract-d", ("contract-c", "contract-d"), ("contract-c", "contract-d"), ("r:contract-c->contract-d",)),
    )


def test_conflicting_shared_contract_models_fail_closed() -> None:
    key = observation()
    lower = replace_payout(relation_problem("contract-a", "contract-b", key), "contract-b", 1)
    higher = replace_payout(relation_problem("contract-b", "contract-c", key), "contract-b", 2)
    generation = {
        "r:ab": row("r:ab", compiled_problem=compiled(lower)),
        "r:bc": row("r:bc", compiled_problem=compiled(higher)),
    }
    with pytest.raises(ValueError, match="terminal state set 'contract-b' conflicts"):
        relation_generation_components(generation)


def replace_payout(
    built: ArbitrageProblem, contract_id: str, payout_units: int
) -> ArbitrageProblem:
    states = tuple(
        TerminalStateSet(
            state_set.market_contract_id,
            state_set.settlement_observation_key,
            state_set.rule_version,
            tuple(
                TerminalAtom(
                    atom.atom_id,
                    atom.kind,
                    atom.rule_version,
                    tuple(
                        ActionPayout(payout.action_id, payout_units)
                        if payout.action_id == contract_id
                        else payout
                        for payout in atom.payouts
                    ),
                    atom.capital_release_at,
                )
                for atom in state_set.atoms
            ),
        )
        for state_set in built.terminal_state_sets
    )
    return ArbitrageProblem(
        built.schema_version,
        built.problem_id,
        built.as_of,
        built.valuation_unit_id,
        built.actions,
        states,
        built.constraint_model,
        built.qualification_constraints,
    )


def qualified_problem(*, min_profit: int = 3) -> ArbitrageProblem:
    key = observation()
    action = CandidateAction(
        action_id="contract-a",
        venue_id="polymarket",
        account_id="test-account",
        chain_id="test-chain",
        market_contract_id="contract-a",
        settlement_observation_key=key,
        side=ActionSide.BUY_YES,
        lot_step_units=1,
        quantity_scale=1,
        min_quantity_lots=1,
        max_quantity_lots=1,
        settlement_asset_id="usd-cents",
        valuation_unit_id="usd-cents",
        asset_valuation_rule_id="usd-cents-v1",
        cost_slices=(ExecutableCostSlice(1, 1, 2),),
    )
    states = (
        TerminalStateSet(
            "contract-a",
            key,
            "v1",
            (
                TerminalAtom(
                    "contract-a:yes",
                    TerminalKind.NORMAL_YES,
                    "v1",
                    (ActionPayout("contract-a", 5),),
                    AS_OF,
                ),
            ),
        ),
    )
    return ArbitrageProblem(
        PROBLEM_SCHEMA_V1,
        "background",
        AS_OF,
        "usd-cents",
        (action,),
        states,
        ConstraintModel((), ()),
        (
            QualificationConstraint(
                "profit",
                "v1",
                QualificationMetric.GUARANTEED_PROFIT_UNITS,
                Comparison.GREATER_THAN_OR_EQUAL,
                min_profit,
                1,
            ),
        ),
    )


def solver_evidence(
    problem: ArbitrageProblem, budget: OracleBudget, *, closed: bool = False
) -> SolverEvidence:
    quantities = (ActionQuantity("contract-a", 1),)
    evaluation = evaluate_fixed_portfolio(problem, quantities, budget)
    return SolverEvidence(
        "FEASIBLE",
        PortfolioCandidate(quantities, evaluation.guaranteed_profit_units),
        ObjectiveBounds(
            evaluation.guaranteed_profit_units,
            evaluation.guaranteed_profit_units if closed else None,
            0 if closed else None,
            closed,
        ),
        evaluation.worst_scenario,
        evaluation.payout_lower_bound_units,
        evaluation.cost_upper_bound_units,
        evaluation.guaranteed_profit_units,
        evaluation.conservative_capital_release_at,
        True,
        closed,
        1,
        1,
        (cut_from_scenario(problem, evaluation.worst_scenario),),
        None,
    )


def test_idle_capacity_defaults_true() -> None:
    assert idle_capacity() is True


def test_background_resolution_skips_when_not_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "open_trader.prediction_monitor_selection.idle_capacity", lambda: False
    )
    assert (
        resolve_background_candidate(
            qualified_problem(),
            budget=OracleBudget(2, 2, 2),
            limits=BenchmarkLimits(100, 200, 1_000_000, 4),
        )
        is None
    )


def test_qualified_candidate_fixes_initial_verified_profit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    problem = qualified_problem()
    budget = OracleBudget(2, 2, 2)
    expected = solver_evidence(problem, budget)
    monkeypatch.setattr(
        "open_trader.prediction_solver_verified.solve_with_constraint_generation",
        lambda request, backend, limits: expected,
    )
    backend = type("Backend", (), {"name": "cp_sat", "version": "test"})()

    result = resolve_background_candidate(
        problem, budget=budget, limits=BenchmarkLimits(100, 200, 1_000_000, 4), backend=backend
    )

    assert isinstance(result, BackgroundResolution)
    assert result.status == VerificationStatus.QUALIFIED_VERIFIED
    assert result.initial_verified_profit == 3
    assert result.optimality == OptimalityStatus.NOT_PROVEN
    assert result.solution is not None
    assert result.solution.payout_proof.guaranteed_profit_units == 3


def test_optimal_global_search_is_recorded_separately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    problem = qualified_problem()
    budget = OracleBudget(2, 2, 2)
    expected = solver_evidence(problem, budget, closed=True)
    monkeypatch.setattr(
        "open_trader.prediction_solver_verified.solve_with_constraint_generation",
        lambda request, backend, limits: expected,
    )
    backend = type("Backend", (), {"name": "cp_sat", "version": "test"})()

    result = resolve_background_candidate(
        problem, budget=budget, limits=BenchmarkLimits(100, 200, 1_000_000, 4), backend=backend
    )

    assert result is not None
    assert result.optimality == OptimalityStatus.OPTIMAL
    assert result.initial_verified_profit == 3


def test_not_qualified_candidate_records_no_verified_profit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    problem = qualified_problem(min_profit=4)
    budget = OracleBudget(2, 2, 2)
    expected = solver_evidence(problem, budget)
    monkeypatch.setattr(
        "open_trader.prediction_solver_verified.solve_with_constraint_generation",
        lambda request, backend, limits: expected,
    )
    backend = type("Backend", (), {"name": "cp_sat", "version": "test"})()

    result = resolve_background_candidate(
        problem, budget=budget, limits=BenchmarkLimits(100, 200, 1_000_000, 4), backend=backend
    )

    assert result is not None
    assert result.status == VerificationStatus.NOT_QUALIFIED
    assert result.initial_verified_profit is None
    assert result.optimality == OptimalityStatus.NOT_APPLICABLE


def test_run_discovery_resolves_each_component(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    problem = qualified_problem()
    budget = OracleBudget(2, 2, 2)
    expected = solver_evidence(problem, budget)
    monkeypatch.setattr(
        "open_trader.prediction_solver_verified.solve_with_constraint_generation",
        lambda request, backend, limits: expected,
    )
    backend = type("Backend", (), {"name": "cp_sat", "version": "test"})()
    components = build_relation_components(problem)

    results = run_discovery(
        problem,
        components,
        budget=budget,
        limits=BenchmarkLimits(100, 200, 1_000_000, 4),
        backend=backend,
    )

    assert len(results) == 1
    assert results[0].status == VerificationStatus.QUALIFIED_VERIFIED
    assert results[0].initial_verified_profit == 3


def test_run_discovery_honors_max_components(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    problem = qualified_problem()
    budget = OracleBudget(2, 2, 2)
    expected = solver_evidence(problem, budget)
    monkeypatch.setattr(
        "open_trader.prediction_solver_verified.solve_with_constraint_generation",
        lambda request, backend, limits: expected,
    )
    backend = type("Backend", (), {"name": "cp_sat", "version": "test"})()
    components = build_relation_components(problem)

    results = run_discovery(
        problem,
        components * 2,
        budget=budget,
        limits=BenchmarkLimits(100, 200, 1_000_000, 4),
        backend=backend,
        max_components=1,
    )

    assert len(results) == 1


def test_run_discovery_skips_when_not_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "open_trader.prediction_monitor_selection.idle_capacity", lambda: False
    )
    problem = qualified_problem()
    components = build_relation_components(problem)

    results = run_discovery(
        problem,
        components,
        budget=OracleBudget(2, 2, 2),
        limits=BenchmarkLimits(100, 200, 1_000_000, 4),
    )

    assert results == ()
