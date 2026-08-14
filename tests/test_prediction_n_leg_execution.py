from __future__ import annotations

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor

import pytest

from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from open_trader.prediction_executable_cost import ExecutionLegEvidence, ExecutionSolution
from open_trader.prediction_n_leg import ActionQuantity, fingerprint
from open_trader.prediction_n_leg_execution import (
    NLegExecutionService,
    OrderReceipt,
    PartialFillProofRecord,
    execution_solution_binding,
    order_receipt_from_payload,
)


def test_order_receipt_is_a_strict_cumulative_contract() -> None:
    receipt = OrderReceipt(
        receipt_id="receipt-1",
        execution_batch_id="batch-1",
        client_order_id="client-1",
        venue_id="venue-a",
        account_id="account-a",
        venue_order_id=None,
        submitted_quantity=10,
        cumulative_filled_quantity=4,
        cumulative_fee_units=2,
        state="OPEN",
        sequence=7,
        rest_confirmed=False,
        observed_at="2026-08-15T00:00:00Z",
        venue_timestamp="2026-08-15T00:00:00Z",
    )

    assert order_receipt_from_payload(receipt.to_payload()) == receipt
    with pytest.raises(ValueError, match="unexpected"):
        order_receipt_from_payload({**receipt.to_payload(), "extra": True})


def solution() -> ExecutionSolution:
    legs = (
        ExecutionLegEvidence("a", "venue-a", "account-a", "chain-a", "asset-a", "BUY_YES", 10, 1, 1, 0, 40, "usd", "usd", "quote-a"),
        ExecutionLegEvidence("b", "venue-b", "account-b", "chain-b", "asset-b", "BUY_NO", 10, 1, 1, 0, 60, "usd", "usd", "quote-b"),
    )
    quantities = (ActionQuantity("a", 10), ActionQuantity("b", 10))
    identity = fingerprint({"market": "market", "account": "account", "quantities": quantities, "capital_use_units": 100, "execution_legs": legs})
    return ExecutionSolution("market", "account", quantities, 100, legs, False, "PARTIAL_FILL_PROOF_REQUIRED", identity)


def proof(
    execution: ExecutionSolution, *, repair_cap: int = 10, partial_cap: int = 100,
    status: str = "PARTIAL_FILL_SAFE", verifier_status: str = "QUALIFIED_VERIFIED",
) -> PartialFillProofRecord:
    values: dict[str, object] = {
        **execution_solution_binding(execution),
        "cap_config_version": "caps-v1",
        "max_partial_fill_loss": partial_cap,
        "max_auto_repair_loss": repair_cap,
        "solver_lower_bound": 0,
        "solver_upper_bound": 100,
        "solver_termination": "CLOSED",
        "solver_evidence_fingerprint": "solver-evidence-v1",
        "verifier_status": verifier_status,
        "verifier_fingerprint": "verifier-v1",
        "verifier_evidence_fingerprint": "verifier-evidence-v1",
        "status": status,
        "schema_version": "open_trader.prediction_n_leg.partial_fill_proof.v1",
    }
    values["fingerprint"] = fingerprint(values)
    return PartialFillProofRecord(**values)  # type: ignore[arg-type]


def service(tmp_path) -> NLegExecutionService:
    return NLegExecutionService(PredictionArbitrageStore(tmp_path / "data"))


def enter(service: NLegExecutionService) -> dict[str, object]:
    execution = solution()
    return service.enter(
        opportunity_episode_id="episode-1", episode_lineage_id="lineage-1", execution_batch_id="batch-1",
        execution_solution=execution, partial_fill_proof=proof(execution), mode="AUTO", cap_config_version="caps-v1",
    )


def receipt(*, leg: str, filled: int, state: str, sequence: int) -> OrderReceipt:
    return OrderReceipt(
        receipt_id=f"{leg}-{sequence}", execution_batch_id="batch-1", client_order_id=f"batch-1:{leg}",
        venue_id=f"venue-{leg}", account_id=f"account-{leg}", venue_order_id=None,
        submitted_quantity=10, cumulative_filled_quantity=filled, cumulative_fee_units=0,
        state=state, sequence=sequence, rest_confirmed=False,
        observed_at="2026-08-15T00:00:00Z", venue_timestamp="2026-08-15T00:00:00Z",
    )


def repair_context(*, candidates: list[dict[str, object]]) -> dict[str, object]:
    return {
        "fresh": True,
        "canonical_order_books_fingerprint": "books-v1",
        "holding_snapshot_fingerprint": "holdings-v1",
        "reservation_version": "batch-1:v1",
        "candidates": candidates,
    }


def reconciliation_context() -> dict[str, object]:
    return {
        "fresh": True,
        "balance_fingerprint": "balance-v1",
        "holding_fingerprint": "holdings-v1",
        "reservation_version": "batch-1:v1",
    }


def test_entry_claims_one_batch_and_survives_reopen(tmp_path) -> None:
    current = service(tmp_path)
    batch = enter(current)

    restarted = service(tmp_path)
    assert restarted.state("batch-1") == batch
    assert restarted.control() == {
        "mode": "AUTO", "breaker_open": False, "breaker_reason": None,
        "active_batch_id": "batch-1", "total_unsettled_capital_units": 100,
    }
    assert restarted.enter(
        opportunity_episode_id="episode-1", episode_lineage_id="lineage-1", execution_batch_id="batch-1",
        execution_solution=solution(), partial_fill_proof=proof(solution()), mode="AUTO", cap_config_version="caps-v1",
    ) == batch
    restarted.apply_receipt(receipt(leg="a", filled=0, state="REJECTED", sequence=1))
    restarted.apply_receipt(receipt(leg="b", filled=0, state="REJECTED", sequence=1))
    restarted.complete_reconciliation("batch-1", context=reconciliation_context())
    with pytest.raises(ValueError, match="LINEAGE_ALREADY_CLAIMED"):
        restarted.enter(
            opportunity_episode_id="episode-2", episode_lineage_id="lineage-1", execution_batch_id="batch-2",
            execution_solution=solution(), partial_fill_proof=proof(solution()), mode="MANUAL", cap_config_version="caps-v1",
        )


def test_partial_fill_opens_incident_downgrades_mode_and_fixes_best_manual_plan(tmp_path) -> None:
    current = service(tmp_path)
    enter(current)

    incident = current.apply_receipt(receipt(leg="a", filled=4, state="OPEN", sequence=1))
    assert incident["state"] == "INCIDENT"
    assert current.control()["mode"] == "MANUAL"
    assert current.control()["active_batch_id"] == "batch-1"

    current.apply_receipt(receipt(leg="b", filled=0, state="REJECTED", sequence=1))
    planned = current.apply_receipt(
        receipt(leg="a", filled=4, state="CANCELLED", sequence=2),
        repair_context=repair_context(candidates=[
            {"family": "COMPLETE_REMAINING", "conservative_total_loss_units": 8, "legs": [{"client_order_id": "batch-1:a", "quantity": 6}, {"client_order_id": "batch-1:b", "quantity": 10}], "additional_capital_units": 0, "uses_expected_proceeds": False},
            {"family": "EXIT_CONFIRMED", "conservative_total_loss_units": 12, "legs": [{"client_order_id": "batch-1:a", "quantity": 4}], "additional_capital_units": 0, "uses_expected_proceeds": False},
        ]),
    )
    plan = planned["repair_plan"]
    assert isinstance(plan, dict)
    assert plan["family"] == "COMPLETE_REMAINING"
    assert plan["auto_eligible"] is False
    assert plan["reason"] == "REPAIR_PROOF_REQUIRED"


def test_same_sequence_conflict_opens_persistent_breaker(tmp_path) -> None:
    current = service(tmp_path)
    enter(current)
    current.apply_receipt(receipt(leg="a", filled=0, state="OPEN", sequence=1))

    conflict = current.apply_receipt(replace(receipt(leg="a", filled=1, state="OPEN", sequence=1), receipt_id="a-conflict"))

    assert conflict["incident"] == {"reason": "SAME_SEQUENCE_CONFLICT", "repair_status": "PENDING_RECONCILIATION"}
    assert current.control()["mode"] == "MANUAL"


def test_entry_fails_closed_for_unsafe_or_over_partial_fill_cap(tmp_path) -> None:
    current = service(tmp_path)
    execution = solution()

    with pytest.raises(ValueError, match="PARTIAL_FILL_PROOF_REQUIRED"):
        current.enter(
            opportunity_episode_id="episode-1", episode_lineage_id="lineage-1", execution_batch_id="batch-1",
            execution_solution=execution, partial_fill_proof=proof(execution, status="UNKNOWN"), mode="MANUAL", cap_config_version="caps-v1",
        )
    with pytest.raises(ValueError, match="PARTIAL_FILL_LOSS_CAP_EXCEEDED"):
        current.enter(
            opportunity_episode_id="episode-1", episode_lineage_id="lineage-1", execution_batch_id="batch-1",
            execution_solution=execution, partial_fill_proof=proof(execution, partial_cap=0), mode="MANUAL", cap_config_version="caps-v1",
        )
    with pytest.raises(ValueError, match="PARTIAL_FILL_PROOF_REQUIRED"):
        current.enter(
            opportunity_episode_id="episode-1", episode_lineage_id="lineage-1", execution_batch_id="batch-1",
            execution_solution=execution, partial_fill_proof=proof(execution), mode="MANUAL", cap_config_version="caps-v2",
        )


def test_all_terminal_zero_fill_releases_active_reservation_without_incident(tmp_path) -> None:
    current = service(tmp_path)
    enter(current)

    current.apply_receipt(receipt(leg="a", filled=0, state="REJECTED", sequence=1))
    completed = current.apply_receipt(receipt(leg="b", filled=0, state="CANCELLED", sequence=1))

    assert completed["state"] == "AWAITING_RECONCILIATION"
    assert completed["incident"] is None
    assert current.control()["active_batch_id"] == "batch-1"
    reconciled = current.complete_reconciliation("batch-1", context=reconciliation_context())
    assert reconciled["state"] == "RECONCILED_ZERO"
    assert current.control()["active_batch_id"] is None
    assert current.control()["total_unsettled_capital_units"] == 0


def test_all_terminal_full_fill_reconciles_without_incident_and_retains_unsettled_holding(tmp_path) -> None:
    current = service(tmp_path)
    enter(current)

    current.apply_receipt(receipt(leg="a", filled=10, state="FILLED", sequence=1))
    completed = current.apply_receipt(receipt(leg="b", filled=10, state="FILLED", sequence=1))

    assert completed["state"] == "AWAITING_RECONCILIATION"
    assert completed["incident"] is None
    assert current.control()["active_batch_id"] == "batch-1"
    reconciled = current.complete_reconciliation("batch-1", context=reconciliation_context())
    assert reconciled["state"] == "RECONCILED_FULL"
    assert current.control()["active_batch_id"] is None
    assert current.control()["total_unsettled_capital_units"] == 100


def test_unknown_receipt_opens_global_breaker_and_stale_receipt_is_ignored(tmp_path) -> None:
    current = service(tmp_path)
    enter(current)
    first = current.apply_receipt(receipt(leg="a", filled=0, state="OPEN", sequence=2))
    assert current.apply_receipt(receipt(leg="a", filled=0, state="OPEN", sequence=1)) == first

    unknown = current.apply_receipt(receipt(leg="b", filled=0, state="UNKNOWN", sequence=1))
    assert unknown["incident"] == {"reason": "UNKNOWN_ORDER_STATE", "repair_status": "PENDING_RECONCILIATION"}
    assert current.control()["breaker_reason"] == "UNKNOWN_ORDER_STATE"


def test_unknown_terminal_reconciliation_without_context_persists_breaker_across_restart(tmp_path) -> None:
    current = service(tmp_path)
    enter(current)
    current.apply_receipt(receipt(leg="a", filled=4, state="CANCELLED", sequence=1))
    failed = current.apply_receipt(receipt(leg="b", filled=0, state="REJECTED", sequence=1))

    assert failed["repair_plan"] is None
    assert current.control()["breaker_open"] is True
    assert current.control()["breaker_reason"] == "REPAIR_CONTEXT_REQUIRED"
    assert service(tmp_path).control()["breaker_open"] is True


def test_over_cap_repair_is_retained_but_breaks_automatic_authority(tmp_path) -> None:
    current = service(tmp_path)
    execution = solution()
    current.enter(
        opportunity_episode_id="episode-1", episode_lineage_id="lineage-1", execution_batch_id="batch-1",
        execution_solution=execution, partial_fill_proof=proof(execution, repair_cap=7), mode="AUTO", cap_config_version="caps-v1",
    )
    current.apply_receipt(receipt(leg="a", filled=4, state="CANCELLED", sequence=1))
    failed = current.apply_receipt(
        receipt(leg="b", filled=0, state="REJECTED", sequence=1),
        repair_context=repair_context(candidates=[{"family": "EXIT_CONFIRMED", "conservative_total_loss_units": 8, "legs": [{"client_order_id": "batch-1:a", "quantity": 4}], "additional_capital_units": 0, "uses_expected_proceeds": False}]),
    )

    assert failed["repair_plan"] == {
        "schema_version": "open_trader.prediction_n_leg.repair_plan.v1",
        "family": "EXIT_CONFIRMED",
        "candidate": failed["repair_plan"]["candidate"],
        "fingerprint": failed["repair_plan"]["fingerprint"],
        "auto_eligible": False,
        "reason": "REPAIR_LOSS_CAP_EXCEEDED",
    }
    assert current.control()["breaker_reason"] == "REPAIR_LOSS_CAP_EXCEEDED"


def test_mixed_terminal_receipts_are_an_incident_not_a_full_reconciliation(tmp_path) -> None:
    current = service(tmp_path)
    enter(current)

    current.apply_receipt(receipt(leg="a", filled=10, state="FILLED", sequence=1))
    mixed = current.apply_receipt(receipt(leg="b", filled=0, state="REJECTED", sequence=1))

    assert mixed["state"] == "INCIDENT"
    assert mixed["incident"]["reason"] == "MIXED_TERMINAL_FILL"
    assert current.control()["active_batch_id"] == "batch-1"


def test_receipts_do_not_release_batch_without_explicit_reconciliation(tmp_path) -> None:
    current = service(tmp_path)
    enter(current)

    current.apply_receipt(receipt(leg="a", filled=0, state="REJECTED", sequence=1))
    terminal = current.apply_receipt(receipt(leg="b", filled=0, state="CANCELLED", sequence=1))

    assert terminal["state"] == "AWAITING_RECONCILIATION"
    assert current.control()["active_batch_id"] == "batch-1"


def test_conflict_keeps_old_and_new_receipt_facts(tmp_path) -> None:
    current = service(tmp_path)
    enter(current)
    old = receipt(leg="a", filled=0, state="OPEN", sequence=1)
    current.apply_receipt(old)
    new = replace(old, receipt_id="a-conflict", cumulative_filled_quantity=1)

    conflict = current.apply_receipt(new)

    assert conflict["receipt_conflicts"] == [{"reason": "SAME_SEQUENCE_CONFLICT", "old": old.to_payload(), "new": new.to_payload()}]


def test_partial_fill_gate_uses_proven_upper_loss_bound_not_principal(tmp_path) -> None:
    current = service(tmp_path)
    execution = solution()
    accepted = proof(execution, partial_cap=100)
    values = accepted.to_payload()
    values["solver_upper_bound"] = 101
    values.pop("fingerprint")
    values["fingerprint"] = fingerprint(values)
    with pytest.raises(ValueError, match="PARTIAL_FILL_LOSS_CAP_EXCEEDED"):
        current.enter(
            opportunity_episode_id="episode-1", episode_lineage_id="lineage-1", execution_batch_id="batch-1",
            execution_solution=execution, partial_fill_proof=PartialFillProofRecord(**values), mode="MANUAL", cap_config_version="caps-v1",
        )


def test_concurrent_different_leg_receipts_do_not_lose_a_transition(tmp_path) -> None:
    current = service(tmp_path)
    enter(current)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(current.apply_receipt, (
            receipt(leg="a", filled=10, state="FILLED", sequence=1),
            receipt(leg="b", filled=10, state="FILLED", sequence=1),
        )))

    durable = current.state("batch-1")
    assert durable is not None
    assert {leg["client_order_id"]: leg["receipt"]["state"] for leg in durable["legs"]} == {"batch-1:a": "FILLED", "batch-1:b": "FILLED"}
    assert durable["state"] == "AWAITING_RECONCILIATION"


def test_partial_incident_can_close_only_after_eventual_full_and_reconciliation(tmp_path) -> None:
    current = service(tmp_path)
    enter(current)
    current.apply_receipt(receipt(leg="a", filled=4, state="OPEN", sequence=1))
    current.apply_receipt(receipt(leg="a", filled=10, state="FILLED", sequence=2))
    awaiting = current.apply_receipt(receipt(leg="b", filled=10, state="FILLED", sequence=1))

    assert awaiting["incident"]["reason"] == "PARTIAL_FILL"
    closed = current.complete_reconciliation("batch-1", context=reconciliation_context())
    assert closed["incident"] is None
    assert closed["state"] == "RECONCILED_FULL"
    assert current.control()["mode"] == "MANUAL"
