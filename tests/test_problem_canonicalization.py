"""Issue #111: canonical direction-aware action identity for IMPLIES problems.

A threshold IMPLIES problem used to compile one action per contract whose id
was only ``{venue}:{contract}``; the relation direction lived solely in the
action's ``side``. Chained families therefore assigned two different sides to
the same action id, and the merge seam (``_merge_one``) fail-closed the whole
group. The fix makes the action identity ``{venue}:{contract}:{side}``:
``canonicalize_directional_actions`` is the one pure normalization shared by
the threshold compiler and the read path, legacy stored payloads upgrade
lazily at decode time, and EXACTLY_ONE-class problems (whose actions are
already distinct contracts, or a single BUY_YES per market) pass through
unchanged.

Payout semantics are the Polymarket settlement truth: a market settling YES
pays 1 USD per lot to YES tokens and 0 to NO tokens; settling NO is symmetric;
VOID pays 0 to both.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from open_trader.prediction_n_leg import (
    OBSERVATION_SCHEMA_V1,
    PROBLEM_SCHEMA_V1,
    ActionPayout,
    ActionSide,
    ArbitrageProblem,
    CandidateAction,
    ConstraintModel,
    ExecutableCostSlice,
    RelationConstraint,
    RelationKind,
    SettlementObservationKey,
    TerminalAtom,
    TerminalKind,
    TerminalStateSet,
    canonical_payload,
    canonicalize_directional_actions,
    validate_problem,
)

AS_OF = datetime(2026, 12, 31, 23, 59, tzinfo=UTC)
RELEASE_A = datetime(2027, 1, 1, 0, 0, tzinfo=UTC)
RELEASE_B = datetime(2027, 1, 2, 0, 0, tzinfo=UTC)


def observation_key(indicator_id: str, rule: str) -> SettlementObservationKey:
    return SettlementObservationKey(
        OBSERVATION_SCHEMA_V1,
        "Binance",
        indicator_id,
        AS_OF,
        AS_OF,
        "UTC",
        rule,
    )


def threshold_action(
    contract_id: str,
    side: ActionSide,
    rule: str,
) -> CandidateAction:
    """One legacy threshold action: the pre-#111 ``{venue}:{contract}`` id."""
    return CandidateAction(
        f"polymarket:{contract_id}",
        venue_id="polymarket",
        account_id="catalog-v2",
        chain_id="polymarket",
        market_contract_id=contract_id,
        settlement_observation_key=observation_key(contract_id, rule),
        side=side,
        lot_step_units=1,
        quantity_scale=1,
        min_quantity_lots=1,
        max_quantity_lots=1,
        settlement_asset_id="USD",
        valuation_unit_id="USD",
        asset_valuation_rule_id="usd-1:1-v1",
        cost_slices=(ExecutableCostSlice(1, 1, 0),),
    )


def legacy_terminal_state(
    contract_id: str,
    side: ActionSide,
    rule: str,
    release_at: datetime,
) -> TerminalStateSet:
    """One legacy threshold state set: single-payout atoms keyed by the old id."""
    action_id = f"polymarket:{contract_id}"
    yes_payout = 1 if side == ActionSide.BUY_YES else 0
    no_payout = 0 if side == ActionSide.BUY_YES else 1
    return TerminalStateSet(
        contract_id,
        observation_key(contract_id, rule),
        rule,
        (
            TerminalAtom(
                f"{contract_id}:NORMAL_YES",
                TerminalKind.NORMAL_YES,
                rule,
                (ActionPayout(action_id, yes_payout),),
                release_at,
            ),
            TerminalAtom(
                f"{contract_id}:NORMAL_NO",
                TerminalKind.NORMAL_NO,
                rule,
                (ActionPayout(action_id, no_payout),),
                release_at,
            ),
            TerminalAtom(
                f"{contract_id}:VOID",
                TerminalKind.VOID,
                rule,
                (ActionPayout(action_id, 0),),
                release_at,
            ),
        ),
    )


def legacy_implies_problem() -> ArbitrageProblem:
    """A legacy threshold-shaped IMPLIES problem over two contracts.

    Mirrors ``relation_catalog._threshold_complete_model`` before #111: one
    ``polymarket:{contract}`` action and one single-payout state set per
    contract, with the direction carried only by the sides.
    """
    return ArbitrageProblem(
        PROBLEM_SCHEMA_V1,
        "threshold:issue-111",
        AS_OF,
        "USD",
        (
            threshold_action("condition-a", ActionSide.BUY_YES, "rules-a"),
            threshold_action("condition-b", ActionSide.BUY_NO, "rules-b"),
        ),
        (
            legacy_terminal_state("condition-a", ActionSide.BUY_YES, "rules-a", RELEASE_A),
            legacy_terminal_state("condition-b", ActionSide.BUY_NO, "rules-b", RELEASE_B),
        ),
        ConstraintModel(
            (
                RelationConstraint(
                    "imply:condition-a->condition-b",
                    RelationKind.IMPLIES,
                    ("condition-a", "condition-b"),
                    "digest-issue-111",
                ),
            ),
            (),
        ),
        (),
    )


class TestCanonicalizeDirectionalActions:
    def test_legacy_single_action_mirrors_to_canonical_pair(self) -> None:
        problem = legacy_implies_problem()

        result = canonicalize_directional_actions(problem)

        # Every contract gains the canonical dual action identity
        # {venue}:{contract}:{BUY_YES|BUY_NO}; the actions stay sorted by id.
        assert [action.action_id for action in result.actions] == [
            "polymarket:condition-a:BUY_NO",
            "polymarket:condition-a:BUY_YES",
            "polymarket:condition-b:BUY_NO",
            "polymarket:condition-b:BUY_YES",
        ]
        original_by_contract = {
            action.market_contract_id: action for action in problem.actions
        }
        for action in result.actions:
            original = original_by_contract[action.market_contract_id]
            # Only action_id and side may differ from the mirrored original.
            for field in (
                "venue_id",
                "account_id",
                "chain_id",
                "market_contract_id",
                "settlement_observation_key",
                "lot_step_units",
                "quantity_scale",
                "min_quantity_lots",
                "max_quantity_lots",
                "settlement_asset_id",
                "valuation_unit_id",
                "asset_valuation_rule_id",
                "cost_slices",
            ):
                assert getattr(action, field) == getattr(original, field), field
        by_id = {action.action_id: action for action in result.actions}
        for contract_id in ("condition-a", "condition-b"):
            assert (
                by_id[f"polymarket:{contract_id}:BUY_YES"].side == ActionSide.BUY_YES
            )
            assert by_id[f"polymarket:{contract_id}:BUY_NO"].side == ActionSide.BUY_NO

        # Atoms keep their identity but carry the canonical dual payouts:
        # NORMAL_YES pays (yes=1, no=0), NORMAL_NO (0, 1), VOID (0, 0).
        states_by_contract = {
            state.market_contract_id: state for state in result.terminal_state_sets
        }
        assert set(states_by_contract) == {"condition-a", "condition-b"}
        expected_payouts = {
            TerminalKind.NORMAL_YES: (1, 0),
            TerminalKind.NORMAL_NO: (0, 1),
            TerminalKind.VOID: (0, 0),
        }
        for contract_id, release_at in (
            ("condition-a", RELEASE_A),
            ("condition-b", RELEASE_B),
        ):
            state = states_by_contract[contract_id]
            assert state.settlement_observation_key == observation_key(
                contract_id, f"rules-{contract_id[-1]}"
            )
            assert state.rule_version == f"rules-{contract_id[-1]}"
            yes_id = f"polymarket:{contract_id}:BUY_YES"
            no_id = f"polymarket:{contract_id}:BUY_NO"
            assert [(atom.atom_id, atom.kind) for atom in state.atoms] == [
                (f"{contract_id}:NORMAL_YES", TerminalKind.NORMAL_YES),
                (f"{contract_id}:NORMAL_NO", TerminalKind.NORMAL_NO),
                (f"{contract_id}:VOID", TerminalKind.VOID),
            ]
            for atom in state.atoms:
                assert atom.rule_version == state.rule_version
                assert atom.capital_release_at == release_at
                yes_units, no_units = expected_payouts[atom.kind]
                assert atom.payouts == (
                    ActionPayout(yes_id, yes_units),
                    ActionPayout(no_id, no_units),
                )

        # The constraint model and every other problem field stay untouched.
        assert result.constraint_model == problem.constraint_model
        assert result.schema_version == problem.schema_version
        assert result.problem_id == problem.problem_id
        assert result.as_of == problem.as_of
        assert result.valuation_unit_id == problem.valuation_unit_id
        assert result.qualification_constraints == problem.qualification_constraints

    def test_canonicalization_is_idempotent(self) -> None:
        problem = legacy_implies_problem()

        once = canonicalize_directional_actions(problem)
        twice = canonicalize_directional_actions(once)

        assert canonical_payload(twice) == canonical_payload(once)

    def test_exactly_one_problem_passes_through_unchanged(self) -> None:
        # Mechanical EXACTLY_ONE shape (mirrors _mechanical_complete_model):
        # one BUY_YES action per contract, single-payout atoms.
        problem = ArbitrageProblem(
            PROBLEM_SCHEMA_V1,
            "exactly-one:issue-111",
            AS_OF,
            "USD",
            (
                threshold_action("condition-a", ActionSide.BUY_YES, "rules-a"),
                threshold_action("condition-b", ActionSide.BUY_YES, "rules-b"),
            ),
            (
                legacy_terminal_state("condition-a", ActionSide.BUY_YES, "rules-a", RELEASE_A),
                legacy_terminal_state("condition-b", ActionSide.BUY_YES, "rules-b", RELEASE_B),
            ),
            ConstraintModel(
                (
                    RelationConstraint(
                        "exactly-one:condition-a:condition-b",
                        RelationKind.EXACTLY_ONE,
                        ("condition-a", "condition-b"),
                        "digest-issue-111",
                    ),
                ),
                (),
            ),
            (),
        )

        result = canonicalize_directional_actions(problem)

        assert result is problem
        assert canonical_payload(result) == canonical_payload(problem)

    def test_extra_action_without_canonical_direction_pair_fails_closed(self) -> None:
        problem = legacy_implies_problem()
        crowded = replace(
            problem,
            actions=problem.actions
            + (
                replace(
                    threshold_action("condition-a", ActionSide.BUY_YES, "rules-a"),
                    action_id="polymarket:condition-a-copy",
                ),
            ),
        )

        with pytest.raises(ValueError, match="condition-a"):
            canonicalize_directional_actions(crowded)

    def test_two_actions_without_direction_suffixes_fail_closed(self) -> None:
        problem = legacy_implies_problem()
        unsuffixed = replace(
            problem,
            actions=(
                replace(
                    threshold_action("condition-a", ActionSide.BUY_YES, "rules-a"),
                    action_id="polymarket:condition-a-yes",
                ),
                replace(
                    threshold_action("condition-a", ActionSide.BUY_NO, "rules-b"),
                    action_id="polymarket:condition-a-no",
                ),
                threshold_action("condition-b", ActionSide.BUY_NO, "rules-b"),
            ),
        )

        with pytest.raises(ValueError, match="condition-a"):
            canonicalize_directional_actions(unsuffixed)


def test_canonical_dual_action_problem_validates() -> None:
    """T5d: a canonical dual-action IMPLIES problem (2 contracts, 4 actions)
    satisfies every structural rule, so it flows through decode, merge and
    the solver unchanged."""
    problem = canonicalize_directional_actions(legacy_implies_problem())

    assert len(problem.actions) == 4
    assert {action.market_contract_id for action in problem.actions} == {
        "condition-a",
        "condition-b",
    }
    assert validate_problem(problem) == ()
