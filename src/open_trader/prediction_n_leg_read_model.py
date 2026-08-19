"""Issue #85: read-model projection of N_LEG Market/Execution solutions.

The projection is pure and stateless: it consumes the serialized #84
``MarketSolution`` / ``ExecutionSolution`` payloads (``canonical_payload``),
the merged n_leg scope contract + readiness entry, and the current unsettled
capital, and emits the dashboard-facing market/execution fields.  ORDER_READY
is bound to the execution solution fingerprint and the scope capability; it
never reuses the legacy YES/NO balance judgment.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Mapping, Sequence

from open_trader.prediction_market_solution import EXECUTABLE_REASON
from open_trader.prediction_n_leg import canonical_payload, fingerprint


_NLEG_UNITS_PER_DOLLAR = Decimal("1000000")

PARTIAL_FILL_PROOF_REQUIRED = "PARTIAL_FILL_PROOF_REQUIRED"
PARTIAL_FILL_UNSAFE = "PARTIAL_FILL_UNSAFE"
SCOPE_OBSERVE_ONLY = "SCOPE_OBSERVE_ONLY"
EXECUTION_FINGERPRINT_MISMATCH = "EXECUTION_FINGERPRINT_MISMATCH"
UNSETTLED_CAP_EXCEEDED = "UNSETTLED_CAP_EXCEEDED"

PARTIAL_FILL_SAFE = "PARTIAL_FILL_SAFE"

_READY_CAPABILITIES = ("MANUAL_CANARY", "AUTO_ELIGIBLE")


def _units_to_dollars(value: object) -> str | None:
    try:
        units = int(value or 0)
    except (TypeError, ValueError):
        return None
    return format(Decimal(units) / _NLEG_UNITS_PER_DOLLAR, "f")


def _quantity_rows(quantities: object) -> list[dict[str, object]]:
    if not isinstance(quantities, (list, tuple)):
        return []
    rows: list[dict[str, object]] = []
    for quantity in quantities:
        if not isinstance(quantity, Mapping):
            continue
        rows.append(
            {
                "action_id": str(quantity.get("action_id") or ""),
                "quantity_lots": int(quantity.get("quantity_lots") or 0),
                "max_price": None,
                "max_cost": None,
                "venue": None,
                "outcome": None,
                "settlement_asset": None,
            }
        )
    return rows


def _merge_leg_display(
    rows: Sequence[Mapping[str, object]],
    legs: Sequence[Mapping[str, object]] | None,
) -> list[dict[str, object]]:
    """Overlay optional per-leg book display facts (no invented prices)."""
    if not legs:
        return [dict(row) for row in rows]
    by_key: dict[str, Mapping[str, object]] = {}
    for leg in legs:
        if not isinstance(leg, Mapping):
            continue
        key = str(leg.get("leg_id") or leg.get("action_id") or "")
        if key:
            by_key.setdefault(key, leg)
    merged: list[dict[str, object]] = []
    for row in rows:
        item = dict(row)
        display = by_key.get(str(item.get("action_id") or ""))
        if display is not None:
            for field in ("max_price", "max_cost", "venue", "outcome", "settlement_asset"):
                if display.get(field) not in (None, ""):
                    item[field] = display.get(field)
        merged.append(item)
    return merged


def project_n_leg_solution(
    *,
    market: Mapping[str, object] | None,
    execution: Mapping[str, object] | None,
    scope: Mapping[str, object] | None,
    component_id: str | None = None,
    max_total_unsettled_capital_units: int,
    total_unsettled_capital_units: int = 0,
    legs: Sequence[Mapping[str, object]] | None = None,
    partial_fill_proof: Mapping[str, object] | None = None,
) -> dict[str, object] | None:
    """Project one solution into dashboard market/execution fields.

    ``scope`` is the merged n_leg entry: ``capability`` from the mode contract
    plus ``order_ready``/``reason``/``action`` from ``n_leg_order_readiness``.
    ``partial_fill_proof`` is the optional #74 proof record payload; when
    absent the projection falls back to the ``partial_fill_proof`` status
    string carried by the execution payload.  Returns ``None`` when no
    MarketSolution exists.
    """
    if not isinstance(market, Mapping):
        return None
    scope = dict(scope) if isinstance(scope, Mapping) else {}
    capability = str(scope.get("capability") or SCOPE_OBSERVE_ONLY)
    scope_ready = bool(scope.get("order_ready"))
    scope_reason = str(scope.get("reason") or "")
    scope_blocked = capability not in _READY_CAPABILITIES

    quantities = market.get("quantities")
    market_legs = _merge_leg_display(_quantity_rows(quantities), legs)
    market_fields = {
        "minimum_profit": _units_to_dollars(market.get("guaranteed_profit_units")),
        "maximum_cost": _units_to_dollars(market.get("bounded_cost_units")),
        "capital_release_at": market.get("capital_release_at"),
        "structure_fingerprint": market.get("structure_fingerprint"),
        "quote_fingerprint": market.get("quote_fingerprint"),
        "verification_fingerprint": market.get("verification_fingerprint"),
        "global_search_closed": market.get("global_search_closed"),
        "legs": market_legs,
    }

    execution_payload = (
        dict(execution) if isinstance(execution, Mapping) else None
    )
    execution_solution_fingerprint = (
        fingerprint(canonical_payload(execution_payload))
        if execution_payload is not None
        else None
    )
    would_submit = (
        execution_payload is not None
        and str(execution_payload.get("reason") or "") == EXECUTABLE_REASON
    )
    projected_total_units = int(execution_payload.get("capital_use_units") or 0) if execution_payload is not None else 0
    projected_with_unsettled = projected_total_units + int(total_unsettled_capital_units or 0)

    # #74 three-state proof status: explicit record payload wins, otherwise
    # the status string carried by the execution solution.
    proof_payload = (
        dict(partial_fill_proof)
        if isinstance(partial_fill_proof, Mapping)
        else None
    )
    proof_status = (
        str(proof_payload.get("status") or "")
        if proof_payload is not None
        else (
            str(execution_payload.get("partial_fill_proof") or "")
            if execution_payload is not None
            else ""
        )
    )

    order_ready = False
    reason = ""
    if scope_blocked:
        reason = SCOPE_OBSERVE_ONLY
    elif execution_payload is None:
        reason = PARTIAL_FILL_PROOF_REQUIRED
    elif str(execution_payload.get("reason") or "") not in (EXECUTABLE_REASON, ""):
        reason = str(execution_payload.get("reason") or PARTIAL_FILL_PROOF_REQUIRED)
    elif proof_status == PARTIAL_FILL_UNSAFE:
        reason = PARTIAL_FILL_UNSAFE
    elif proof_status != PARTIAL_FILL_SAFE:
        # Missing, unknown-semantics, timeout or unclosed proofs never unlock.
        reason = PARTIAL_FILL_PROOF_REQUIRED
    elif execution_payload.get("market_solution_fingerprint") != fingerprint(
        canonical_payload(market)
    ):
        reason = EXECUTION_FINGERPRINT_MISMATCH
    elif (
        int(max_total_unsettled_capital_units or 0) > 0
        and projected_with_unsettled > int(max_total_unsettled_capital_units)
    ):
        reason = UNSETTLED_CAP_EXCEEDED
    elif scope_ready:
        order_ready = True
        reason = scope_reason or "MANUAL_CANARY"
    else:
        reason = scope_reason or SCOPE_OBSERVE_ONLY

    execution_fields: dict[str, object] = {
        "would_submit": would_submit,
        "order_ready": order_ready,
        "reason": reason,
        "execution_solution_fingerprint": execution_solution_fingerprint,
        "projected_total_units": projected_total_units,
        "total_unsettled_capital_units": int(total_unsettled_capital_units or 0),
        "max_total_unsettled_capital_units": int(max_total_unsettled_capital_units or 0),
        # #74 proof state: three-state status plus the closed upper bound and
        # cap for display, and a counterexample reference when UNSAFE.
        "partial_fill_proof": proof_status,
        "partial_fill_upper_bound_units": (
            int(proof_payload["solver_upper_bound"])
            if proof_payload is not None
            and proof_payload.get("solver_upper_bound") is not None
            else None
        ),
        "partial_fill_cap_units": (
            int(proof_payload["max_partial_fill_loss"])
            if proof_payload is not None
            and proof_payload.get("max_partial_fill_loss") is not None
            else None
        ),
        "partial_fill_proof_fingerprint": (
            str(proof_payload.get("fingerprint") or "")
            if proof_payload is not None
            else None
        ),
        "legs": (
            _merge_leg_display(_quantity_rows(execution_payload.get("quantities")), legs)
            if execution_payload is not None
            else []
        ),
    }
    return {
        "component_id": str(
            component_id if component_id is not None else (market.get("component_id") or "")
        ),
        "market": market_fields,
        "execution": execution_fields,
    }
