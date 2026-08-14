from __future__ import annotations

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from open_trader.prediction_executable_cost import ExecutionLegEvidence, ExecutionSolution
from open_trader.prediction_n_leg import ActionQuantity, fingerprint
from open_trader.prediction_n_leg_execution import (
    ConfirmedHolding,
    CanonicalBookLevel,
    ExecutionSolutionSource,
    ReconciliationContext,
    RepairQuote,
    RepairContext,
    SettlementCashFlow,
    NLegExecutionService,
    OrderReceipt,
    PartialFillProofRecord,
    execution_solution_binding,
    order_receipt_from_payload,
    partial_fill_proof_from_payload,
)
from test_prediction_executable_cost import AS_OF, two_leg_books, two_leg_component
from test_prediction_solver import BruteForceBackend
from open_trader.prediction_executable_cost import AccountBalance, AccountSnapshot, VerifiedComponent, component_fingerprint, resolve_component
from open_trader.prediction_solver import BenchmarkLimits
from open_trader.prediction_n_leg import ExecutableCostSlice, OracleBudget, canonical_payload
from open_trader.prediction_arbitrage import BookLevel, ThresholdOrderBook


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
        cumulative_cost_units=0,
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


def source_and_solution() -> tuple[ExecutionSolutionSource, ExecutionSolution]:
    original = two_leg_component()
    problem = replace(original.problem, actions=tuple(replace(action, min_quantity_lots=10, max_quantity_lots=10, cost_slices=(ExecutableCostSlice(1, 10, 1),)) for action in original.problem.actions))
    component = VerifiedComponent(problem, component_fingerprint(problem, original.book_bindings, 100), 100, original.book_bindings)
    books = tuple(replace(book, book=ThresholdOrderBook(book.native_id, (BookLevel(Decimal("0.40"), Decimal("10")),), (), AS_OF)) for book in two_leg_books())
    account = AccountSnapshot(AS_OF, (AccountBalance("venue-a", "account-a", "usd-cents", 10_000, 10_000), AccountBalance("venue-b", "account-b", "usd-cents", 10_000, 10_000)), 10_000, 0, 10_000)
    result = resolve_component(component, books, account, BenchmarkLimits(100, 200, 1_000_000, 4), OracleBudget(2, 2, 2), now=AS_OF, solver_backend=BruteForceBackend())
    assert result.execution_solution is not None and result.market_solution is not None
    source = ExecutionSolutionSource(canonical_payload(result.execution_solution), canonical_payload(result.market_solution), component, books, account, AS_OF)
    return source, result.execution_solution


def solution() -> ExecutionSolution:
    return source_and_solution()[1]


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
    source, execution = source_and_solution()
    return service.enter(
        opportunity_episode_id="episode-1", episode_lineage_id="lineage-1", execution_batch_id="batch-1",
        source=source, partial_fill_proof=proof(execution), mode="AUTO", cap_config_version="caps-v1",
    )


def receipt(*, leg: str, filled: int, state: str, sequence: int) -> OrderReceipt:
    action = f"action-{leg}"
    return OrderReceipt(
        receipt_id=f"{leg}-{sequence}", execution_batch_id="batch-1", client_order_id=f"batch-1:{action}",
        venue_id=f"venue-{leg}", account_id=f"account-{leg}", venue_order_id=None,
        submitted_quantity=10, cumulative_filled_quantity=filled, cumulative_cost_units=0, cumulative_fee_units=0,
        state=state, sequence=sequence, rest_confirmed=False,
        observed_at="2026-08-15T00:00:00Z", venue_timestamp="2026-08-15T00:00:00Z",
    )


def repair_context(*, b_buy: int = 1, b_venue: str = "venue-b", occurred_cost: int = 0, a_sequence: int = 2) -> RepairContext:
    return RepairContext(
        "batch-1:v1",
        (
            RepairQuote("batch-1:action-a", "action-a", "venue-a", "account-a", "usd-cents", (CanonicalBookLevel(1, 10, 1, 2),), AS_OF, AS_OF),
            RepairQuote("batch-1:action-b", "action-b", b_venue, "account-b", "usd-cents", (CanonicalBookLevel(1, 10, b_buy, 2),), AS_OF, AS_OF),
        ),
        (ConfirmedHolding("venue-a", "account-a", "action-a", 4, AS_OF, AS_OF), ConfirmedHolding("venue-b", "account-b", "action-b", 0, AS_OF, AS_OF)),
        source_and_solution()[0].account_snapshot,
        (
            SettlementCashFlow("batch-1:action-a", None, "venue-a", "account-a", "usd-cents", occurred_cost, 0, AS_OF, AS_OF, a_sequence),
            SettlementCashFlow("batch-1:action-b", None, "venue-b", "account-b", "usd-cents", 0, 0, AS_OF, AS_OF, 1),
        ),
        AS_OF,
    )


def reconciliation_context(batch_id: str = "batch-1", *, full: bool = False) -> ReconciliationContext:
    holdings = (ConfirmedHolding("venue-a", "account-a", "action-a", 10, AS_OF, AS_OF), ConfirmedHolding("venue-b", "account-b", "action-b", 10, AS_OF, AS_OF)) if full else ()
    return ReconciliationContext(f"{batch_id}:v1", source_and_solution()[0].account_snapshot, holdings, AS_OF, AS_OF, AS_OF)


def test_entry_claims_one_batch_and_survives_reopen(tmp_path) -> None:
    current = service(tmp_path)
    batch = enter(current)

    restarted = service(tmp_path)
    assert restarted.state("batch-1") == batch
    assert restarted.control() == {
        "mode": "AUTO", "breaker_open": False, "breaker_reason": None,
        "active_batch_id": "batch-1", "total_unsettled_capital_units": 1020,
    }
    assert restarted.enter(
        opportunity_episode_id="episode-1", episode_lineage_id="lineage-1", execution_batch_id="batch-1",
        source=source_and_solution()[0], partial_fill_proof=proof(solution()), mode="AUTO", cap_config_version="caps-v1",
    ) == batch
    restarted.apply_receipt(receipt(leg="a", filled=0, state="REJECTED", sequence=1))
    restarted.apply_receipt(receipt(leg="b", filled=0, state="REJECTED", sequence=1))
    restarted.complete_reconciliation("batch-1", context=reconciliation_context())
    with pytest.raises(ValueError, match="LINEAGE_ALREADY_CLAIMED"):
        restarted.enter(
            opportunity_episode_id="episode-2", episode_lineage_id="lineage-1", execution_batch_id="batch-2",
            source=source_and_solution()[0], partial_fill_proof=proof(solution()), mode="MANUAL", cap_config_version="caps-v1",
        )


def test_entry_retry_uses_immutable_fingerprint_after_receipt_evolves(tmp_path) -> None:
    current = service(tmp_path)
    enter(current)
    evolved = current.apply_receipt(receipt(leg="a", filled=0, state="REJECTED", sequence=1))
    assert current.enter(
        opportunity_episode_id="episode-1", episode_lineage_id="lineage-1", execution_batch_id="batch-1",
        source=source_and_solution()[0], partial_fill_proof=proof(solution()), mode="AUTO", cap_config_version="caps-v1",
    ) == evolved


def test_proof_decoder_is_strict_at_public_contract() -> None:
    accepted = proof(solution())
    assert partial_fill_proof_from_payload(accepted.to_payload()) == accepted
    with pytest.raises(ValueError, match="unexpected"):
        partial_fill_proof_from_payload({**accepted.to_payload(), "extra": True})


def test_partial_fill_opens_incident_downgrades_mode_and_fixes_best_manual_plan(tmp_path) -> None:
    current = service(tmp_path)
    enter(current)

    incident = current.apply_receipt(receipt(leg="a", filled=4, state="OPEN", sequence=1))
    assert incident["state"] == "INCIDENT"
    assert incident["execution_controls"] == [
        {"client_order_id": "batch-1:action-a", "intent": "CANCEL_REQUIRED"},
        {"client_order_id": "batch-1:action-b", "intent": "STOPPED_UNSENT"},
    ]
    assert current.control()["mode"] == "MANUAL"
    assert current.control()["active_batch_id"] == "batch-1"

    current.apply_receipt(receipt(leg="b", filled=0, state="REJECTED", sequence=1))
    planned = current.apply_receipt(
        receipt(leg="a", filled=4, state="CANCELLED", sequence=2),
        repair_context=repair_context(),
    )
    plan = planned["repair_plan"]
    assert isinstance(plan, dict)
    assert plan["family"] == "COMPLETE_REMAINING"
    assert plan["auto_eligible"] is False
    assert plan["reason"] == "REPAIR_PROOF_REQUIRED"


def test_repair_context_rejects_structural_venue_mismatch(tmp_path) -> None:
    current = service(tmp_path)
    enter(current)
    current.apply_receipt(replace(receipt(leg="a", filled=4, state="CANCELLED", sequence=1), cumulative_cost_units=8))
    rejected = current.apply_receipt(receipt(leg="b", filled=0, state="REJECTED", sequence=1), repair_context=repair_context(b_venue="other-venue"))
    assert rejected["repair_plan"] is None


def test_reconciliation_replay_cannot_clear_a_later_active_batch(tmp_path) -> None:
    current = service(tmp_path)
    enter(current)
    current.apply_receipt(receipt(leg="a", filled=0, state="REJECTED", sequence=1))
    current.apply_receipt(receipt(leg="b", filled=0, state="REJECTED", sequence=1))
    current.complete_reconciliation("batch-1", context=reconciliation_context())
    # A future batch is deliberately represented by the Store control boundary;
    # replaying batch-1 must be a no-op rather than clearing this ownership.
    current._store.n_leg_create_batch({**current.state("batch-1"), "execution_batch_id": "batch-2", "episode_lineage_id": "lineage-2", "opportunity_episode_id": "episode-2", "state": "ACTIVE"})
    before = current.control()
    current.complete_reconciliation("batch-1", context=reconciliation_context())
    assert current.control() == before


def test_repair_cannot_use_one_leg_reservation_for_another_leg(tmp_path) -> None:
    current = service(tmp_path)
    enter(current)
    current.apply_receipt(receipt(leg="a", filled=4, state="CANCELLED", sequence=1))
    context = repair_context(a_sequence=1)
    # Aggregate reservation is ample, but action-b cannot consume action-a's key.
    rejected = current.apply_receipt(receipt(leg="b", filled=0, state="REJECTED", sequence=1), repair_context=repair_context(b_buy=511, a_sequence=1))
    assert rejected["repair_plan"]["family"] == "EXIT_CONFIRMED"


def test_same_sequence_conflict_opens_persistent_breaker(tmp_path) -> None:
    current = service(tmp_path)
    enter(current)
    current.apply_receipt(receipt(leg="a", filled=0, state="OPEN", sequence=1))

    conflict = current.apply_receipt(replace(receipt(leg="a", filled=1, state="OPEN", sequence=1), receipt_id="a-conflict"))

    assert conflict["incident"] == {"reason": "SAME_SEQUENCE_CONFLICT", "repair_status": "PENDING_RECONCILIATION"}
    assert current.control()["mode"] == "MANUAL"


def test_same_sequence_semantic_duplicate_ignores_receipt_id_and_time(tmp_path) -> None:
    current = service(tmp_path)
    enter(current)
    first = current.apply_receipt(receipt(leg="a", filled=0, state="OPEN", sequence=1))
    duplicate = replace(receipt(leg="a", filled=0, state="OPEN", sequence=1), receipt_id="different-id", observed_at="2026-08-15T00:00:01Z", venue_timestamp="2026-08-15T00:00:01Z")
    assert current.apply_receipt(duplicate) == first


def test_unknown_receipt_requires_rest_confirmation_intent(tmp_path) -> None:
    current = service(tmp_path)
    enter(current)
    incident = current.apply_receipt(receipt(leg="a", filled=0, state="UNKNOWN", sequence=1))
    assert incident["execution_controls"][0] == {"client_order_id": "batch-1:action-a", "intent": "REST_CONFIRMATION_REQUIRED"}


def test_venue_order_id_is_stable_after_first_confirmation(tmp_path) -> None:
    current = service(tmp_path)
    enter(current)
    first = replace(receipt(leg="a", filled=0, state="OPEN", sequence=1), venue_order_id="venue-order-1")
    current.apply_receipt(first)
    conflict = current.apply_receipt(replace(first, receipt_id="next", venue_order_id="venue-order-2", sequence=2))
    assert conflict["incident"]["reason"] == "VENUE_ORDER_ID_DRIFT"


def test_entry_fails_closed_for_unsafe_or_over_partial_fill_cap(tmp_path) -> None:
    current = service(tmp_path)
    execution = solution()

    with pytest.raises(ValueError, match="PARTIAL_FILL_PROOF_REQUIRED"):
        current.enter(
            opportunity_episode_id="episode-1", episode_lineage_id="lineage-1", execution_batch_id="batch-1",
            source=source_and_solution()[0], partial_fill_proof=proof(execution, status="UNKNOWN"), mode="MANUAL", cap_config_version="caps-v1",
        )
    with pytest.raises(ValueError, match="PARTIAL_FILL_LOSS_CAP_EXCEEDED"):
        current.enter(
            opportunity_episode_id="episode-1", episode_lineage_id="lineage-1", execution_batch_id="batch-1",
            source=source_and_solution()[0], partial_fill_proof=proof(execution, partial_cap=0), mode="MANUAL", cap_config_version="caps-v1",
        )
    with pytest.raises(ValueError, match="PARTIAL_FILL_PROOF_REQUIRED"):
        current.enter(
            opportunity_episode_id="episode-1", episode_lineage_id="lineage-1", execution_batch_id="batch-1",
            source=source_and_solution()[0], partial_fill_proof=proof(execution), mode="MANUAL", cap_config_version="caps-v2",
        )


def test_all_terminal_zero_fill_releases_active_reservation_without_incident(tmp_path) -> None:
    current = service(tmp_path)
    enter(current)

    current.apply_receipt(receipt(leg="a", filled=0, state="REJECTED", sequence=1))
    completed = current.apply_receipt(receipt(leg="b", filled=0, state="CANCELLED", sequence=1))

    assert completed["state"] == "AWAITING_RECONCILIATION"
    assert completed["incident"] is None
    assert current.control()["active_batch_id"] == "batch-1"
    reconciled = current.complete_reconciliation("batch-1", context=reconciliation_context(full=True))
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
    reconciled = current.complete_reconciliation("batch-1", context=reconciliation_context(full=True))
    assert reconciled["state"] == "RECONCILED_FULL"
    assert current.control()["active_batch_id"] is None
    assert current.control()["total_unsettled_capital_units"] == 1020


def test_prior_unsettled_capital_survives_a_later_zero_fill_batch(tmp_path) -> None:
    current = service(tmp_path)
    enter(current)
    current.apply_receipt(receipt(leg="a", filled=10, state="FILLED", sequence=1))
    current.apply_receipt(receipt(leg="b", filled=10, state="FILLED", sequence=1))
    current.complete_reconciliation("batch-1", context=reconciliation_context(full=True))
    current.enter(
        opportunity_episode_id="episode-2", episode_lineage_id="lineage-2", execution_batch_id="batch-2",
        source=source_and_solution()[0], partial_fill_proof=proof(solution()), mode="MANUAL", cap_config_version="caps-v1",
    )
    current.apply_receipt(replace(receipt(leg="a", filled=0, state="REJECTED", sequence=1), execution_batch_id="batch-2", client_order_id="batch-2:action-a"))
    current.apply_receipt(replace(receipt(leg="b", filled=0, state="REJECTED", sequence=1), execution_batch_id="batch-2", client_order_id="batch-2:action-b"))
    current.complete_reconciliation("batch-2", context=reconciliation_context("batch-2"))
    assert current.control()["total_unsettled_capital_units"] == 1020


def test_unknown_receipt_opens_global_breaker_and_stale_receipt_is_ignored(tmp_path) -> None:
    current = service(tmp_path)
    enter(current)
    first = current.apply_receipt(receipt(leg="a", filled=0, state="OPEN", sequence=2))
    assert current.apply_receipt(receipt(leg="a", filled=0, state="OPEN", sequence=1)) == first

    unknown = current.apply_receipt(receipt(leg="b", filled=0, state="UNKNOWN", sequence=1))
    assert unknown["incident"] == {"reason": "UNKNOWN_ORDER_STATE", "repair_status": "PENDING_RECONCILIATION"}
    assert current.control()["breaker_reason"] == "UNKNOWN_ORDER_STATE"


@pytest.mark.parametrize("filled,state,cost,reason", ((4, "CANCELLED", 511, "COST_RESERVATION_BREACH"), (10, "FILLED", 511, "COST_RESERVATION_BREACH"), (0, "REJECTED", 1, "ZERO_FILL_CASH_BREACH")))
def test_receipt_cash_breach_persists_incident_and_breaker(tmp_path, filled, state, cost, reason) -> None:
    current = service(tmp_path)
    enter(current)
    breached = current.apply_receipt(replace(receipt(leg="a", filled=filled, state=state, sequence=1), cumulative_cost_units=cost))
    assert breached["incident"]["reason"] == reason
    assert current.control()["mode"] == "MANUAL"
    assert current.control()["breaker_reason"] == reason


def test_unknown_terminal_reconciliation_without_context_persists_breaker_across_restart(tmp_path) -> None:
    current = service(tmp_path)
    enter(current)
    current.apply_receipt(receipt(leg="a", filled=4, state="CANCELLED", sequence=1))
    failed = current.apply_receipt(receipt(leg="b", filled=0, state="REJECTED", sequence=1))

    assert failed["repair_plan"] is None
    assert current.control()["breaker_open"] is True
    assert current.control()["breaker_reason"] == "REPAIR_CONTEXT_REQUIRED"
    assert service(tmp_path).control()["breaker_open"] is True


def test_shallow_valid_repair_book_persists_incident_and_breaker(tmp_path) -> None:
    current = service(tmp_path)
    enter(current)
    current.apply_receipt(receipt(leg="a", filled=4, state="CANCELLED", sequence=1))
    source = repair_context(a_sequence=1)
    shallow = replace(source, quotes=tuple(replace(quote, levels=(CanonicalBookLevel(1, 1, 1, 2),)) for quote in source.quotes))
    result = current.apply_receipt(receipt(leg="b", filled=0, state="REJECTED", sequence=1), repair_context=shallow)
    assert result["incident"] == {"reason": "PARTIAL_FILL", "repair_status": "REPAIR_INSUFFICIENT_DEPTH"}
    assert result["repair_plan"] is None
    assert current.control()["mode"] == "MANUAL"
    assert current.control()["breaker_reason"] == "REPAIR_INSUFFICIENT_DEPTH"


def test_over_cap_repair_is_retained_but_breaks_automatic_authority(tmp_path) -> None:
    current = service(tmp_path)
    execution = solution()
    current.enter(
        opportunity_episode_id="episode-1", episode_lineage_id="lineage-1", execution_batch_id="batch-1",
        source=source_and_solution()[0], partial_fill_proof=proof(execution, repair_cap=0), mode="AUTO", cap_config_version="caps-v1",
    )
    current.apply_receipt(replace(receipt(leg="a", filled=4, state="CANCELLED", sequence=1), cumulative_cost_units=8))
    failed = current.apply_receipt(
        receipt(leg="b", filled=0, state="REJECTED", sequence=1),
        repair_context=repair_context(b_buy=511, occurred_cost=8, a_sequence=1),
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


def test_repair_loss_cap_accepts_exact_boundary_and_rejects_one_unit_over(tmp_path) -> None:
    def plan_for(cap: int):
        current = service(tmp_path / str(cap))
        current.enter(opportunity_episode_id=f"episode-{cap}", episode_lineage_id=f"lineage-{cap}", execution_batch_id=f"batch-{cap}", source=source_and_solution()[0], partial_fill_proof=proof(solution(), repair_cap=cap), mode="AUTO", cap_config_version="caps-v1")
        first = replace(receipt(leg="a", filled=4, state="CANCELLED", sequence=1), execution_batch_id=f"batch-{cap}", client_order_id=f"batch-{cap}:action-a", cumulative_cost_units=5)
        second = replace(receipt(leg="b", filled=0, state="REJECTED", sequence=1), execution_batch_id=f"batch-{cap}", client_order_id=f"batch-{cap}:action-b")
        current.apply_receipt(first)
        context = repair_context(b_buy=511, occurred_cost=5, a_sequence=1)
        context = replace(context, reservation_version=f"batch-{cap}:v1", quotes=tuple(replace(quote, client_order_id=quote.client_order_id.replace("batch-1", f"batch-{cap}")) for quote in context.quotes), cash_flows=tuple(replace(flow, client_order_id=flow.client_order_id.replace("batch-1", f"batch-{cap}")) for flow in context.cash_flows))
        return current.apply_receipt(second, repair_context=context)
    assert plan_for(1)["repair_plan"]["reason"] == "REPAIR_PROOF_REQUIRED"
    assert plan_for(0)["repair_plan"]["reason"] == "REPAIR_LOSS_CAP_EXCEEDED"


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

    assert conflict["receipt_conflicts"][0] == {
        "transition_id": fingerprint({"reason": "SAME_SEQUENCE_CONFLICT", "old": old.to_payload(), "new": new.to_payload()}),
        "reason": "SAME_SEQUENCE_CONFLICT", "old": old.to_payload(), "new": new.to_payload(),
    }


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
            source=source_and_solution()[0], partial_fill_proof=PartialFillProofRecord(**values), mode="MANUAL", cap_config_version="caps-v1",
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
    assert {leg["client_order_id"]: leg["receipt"]["state"] for leg in durable["legs"]} == {"batch-1:action-a": "FILLED", "batch-1:action-b": "FILLED"}
    assert durable["state"] == "AWAITING_RECONCILIATION"


def test_partial_incident_can_close_only_after_eventual_full_and_reconciliation(tmp_path) -> None:
    current = service(tmp_path)
    enter(current)
    current.apply_receipt(receipt(leg="a", filled=4, state="OPEN", sequence=1))
    current.apply_receipt(receipt(leg="a", filled=10, state="FILLED", sequence=2))
    awaiting = current.apply_receipt(receipt(leg="b", filled=10, state="FILLED", sequence=1))

    assert awaiting["incident"]["reason"] == "PARTIAL_FILL"
    closed = current.complete_reconciliation("batch-1", context=reconciliation_context(full=True))
    assert closed["incident"] is None
    assert closed["state"] == "RECONCILED_FULL"
    assert current.control()["mode"] == "MANUAL"
