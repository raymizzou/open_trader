from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from fractions import Fraction
import hashlib
import json
from pathlib import Path

import pytest

from open_trader.prediction_n_leg import (
    ActionPayout,
    ActionQuantity,
    ActionSide,
    ArbitrageProblem,
    BusinessStatus,
    CandidateAction,
    ConstraintModel,
    ExecutableCostSlice,
    ForbiddenAtomCombination,
    OracleBudget,
    OracleRequest,
    OptimalityStatus,
    ProofStatus,
    QualificationConstraint,
    QualificationMetric,
    Comparison,
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
    canonical_payload,
    fingerprint,
    request_from_payload,
    result_from_payload,
)
import open_trader.prediction_n_leg_oracle as oracle
from open_trader.prediction_n_leg_oracle import (
    PortfolioEvaluation,
    RelationComponent,
    build_relation_components,
    build_portfolio_solution,
    cut_from_scenario,
    cost_upper_bound,
    derive_selected_support_graph,
    enumerate_allowed_scenarios,
    evaluate_fixed_portfolio,
    split_disconnected_solution,
)


AS_OF = datetime(2026, 8, 12, tzinfo=UTC)


ORACLE_CORPUS_PATH = Path(__file__).with_name("fixtures") / "prediction_n_leg_v1.json"
ORACLE_CORPUS_SHA256 = "dbac567c50fc3bb33f38ae9346a25f66256a94a893a16b84677fbf6ac6befbe4"


def _run_corpus_case(request: OracleRequest):
    if request.mode == SearchMode.ADMISSION:
        return oracle.find_qualified(request)
    if request.mode == SearchMode.OPTIMIZATION:
        return oracle.solve_optimal(request)
    if request.mode == SearchMode.RAW_ARBITRAGE_DIAGNOSTIC:
        return oracle.diagnose_raw_arbitrage(request.problem, request.budget)
    raise AssertionError(f"unsupported corpus mode: {request.mode}")


def _assert_corpus_replay(replay: dict[str, object]) -> None:
    expected_request = request_from_payload(replay["request"])  # type: ignore[arg-type]
    expected_result = result_from_payload(replay["expected_result"])  # type: ignore[arg-type]
    assert fingerprint(expected_request) == replay["expected_request_fingerprint"]
    assert fingerprint(expected_result) == replay["expected_result_fingerprint"]

    first = _run_corpus_case(request_from_payload(replay["request"]))  # type: ignore[arg-type]
    second = _run_corpus_case(request_from_payload(replay["request"]))  # type: ignore[arg-type]
    assert canonical_payload(first) == replay["expected_result"]
    assert canonical_payload(second) == replay["expected_result"]
    assert fingerprint(first) == replay["expected_result_fingerprint"]
    assert fingerprint(second) == replay["expected_result_fingerprint"]
    assert first == expected_result == second


def test_frozen_oracle_corpus_replays_literal_results_deterministically() -> None:
    corpus_bytes = ORACLE_CORPUS_PATH.read_bytes()
    corpus = json.loads(corpus_bytes)

    assert hashlib.sha256(corpus_bytes).hexdigest() == ORACLE_CORPUS_SHA256
    for case in corpus["cases"]:
        for replay in (case, *case.get("additional_replays", ())):
            _assert_corpus_replay(replay)


@pytest.mark.parametrize(
    "case_id",
    ("qualified-not-optimal", "no-qualified-positive-raw", "no-arbitrage"),
    ids=("qualified_not_optimal", "no_qualified_positive_raw", "no_arbitrage"),
)
def test_oracle_corpus_direct_replay_cases(case_id: str) -> None:
    corpus = json.loads(ORACLE_CORPUS_PATH.read_text(encoding="utf-8"))
    case = next(item for item in corpus["cases"] if item["case_id"] == case_id)

    for replay in (case, *case.get("additional_replays", ())):
        _assert_corpus_replay(replay)


def observation(suffix: str = "a") -> SettlementObservationKey:
    return SettlementObservationKey(
        "open_trader.prediction_n_leg.observation.v1",
        f"oracle-{suffix}",
        f"indicator-{suffix}",
        AS_OF,
        AS_OF + timedelta(hours=1),
        "UTC",
        "v1",
    )


def action(
    contract_id: str,
    action_id: str,
    key: SettlementObservationKey,
    cost_slices: tuple[ExecutableCostSlice, ...] = (ExecutableCostSlice(1, 1, 1),),
) -> CandidateAction:
    return CandidateAction(
        action_id,
        contract_id,
        key,
        ActionSide.BUY_YES,
        1,
        1,
        "usd-cents",
        "usd-cents",
        "usd-cents-v1",
        cost_slices,
    )


def state(
    contract_id: str,
    key: SettlementObservationKey,
    action_id: str,
    atoms: tuple[tuple[str, TerminalKind, int], ...],
    capital_release_at: datetime = AS_OF,
) -> TerminalStateSet:
    return TerminalStateSet(
        contract_id,
        key,
        "v1",
        tuple(
            TerminalAtom(atom_id, kind, "v1", (ActionPayout(action_id, payout),), capital_release_at)
            for atom_id, kind, payout in atoms
        ),
    )


def problem(
    actions: tuple[CandidateAction, ...],
    states: tuple[TerminalStateSet, ...],
    relations: tuple[RelationConstraint, ...] = (),
    forbidden: tuple[ForbiddenAtomCombination, ...] = (),
) -> ArbitrageProblem:
    return ArbitrageProblem(
        "open_trader.prediction_n_leg.problem.v1",
        "oracle-test",
        AS_OF,
        "usd-cents",
        actions,
        states,
        ConstraintModel(relations, forbidden),
        (),
    )


def test_components_join_actions_on_one_market_contract() -> None:
    key = observation()
    built = problem(
        (action("contract-a", "action-z", key), action("contract-a", "action-a", key)),
        (state("contract-a", key, "action-a", (("a-no", TerminalKind.NORMAL_NO, 0),)),),
    )
    built = replace(
        built,
        terminal_state_sets=(
            replace(
                built.terminal_state_sets[0],
                atoms=(
                    TerminalAtom(
                        "a-no",
                        TerminalKind.NORMAL_NO,
                        "v1",
                        (ActionPayout("action-a", 0), ActionPayout("action-z", 0)),
                        AS_OF,
                    ),
                ),
            ),
        ),
    )

    assert build_relation_components(built) == (
        RelationComponent("component:contract-a", ("action-a", "action-z"), ("contract-a",), ()),
    )


def test_components_join_exact_observation_keys_but_not_near_matches() -> None:
    shared = observation()
    built = problem(
        (action("contract-a", "action-a", shared), action("contract-b", "action-b", shared)),
        (
            state("contract-a", shared, "action-a", (("a-no", TerminalKind.NORMAL_NO, 0),)),
            state("contract-b", shared, "action-b", (("b-no", TerminalKind.NORMAL_NO, 0),)),
        ),
    )

    assert build_relation_components(built) == (
        RelationComponent("component:contract-a:contract-b", ("action-a", "action-b"), ("contract-a", "contract-b"), ()),
    )
    assert {"title", "discovery_source", "llm_similarity"}.isdisjoint(CandidateAction.__slots__)

    for changed in (
        replace(shared, timezone="Asia/Shanghai"),
        replace(shared, observation_end=shared.observation_end + timedelta(seconds=1)),
        replace(shared, oracle_id="oracle-other"),
        replace(shared, rule_version="v2"),
    ):
        separate = replace(
            built,
            actions=(built.actions[0], replace(built.actions[1], settlement_observation_key=changed)),
            terminal_state_sets=(built.terminal_state_sets[0], replace(built.terminal_state_sets[1], settlement_observation_key=changed)),
        )

        assert build_relation_components(separate) == (
            RelationComponent("component:contract-a", ("action-a",), ("contract-a",), ()),
            RelationComponent("component:contract-b", ("action-b",), ("contract-b",), ()),
        )


def test_components_join_explicit_versioned_relations_transitively_only() -> None:
    keys = (observation("a"), observation("b"), observation("c"))
    built = problem(
        tuple(action(f"contract-{suffix}", f"action-{suffix}", key) for suffix, key in zip("abc", keys, strict=True)),
        tuple(state(f"contract-{suffix}", key, f"action-{suffix}", ((f"{suffix}-no", TerminalKind.NORMAL_NO, 0),)) for suffix, key in zip("abc", keys, strict=True)),
        (
            RelationConstraint("relation-b", RelationKind.MUTUALLY_EXCLUSIVE, ("contract-b", "contract-c"), "v7"),
            RelationConstraint("relation-a", RelationKind.IMPLIES, ("contract-a", "contract-b"), "v9"),
        ),
    )

    assert build_relation_components(built) == (
        RelationComponent(
            "component:contract-a:contract-b:contract-c",
            ("action-a", "action-b", "action-c"),
            ("contract-a", "contract-b", "contract-c"),
            ("relation-a", "relation-b"),
        ),
    )


def test_components_join_cross_contract_forbidden_atom_constraints() -> None:
    key_a, key_b = observation("a"), observation("b")
    built = problem(
        (action("contract-a", "action-a", key_a), action("contract-b", "action-b", key_b)),
        (
            state("contract-a", key_a, "action-a", (("a-yes", TerminalKind.NORMAL_YES, 1), ("a-no", TerminalKind.NORMAL_NO, 0))),
            state("contract-b", key_b, "action-b", (("b-yes", TerminalKind.NORMAL_YES, 1), ("b-no", TerminalKind.NORMAL_NO, 0))),
        ),
        forbidden=(ForbiddenAtomCombination("forbid", ("a-yes", "b-no"), "v1"),),
    )

    assert build_relation_components(built) == (
        RelationComponent("component:contract-a:contract-b", ("action-a", "action-b"), ("contract-a", "contract-b"), ("forbid",)),
    )

def test_enumeration_selects_one_atom_per_contract_and_implies_only_normal_yes_no() -> None:
    key_a, key_b = observation("a"), observation("b")
    built = problem(
        (action("contract-a", "action-a", key_a), action("contract-b", "action-b", key_b)),
        (
            state("contract-a", key_a, "action-a", (("a-yes", TerminalKind.NORMAL_YES, 10), ("a-no", TerminalKind.NORMAL_NO, 0))),
            state("contract-b", key_b, "action-b", (("b-yes", TerminalKind.NORMAL_YES, 20), ("b-no", TerminalKind.NORMAL_NO, 0))),
        ),
        (RelationConstraint("implies", RelationKind.IMPLIES, ("contract-a", "contract-b"), "v1"),),
    )

    result = enumerate_allowed_scenarios(built, OracleBudget(1, 4, 1))

    assert result.raw_joint_state_count == 4
    assert result.unknown_reason is None
    assert result.scenarios == (
        SettlementScenario((SelectedAtom("contract-a", "a-no"), SelectedAtom("contract-b", "b-no"))),
        SettlementScenario((SelectedAtom("contract-a", "a-no"), SelectedAtom("contract-b", "b-yes"))),
        SettlementScenario((SelectedAtom("contract-a", "a-yes"), SelectedAtom("contract-b", "b-yes"))),
    )


def test_normal_relations_skip_exceptional_atoms_and_forbidden_combinations_still_apply() -> None:
    key_a, key_b, key_c = observation("a"), observation("b"), observation("c")
    built = problem(
        (
            action("contract-a", "action-a", key_a),
            action("contract-b", "action-b", key_b),
            action("contract-c", "action-c", key_c),
        ),
        (
            state("contract-a", key_a, "action-a", (("a-void", TerminalKind.VOID, 0),)),
            state("contract-b", key_b, "action-b", (("b-refund", TerminalKind.REFUND, 0),)),
            state("contract-c", key_c, "action-c", (("c-split", TerminalKind.SPLIT, 0),)),
        ),
        (RelationConstraint("exactly-one", RelationKind.EXACTLY_ONE, ("contract-a", "contract-b", "contract-c"), "v1"),),
    )

    allowed = enumerate_allowed_scenarios(built, OracleBudget(1, 1, 1))
    forbidden = enumerate_allowed_scenarios(
        replace(built, constraint_model=ConstraintModel(built.constraint_model.relations, (ForbiddenAtomCombination("forbid-exception", ("a-void", "b-refund", "c-split"), "v2"),))),
        OracleBudget(1, 1, 1),
    )

    assert allowed.scenarios == (
        SettlementScenario(
            (
                SelectedAtom("contract-a", "a-void"),
                SelectedAtom("contract-b", "b-refund"),
                SelectedAtom("contract-c", "c-split"),
            )
        ),
    )
    assert forbidden.scenarios is None
    assert forbidden.unknown_reason == UnknownReason.CONTRADICTORY_CONSTRAINT_MODEL


def test_mutual_exclusion_and_exactly_one_apply_only_when_every_atom_is_normal() -> None:
    key_a, key_b = observation("a"), observation("b")
    states = (
        state(
            "contract-a",
            key_a,
            "action-a",
            (("a-yes", TerminalKind.NORMAL_YES, 1), ("a-no", TerminalKind.NORMAL_NO, 0), ("a-void", TerminalKind.VOID, 0)),
        ),
        state("contract-b", key_b, "action-b", (("b-yes", TerminalKind.NORMAL_YES, 1), ("b-no", TerminalKind.NORMAL_NO, 0))),
    )
    actions = (action("contract-a", "action-a", key_a), action("contract-b", "action-b", key_b))

    mutually_exclusive = enumerate_allowed_scenarios(
        problem(actions, states, (RelationConstraint("exclusive", RelationKind.MUTUALLY_EXCLUSIVE, ("contract-a", "contract-b"), "v1"),)),
        OracleBudget(1, 6, 1),
    )
    exactly_one = enumerate_allowed_scenarios(
        problem(actions, states, (RelationConstraint("one", RelationKind.EXACTLY_ONE, ("contract-a", "contract-b"), "v1"),)),
        OracleBudget(1, 6, 1),
    )

    assert mutually_exclusive.scenarios == (
        SettlementScenario((SelectedAtom("contract-a", "a-no"), SelectedAtom("contract-b", "b-no"))),
        SettlementScenario((SelectedAtom("contract-a", "a-no"), SelectedAtom("contract-b", "b-yes"))),
        SettlementScenario((SelectedAtom("contract-a", "a-void"), SelectedAtom("contract-b", "b-no"))),
        SettlementScenario((SelectedAtom("contract-a", "a-void"), SelectedAtom("contract-b", "b-yes"))),
        SettlementScenario((SelectedAtom("contract-a", "a-yes"), SelectedAtom("contract-b", "b-no"))),
    )
    assert exactly_one.scenarios == (
        SettlementScenario((SelectedAtom("contract-a", "a-no"), SelectedAtom("contract-b", "b-yes"))),
        SettlementScenario((SelectedAtom("contract-a", "a-void"), SelectedAtom("contract-b", "b-no"))),
        SettlementScenario((SelectedAtom("contract-a", "a-void"), SelectedAtom("contract-b", "b-yes"))),
        SettlementScenario((SelectedAtom("contract-a", "a-yes"), SelectedAtom("contract-b", "b-no"))),
    )


def test_cut_carries_each_selected_action_payout_coefficient() -> None:
    key_a, key_b = observation("a"), observation("b")
    built = problem(
        (action("contract-a", "action-z", key_a), action("contract-b", "action-a", key_b)),
        (
            state("contract-a", key_a, "action-z", (("a-yes", TerminalKind.NORMAL_YES, 17),)),
            state("contract-b", key_b, "action-a", (("b-no", TerminalKind.NORMAL_NO, -3),)),
        ),
    )
    scenario = SettlementScenario((SelectedAtom("contract-b", "b-no"), SelectedAtom("contract-a", "a-yes")))

    cut = cut_from_scenario(built, scenario)

    assert cut.cut_id == f"cut:{fingerprint(scenario)}"
    assert cut.scenario == scenario
    assert cut.payout_per_lot == (ActionPayout("action-a", -3), ActionPayout("action-z", 17))


def test_cut_rejects_duplicate_contract_selection() -> None:
    key_a, key_b = observation("a"), observation("b")
    built = problem(
        (action("contract-a", "action-a", key_a), action("contract-b", "action-b", key_b)),
        (
            state("contract-a", key_a, "action-a", (("a-yes", TerminalKind.NORMAL_YES, 1),)),
            state("contract-b", key_b, "action-b", (("b-no", TerminalKind.NORMAL_NO, 2),)),
        ),
    )

    with pytest.raises(ValueError, match="contract more than once"):
        cut_from_scenario(
            built,
            SettlementScenario(
                (
                    SelectedAtom("contract-a", "a-yes"),
                    SelectedAtom("contract-a", "a-yes"),
                    SelectedAtom("contract-b", "b-no"),
                )
            ),
        )


def test_cut_id_uses_canonical_scenario_fingerprint_without_delimiter_collisions() -> None:
    key_a, key_b = observation("a"), observation("b")
    built = problem(
        (action("contract-a", "action-a", key_a), action("contract-b", "action-b", key_b)),
        (
            state(
                "contract-a",
                key_a,
                "action-a",
                (("a:b", TerminalKind.NORMAL_YES, 1), ("a", TerminalKind.NORMAL_NO, 2)),
            ),
            state(
                "contract-b",
                key_b,
                "action-b",
                (("c", TerminalKind.NORMAL_YES, 3), ("b:c", TerminalKind.NORMAL_NO, 4)),
            ),
        ),
    )
    first = SettlementScenario((SelectedAtom("contract-a", "a:b"), SelectedAtom("contract-b", "c")))
    second = SettlementScenario((SelectedAtom("contract-a", "a"), SelectedAtom("contract-b", "b:c")))

    first_cut = cut_from_scenario(built, first)
    second_cut = cut_from_scenario(built, second)

    assert first_cut.cut_id == f"cut:{fingerprint(first)}"
    assert second_cut.cut_id == f"cut:{fingerprint(second)}"
    assert first_cut.cut_id != second_cut.cut_id


def test_enumeration_reports_the_state_limit_before_partial_enumeration() -> None:
    key_a, key_b = observation("a"), observation("b")
    built = problem(
        (action("contract-a", "action-a", key_a), action("contract-b", "action-b", key_b)),
        (
            state("contract-a", key_a, "action-a", (("a-yes", TerminalKind.NORMAL_YES, 0), ("a-no", TerminalKind.NORMAL_NO, 0))),
            state("contract-b", key_b, "action-b", (("b-yes", TerminalKind.NORMAL_YES, 0), ("b-no", TerminalKind.NORMAL_NO, 0))),
        ),
    )

    result = enumerate_allowed_scenarios(built, OracleBudget(1, 3, 1))

    assert result.scenarios is None
    assert result.raw_joint_state_count == 4
    assert result.unknown_reason == UnknownReason.ORACLE_STATE_LIMIT_EXCEEDED


def test_enumeration_reports_a_contradictory_constraint_model() -> None:
    key_a, key_b = observation("a"), observation("b")
    built = problem(
        (action("contract-a", "action-a", key_a), action("contract-b", "action-b", key_b)),
        (
            state("contract-a", key_a, "action-a", (("a-yes", TerminalKind.NORMAL_YES, 0),)),
            state("contract-b", key_b, "action-b", (("b-no", TerminalKind.NORMAL_NO, 0),)),
        ),
        (RelationConstraint("implies", RelationKind.IMPLIES, ("contract-a", "contract-b"), "v1"),),
    )

    result = enumerate_allowed_scenarios(built, OracleBudget(1, 1, 1))

    assert result.scenarios is None
    assert result.raw_joint_state_count == 1
    assert result.unknown_reason == UnknownReason.CONTRADICTORY_CONSTRAINT_MODEL


def test_fixed_portfolio_accumulates_integer_costs_and_discards_zero_quantities() -> None:
    key = observation()
    priced = action(
        "contract-a",
        "action-a",
        key,
        (ExecutableCostSlice(1, 2, 3), ExecutableCostSlice(3, 4, 5)),
    )
    zero = action("contract-a", "action-zero", key)
    built = problem((priced, zero), (state("contract-a", key, "action-a", (("a", TerminalKind.NORMAL_YES, 7),)),))
    built = replace(
        built,
        terminal_state_sets=(
            replace(
                built.terminal_state_sets[0],
                atoms=(
                    replace(
                        built.terminal_state_sets[0].atoms[0],
                        payouts=(ActionPayout("action-a", 7), ActionPayout("action-zero", 99)),
                    ),
                ),
            ),
        ),
    )

    quantities = (ActionQuantity("action-zero", 0), ActionQuantity("action-a", 3))
    evaluation = evaluate_fixed_portfolio(built, quantities, OracleBudget(1, 1, 1))

    assert cost_upper_bound(built, quantities) == 11
    assert evaluation.quantities == (ActionQuantity("action-a", 3),)
    assert evaluation.payout_lower_bound_units == 21
    assert evaluation.cost_upper_bound_units == 11
    assert evaluation.guaranteed_profit_units == 10


def test_fixed_portfolio_uses_stable_worst_scenario_and_independent_latest_release() -> None:
    key = observation()
    built = problem(
        (action("contract-a", "action-a", key),),
        (
            state(
                "contract-a",
                key,
                "action-a",
                (("z-payout", TerminalKind.NORMAL_YES, 10), ("a-payout", TerminalKind.NORMAL_NO, 10)),
                AS_OF + timedelta(days=1),
            ),
        ),
    )
    built = replace(
        built,
        terminal_state_sets=(
            replace(
                built.terminal_state_sets[0],
                atoms=(
                    replace(built.terminal_state_sets[0].atoms[0], capital_release_at=AS_OF + timedelta(days=4)),
                    built.terminal_state_sets[0].atoms[1],
                ),
            ),
        ),
    )

    evaluation = evaluate_fixed_portfolio(built, (ActionQuantity("action-a", 1),), OracleBudget(1, 2, 1))
    scenarios = enumerate_allowed_scenarios(built, OracleBudget(1, 2, 1)).scenarios

    assert scenarios is not None
    assert evaluation.worst_scenario == min(scenarios, key=fingerprint)
    assert evaluation.conservative_capital_release_at == AS_OF + timedelta(days=4)


def test_fixed_portfolio_qualifies_only_from_exact_integer_constraints() -> None:
    key = observation()
    built = problem(
        (action("contract-a", "action-a", key, (ExecutableCostSlice(1, 1, 2),)),),
        (state("contract-a", key, "action-a", (("a", TerminalKind.NORMAL_YES, 3),), AS_OF + timedelta(days=365)),),
    )
    built = replace(
        built,
        qualification_constraints=(
            QualificationConstraint("profit", "v1", QualificationMetric.GUARANTEED_PROFIT_UNITS, Comparison.GREATER_THAN_OR_EQUAL, 1, 1),
            QualificationConstraint("margin", "v1", QualificationMetric.NET_MARGIN_PPM, Comparison.GREATER_THAN_OR_EQUAL, 500_000, 1),
            QualificationConstraint("annual", "v1", QualificationMetric.ANNUALIZED_RETURN_PPM, Comparison.GREATER_THAN_OR_EQUAL, 500_000, 1),
        ),
    )

    passed = evaluate_fixed_portfolio(built, (ActionQuantity("action-a", 1),), OracleBudget(1, 1, 1))
    rounded_down = evaluate_fixed_portfolio(
        replace(built, qualification_constraints=(replace(built.qualification_constraints[0], threshold_numerator=2),)),
        (ActionQuantity("action-a", 1),),
        OracleBudget(1, 1, 1),
    )

    assert passed.failed_qualification_ids == ()
    assert rounded_down.guaranteed_profit_units == 1
    assert rounded_down.failed_qualification_ids == ("profit",)


def test_subsecond_release_delay_is_conservatively_rounded_up() -> None:
    key = observation()
    built = replace(
        problem(
            (action("contract-a", "action-a", key),),
            (
                state(
                    "contract-a",
                    key,
                    "action-a",
                    (("a", TerminalKind.NORMAL_YES, 2),),
                    AS_OF + timedelta(microseconds=500_000),
                ),
            ),
        ),
        qualification_constraints=(
            QualificationConstraint(
                "annual",
                "v1",
                QualificationMetric.ANNUALIZED_RETURN_PPM,
                Comparison.GREATER_THAN_OR_EQUAL,
                31_536_000_000_001,
                1,
            ),
            QualificationConstraint(
                "max-zero-delay",
                "v1",
                QualificationMetric.MAX_CAPITAL_RELEASE_DELAY_SECONDS,
                Comparison.LESS_THAN_OR_EQUAL,
                0,
                1,
            ),
        ),
    )

    evaluation = evaluate_fixed_portfolio(
        built,
        (ActionQuantity("action-a", 1),),
        OracleBudget(2, 1, 1),
    )

    assert evaluation.failed_qualification_ids == ("annual", "max-zero-delay")


def test_admission_preflights_payout_aggregate_overflow_before_early_result() -> None:
    maximum = 2**63 - 1
    key = observation()
    built = problem(
        (action("contract-a", "action-a", key), action("contract-a", "action-b", key)),
        (state("contract-a", key, "action-a", (("a", TerminalKind.NORMAL_YES, maximum),)),),
    )
    built = replace(
        built,
        terminal_state_sets=(
            replace(
                built.terminal_state_sets[0],
                atoms=(
                    replace(
                        built.terminal_state_sets[0].atoms[0],
                        payouts=(ActionPayout("action-a", maximum), ActionPayout("action-b", maximum)),
                    ),
                ),
            ),
        ),
    )

    result = oracle.find_qualified(_admission_request(built, OracleBudget(4, 1, 1)))

    assert result == result_from_payload(canonical_payload(result))
    assert result.business_status == BusinessStatus.UNKNOWN
    assert result.unknown_reason == UnknownReason.NUMERIC_OVERFLOW


def test_oracle_returns_unknown_for_cost_aggregate_and_profit_overflow() -> None:
    maximum = 2**63 - 1
    minimum = -(2**63)
    key = observation()
    aggregate_cost = problem(
        (
            action("contract-a", "action-a", key, (ExecutableCostSlice(1, 1, maximum),)),
            action("contract-a", "action-b", key, (ExecutableCostSlice(1, 1, maximum),)),
        ),
        (state("contract-a", key, "action-a", (("a", TerminalKind.NORMAL_YES, 0),)),),
    )
    aggregate_cost = replace(
        aggregate_cost,
        terminal_state_sets=(
            replace(
                aggregate_cost.terminal_state_sets[0],
                atoms=(
                    replace(
                        aggregate_cost.terminal_state_sets[0].atoms[0],
                        payouts=(ActionPayout("action-a", 0), ActionPayout("action-b", 0)),
                    ),
                ),
            ),
        ),
    )
    profit = problem(
        (action("contract-a", "action-a", key),),
        (state("contract-a", key, "action-a", (("a", TerminalKind.NORMAL_YES, minimum),)),),
    )

    results = (
        oracle.find_qualified(_admission_request(aggregate_cost, OracleBudget(4, 1, 1))),
        oracle.find_qualified(_admission_request(profit, OracleBudget(2, 1, 1))),
    )

    assert all(result == result_from_payload(canonical_payload(result)) for result in results)
    assert all(result.business_status == BusinessStatus.UNKNOWN for result in results)
    assert all(result.unknown_reason == UnknownReason.NUMERIC_OVERFLOW for result in results)


def test_oracle_returns_unknown_for_qualification_cross_product_overflow() -> None:
    maximum = 2**63 - 1
    key = observation()
    built = replace(
        problem(
            (action("contract-a", "action-a", key, (ExecutableCostSlice(1, 1, 0),)),),
            (state("contract-a", key, "action-a", (("a", TerminalKind.NORMAL_YES, maximum),)),),
        ),
        qualification_constraints=(
            QualificationConstraint(
                "minimum-profit",
                "v1",
                QualificationMetric.GUARANTEED_PROFIT_UNITS,
                Comparison.GREATER_THAN_OR_EQUAL,
                1,
                2,
            ),
        ),
    )

    result = oracle.solve_optimal(_optimization_request(built, OracleBudget(2, 1, 1)))

    assert result == result_from_payload(canonical_payload(result))
    assert result.business_status == BusinessStatus.UNKNOWN
    assert result.unknown_reason == UnknownReason.NUMERIC_OVERFLOW


def test_fixed_portfolio_rejects_a_nominal_spread_after_conservative_integer_rounding() -> None:
    raw_payout, raw_cost = Fraction(106, 10), Fraction(104, 10)
    key = observation()
    built = problem(
        (action("contract-a", "action-a", key, (ExecutableCostSlice(1, 1, 11),)),),
        (state("contract-a", key, "action-a", (("a", TerminalKind.NORMAL_YES, 10),)),),
    )
    built = replace(
        built,
        qualification_constraints=(
            QualificationConstraint("non-negative", "v1", QualificationMetric.GUARANTEED_PROFIT_UNITS, Comparison.GREATER_THAN_OR_EQUAL, 0, 1),
        ),
    )

    evaluation = evaluate_fixed_portfolio(built, (ActionQuantity("action-a", 1),), OracleBudget(1, 1, 1))

    assert raw_payout - raw_cost > 0
    assert evaluation.guaranteed_profit_units == -1
    assert evaluation.failed_qualification_ids == ("non-negative",)


def _two_independent_supported_portfolios() -> ArbitrageProblem:
    keys = tuple(observation(suffix) for suffix in "abcd")
    actions = tuple(action(f"contract-{suffix}", f"action-{suffix}", key) for suffix, key in zip("abcd", keys, strict=True))
    states = tuple(
        state(
            f"contract-{suffix}",
            key,
            f"action-{suffix}",
            ((f"{suffix}-yes", TerminalKind.NORMAL_YES, 0), (f"{suffix}-no", TerminalKind.NORMAL_NO, 10)),
        )
        for suffix, key in zip("ac", keys[::2], strict=True)
    ) + tuple(
        state(
            f"contract-{suffix}",
            key,
            f"action-{suffix}",
            ((f"{suffix}-yes", TerminalKind.NORMAL_YES, 10), (f"{suffix}-no", TerminalKind.NORMAL_NO, 0)),
        )
        for suffix, key in zip("bd", keys[1::2], strict=True)
    )
    return problem(
        actions,
        states,
        (
            RelationConstraint("support-a", RelationKind.IMPLIES, ("contract-a", "contract-b"), "v1"),
            RelationConstraint("support-c", RelationKind.IMPLIES, ("contract-c", "contract-d"), "v1"),
            RelationConstraint("redundant", RelationKind.IMPLIES, ("contract-a", "contract-c"), "v1"),
        ),
    )


def test_support_derivation_retains_only_needed_edges_and_splits_stably() -> None:
    built = _two_independent_supported_portfolios()
    quantities = tuple(ActionQuantity(f"action-{suffix}", 1) for suffix in "abcd")
    budget = OracleBudget(1, 16, 3)
    evaluation = evaluate_fixed_portfolio(built, quantities, budget)

    support = derive_selected_support_graph(built, evaluation, budget)
    groups = split_disconnected_solution(built, evaluation, support)

    assert support.constraint_ids == ("support-a", "support-c")
    assert support.hyperedges == (("support-a", ("contract-a", "contract-b")), ("support-c", ("contract-c", "contract-d")))
    assert groups == (
        (ActionQuantity("action-a", 1), ActionQuantity("action-b", 1)),
        (ActionQuantity("action-c", 1), ActionQuantity("action-d", 1)),
    )
    assert tuple(evaluate_fixed_portfolio(built, group, budget).payout_lower_bound_units for group in groups) == (10, 10)

    with pytest.raises(ValueError, match="disconnected portfolio requires separate evaluation"):
        build_portfolio_solution(built, evaluation, support)

    child_solutions = []
    for group in groups:
        child_evaluation = evaluate_fixed_portfolio(built, group, budget)
        child_support = derive_selected_support_graph(built, child_evaluation, budget)
        child_solutions.append(build_portfolio_solution(built, child_evaluation, child_support))
    assert tuple(solution.quantities for solution in child_solutions) == groups


def test_support_derivation_returns_exact_unknown_reason_when_rechecks_exceed_budget() -> None:
    built = _two_independent_supported_portfolios()
    evaluation = evaluate_fixed_portfolio(built, tuple(ActionQuantity(f"action-{suffix}", 1) for suffix in "abcd"), OracleBudget(1, 16, 3))

    assert derive_selected_support_graph(built, evaluation, OracleBudget(1, 16, 2)) == UnknownReason.ORACLE_SUPPORT_LIMIT_EXCEEDED


def test_support_minimization_retains_constraint_when_release_qualification_changes() -> None:
    key_a, key_b = observation("a"), observation("b")
    active_release = AS_OF + timedelta(hours=1)
    late_release = AS_OF + timedelta(days=10)
    built = problem(
        (action("contract-a", "action-a", key_a), action("contract-b", "action-b", key_b)),
        (
            state(
                "contract-a",
                key_a,
                "action-a",
                (("a-no", TerminalKind.NORMAL_NO, 10), ("a-yes", TerminalKind.NORMAL_YES, 10)),
                active_release,
            ),
            state(
                "contract-b",
                key_b,
                "action-b",
                (("b-no", TerminalKind.NORMAL_NO, 10), ("b-yes", TerminalKind.NORMAL_YES, 10)),
                active_release,
            ),
        ),
        (RelationConstraint("support", RelationKind.IMPLIES, ("contract-a", "contract-b"), "v1"),),
        forbidden=(ForbiddenAtomCombination("block-no-no", ("a-no", "b-no"), "v1"),),
    )
    built = replace(
        built,
        terminal_state_sets=(
            built.terminal_state_sets[0],
            replace(
                built.terminal_state_sets[1],
                atoms=(
                    replace(built.terminal_state_sets[1].atoms[0], capital_release_at=late_release),
                    built.terminal_state_sets[1].atoms[1],
                ),
            ),
        ),
        qualification_constraints=(
            QualificationConstraint(
                "release",
                "v1",
                QualificationMetric.MAX_CAPITAL_RELEASE_DELAY_SECONDS,
                Comparison.LESS_THAN_OR_EQUAL,
                2 * 60 * 60,
                1,
            ),
        ),
    )
    budget = OracleBudget(1, 4, 2)
    evaluation = evaluate_fixed_portfolio(
        built,
        (ActionQuantity("action-a", 1), ActionQuantity("action-b", 1)),
        budget,
    )

    assert evaluation.payout_lower_bound_units == 20
    assert evaluation.conservative_capital_release_at == active_release
    assert evaluation.failed_qualification_ids == ()
    support = derive_selected_support_graph(built, evaluation, budget)

    assert isinstance(support, SelectedSupportGraph)
    assert support.constraint_ids == ("block-no-no", "support")


def test_forbidden_combination_can_be_the_only_selected_proof_support() -> None:
    key_a, key_b = observation("a"), observation("b")
    built = problem(
        (action("contract-a", "action-a", key_a), action("contract-b", "action-b", key_b)),
        (
            state("contract-a", key_a, "action-a", (("a-yes", TerminalKind.NORMAL_YES, 0), ("a-no", TerminalKind.NORMAL_NO, 10))),
            state("contract-b", key_b, "action-b", (("b-yes", TerminalKind.NORMAL_YES, 10), ("b-no", TerminalKind.NORMAL_NO, 0))),
        ),
        forbidden=(ForbiddenAtomCombination("forbid-loss", ("a-yes", "b-no"), "v1"),),
    )
    evaluation = evaluate_fixed_portfolio(built, (ActionQuantity("action-a", 1), ActionQuantity("action-b", 1)), OracleBudget(1, 4, 1))

    support = derive_selected_support_graph(built, evaluation, OracleBudget(1, 4, 1))

    assert support.constraint_ids == ("forbid-loss",)
    assert support.hyperedges == (("forbid-loss", ("contract-a", "contract-b")),)


def test_split_keeps_actions_on_one_contract_together_without_identity_support_edges() -> None:
    key = observation()
    built = problem(
        (action("contract-a", "action-a", key), action("contract-a", "action-b", key)),
        (state("contract-a", key, "action-a", (("a", TerminalKind.NORMAL_YES, 1),)),),
    )
    built = replace(
        built,
        terminal_state_sets=(
            replace(
                built.terminal_state_sets[0],
                atoms=(replace(built.terminal_state_sets[0].atoms[0], payouts=(ActionPayout("action-a", 1), ActionPayout("action-b", 1))),),
            ),
        ),
    )
    evaluation = evaluate_fixed_portfolio(built, (ActionQuantity("action-a", 1), ActionQuantity("action-b", 1)), OracleBudget(1, 1, 1))
    support = derive_selected_support_graph(built, evaluation, OracleBudget(1, 1, 1))

    assert support.constraint_ids == ()
    assert split_disconnected_solution(built, evaluation, support) == ((ActionQuantity("action-a", 1), ActionQuantity("action-b", 1)),)
    assert build_portfolio_solution(built, evaluation, support).payout_proof.portfolio_fingerprint == fingerprint({"quantities": evaluation.quantities})


def test_transitive_support_contracts_keep_selected_actions_in_one_component() -> None:
    key_a, key_b, key_c = observation("a"), observation("b"), observation("c")
    built = problem(
        (
            action("contract-a", "action-a", key_a),
            action("contract-b", "action-b", key_b),
            action("contract-c", "action-c", key_c),
        ),
        (
            state("contract-a", key_a, "action-a", (("a-yes", TerminalKind.NORMAL_YES, 0), ("a-no", TerminalKind.NORMAL_NO, 10))),
            state("contract-b", key_b, "action-b", (("b-yes", TerminalKind.NORMAL_YES, 0), ("b-no", TerminalKind.NORMAL_NO, 0))),
            state("contract-c", key_c, "action-c", (("c-yes", TerminalKind.NORMAL_YES, 0), ("c-no", TerminalKind.NORMAL_NO, 0))),
        ),
        (
            RelationConstraint("a-implies-b", RelationKind.IMPLIES, ("contract-a", "contract-b"), "v1"),
            RelationConstraint("b-implies-c", RelationKind.IMPLIES, ("contract-b", "contract-c"), "v1"),
            RelationConstraint("b-exclusive-c", RelationKind.MUTUALLY_EXCLUSIVE, ("contract-b", "contract-c"), "v1"),
        ),
    )
    evaluation = evaluate_fixed_portfolio(built, (ActionQuantity("action-a", 1), ActionQuantity("action-c", 1)), OracleBudget(1, 8, 3))

    support = derive_selected_support_graph(built, evaluation, OracleBudget(1, 8, 3))

    assert support.constraint_ids == ("a-implies-b", "b-exclusive-c", "b-implies-c")
    assert split_disconnected_solution(built, evaluation, support) == ((ActionQuantity("action-a", 1), ActionQuantity("action-c", 1)),)


def test_solution_builder_does_not_reenumerate_a_huge_bounded_evaluation() -> None:
    indexes = tuple(range(30))
    keys = tuple(observation(str(index)) for index in indexes)
    built = problem(
        tuple(action(f"contract-{index}", f"action-{index}", key) for index, key in zip(indexes, keys, strict=True)),
        tuple(
            state(
                f"contract-{index}",
                key,
                f"action-{index}",
                ((f"atom-{index}-yes", TerminalKind.NORMAL_YES, 10 if index == 0 else 0), (f"atom-{index}-no", TerminalKind.NORMAL_NO, 10 if index == 0 else 0)),
            )
            for index, key in zip(indexes, keys, strict=True)
        ),
    )
    scenario = SettlementScenario(tuple(SelectedAtom(f"contract-{index}", f"atom-{index}-yes") for index in indexes))
    quantities = (ActionQuantity("action-0", 1),)
    evaluation = PortfolioEvaluation(quantities, 10, 1, 9, scenario, cut_from_scenario(built, scenario), AS_OF, ())
    support = SelectedSupportGraph(("action-0",), ("contract-0",), (), ())

    solution = build_portfolio_solution(built, evaluation, support)

    assert solution.quantities == quantities


def _admission_request(built: ArbitrageProblem, budget: OracleBudget) -> OracleRequest:
    return OracleRequest("open_trader.prediction_n_leg.request.v1", SearchMode.ADMISSION, built, budget)


def _optimization_request(built: ArbitrageProblem, budget: OracleBudget) -> OracleRequest:
    return OracleRequest("open_trader.prediction_n_leg.request.v1", SearchMode.OPTIMIZATION, built, budget)


def _minimum_profit(units: int) -> QualificationConstraint:
    return QualificationConstraint(
        "minimum-profit",
        "v1",
        QualificationMetric.GUARANTEED_PROFIT_UNITS,
        Comparison.GREATER_THAN_OR_EQUAL,
        units,
        1,
    )


def test_admission_chooses_the_first_stable_integer_vector_without_claiming_global_optimum() -> None:
    key_a, key_z = observation("a"), observation("z")
    built = replace(
        problem(
            (action("contract-z", "action-z", key_z), action("contract-a", "action-a", key_a)),
            (
                state("contract-z", key_z, "action-z", (("z", TerminalKind.NORMAL_YES, 3),)),
                state("contract-a", key_a, "action-a", (("a", TerminalKind.NORMAL_YES, 21),)),
            ),
        ),
        qualification_constraints=(_minimum_profit(1),),
    )

    result = oracle.find_qualified(_admission_request(built, OracleBudget(4, 1, 1)))

    assert oracle.quantity_vector_count(built) == 4
    assert result.solve_status == SolveStatus.FEASIBLE
    assert result.proof_status == ProofStatus.PROVEN
    assert result.business_status == BusinessStatus.QUALIFIED_FEASIBLE
    assert result.optimality_status == OptimalityStatus.NOT_PROVEN
    assert result.objective_bounds.lower_bound_units == 2
    assert result.objective_bounds.upper_bound_units is None
    assert result.objective_bounds.gap_units is None
    assert result.objective_bounds.closed is False
    assert result.solution is not None
    assert result.solution.quantities == (ActionQuantity("action-z", 1),)


def test_admission_rechecks_disconnected_parts_and_continues_to_a_later_vector() -> None:
    key_a, key_b = observation("a"), observation("b")
    built = replace(
        problem(
            (
                action("contract-a", "action-a", key_a, (ExecutableCostSlice(1, 2, 1),)),
                action("contract-b", "action-b", key_b),
            ),
            (
                state("contract-a", key_a, "action-a", (("a", TerminalKind.NORMAL_YES, 3),)),
                state("contract-b", key_b, "action-b", (("b", TerminalKind.NORMAL_YES, 3),)),
            ),
        ),
        qualification_constraints=(_minimum_profit(3),),
    )

    result = oracle.find_qualified(_admission_request(built, OracleBudget(6, 1, 1)))

    assert result.solution is not None
    assert result.solution.quantities == (ActionQuantity("action-a", 2),)
    assert result.solution.payout_proof.guaranteed_profit_units == 4


def test_admission_splits_identity_only_actions_and_rechecks_each_child() -> None:
    key = observation("shared")
    built = replace(
        problem(
            (action("contract-a", "action-a", key), action("contract-b", "action-b", key)),
            (
                state("contract-a", key, "action-a", (("a", TerminalKind.NORMAL_YES, 3),)),
                state("contract-b", key, "action-b", (("b", TerminalKind.NORMAL_YES, 3),)),
            ),
        ),
        qualification_constraints=(_minimum_profit(3),),
    )

    result = oracle.find_qualified(_admission_request(built, OracleBudget(4, 1, 1)))

    assert result.business_status == BusinessStatus.NO_QUALIFIED_OPPORTUNITY
    assert result.solution is None
    assert result.negative_proof is not None
    assert result.negative_proof.rejection_counts == (("ALL_ZERO", 1), ("minimum-profit", 3))


def test_disconnected_qualified_portfolio_returns_its_first_qualified_part() -> None:
    key_a, key_b = observation("a"), observation("b")
    built = replace(
        problem(
            (action("contract-a", "action-a", key_a), action("contract-b", "action-b", key_b)),
            (
                state("contract-a", key_a, "action-a", (("a", TerminalKind.NORMAL_YES, 3),)),
                state("contract-b", key_b, "action-b", (("b", TerminalKind.NORMAL_YES, 3),)),
            ),
        ),
        qualification_constraints=(_minimum_profit(1),),
    )
    budget = OracleBudget(4, 1, 1)
    evaluation = evaluate_fixed_portfolio(built, (ActionQuantity("action-a", 1), ActionQuantity("action-b", 1)), budget)

    solution = oracle._connected_qualified_solution(built, evaluation, budget, set())

    assert solution is not None
    assert not isinstance(solution, UnknownReason)
    assert solution.quantities == (ActionQuantity("action-a", 1),)


def test_admission_emits_replayable_exhaustive_negative_proof_only_after_full_exhaustion() -> None:
    key = observation()
    built = replace(
        problem(
            (action("contract-a", "action-a", key),),
            (state("contract-a", key, "action-a", (("a", TerminalKind.NORMAL_YES, 1),)),),
        ),
        qualification_constraints=(_minimum_profit(1),),
    )
    request = _admission_request(built, OracleBudget(2, 1, 1))

    result = oracle.find_qualified(request)
    replay = oracle.find_qualified(request)

    assert result.solve_status == SolveStatus.INFEASIBLE
    assert result.proof_status == ProofStatus.PROVEN
    assert result.business_status == BusinessStatus.NO_QUALIFIED_OPPORTUNITY
    assert result.negative_proof is not None
    assert result.negative_proof.proof_method == "EXHAUSTIVE_ORACLE_V1"
    assert result.negative_proof.request_fingerprint == fingerprint(request)
    assert result.negative_proof.problem_fingerprint == fingerprint(built)
    assert result.negative_proof.qualification_fingerprint == fingerprint({"qualification_constraints": built.qualification_constraints})
    assert result.negative_proof.quantity_vectors_total == 2
    assert result.negative_proof.quantity_vectors_examined == 2
    assert result.negative_proof.joint_states_per_vector == 1
    assert result.negative_proof.rejection_counts == (("ALL_ZERO", 1), ("minimum-profit", 1))
    assert replay.negative_proof == result.negative_proof
    assert fingerprint(replay.negative_proof) == fingerprint(result.negative_proof)

    limited = oracle.find_qualified(_admission_request(built, OracleBudget(1, 1, 1)))

    assert limited.business_status == BusinessStatus.UNKNOWN
    assert limited.unknown_reason == UnknownReason.ORACLE_DECISION_LIMIT_EXCEEDED
    assert limited.negative_proof is None

    state_limited = oracle.find_qualified(
        _admission_request(
            replace(
                built,
                terminal_state_sets=(
                    replace(
                        built.terminal_state_sets[0],
                        atoms=(
                            built.terminal_state_sets[0].atoms[0],
                            replace(built.terminal_state_sets[0].atoms[0], atom_id="a-other"),
                        ),
                    ),
                ),
            ),
            OracleBudget(2, 1, 1),
        )
    )

    assert state_limited.business_status == BusinessStatus.UNKNOWN
    assert state_limited.unknown_reason == UnknownReason.ORACLE_STATE_LIMIT_EXCEEDED
    assert state_limited.negative_proof is None


def test_negative_proof_rejection_counts_deduplicate_disconnected_children_per_vector() -> None:
    key_a, key_b = observation("a"), observation("b")
    built = replace(
        problem(
            (action("contract-a", "action-a", key_a), action("contract-b", "action-b", key_b)),
            (
                state("contract-a", key_a, "action-a", (("a", TerminalKind.NORMAL_YES, 3),)),
                state("contract-b", key_b, "action-b", (("b", TerminalKind.NORMAL_YES, 3),)),
            ),
        ),
        qualification_constraints=(_minimum_profit(3),),
    )

    result = oracle.find_qualified(_admission_request(built, OracleBudget(4, 1, 1)))

    assert result.business_status == BusinessStatus.NO_QUALIFIED_OPPORTUNITY
    assert result.negative_proof is not None
    assert result.negative_proof.quantity_vectors_total == 4
    assert result.negative_proof.quantity_vectors_examined == 4
    assert result.negative_proof.rejection_counts == (("ALL_ZERO", 1), ("minimum-profit", 3))


def test_admission_rejects_non_admission_modes_as_invalid_model() -> None:
    key = observation()
    built = problem((action("contract-a", "action-a", key),), (state("contract-a", key, "action-a", (("a", TerminalKind.NORMAL_YES, 2),)),))

    result = oracle.find_qualified(OracleRequest("open_trader.prediction_n_leg.request.v1", SearchMode.OPTIMIZATION, built, OracleBudget(2, 1, 1)))

    assert result.business_status == BusinessStatus.UNKNOWN
    assert result.unknown_reason == UnknownReason.INVALID_MODEL


def test_optimal_finds_the_global_profit_maximum_after_admission_stops() -> None:
    key = observation()
    built = replace(
        problem(
            (action("contract-a", "action-a", key, (ExecutableCostSlice(1, 2, 1),)),),
            (state("contract-a", key, "action-a", (("a", TerminalKind.NORMAL_YES, 11),)),),
        ),
        qualification_constraints=(_minimum_profit(1),),
    )

    admission = oracle.find_qualified(_admission_request(built, OracleBudget(3, 1, 1)))
    optimal = oracle.solve_optimal(_optimization_request(built, OracleBudget(3, 1, 1)))

    assert admission.business_status == BusinessStatus.QUALIFIED_FEASIBLE
    assert admission.optimality_status == OptimalityStatus.NOT_PROVEN
    assert admission.objective_bounds.closed is False
    assert admission.solution is not None
    assert admission.solution.payout_proof.guaranteed_profit_units == 10
    assert optimal.business_status == BusinessStatus.QUALIFIED_FEASIBLE
    assert optimal.optimality_status == OptimalityStatus.OPTIMAL
    assert optimal.objective_bounds == oracle.ObjectiveBounds(20, 20, 0, True)
    assert optimal.solution is not None
    assert optimal.solution.payout_proof.guaranteed_profit_units == 20


def test_optimal_tie_break_prefers_lower_cost_for_equal_profit() -> None:
    key_a, key_z = observation("a"), observation("z")
    built = problem(
        (
            action("contract-z", "action-z", key_z, (ExecutableCostSlice(1, 1, 2),)),
            action("contract-a", "action-a", key_a, (ExecutableCostSlice(1, 1, 1),)),
        ),
        (
            state("contract-z", key_z, "action-z", (("z", TerminalKind.NORMAL_YES, 12),)),
            state("contract-a", key_a, "action-a", (("a", TerminalKind.NORMAL_YES, 11),)),
        ),
    )
    built = replace(built, qualification_constraints=(_minimum_profit(10),))

    result = oracle.solve_optimal(_optimization_request(built, OracleBudget(4, 1, 1)))

    assert result.solution is not None
    assert result.solution.quantities == (ActionQuantity("action-a", 1),)
    assert result.solution.payout_proof.guaranteed_profit_units == 10
    assert result.solution.payout_proof.cost_upper_bound_units == 1


def test_optimal_tie_break_prefers_fewer_legs_after_profit_and_cost() -> None:
    key_a, shared = observation("a"), observation("shared")
    built = replace(
        problem(
            (
                action("contract-a", "action-a", key_a, (ExecutableCostSlice(1, 1, 2),)),
                action("contract-b", "action-b", shared),
                action("contract-z", "action-z", shared),
            ),
            (
                state("contract-a", key_a, "action-a", (("a", TerminalKind.NORMAL_YES, 12),)),
                state("contract-b", shared, "action-b", (("b", TerminalKind.NORMAL_YES, 6),)),
                state("contract-z", shared, "action-z", (("z", TerminalKind.NORMAL_YES, 6),)),
            ),
        ),
        qualification_constraints=(_minimum_profit(10),),
    )

    result = oracle.solve_optimal(_optimization_request(built, OracleBudget(8, 1, 1)))

    assert result.solution is not None
    assert result.solution.quantities == (ActionQuantity("action-a", 1),)
    assert result.solution.payout_proof.guaranteed_profit_units == 10
    assert result.solution.payout_proof.cost_upper_bound_units == 2


def test_optimal_tie_break_uses_stable_action_quantities_last() -> None:
    key_a, key_z = observation("a"), observation("z")
    built = replace(
        problem(
            (
                action("contract-z", "action-z", key_z),
                action("contract-a", "action-a", key_a),
            ),
            (
                state("contract-z", key_z, "action-z", (("z", TerminalKind.NORMAL_YES, 11),)),
                state("contract-a", key_a, "action-a", (("a", TerminalKind.NORMAL_YES, 11),)),
            ),
        ),
        qualification_constraints=(_minimum_profit(10),),
    )

    result = oracle.solve_optimal(_optimization_request(built, OracleBudget(4, 1, 1)))

    assert result.solution is not None
    assert result.solution.quantities == (ActionQuantity("action-a", 1),)


def test_optimal_is_unknown_when_budget_cannot_close_even_after_admission_proves_a_solution() -> None:
    key = observation()
    built = replace(
        problem(
            (action("contract-a", "action-a", key, (ExecutableCostSlice(1, 2, 1),)),),
            (state("contract-a", key, "action-a", (("a", TerminalKind.NORMAL_YES, 11),)),),
        ),
        qualification_constraints=(_minimum_profit(1),),
    )

    admission = oracle.find_qualified(_admission_request(built, OracleBudget(3, 1, 1)))
    incomplete = oracle.solve_optimal(_optimization_request(built, OracleBudget(2, 1, 1)))

    assert admission.business_status == BusinessStatus.QUALIFIED_FEASIBLE
    assert incomplete.business_status == BusinessStatus.UNKNOWN
    assert incomplete.optimality_status != OptimalityStatus.OPTIMAL
    assert incomplete.unknown_reason == UnknownReason.ORACLE_DECISION_LIMIT_EXCEEDED


def test_optimal_rejects_non_optimization_mode_and_proves_exhaustive_no_qualification() -> None:
    key = observation()
    built = replace(
        problem(
            (action("contract-a", "action-a", key),),
            (state("contract-a", key, "action-a", (("a", TerminalKind.NORMAL_YES, 1),)),),
        ),
        qualification_constraints=(_minimum_profit(1),),
    )

    invalid = oracle.solve_optimal(_admission_request(built, OracleBudget(2, 1, 1)))
    exhausted = oracle.solve_optimal(_optimization_request(built, OracleBudget(2, 1, 1)))

    assert invalid.business_status == BusinessStatus.UNKNOWN
    assert invalid.unknown_reason == UnknownReason.INVALID_MODEL
    assert exhausted.business_status == BusinessStatus.NO_QUALIFIED_OPPORTUNITY
    assert exhausted.negative_proof is not None
    proof = exhausted.negative_proof
    request = _optimization_request(built, OracleBudget(2, 1, 1))
    assert proof.proof_method == "EXHAUSTIVE_ORACLE_V1"
    assert proof.conclusion == BusinessStatus.NO_QUALIFIED_OPPORTUNITY
    assert proof.request_fingerprint == fingerprint(request)
    assert proof.problem_fingerprint == fingerprint(built)
    assert proof.source_problem_fingerprint is None
    assert proof.qualification_fingerprint == fingerprint({"qualification_constraints": built.qualification_constraints})
    assert proof.quantity_vectors_total == proof.quantity_vectors_examined == 2
    assert proof.joint_states_per_vector == 1
    assert proof.rejection_counts == (("ALL_ZERO", 1), ("minimum-profit", 1))


@pytest.mark.parametrize("payout", [1, 0])
def test_raw_arbitrage_proves_no_arbitrage_only_when_global_profit_is_not_positive(payout: int) -> None:
    key = observation()
    built = problem(
        (action("contract-a", "action-a", key),),
        (state("contract-a", key, "action-a", (("a", TerminalKind.NORMAL_YES, payout),)),),
    )

    result = oracle.diagnose_raw_arbitrage(built, OracleBudget(2, 1, 1))

    assert result.business_status == BusinessStatus.NO_ARBITRAGE
    assert result.objective_bounds == oracle.ObjectiveBounds(payout - 1, payout - 1, 0, True)
    assert result.negative_proof is not None
    assert result.negative_proof.proof_method == "EXHAUSTIVE_ORACLE_V1"
    assert result.negative_proof.problem_fingerprint != fingerprint(built)
    diagnostic_problem = replace(built, problem_id=f"{built.problem_id}:raw-arbitrage-diagnostic", qualification_constraints=())
    assert result.negative_proof.problem_fingerprint == fingerprint(diagnostic_problem)
    assert result.negative_proof.request_fingerprint == fingerprint(
        OracleRequest(
            "open_trader.prediction_n_leg.request.v1",
            SearchMode.RAW_ARBITRAGE_DIAGNOSTIC,
            diagnostic_problem,
            OracleBudget(2, 1, 1),
        )
    )
    assert result.negative_proof.source_problem_fingerprint == fingerprint(built)
    assert result.negative_proof.quantity_vectors_total == result.negative_proof.quantity_vectors_examined == 2


def test_raw_arbitrage_keeps_positive_raw_profit_separate_from_admission_and_is_never_automatic(monkeypatch: pytest.MonkeyPatch) -> None:
    key = observation()
    built = replace(
        problem(
            (action("contract-a", "action-a", key),),
            (state("contract-a", key, "action-a", (("a", TerminalKind.NORMAL_YES, 11),)),),
        ),
        qualification_constraints=(_minimum_profit(11),),
    )
    monkeypatch.setattr(oracle, "diagnose_raw_arbitrage", lambda *_: (_ for _ in ()).throw(AssertionError("must be explicit")), raising=False)

    admission = oracle.find_qualified(_admission_request(built, OracleBudget(2, 1, 1)))
    monkeypatch.undo()
    diagnostic = oracle.diagnose_raw_arbitrage(built, OracleBudget(2, 1, 1))

    assert admission.business_status == BusinessStatus.NO_QUALIFIED_OPPORTUNITY
    assert diagnostic.business_status == BusinessStatus.QUALIFIED_FEASIBLE
    assert diagnostic.business_status != BusinessStatus.NO_ARBITRAGE
    assert diagnostic.solution is not None
    assert diagnostic.solution.payout_proof.guaranteed_profit_units == 10


def test_raw_arbitrage_returns_unknown_when_its_derived_optimization_cannot_close() -> None:
    key = observation()
    built = problem(
        (action("contract-a", "action-a", key, (ExecutableCostSlice(1, 2, 1),)),),
        (state("contract-a", key, "action-a", (("a", TerminalKind.NORMAL_YES, 11),)),),
    )

    result = oracle.diagnose_raw_arbitrage(built, OracleBudget(2, 1, 1))

    assert result.business_status == BusinessStatus.UNKNOWN
    assert result.unknown_reason == UnknownReason.ORACLE_DECISION_LIMIT_EXCEEDED


def test_oracle_classifies_incomplete_terminal_data_and_cross_asset_valuation_as_unknown() -> None:
    key = observation()
    complete = problem(
        (action("contract-a", "action-a", key),),
        (state("contract-a", key, "action-a", (("a", TerminalKind.NORMAL_YES, 1),)),),
    )
    terminal_data_missing = replace(
        complete,
        terminal_state_sets=(
            replace(
                complete.terminal_state_sets[0],
                atoms=(replace(complete.terminal_state_sets[0].atoms[0], payouts=()),),
            ),
        ),
    )
    cross_asset = replace(
        complete,
        actions=(replace(complete.actions[0], settlement_asset_id="eur", asset_valuation_rule_id="eur-usd-v1"),),
    )

    assert oracle.find_qualified(_admission_request(terminal_data_missing, OracleBudget(1, 1, 1))).unknown_reason == UnknownReason.UNKNOWN_TERMINAL_DATA
    assert oracle.find_qualified(_admission_request(cross_asset, OracleBudget(1, 1, 1))).unknown_reason == UnknownReason.UNKNOWN_VALUATION


@pytest.mark.parametrize("mode", (SearchMode.ADMISSION, SearchMode.OPTIMIZATION, SearchMode.RAW_ARBITRAGE_DIAGNOSTIC))
def test_oracle_rejects_malformed_action_containers_without_dereferencing_them(mode: SearchMode) -> None:
    key = observation()
    complete = problem(
        (action("contract-a", "action-a", key),),
        (state("contract-a", key, "action-a", (("a", TerminalKind.NORMAL_YES, 1),)),),
    )
    malformed = replace(complete, actions=None)
    request = OracleRequest("open_trader.prediction_n_leg.request.v1", mode, malformed, OracleBudget(1, 1, 1))

    result = (
        oracle.diagnose_raw_arbitrage(malformed, request.budget)
        if mode == SearchMode.RAW_ARBITRAGE_DIAGNOSTIC
        else oracle.find_qualified(request)
        if mode == SearchMode.ADMISSION
        else oracle.solve_optimal(request)
    )

    assert result.business_status == BusinessStatus.UNKNOWN
    assert result.unknown_reason == UnknownReason.INVALID_MODEL
