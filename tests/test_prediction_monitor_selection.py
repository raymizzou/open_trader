"""Issue #77 step 2: relation generation -> canonical N-leg components."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from open_trader.prediction_monitor_selection import relation_generation_components
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
)
from open_trader.prediction_n_leg_oracle import RelationComponent


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
