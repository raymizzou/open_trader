from __future__ import annotations

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal

import pytest

from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from open_trader.prediction_executable_cost import ExecutionLegEvidence, ExecutionSolution
from open_trader.prediction_n_leg import ActionQuantity, fingerprint
from open_trader.prediction_n_leg_execution import (
    ConfirmedHolding,
    ConflictResolutionEvidence,
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


def shared_key_source_and_solution() -> tuple[ExecutionSolutionSource, ExecutionSolution]:
    original = two_leg_component()
    actions = tuple(replace(action, venue_id="venue-a", account_id="account-a", min_quantity_lots=10, max_quantity_lots=10, cost_slices=(ExecutableCostSlice(1, 10, 1),)) for action in original.problem.actions)
    problem = replace(original.problem, actions=actions)
    bindings = tuple(replace(binding, venue_id="venue-a") for binding in original.book_bindings)
    component = VerifiedComponent(problem, component_fingerprint(problem, bindings, 100), 100, bindings)
    books = tuple(replace(book, venue_id="venue-a", book=ThresholdOrderBook(book.native_id, (BookLevel(Decimal("0.40"), Decimal("10")),), (), AS_OF)) for book in two_leg_books())
    account = AccountSnapshot(AS_OF, (AccountBalance("venue-a", "account-a", "usd-cents", 10_000, 10_000),), 10_000, 0, 10_000)
    result = resolve_component(component, books, account, BenchmarkLimits(100, 200, 1_000_000, 4), OracleBudget(2, 2, 2), now=AS_OF, solver_backend=BruteForceBackend())
    assert result.execution_solution is not None and result.market_solution is not None
    return ExecutionSolutionSource(canonical_payload(result.execution_solution), canonical_payload(result.market_solution), component, books, account, AS_OF), result.execution_solution


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
        observed_at="2026-08-14T00:00:00Z", venue_timestamp="2026-08-14T00:00:00Z",
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
            SettlementCashFlow("batch-1:action-a", None, "venue-a", "account-a", "usd-cents", occurred_cost, 0, AS_OF, AS_OF, a_sequence, False),
            SettlementCashFlow("batch-1:action-b", None, "venue-b", "account-b", "usd-cents", 0, 0, AS_OF, AS_OF, 1, False),
        ),
        AS_OF,
    )


def reconciliation_context(batch_id: str = "batch-1", *, full: bool = False, a_sequence: int = 1, a_venue_order: str | None = None, resolutions: tuple[ConflictResolutionEvidence, ...] = (), now=AS_OF) -> ReconciliationContext:
    holdings = (ConfirmedHolding("venue-a", "account-a", "action-a", 10, AS_OF, AS_OF), ConfirmedHolding("venue-b", "account-b", "action-b", 10, AS_OF, AS_OF)) if full else ()
    return ReconciliationContext(f"{batch_id}:v1", source_and_solution()[0].account_snapshot, holdings, (SettlementCashFlow(f"{batch_id}:action-a", a_venue_order, "venue-a", "account-a", "usd-cents", 0, 0, AS_OF, AS_OF, a_sequence, False), SettlementCashFlow(f"{batch_id}:action-b", None, "venue-b", "account-b", "usd-cents", 0, 0, AS_OF, AS_OF, 1, False)), AS_OF, AS_OF, now, resolutions)


def resolution_for(conflict: dict[str, object], *, outcome: str = "TERMINAL", sequence: int = 1_000, observation_version: int = 1) -> ConflictResolutionEvidence:
    client, venue_order, venue, account, asset, settlement = conflict["physical_identity"]
    return ConflictResolutionEvidence(
        conflict["conflict_id"], client, venue_order, venue, account, asset, settlement,
        outcome, "REJECTED" if outcome == "TERMINAL" else None,
        conflict["max_actual_filled_quantity"], conflict["max_actual_cash_units"], 0,
        sequence if outcome == "TERMINAL" else None, outcome == "NOT_FOUND", observation_version, AS_OF + timedelta(seconds=1), AS_OF + timedelta(seconds=1),
    )


def shared_key_repair_context() -> RepairContext:
    source, _ = shared_key_source_and_solution()
    return RepairContext(
        "shared:v1",
        (
            RepairQuote("shared:action-a", "action-a", "venue-a", "account-a", "usd-cents", (CanonicalBookLevel(1, 10, 1, 2),), AS_OF, AS_OF),
            RepairQuote("shared:action-b", "action-b", "venue-a", "account-a", "usd-cents", (CanonicalBookLevel(1, 10, 1, 2),), AS_OF, AS_OF),
        ),
        (ConfirmedHolding("venue-a", "account-a", "action-a", 4, AS_OF, AS_OF), ConfirmedHolding("venue-a", "account-a", "action-b", 0, AS_OF, AS_OF)),
        source.account_snapshot,
        (SettlementCashFlow("shared:action-a", None, "venue-a", "account-a", "usd-cents", 0, 0, AS_OF, AS_OF, 1, False), SettlementCashFlow("shared:action-b", None, "venue-a", "account-a", "usd-cents", 0, 0, AS_OF, AS_OF, 1, False)),
        AS_OF,
    )


def shared_key_reconciliation_context() -> ReconciliationContext:
    source, _ = shared_key_source_and_solution()
    return ReconciliationContext(
        "shared:v1", source.account_snapshot,
        (ConfirmedHolding("venue-a", "account-a", "action-a", 10, AS_OF, AS_OF), ConfirmedHolding("venue-a", "account-a", "action-b", 10, AS_OF, AS_OF)),
        (SettlementCashFlow("shared:action-a", None, "venue-a", "account-a", "usd-cents", 0, 0, AS_OF, AS_OF, 1, False), SettlementCashFlow("shared:action-b", None, "venue-a", "account-a", "usd-cents", 0, 0, AS_OF, AS_OF, 1, False)),
        AS_OF, AS_OF, AS_OF,
    )


def test_entry_claims_one_batch_and_survives_reopen(tmp_path) -> None:
    current = service(tmp_path)
    batch = enter(current)

    restarted = service(tmp_path)
    assert restarted.state("batch-1") == batch
    assert restarted.control() == {
        "mode": "AUTO", "breaker_open": False, "breaker_reason": None,
        "active_batch_id": "batch-1", "total_unsettled_capital_units": 1020,
        "contract_generation": 1,
        "qualification_policy_version": 1,
        "safety_config_version": 1,
        "enabled_execution_scope_version": [],
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


def test_shared_reservation_key_entry_aggregates_repair_and_reconciliation(tmp_path) -> None:
    current = service(tmp_path)
    source, execution = shared_key_source_and_solution()
    current.enter(
        opportunity_episode_id="shared-episode", episode_lineage_id="shared-lineage", execution_batch_id="shared",
        source=source, partial_fill_proof=proof(execution), mode="AUTO", cap_config_version="caps-v1",
    )
    a_partial = replace(receipt(leg="a", filled=4, state="CANCELLED", sequence=1), execution_batch_id="shared", client_order_id="shared:action-a", venue_id="venue-a", account_id="account-a", receipt_id="shared-a")
    b_rejected = replace(receipt(leg="b", filled=0, state="REJECTED", sequence=1), execution_batch_id="shared", client_order_id="shared:action-b", venue_id="venue-a", account_id="account-a", receipt_id="shared-b")
    current.apply_receipt(a_partial)
    planned = current.apply_receipt(b_rejected, repair_context=shared_key_repair_context())
    assert planned["repair_plan"]["family"] == "COMPLETE_REMAINING"

    fresh = service(tmp_path / "reconciliation")
    source, execution = shared_key_source_and_solution()
    fresh.enter(
        opportunity_episode_id="shared-episode-2", episode_lineage_id="shared-lineage-2", execution_batch_id="shared",
        source=source, partial_fill_proof=proof(execution), mode="AUTO", cap_config_version="caps-v1",
    )
    for leg in ("a", "b"):
        fresh.apply_receipt(replace(receipt(leg=leg, filled=10, state="FILLED", sequence=1), execution_batch_id="shared", client_order_id=f"shared:action-{leg}", venue_id="venue-a", account_id="account-a", receipt_id=f"shared-{leg}"))
    assert fresh.complete_reconciliation("shared", context=shared_key_reconciliation_context())["state"] == "RECONCILED_FULL"


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

    assert conflict["incident"] == {"reason": "SAME_SEQUENCE_CONFLICT", "repair_status": "CONFLICT_UNRESOLVED"}
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
    assert conflict["unresolved_conflicts"][0]["kind"] == "SAME_PHYSICAL"


def test_reconciliation_rejects_unresolved_receipt_conflict(tmp_path) -> None:
    current = service(tmp_path)
    enter(current)
    old = receipt(leg="a", filled=10, state="FILLED", sequence=1)
    current.apply_receipt(old)
    current.apply_receipt(replace(old, receipt_id="a-conflict", cumulative_cost_units=1))
    current.apply_receipt(receipt(leg="b", filled=10, state="FILLED", sequence=1))

    with pytest.raises(ValueError, match="N_LEG_RECONCILIATION_CONFLICT_UNRESOLVED"):
        current.complete_reconciliation("batch-1", context=reconciliation_context(full=True))


def test_contexts_reject_malformed_members_before_dereference() -> None:
    repair = repair_context()
    with pytest.raises(ValueError, match="repair cash flows"):
        RepairContext(repair.reservation_version, repair.quotes, repair.holdings, repair.account_snapshot, (object(),), repair.now)  # type: ignore[arg-type]
    reconciliation = reconciliation_context()
    with pytest.raises(ValueError, match="reconciliation holdings"):
        ReconciliationContext(reconciliation.reservation_version, reconciliation.account_snapshot, (object(),), reconciliation.cash_flows, reconciliation.source_timestamp, reconciliation.received_at, reconciliation.now)  # type: ignore[arg-type]


def test_repair_context_allows_multiple_orders_for_one_reservation_key() -> None:
    repair = repair_context()
    duplicate_key = SettlementCashFlow("another-order", None, "venue-a", "account-a", "usd-cents", 0, 0, AS_OF, AS_OF, 1, False)
    accepted = RepairContext(repair.reservation_version, repair.quotes, repair.holdings, repair.account_snapshot, (*repair.cash_flows, duplicate_key), repair.now)

    assert len(accepted.cash_flows) == 3


def test_conflicts_sum_uncertain_exposure_per_order(tmp_path) -> None:
    current = service(tmp_path)
    enter(current)
    old_a = receipt(leg="a", filled=10, state="FILLED", sequence=1)
    old_b = receipt(leg="b", filled=10, state="FILLED", sequence=1)
    current.apply_receipt(old_a)
    current.apply_receipt(old_b)
    current.apply_receipt(replace(old_a, receipt_id="a-cost-conflict", cumulative_cost_units=700))
    conflicted = current.apply_receipt(replace(old_b, receipt_id="b-cost-conflict", cumulative_cost_units=800))

    assert sorted(conflicted["conflict_exposure_by_physical_order"].values()) == [190, 290]
    assert current.control()["total_unsettled_capital_units"] == 1500


def test_same_order_newer_terminal_receipt_resolves_conflict_and_reconciles(tmp_path) -> None:
    current = service(tmp_path)
    enter(current)
    old = receipt(leg="a", filled=0, state="OPEN", sequence=1)
    current.apply_receipt(old)
    conflicted = current.apply_receipt(replace(old, receipt_id="a-conflict", state="UNKNOWN"))
    assert conflicted["unresolved_conflicts"]

    resolved = current.apply_receipt(replace(old, receipt_id="a-final", state="REJECTED", sequence=2))
    assert resolved["unresolved_conflicts"] == []
    assert resolved["receipt_conflicts"]  # immutable audit remains
    current.apply_receipt(receipt(leg="b", filled=0, state="REJECTED", sequence=1))

    assert current.complete_reconciliation("batch-1", context=reconciliation_context(a_sequence=2))["state"] == "RECONCILED_ZERO"


def test_intended_order_rest_cannot_clear_drifted_physical_order(tmp_path) -> None:
    current = service(tmp_path)
    enter(current)
    original = replace(receipt(leg="a", filled=0, state="OPEN", sequence=1), venue_order_id="venue-order-1")
    current.apply_receipt(original)
    drift = current.apply_receipt(replace(original, receipt_id="drift", venue_order_id="venue-order-2", sequence=2))
    assert drift["unresolved_conflicts"]

    untrusted = current.apply_receipt(replace(original, receipt_id="untrusted", state="REJECTED", sequence=3))
    assert untrusted["unresolved_conflicts"]
    resolved = current.apply_receipt(replace(original, receipt_id="rest-final", state="REJECTED", sequence=None, rest_confirmed=True, rest_observation_version=1))

    assert resolved["unresolved_conflicts"]
    assert resolved["receipt_conflicts"]


def test_conflict_invalidates_existing_repair_plan(tmp_path) -> None:
    current = service(tmp_path)
    enter(current)
    current.apply_receipt(receipt(leg="a", filled=4, state="OPEN", sequence=1))
    current.apply_receipt(receipt(leg="b", filled=0, state="REJECTED", sequence=1))
    planned = current.apply_receipt(receipt(leg="a", filled=4, state="CANCELLED", sequence=2), repair_context=repair_context())
    assert planned["repair_plan"] is not None

    conflicted = current.apply_receipt(replace(receipt(leg="a", filled=4, state="CANCELLED", sequence=2), receipt_id="a-conflict", cumulative_cost_units=1))
    assert conflicted["unresolved_conflicts"]
    assert conflicted["repair_plan"] is None


def test_same_physical_conflict_requires_sequence_newer_than_all_conflicting_evidence(tmp_path) -> None:
    current = service(tmp_path)
    enter(current)
    first = receipt(leg="a", filled=0, state="OPEN", sequence=1)
    current.apply_receipt(first)
    high = current.apply_receipt(replace(first, cumulative_filled_quantity=1, sequence=100))
    assert high["unresolved_conflicts"][0]["max_sequence"] == 100

    stale = current.apply_receipt(replace(first, receipt_id="a-final", state="REJECTED", sequence=2))
    assert stale["unresolved_conflicts"]


def test_sequence_none_rest_conflict_requires_newer_rest_observation_version(tmp_path) -> None:
    current = service(tmp_path)
    enter(current)
    initial = replace(receipt(leg="a", filled=0, state="OPEN", sequence=1), receipt_id="a-rest", sequence=None, rest_confirmed=True, rest_observation_version=10)
    current.apply_receipt(initial)
    current.apply_receipt(replace(initial, state="UNKNOWN", rest_observation_version=11))

    same_version = current.apply_receipt(replace(initial, receipt_id="a-final-11", state="REJECTED", rest_observation_version=11, observed_at="2026-08-14T00:00:01Z", venue_timestamp="2026-08-14T00:00:01Z"))
    assert same_version["unresolved_conflicts"]
    newer = current.apply_receipt(replace(initial, receipt_id="a-final-12", state="REJECTED", rest_observation_version=12, observed_at="2026-08-14T00:00:02Z", venue_timestamp="2026-08-14T00:00:02Z"))
    assert newer["unresolved_conflicts"] == []


def test_rest_versions_are_monotonic_across_receipt_ids_and_times(tmp_path) -> None:
    current = service(tmp_path)
    enter(current)
    first = replace(receipt(leg="a", filled=0, state="OPEN", sequence=1), receipt_id="rest-1", sequence=None, rest_confirmed=True, rest_observation_version=1)
    accepted = current.apply_receipt(replace(first, receipt_id="rest-2", rest_observation_version=2, observed_at="2026-08-14T00:00:01Z", venue_timestamp="2026-08-14T00:00:01Z"))
    assert current.apply_receipt(replace(first, receipt_id="rest-old", observed_at="2026-08-13T00:00:00Z", venue_timestamp="2026-08-13T00:00:00Z")) == accepted
    conflicted = current.apply_receipt(replace(first, receipt_id="rest-same", state="UNKNOWN", rest_observation_version=2, observed_at="2026-08-14T00:00:02Z", venue_timestamp="2026-08-14T00:00:02Z"))
    assert conflicted["incident"]["reason"] == "REST_OBSERVATION_CONFLICT"
    timed = current.apply_receipt(replace(first, receipt_id="rest-time", rest_observation_version=3, observed_at="2026-08-14T00:00:00Z", venue_timestamp="2026-08-14T00:00:00Z"))
    assert timed["incident"]["reason"] == "RECEIPT_TIME_REGRESSION"

    sequenced = service(tmp_path / "sequenced")
    enter(sequenced)
    sequenced.apply_receipt(receipt(leg="a", filled=0, state="OPEN", sequence=1))
    time_regression = sequenced.apply_receipt(replace(receipt(leg="a", filled=0, state="OPEN", sequence=2), observed_at="2026-08-13T00:00:00Z", venue_timestamp="2026-08-13T00:00:00Z"))
    assert time_regression["incident"]["reason"] == "RECEIPT_TIME_REGRESSION"


def test_drift_and_unknown_conflicts_reserve_reported_or_known_exposure(tmp_path) -> None:
    current = service(tmp_path)
    enter(current)
    original = replace(receipt(leg="a", filled=0, state="OPEN", sequence=1), venue_order_id="venue-order-1")
    current.apply_receipt(original)
    first = current.apply_receipt(replace(original, receipt_id="drift-2", venue_order_id="venue-order-2", sequence=2, cumulative_cost_units=700))
    second = current.apply_receipt(replace(original, receipt_id="drift-3", venue_order_id="venue-order-3", sequence=3, cumulative_cost_units=900))
    assert sorted(second["conflict_exposure_by_physical_order"].values()) == [700, 900]
    assert current.control()["total_unsettled_capital_units"] == 2620

    unknown = service(tmp_path / "unknown-exposure")
    enter(unknown)
    state = unknown.apply_receipt(replace(receipt(leg="a", filled=0, state="OPEN", sequence=1), client_order_id="unknown", receipt_id="unknown-zero"))
    assert state["capital_exposure_unknown"] is True
    assert list(state["conflict_exposure_by_physical_order"].values()) == [510]


def test_terminal_conflict_evidence_is_durable_economic_state_not_zero_reconciliation(tmp_path) -> None:
    current = service(tmp_path)
    enter(current)
    original = replace(receipt(leg="a", filled=0, state="OPEN", sequence=1), venue_order_id="venue-order-1")
    current.apply_receipt(original)
    conflict = current.apply_receipt(replace(original, receipt_id="drift", venue_order_id="venue-order-2", sequence=2))["unresolved_conflicts"][0]
    current.apply_receipt(replace(original, receipt_id="a-final", state="REJECTED", sequence=3))
    current.apply_receipt(receipt(leg="b", filled=0, state="REJECTED", sequence=1))
    evidence = replace(resolution_for(conflict), cumulative_filled_quantity=1, cumulative_cost_units=0, sequence=4)
    with pytest.raises(ValueError, match="CONFLICT_UNRESOLVED"):
        current.complete_reconciliation("batch-1", context=reconciliation_context(a_sequence=3, a_venue_order="venue-order-1", resolutions=(replace(evidence, source_timestamp=AS_OF, observed_at=AS_OF),)))
    assert current.state("batch-1")["unresolved_conflicts"]
    context = replace(
        reconciliation_context(a_sequence=3, a_venue_order="venue-order-1", resolutions=(evidence,), now=AS_OF + timedelta(seconds=2)),
        holdings=(ConfirmedHolding("venue-a", "account-a", "action-a", 1, AS_OF, AS_OF),),
    )
    result = current.complete_reconciliation("batch-1", context=context)
    assert result["state"] == "RECONCILED_FULL"
    assert result["conflict_terminal_ledger"][conflict["physical_key"]]["holding_capital_units"] == 51
    assert result["total_unsettled_capital_units"] == current.control()["total_unsettled_capital_units"] == 51


def test_same_physical_terminal_resolution_merges_one_canonical_holding(tmp_path) -> None:
    current = service(tmp_path)
    enter(current)
    first = receipt(leg="a", filled=0, state="OPEN", sequence=1)
    current.apply_receipt(first)
    conflict = current.apply_receipt(replace(first, cumulative_filled_quantity=1, sequence=100))["unresolved_conflicts"][0]
    current.apply_receipt(replace(first, receipt_id="a-final", state="REJECTED", sequence=2))
    current.apply_receipt(receipt(leg="b", filled=0, state="REJECTED", sequence=1))
    evidence = replace(resolution_for(conflict), cumulative_filled_quantity=10, sequence=101)
    context = replace(
        reconciliation_context(a_sequence=101, resolutions=(evidence,), now=AS_OF + timedelta(seconds=2)),
        holdings=(ConfirmedHolding("venue-a", "account-a", "action-a", 10, AS_OF, AS_OF),),
        cash_flows=(SettlementCashFlow("batch-1:action-a", None, "venue-a", "account-a", "usd-cents", 0, 0, AS_OF + timedelta(seconds=1), AS_OF + timedelta(seconds=1), 101, False), SettlementCashFlow("batch-1:action-b", None, "venue-b", "account-b", "usd-cents", 0, 0, AS_OF, AS_OF, 1, False)),
    )
    result = current.complete_reconciliation("batch-1", context=context)
    assert result["conflict_terminal_ledger"] == {}
    assert result["confirmed_holdings"] == [{"venue_id": "venue-a", "account_id": "account-a", "asset_id": "action-a", "quantity": 10}]


def test_cross_domain_and_conflicting_same_physical_evidence_fail_atomically(tmp_path) -> None:
    current = service(tmp_path)
    enter(current)
    rest = replace(receipt(leg="a", filled=0, state="OPEN", sequence=1), receipt_id="rest", sequence=None, rest_confirmed=True, rest_observation_version=1)
    current.apply_receipt(rest)
    conflict = current.apply_receipt(replace(rest, state="UNKNOWN", rest_observation_version=100))["unresolved_conflicts"][0]
    current.apply_receipt(replace(rest, receipt_id="a-final", state="REJECTED", sequence=2, rest_confirmed=False, rest_observation_version=None, observed_at="2026-08-14T00:00:01Z", venue_timestamp="2026-08-14T00:00:01Z"))
    current.apply_receipt(receipt(leg="b", filled=0, state="REJECTED", sequence=1))
    with pytest.raises(ValueError, match="CONFLICT_UNRESOLVED"):
        current.complete_reconciliation("batch-1", context=reconciliation_context(a_sequence=3, resolutions=(resolution_for(conflict),), now=AS_OF + timedelta(seconds=2)))
    assert current.state("batch-1")["unresolved_conflicts"]

    multiple = service(tmp_path / "multiple")
    enter(multiple)
    first = receipt(leg="a", filled=0, state="OPEN", sequence=1)
    multiple.apply_receipt(first)
    one = multiple.apply_receipt(replace(first, cumulative_cost_units=5, sequence=100))["unresolved_conflicts"][0]
    two = multiple.apply_receipt(replace(first, receipt_id="a-second", state="UNKNOWN"))["unresolved_conflicts"][1]
    multiple.apply_receipt(replace(first, receipt_id="a-final", state="REJECTED", sequence=2))
    multiple.apply_receipt(receipt(leg="b", filled=0, state="REJECTED", sequence=1))
    high = replace(resolution_for(one), cumulative_cost_units=5, sequence=101)
    low = replace(resolution_for(two), cumulative_cost_units=0, sequence=101)
    with pytest.raises(ValueError, match="CONFLICT_UNRESOLVED"):
        multiple.complete_reconciliation("batch-1", context=reconciliation_context(a_sequence=2, resolutions=(high, low), now=AS_OF + timedelta(seconds=2)))
    assert multiple.state("batch-1")["unresolved_conflicts"]


def test_distinct_drift_ledger_requires_its_own_settlement_account_balance(tmp_path) -> None:
    current = service(tmp_path)
    enter(current)
    current.apply_receipt(receipt(leg="a", filled=0, state="OPEN", sequence=1))
    conflict = current.apply_receipt(replace(receipt(leg="a", filled=0, state="OPEN", sequence=2), receipt_id="foreign", venue_id="venue-c", account_id="account-c"))["unresolved_conflicts"][0]
    current.apply_receipt(receipt(leg="a", filled=0, state="REJECTED", sequence=3))
    current.apply_receipt(receipt(leg="b", filled=0, state="REJECTED", sequence=1))
    evidence = replace(resolution_for(conflict), cumulative_filled_quantity=1, sequence=4)
    context = replace(
        reconciliation_context(a_sequence=3, resolutions=(evidence,), now=AS_OF + timedelta(seconds=2)),
        holdings=(ConfirmedHolding("venue-c", "account-c", "action-a", 1, AS_OF, AS_OF),),
    )
    with pytest.raises(ValueError, match="PROOF_REQUIRED"):
        current.complete_reconciliation("batch-1", context=context)
    assert current.state("batch-1")["unresolved_conflicts"]


def test_unbound_terminal_evidence_cannot_proxy_known_leg_binding(tmp_path) -> None:
    current = service(tmp_path)
    enter(current)
    conflict = current.apply_receipt(replace(receipt(leg="a", filled=0, state="OPEN", sequence=1), client_order_id="unknown-client", receipt_id="unknown"))["unresolved_conflicts"][0]
    current.apply_receipt(receipt(leg="a", filled=0, state="REJECTED", sequence=1))
    current.apply_receipt(receipt(leg="b", filled=0, state="REJECTED", sequence=1))
    with pytest.raises(ValueError, match="CONFLICT_UNRESOLVED"):
        current.complete_reconciliation("batch-1", context=reconciliation_context(resolutions=(resolution_for(conflict),), now=AS_OF + timedelta(seconds=2)))
    assert current.state("batch-1")["capital_exposure_unknown"] is True


def test_exact_conflict_evidence_reconciles_drift_and_unknown_order(tmp_path) -> None:
    current = service(tmp_path)
    enter(current)
    original = replace(receipt(leg="a", filled=0, state="OPEN", sequence=1), venue_order_id="venue-order-1")
    current.apply_receipt(original)
    drift = current.apply_receipt(replace(original, receipt_id="drift", venue_order_id="venue-order-2", sequence=2))
    conflict = drift["unresolved_conflicts"][0]
    current.apply_receipt(replace(original, receipt_id="a-final", state="REJECTED", sequence=3))
    current.apply_receipt(receipt(leg="b", filled=0, state="REJECTED", sequence=1))
    with pytest.raises(ValueError, match="CONFLICT_UNRESOLVED"):
        current.complete_reconciliation("batch-1", context=reconciliation_context(a_sequence=3, a_venue_order="venue-order-1"))
    assert current.complete_reconciliation("batch-1", context=reconciliation_context(a_sequence=3, a_venue_order="venue-order-1", resolutions=(resolution_for(conflict),), now=AS_OF + timedelta(seconds=2)))["state"] == "RECONCILED_ZERO"

    unknown = service(tmp_path / "unknown")
    enter(unknown)
    unknown_receipt = replace(receipt(leg="a", filled=0, state="OPEN", sequence=1), client_order_id="unknown-client", receipt_id="unknown")
    unresolved = unknown.apply_receipt(unknown_receipt)
    assert unresolved["capital_exposure_unknown"] is True
    conflict = unresolved["unresolved_conflicts"][0]
    unknown.apply_receipt(receipt(leg="a", filled=0, state="REJECTED", sequence=1))
    unknown.apply_receipt(receipt(leg="b", filled=0, state="REJECTED", sequence=1))
    assert unknown.complete_reconciliation("batch-1", context=reconciliation_context(resolutions=(resolution_for(conflict, outcome="NOT_FOUND"),), now=AS_OF + timedelta(seconds=2)))["state"] == "RECONCILED_ZERO"


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
    closed = current.complete_reconciliation("batch-1", context=reconciliation_context(full=True, a_sequence=2))
    assert closed["incident"] is None
    assert closed["state"] == "RECONCILED_FULL"
    assert current.control()["mode"] == "MANUAL"
