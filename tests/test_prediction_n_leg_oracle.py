from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from open_trader.prediction_n_leg import (
    ActionPayout,
    ActionSide,
    ArbitrageProblem,
    CandidateAction,
    ConstraintModel,
    ExecutableCostSlice,
    ForbiddenAtomCombination,
    OracleBudget,
    RelationConstraint,
    RelationKind,
    SelectedAtom,
    SettlementObservationKey,
    SettlementScenario,
    TerminalAtom,
    TerminalKind,
    TerminalStateSet,
    UnknownReason,
    fingerprint,
)
from open_trader.prediction_n_leg_oracle import (
    RelationComponent,
    build_relation_components,
    cut_from_scenario,
    enumerate_allowed_scenarios,
)


AS_OF = datetime(2026, 8, 12, tzinfo=UTC)


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


def action(contract_id: str, action_id: str, key: SettlementObservationKey) -> CandidateAction:
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
        (ExecutableCostSlice(1, 1, 1),),
    )


def state(
    contract_id: str,
    key: SettlementObservationKey,
    action_id: str,
    atoms: tuple[tuple[str, TerminalKind, int], ...],
) -> TerminalStateSet:
    return TerminalStateSet(
        contract_id,
        key,
        "v1",
        tuple(
            TerminalAtom(atom_id, kind, "v1", (ActionPayout(action_id, payout),), AS_OF)
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
