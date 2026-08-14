"""Durable, no-submit N-leg receipt reduction and repair-plan selection."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Mapping

from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from open_trader.prediction_executable_cost import (
    AccountSnapshot, ExecutionSolution, ImmutableBook, VerifiedComponent,
    execution_solution_from_payload, market_solution_from_payload,
)
from open_trader.prediction_n_leg import canonical_payload, fingerprint


ORDER_RECEIPT_SCHEMA_V1 = "open_trader.prediction_n_leg.order_receipt.v1"
PARTIAL_FILL_PROOF_SCHEMA_V1 = "open_trader.prediction_n_leg.partial_fill_proof.v1"
REPAIR_PLAN_SCHEMA_V1 = "open_trader.prediction_n_leg.repair_plan.v1"
_TERMINAL = frozenset({"FILLED", "REJECTED", "CANCELLED"})
_STATES = _TERMINAL | {"OPEN", "UNKNOWN"}


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{name} must be non-empty text")
    return value


def _nonnegative(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _timestamp(value: object, name: str) -> str:
    text = _text(value, name)
    parsed = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        moment = datetime.fromisoformat(parsed)
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO timestamp") from exc
    if moment.tzinfo is None:
        raise ValueError(f"{name} must include timezone")
    return moment.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class OrderReceipt:
    receipt_id: str
    execution_batch_id: str
    client_order_id: str
    venue_id: str
    account_id: str
    venue_order_id: str | None
    submitted_quantity: int
    cumulative_filled_quantity: int
    cumulative_fee_units: int
    state: Literal["OPEN", "FILLED", "REJECTED", "CANCELLED", "UNKNOWN"]
    sequence: int | None
    rest_confirmed: bool
    observed_at: str
    venue_timestamp: str
    schema_version: str = ORDER_RECEIPT_SCHEMA_V1

    def __post_init__(self) -> None:
        if self.schema_version != ORDER_RECEIPT_SCHEMA_V1:
            raise ValueError("unsupported order receipt schema")
        for name in ("receipt_id", "execution_batch_id", "client_order_id", "venue_id", "account_id"):
            _text(getattr(self, name), name)
        if self.venue_order_id is not None:
            _text(self.venue_order_id, "venue_order_id")
        submitted = _nonnegative(self.submitted_quantity, "submitted_quantity")
        filled = _nonnegative(self.cumulative_filled_quantity, "cumulative_filled_quantity")
        _nonnegative(self.cumulative_fee_units, "cumulative_fee_units")
        if submitted == 0 or filled > submitted or self.state not in _STATES:
            raise ValueError("invalid cumulative order receipt")
        if self.sequence is not None and (type(self.sequence) is not int or self.sequence < 0):
            raise ValueError("sequence must be a non-negative integer or null")
        if type(self.rest_confirmed) is not bool:
            raise ValueError("rest_confirmed must be boolean")
        if self.sequence is None and not self.rest_confirmed:
            raise ValueError("sequence-less receipt requires REST confirmation")
        object.__setattr__(self, "observed_at", _timestamp(self.observed_at, "observed_at"))
        object.__setattr__(self, "venue_timestamp", _timestamp(self.venue_timestamp, "venue_timestamp"))

    def to_payload(self) -> dict[str, object]:
        return canonical_payload(asdict(self))


def order_receipt_from_payload(payload: object) -> OrderReceipt:
    keys = {
        "schema_version", "receipt_id", "execution_batch_id", "client_order_id", "venue_id", "account_id",
        "venue_order_id", "submitted_quantity", "cumulative_filled_quantity", "cumulative_fee_units", "state",
        "sequence", "rest_confirmed", "observed_at", "venue_timestamp",
    }
    if not isinstance(payload, dict) or set(payload) != keys:
        raise ValueError("unexpected OrderReceipt fields")
    return OrderReceipt(**payload)  # type: ignore[arg-type]


def execution_solution_binding(solution: ExecutionSolution) -> dict[str, str]:
    """The stable facts that #74 must bind in its final proof record."""
    if not isinstance(solution, ExecutionSolution) or solution.order_ready or solution.reason != "PARTIAL_FILL_PROOF_REQUIRED":
        raise ValueError("execution solution is not a no-submit partial-fill handoff")
    expected = fingerprint({
        "market": solution.market_solution_fingerprint,
        "account": solution.account_snapshot_fingerprint,
        "quantities": solution.quantities,
        "capital_use_units": solution.capital_use_units,
        "execution_legs": solution.execution_legs,
    })
    if solution.fingerprint != expected:
        raise ValueError("execution solution fingerprint mismatch")
    return {
        "execution_solution_fingerprint": solution.fingerprint,
        "execution_solution_payload_fingerprint": fingerprint(canonical_payload(solution)),
        "model_fingerprint": solution.market_solution_fingerprint,
        "quote_fingerprint": fingerprint({"sources": tuple(leg.source_fingerprint for leg in solution.execution_legs)}),
        "cost_fingerprint": fingerprint({"capital_use_units": solution.capital_use_units, "legs": tuple((leg.action_id, leg.max_cost_units, leg.max_fee_units) for leg in solution.execution_legs)}),
        "order_semantics_fingerprint": fingerprint({"legs": solution.execution_legs}),
    }


@dataclass(frozen=True, slots=True)
class PartialFillProofRecord:
    execution_solution_fingerprint: str
    execution_solution_payload_fingerprint: str
    model_fingerprint: str
    quote_fingerprint: str
    cost_fingerprint: str
    order_semantics_fingerprint: str
    cap_config_version: str
    max_partial_fill_loss: int
    max_auto_repair_loss: int
    solver_lower_bound: int
    solver_upper_bound: int
    solver_termination: str
    solver_evidence_fingerprint: str
    verifier_status: str
    verifier_fingerprint: str
    verifier_evidence_fingerprint: str
    status: str
    fingerprint: str
    schema_version: str = PARTIAL_FILL_PROOF_SCHEMA_V1

    def __post_init__(self) -> None:
        if self.schema_version != PARTIAL_FILL_PROOF_SCHEMA_V1:
            raise ValueError("unsupported partial-fill proof schema")
        for name in (
            "execution_solution_fingerprint", "execution_solution_payload_fingerprint", "model_fingerprint", "quote_fingerprint", "cost_fingerprint",
            "order_semantics_fingerprint", "cap_config_version", "solver_termination", "verifier_status",
            "solver_evidence_fingerprint", "verifier_fingerprint", "verifier_evidence_fingerprint", "status", "fingerprint",
        ):
            _text(getattr(self, name), name)
        for name in ("max_partial_fill_loss", "max_auto_repair_loss", "solver_lower_bound", "solver_upper_bound"):
            _nonnegative(getattr(self, name), name)
        if self.solver_lower_bound > self.solver_upper_bound:
            raise ValueError("solver bounds are inverted")
        expected = fingerprint({key: value for key, value in asdict(self).items() if key != "fingerprint"})
        if self.fingerprint != expected:
            raise ValueError("partial-fill proof fingerprint mismatch")

    def to_payload(self) -> dict[str, object]:
        return canonical_payload(asdict(self))


def partial_fill_proof_from_payload(payload: object) -> PartialFillProofRecord:
    keys = set(PartialFillProofRecord.__dataclass_fields__)
    if not isinstance(payload, dict) or set(payload) != keys:
        raise ValueError("unexpected PartialFillProofRecord fields")
    return PartialFillProofRecord(**payload)  # type: ignore[arg-type]


def _proof_is_bound(proof: PartialFillProofRecord, solution: ExecutionSolution) -> bool:
    try:
        binding = execution_solution_binding(solution)
    except ValueError:
        return False
    return (
        proof.status == "PARTIAL_FILL_SAFE"
        and proof.verifier_status == "QUALIFIED_VERIFIED"
        and proof.solver_termination == "CLOSED"
        and all(proof.to_payload()[key] == value for key, value in binding.items())
    )


@dataclass(frozen=True, slots=True)
class ExecutionSolutionSource:
    """All public #51 inputs required to independently re-decode an Entry."""
    execution_solution_payload: Mapping[str, object]
    market_solution_payload: Mapping[str, object]
    component: VerifiedComponent
    books: tuple[ImmutableBook, ...]
    account_snapshot: AccountSnapshot
    now: datetime

    def decode(self) -> ExecutionSolution:
        market = market_solution_from_payload(
            dict(self.market_solution_payload), component=self.component, books=self.books, now=self.now,
        )
        return execution_solution_from_payload(
            dict(self.execution_solution_payload), market_solution=market,
            account_snapshot=self.account_snapshot, now=self.now,
        )


@dataclass(frozen=True, slots=True)
class RepairContext:
    """Canonical, caller-supplied current quotes and reconciled inventory facts."""
    reservation_version: str
    model_fingerprint: str
    quote_fingerprint: str
    account_fingerprint: str
    occurred_cost_units: int
    occurred_fee_units: int
    quotes: tuple[tuple[str, int, int], ...]  # client order, BUY max cost, SELL cost
    holdings: tuple[tuple[str, int], ...]
    fingerprint: str

    def __post_init__(self) -> None:
        for name in ("reservation_version", "model_fingerprint", "quote_fingerprint", "account_fingerprint", "fingerprint"):
            _text(getattr(self, name), name)
        _nonnegative(self.occurred_cost_units, "occurred_cost_units")
        _nonnegative(self.occurred_fee_units, "occurred_fee_units")
        if any(not isinstance(item, tuple) or len(item) != 3 or not isinstance(item[0], str) or type(item[1]) is not int or type(item[2]) is not int or item[1] < 0 or item[2] < 0 for item in self.quotes):
            raise ValueError("invalid repair quotes")
        if any(not isinstance(item, tuple) or len(item) != 2 or not isinstance(item[0], str) or type(item[1]) is not int or item[1] < 0 for item in self.holdings):
            raise ValueError("invalid repair holdings")
        expected = fingerprint({key: value for key, value in asdict(self).items() if key != "fingerprint"})
        if self.fingerprint != expected:
            raise ValueError("repair context fingerprint mismatch")


class NLegExecutionService:
    """The single public behavior seam for durable no-submit N-leg execution."""

    def __init__(self, store: PredictionArbitrageStore) -> None:
        self._store = store

    def enter(
        self,
        *,
        opportunity_episode_id: str,
        episode_lineage_id: str,
        execution_batch_id: str,
        source: ExecutionSolutionSource,
        partial_fill_proof: PartialFillProofRecord,
        mode: Literal["MANUAL", "AUTO"],
        cap_config_version: str,
    ) -> dict[str, object]:
        for name, value in (("opportunity_episode_id", opportunity_episode_id), ("episode_lineage_id", episode_lineage_id), ("execution_batch_id", execution_batch_id)):
            _text(value, name)
        if mode not in {"MANUAL", "AUTO"}:
            raise ValueError("new N-leg mode must be MANUAL or AUTO")
        try:
            execution_solution = source.decode()
        except (TypeError, ValueError) as exc:
            raise ValueError("EXECUTION_SOLUTION_SOURCE_REQUIRED") from exc
        if not _proof_is_bound(partial_fill_proof, execution_solution) or partial_fill_proof.cap_config_version != _text(cap_config_version, "cap_config_version"):
            raise ValueError("PARTIAL_FILL_PROOF_REQUIRED")
        if partial_fill_proof.solver_upper_bound > partial_fill_proof.max_partial_fill_loss:
            raise ValueError("PARTIAL_FILL_LOSS_CAP_EXCEEDED")
        legs = []
        for leg in execution_solution.execution_legs:
            client_order_id = f"{execution_batch_id}:{leg.action_id}"
            legs.append({
                "action_id": leg.action_id, "client_order_id": client_order_id, "venue_id": leg.venue_id,
                "account_id": leg.account_id, "asset_id": leg.native_id, "side": leg.side,
                "submitted_quantity": leg.quantity_lots, "max_cost_units": leg.max_cost_units,
                "max_fee_units": leg.max_fee_units, "receipt": None,
            })
        if not legs or len({leg["client_order_id"] for leg in legs}) != len(legs):
            raise ValueError("execution solution has invalid legs")
        payload: dict[str, object] = {
            "execution_batch_id": execution_batch_id,
            "opportunity_episode_id": opportunity_episode_id,
            "episode_lineage_id": episode_lineage_id,
            "mode": mode,
            "submission_enabled": False,
            "state": "ACTIVE",
            "execution_solution_fingerprint": execution_solution.fingerprint,
            "partial_fill_proof": partial_fill_proof.to_payload(),
            "max_partial_fill_loss": partial_fill_proof.max_partial_fill_loss,
            "max_auto_repair_loss": partial_fill_proof.max_auto_repair_loss,
            "cap_config_version": partial_fill_proof.cap_config_version,
            "reservation_units": execution_solution.capital_use_units,
            "reservation_version": f"{execution_batch_id}:v1",
            "total_unsettled_capital_units": execution_solution.capital_use_units,
            "legs": legs,
            "receipts": {},
            "confirmed_holdings": [],
            "receipt_conflicts": [],
            "incident": None,
            "repair_plan": None,
        }
        return self._store.n_leg_create_batch(payload)

    def state(self, execution_batch_id: str) -> dict[str, object] | None:
        return self._store.n_leg_batch(execution_batch_id)

    def control(self) -> dict[str, object]:
        return self._store.n_leg_control()

    def apply_receipt(
        self,
        receipt: OrderReceipt,
        *,
        repair_context: RepairContext | None = None,
    ) -> dict[str, object]:
        def reduce(batch: dict[str, object], control: dict[str, object]) -> tuple[dict[str, object], dict[str, object], bool]:
            next_batch, incident_reason, changed = self._reduce(batch, receipt)
            if not changed:
                return batch, control, False
            existing_incident = next_batch.get("incident")
            if incident_reason is not None:
                next_batch, control = self._open_incident(next_batch, control, incident_reason, repair_context)
            elif isinstance(existing_incident, dict):
                if self._all_terminal(next_batch):
                    if self._all_full(next_batch) or self._all_zero(next_batch):
                        next_batch["state"] = "AWAITING_RECONCILIATION"
                        control["mode"] = "MANUAL"
                        control["active_batch_id"] = next_batch["execution_batch_id"]
                    else:
                        next_batch, control = self._open_incident(next_batch, control, str(existing_incident.get("reason", "INCIDENT")), repair_context)
                else:
                    control["mode"] = "MANUAL"
                    control["active_batch_id"] = next_batch["execution_batch_id"]
            elif self._all_terminal(next_batch):
                if self._all_full(next_batch) or self._all_zero(next_batch):
                    next_batch["state"] = "AWAITING_RECONCILIATION"
                else:
                    next_batch, control = self._open_incident(next_batch, control, "MIXED_TERMINAL_FILL", repair_context)
            return next_batch, control, True
        return self._store.n_leg_reduce(receipt.execution_batch_id, idempotency_key=receipt.receipt_id, reducer=reduce)

    def complete_reconciliation(
        self, execution_batch_id: str, *, context: Mapping[str, object]
    ) -> dict[str, object]:
        batch = self.state(execution_batch_id)
        if batch is None or not self._all_terminal(batch):
            raise ValueError("N_LEG_RECONCILIATION_NOT_READY")
        required = {"fresh", "balance_fingerprint", "holding_fingerprint", "reservation_version"}
        if (
            not isinstance(context, Mapping) or set(context) != required or context.get("fresh") is not True
            or context.get("reservation_version") != batch.get("reservation_version")
            or any(not isinstance(context.get(key), str) or not context[key] for key in ("balance_fingerprint", "holding_fingerprint"))
            or not (self._all_full(batch) or self._all_zero(batch))
        ):
            raise ValueError("N_LEG_RECONCILIATION_PROOF_REQUIRED")
        control = self.control()
        result = dict(batch)
        had_incident = result.get("incident") is not None
        result["reconciliation"] = dict(context)
        result["state"] = "RECONCILED_ZERO" if self._all_zero(batch) else "RECONCILED_FULL"
        result["incident"] = None
        control["active_batch_id"] = None
        if self._all_zero(batch):
            control["total_unsettled_capital_units"] = max(0, int(control["total_unsettled_capital_units"]) - int(batch["reservation_units"]))
        if had_incident:
            control["mode"] = "MANUAL"
        return self._store.n_leg_replace_batch(
            result, control=control, transition_kind="RECONCILIATION", idempotency_key=f"reconcile:{execution_batch_id}:{fingerprint(dict(context))}",
        )

    def _reduce(self, batch: dict[str, object], receipt: OrderReceipt) -> tuple[dict[str, object], str | None, bool]:
        if receipt.execution_batch_id != batch["execution_batch_id"]:
            raise ValueError("receipt batch mismatch")
        copy = dict(batch)
        raw_legs = copy.get("legs")
        raw_receipts = copy.get("receipts")
        if not isinstance(raw_legs, list) or not isinstance(raw_receipts, dict):
            raise ValueError("corrupt n-leg batch")
        legs = [dict(leg) for leg in raw_legs]
        receipts = dict(raw_receipts)
        encoded = receipt.to_payload()
        known = receipts.get(receipt.receipt_id)
        if known is not None:
            if known == encoded:
                return batch, None, False
            return self._incident_copy(copy, legs, receipts, receipt, "RECEIPT_ID_CONFLICT")
        matches = [leg for leg in legs if leg.get("client_order_id") == receipt.client_order_id]
        if len(matches) != 1:
            return self._incident_copy(copy, legs, receipts, receipt, "RECEIPT_IDENTITY_DRIFT")
        leg = matches[0]
        if any(leg.get(name) != getattr(receipt, name) for name in ("venue_id", "account_id")) or leg.get("submitted_quantity") != receipt.submitted_quantity:
            return self._incident_copy(copy, legs, receipts, receipt, "RECEIPT_IDENTITY_DRIFT")
        prior = leg.get("receipt")
        if isinstance(prior, dict):
            prior_sequence = prior.get("sequence")
            if isinstance(prior_sequence, int) and isinstance(receipt.sequence, int) and receipt.sequence < prior_sequence:
                return batch, None, False
            if isinstance(prior_sequence, int) and isinstance(receipt.sequence, int) and receipt.sequence == prior_sequence:
                if prior == encoded:
                    return batch, None, False
                return self._incident_copy(copy, legs, receipts, receipt, "SAME_SEQUENCE_CONFLICT")
            if receipt.cumulative_filled_quantity < prior.get("cumulative_filled_quantity", 0) or receipt.cumulative_fee_units < prior.get("cumulative_fee_units", 0):
                return self._incident_copy(copy, legs, receipts, receipt, "CUMULATIVE_REGRESSION")
            if prior.get("state") in _TERMINAL and receipt.state not in _TERMINAL:
                return self._incident_copy(copy, legs, receipts, receipt, "TERMINAL_REOPENED")
        leg["receipt"] = encoded
        receipts[receipt.receipt_id] = encoded
        copy["legs"] = legs
        copy["receipts"] = receipts
        copy["confirmed_holdings"] = [
            {
                "venue_id": leg["venue_id"], "account_id": leg["account_id"],
                "asset_id": leg["asset_id"], "quantity": leg["receipt"]["cumulative_filled_quantity"],
            }
            for leg in legs
            if isinstance(leg.get("receipt"), dict) and leg["receipt"]["cumulative_filled_quantity"] > 0
        ]
        if receipt.state == "UNKNOWN" or (0 < receipt.cumulative_filled_quantity < receipt.submitted_quantity):
            return copy, "UNKNOWN_ORDER_STATE" if receipt.state == "UNKNOWN" else "PARTIAL_FILL", True
        return copy, None, True

    @staticmethod
    def _incident_copy(copy: dict[str, object], legs: list[dict[str, object]], receipts: dict[str, object], receipt: OrderReceipt, reason: str) -> tuple[dict[str, object], str, bool]:
        receipts[receipt.receipt_id] = receipt.to_payload()
        copy["legs"] = legs
        copy["receipts"] = receipts
        prior = next(
            (leg.get("receipt") for leg in legs if leg.get("client_order_id") == receipt.client_order_id and isinstance(leg.get("receipt"), dict)),
            None,
        )
        if isinstance(prior, dict):
            existing = copy.get("receipt_conflicts")
            copy["receipt_conflicts"] = [*(existing if isinstance(existing, list) else []), {"reason": reason, "old": prior, "new": receipt.to_payload()}]
        return copy, reason, True

    @staticmethod
    def _all_terminal(batch: Mapping[str, object]) -> bool:
        legs = batch.get("legs")
        return isinstance(legs, list) and bool(legs) and all(isinstance(leg, dict) and isinstance(leg.get("receipt"), dict) and leg["receipt"].get("state") in _TERMINAL for leg in legs)

    @staticmethod
    def _all_zero(batch: Mapping[str, object]) -> bool:
        legs = batch.get("legs")
        return isinstance(legs, list) and bool(legs) and all(isinstance(leg, dict) and isinstance(leg.get("receipt"), dict) and leg["receipt"].get("cumulative_filled_quantity") == 0 for leg in legs)

    @staticmethod
    def _all_full(batch: Mapping[str, object]) -> bool:
        legs = batch.get("legs")
        return isinstance(legs, list) and bool(legs) and all(
            isinstance(leg, dict) and isinstance(leg.get("receipt"), dict)
            and leg["receipt"].get("cumulative_filled_quantity") == leg.get("submitted_quantity")
            for leg in legs
        )

    def _open_incident(
        self,
        batch: dict[str, object],
        control: dict[str, object],
        reason: str,
        repair_context: RepairContext | None,
    ) -> tuple[dict[str, object], dict[str, object]]:
        batch["state"] = "INCIDENT"
        batch["incident"] = {"reason": reason, "repair_status": "PENDING_RECONCILIATION"}
        control = dict(control)
        control["mode"] = "MANUAL"
        control["active_batch_id"] = batch["execution_batch_id"]
        control["total_unsettled_capital_units"] = batch["total_unsettled_capital_units"]
        if reason == "UNKNOWN_ORDER_STATE":
            control["breaker_open"] = True
            control["breaker_reason"] = "UNKNOWN_ORDER_STATE"
        if not self._all_terminal(batch):
            return batch, control
        if isinstance(batch.get("repair_plan"), dict):
            return batch, control
        plan = self._repair_plan(batch, repair_context)
        batch["repair_plan"] = plan
        if plan is None:
            control["breaker_open"] = True
            control["breaker_reason"] = "REPAIR_CONTEXT_REQUIRED"
        elif plan["reason"] == "REPAIR_LOSS_CAP_EXCEEDED":
            control["breaker_open"] = True
            control["breaker_reason"] = "REPAIR_LOSS_CAP_EXCEEDED"
        return batch, control

    @staticmethod
    def _repair_plan(batch: Mapping[str, object], context: RepairContext | None) -> dict[str, object] | None:
        if not isinstance(context, RepairContext) or context.reservation_version != batch.get("reservation_version"):
            return None
        raw_legs = batch.get("legs")
        if not isinstance(raw_legs, list):
            return None
        by_client = {leg.get("client_order_id"): leg for leg in raw_legs if isinstance(leg, dict) and isinstance(leg.get("client_order_id"), str)}
        quotes = {client: (buy, sell) for client, buy, sell in context.quotes}
        holds = dict(context.holdings)
        if set(quotes) != set(by_client) or set(holds) != set(by_client): return None
        complete = {client: int(leg["submitted_quantity"]) - int(leg["receipt"]["cumulative_filled_quantity"]) for client, leg in by_client.items() if isinstance(leg.get("receipt"), dict) and int(leg["submitted_quantity"]) > int(leg["receipt"]["cumulative_filled_quantity"])}
        exit_ = {client: int(leg["receipt"]["cumulative_filled_quantity"]) for client, leg in by_client.items() if isinstance(leg.get("receipt"), dict) and int(leg["receipt"]["cumulative_filled_quantity"]) > 0 and holds[client] >= int(leg["receipt"]["cumulative_filled_quantity"])}
        base = context.occurred_cost_units + context.occurred_fee_units
        candidates = []
        if complete:
            cost = sum(quantity * quotes[client][0] for client, quantity in complete.items())
            if cost <= int(batch["reservation_units"]): candidates.append({"family": "COMPLETE_REMAINING", "legs": complete, "conservative_total_loss_units": base + cost})
        if exit_:
            candidates.append({"family": "EXIT_CONFIRMED", "legs": exit_, "conservative_total_loss_units": base + sum(quantity * quotes[client][1] for client, quantity in exit_.items())})
        for candidate in candidates: candidate["fingerprint"] = fingerprint({"context": context.fingerprint, "candidate": candidate})
        if not candidates:
            return None
        selected = min(candidates, key=lambda item: (int(item["conservative_total_loss_units"]), str(item["fingerprint"])))
        cap = batch.get("max_auto_repair_loss")
        over_cap = type(cap) is not int or int(selected["conservative_total_loss_units"]) > cap
        return {
            "schema_version": REPAIR_PLAN_SCHEMA_V1,
            "family": selected["family"],
            "candidate": selected,
            "fingerprint": fingerprint(selected),
            "auto_eligible": False,
            "reason": "REPAIR_LOSS_CAP_EXCEEDED" if over_cap else "REPAIR_PROOF_REQUIRED",
        }

    @staticmethod
    def _reconcile_terminal(batch: Mapping[str, object], control: dict[str, object]) -> dict[str, object]:
        result = dict(control)
        legs = batch["legs"]
        assert isinstance(legs, list)
        result["active_batch_id"] = None
        result["total_unsettled_capital_units"] = 0 if NLegExecutionService._all_zero(batch) else int(batch["total_unsettled_capital_units"])
        return result
