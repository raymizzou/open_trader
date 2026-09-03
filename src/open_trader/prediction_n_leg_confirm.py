"""Issue #64: manual confirmation of one N-leg real order (enqueue side).

The engine here is only half of the pair: the user's confirmation replaces
the AUTO decision, never any safety gate. ``confirm_enqueue`` is the single
public seam — the server re-fetches the component's CURRENT solution at POST
time, re-verifies eligibility + caps + proof against it, and only then
freezes and enqueues it. A rotation that stays qualified binds the current
solution (audit block records displayed vs bound fingerprint); a rotation
that drops out of qualification rejects back to monitoring. There is
deliberately no "solution rotated" hard-409 path.

The frozen row is the FIFO handoff to the head driver (issue #64 Slice 5):
admission, preflight and submission read the queue, never this module.
"""

from __future__ import annotations

from datetime import datetime
from typing import Mapping, Sequence

from open_trader.prediction_n_leg import canonical_payload, fingerprint
from open_trader.prediction_n_leg_mode import (
    n_leg_caps_gate,
    n_leg_mode_contract,
    n_leg_order_readiness,
)
from open_trader.prediction_n_leg_read_model import (
    FEE_STATE_CHARGING,
    FEE_STATE_FREE,
    PARTIAL_FILL_PROOF_REQUIRED,
    PARTIAL_FILL_SAFE,
    QUALIFIED_VERIFIED,
    project_n_leg_solution,
)

#: Maximum number of simultaneous requests across all components (ruling 3).
QUEUE_LIMIT = 5

SCOPE_OBSERVE_ONLY = "SCOPE_OBSERVE_ONLY"
CAPS_NOT_CONFIGURED = "CAPS_NOT_CONFIGURED"
QUEUE_DUPLICATE = "QUEUE_DUPLICATE"
QUEUE_FULL = "QUEUE_FULL"
COMPONENT_SOLUTION_UNAVAILABLE = "COMPONENT_SOLUTION_UNAVAILABLE"
COMPONENT_NOT_QUALIFIED = "COMPONENT_NOT_QUALIFIED"
PER_TRADE_CAP_EXCEEDED = "PER_TRADE_CAP_EXCEEDED"


class NLegConfirmRejected(ValueError):
    """A confirm attempt failed one gate; ``reason`` is the stable literal."""

    def __init__(self, reason: str, *, http_status: int = 409) -> None:
        super().__init__(reason)
        self.reason = reason
        self.http_status = http_status


def _current_safety_values(store: object) -> tuple[int, dict[str, object]]:
    stored = store.n_leg_safety_config_latest()
    if stored is None:
        from open_trader.prediction_n_leg_mode import DEFAULT_SAFETY_CONFIG

        return 1, dict(DEFAULT_SAFETY_CONFIG)
    config = stored["config"]
    if not isinstance(config, Mapping):
        config = {}
    return (
        int(stored["version"]),
        {
            "max_per_trade_cost_units": int(
                config.get("max_per_trade_cost_units", 0)
            ),
            "max_total_unsettled_capital_units": int(
                config.get("max_total_unsettled_capital_units", 0)
            ),
            "max_partial_fill_loss_units": int(
                config.get("max_partial_fill_loss_units", 0)
            ),
            "max_auto_repair_loss_units": int(
                config.get("max_auto_repair_loss_units", 0)
            ),
        },
    )


def _fee_is_known(fee: Mapping[str, object] | None) -> bool:
    """The #112/#117 fee veto: only a proven fee-free block or a charging
    block with the frozen modeled invariant counts as known."""
    if not isinstance(fee, Mapping):
        return False
    status = str(fee.get("status") or "")
    if status == FEE_STATE_FREE:
        return True
    if status == FEE_STATE_CHARGING:
        return fee.get("modeled") is True
    return False


def confirm_enqueue(
    store: object,
    solutions: Sequence[Mapping[str, object]],
    *,
    component_id: str,
    displayed_fingerprint: str,
    idempotency_key: str,
    now: datetime,
    partial_fill_proof: Mapping[str, object] | None = None,
    execution_source: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Re-verify and enqueue one manual-confirm N-leg order request.

    Gate order (rulings 2-5): execution gates, scope/mode readiness, caps
    marker, idempotency replay, FIFO queue rules, then re-verification of the
    component's CURRENT solution. ``partial_fill_proof`` is the bound #74
    proof record payload for the current solution (the resolver caches it per
    component); the freeze stores it so admission can re-bind it.
    ``execution_source`` is the resolver's retained admission-grade material
    ({"market", "execution"} heavy #51 payloads that the queue-head admission
    re-decodes faithfully); when absent the entry's own payloads are frozen.
    Returns the stored queue row (echoed by the API); raises
    ``NLegConfirmRejected`` with a stable reason otherwise.
    """
    if not isinstance(component_id, str) or not component_id:
        raise ValueError("component id must be non-empty text")
    if not isinstance(displayed_fingerprint, str) or not displayed_fingerprint:
        raise ValueError("displayed fingerprint must be non-empty text")
    if not isinstance(idempotency_key, str) or not idempotency_key:
        raise ValueError("idempotency key must be non-empty text")

    # 1. Execution gates. An unacknowledged N_LEG execution-incident batch
    # is this feature's own stop-the-world gate and outranks the legacy
    # breaker here (the global breaker additionally holds through the mode
    # contract either way); then breaker, then active batch.
    if store.n_leg_incident_batch() is not None:
        raise NLegConfirmRejected("EXECUTION_INCIDENT_ACTIVE")
    readiness = n_leg_order_readiness(store)
    gates = readiness.get("gates")
    if isinstance(gates, Mapping):
        if gates.get("breaker_open"):
            raise NLegConfirmRejected("GLOBAL_BREAKER_OPEN")
        if gates.get("incident_active"):
            raise NLegConfirmRejected("EXECUTION_INCIDENT_ACTIVE")
        if gates.get("batch_active"):
            raise NLegConfirmRejected("EXECUTION_BATCH_ACTIVE")
    ready_scopes = [
        row
        for row in readiness.get("scopes", {}).values()
        if isinstance(row, Mapping) and row.get("order_ready") is True
    ]
    if not ready_scopes:
        scopes = readiness.get("scopes")
        reasons = [
            str(row.get("reason") or "")
            for row in scopes.values()
            if isinstance(row, Mapping)
        ] if isinstance(scopes, Mapping) else []
        if not reasons or all(reason == SCOPE_OBSERVE_ONLY for reason in reasons):
            raise NLegConfirmRejected(SCOPE_OBSERVE_ONLY, http_status=403)
        raise NLegConfirmRejected(reasons[0])
    contract = n_leg_mode_contract(store)
    if str(contract["mode"]) != "MANUAL":
        raise NLegConfirmRejected("N_LEG_MODE_NOT_MANUAL")

    # 2. Caps confirmation marker (ruling 5: the order gate reads only it).
    ok, reason = n_leg_caps_gate(store)
    if not ok:
        raise NLegConfirmRejected(reason)

    # 3. Idempotency replay: the same key returns the same row, whatever the
    # queue rules would say about a brand-new request.
    replayed = store.n_leg_request_by_idempotency_key(idempotency_key)
    if replayed is not None:
        return _request_result(replayed)

    # 4. FIFO queue rules (ruling 3).
    rows = store.n_leg_requests()
    pending = [row for row in rows if row["state"] == "PENDING"]
    if any(row["component_id"] == component_id for row in pending):
        raise NLegConfirmRejected(QUEUE_DUPLICATE)
    if len(pending) >= QUEUE_LIMIT:
        raise NLegConfirmRejected(QUEUE_FULL)

    # 5. Re-verify the component's CURRENT solution (ruling 2: bind the
    # opportunity, not the snapshot the user saw).
    entry = next(
        (
            item
            for item in solutions
            if isinstance(item, Mapping)
            and str(item.get("component_id") or "") == component_id
        ),
        None,
    )
    if entry is None or not isinstance(entry.get("market"), Mapping):
        raise NLegConfirmRejected(COMPONENT_SOLUTION_UNAVAILABLE)
    scope_id = str(ready_scopes[0].get("scope_id") or "")
    scope = contract["execution_scopes"].get(scope_id, {})
    capability = str(scope.get("capability") or "")
    safety_version, caps = _current_safety_values(store)
    control = store.n_leg_control()
    policy = contract["qualification_policy"]
    if isinstance(policy, Mapping) and isinstance(policy.get("policy"), Mapping):
        policy = policy["policy"]
    item = project_n_leg_solution(
        market=entry["market"],
        execution=entry.get("execution") if isinstance(entry.get("execution"), Mapping) else None,
        scope={
            "capability": capability,
            "order_ready": True,
            "reason": ready_scopes[0].get("reason"),
            "action": ready_scopes[0].get("action"),
        },
        component_id=component_id,
        max_total_unsettled_capital_units=caps["max_total_unsettled_capital_units"],
        total_unsettled_capital_units=int(
            control.get("total_unsettled_capital_units", 0) or 0
        ),
        qualification_policy=policy,
        fee=entry.get("fee") if isinstance(entry.get("fee"), Mapping) else None,
        now=now,
    )
    if item is None:
        raise NLegConfirmRejected(COMPONENT_SOLUTION_UNAVAILABLE)
    execution_view = item["execution"]
    # Ruling 2 re-verification, exactly: qualification + proof + fee + caps.
    # (The projection's full order_ready gate chain — including its
    # execution-vs-market fingerprint comparison — HOLDS for real resolver
    # payloads: the payload fingerprint round-trips, so the gate can pass.
    # The real-resolver e2e locks order_ready=True reachability. Eligibility
    # here is the four approved checks, and admission re-checks the versions
    # authoritatively in Slice 4.)
    if item["qualification"]["status"] != QUALIFIED_VERIFIED:
        raise NLegConfirmRejected(COMPONENT_NOT_QUALIFIED)
    if not _fee_is_known(entry.get("fee") if isinstance(entry.get("fee"), Mapping) else None):
        raise NLegConfirmRejected("FEE_UNKNOWN")
    proof_status = str(execution_view.get("partial_fill_proof") or "")
    if proof_status != PARTIAL_FILL_SAFE:
        raise NLegConfirmRejected(
            proof_status or PARTIAL_FILL_PROOF_REQUIRED
        )
    projected_units = int(execution_view.get("projected_total_units") or 0)
    if projected_units > caps["max_per_trade_cost_units"]:
        raise NLegConfirmRejected(PER_TRADE_CAP_EXCEEDED)
    max_unsettled = caps["max_total_unsettled_capital_units"]
    unsettled = int(control.get("total_unsettled_capital_units", 0) or 0)
    if max_unsettled > 0 and projected_units + unsettled > max_unsettled:
        raise NLegConfirmRejected("UNSETTLED_CAP_EXCEEDED")

    # 6. Freeze the current solution and enqueue (ruling 2 audit block).
    # The frozen payloads are the admission-grade heavy #51 ones when the
    # resolver retained them (the queue-head re-decode consumes exactly
    # those); otherwise the entry's own payloads are frozen unchanged.
    source_block = (
        execution_source if isinstance(execution_source, Mapping) else None
    )
    source_market = (
        source_block.get("market")
        if source_block is not None and isinstance(source_block.get("market"), Mapping)
        else None
    )
    source_execution = (
        source_block.get("execution")
        if source_block is not None and isinstance(source_block.get("execution"), Mapping)
        else None
    )
    execution_payload = dict(
        source_execution
        if source_execution is not None
        else entry["execution"]
    )
    bound_fingerprint = fingerprint(canonical_payload(execution_payload))
    # Repair round 3 (F5): the card's fingerprint comes from the LIGHT
    # read-model family, the frozen heavy #51 payload from the heavy family —
    # comparing the two structurally differs every generation and made
    # ``rotated`` a constant True. ``rotated`` is now a same-generation
    # signal: the incoming card fingerprint against the CURRENT entry's own
    # read-model fingerprint (exactly the formula the card renders). The
    # light/heavy family split of the FROZEN material is recorded explicitly
    # as ``payload_family`` instead of polluting ``rotated`` (the light
    # fallback keeps the exact approved B2 block shape).
    current_display_family_fingerprint = fingerprint(
        canonical_payload(dict(entry["execution"]))
    )
    fee_payload = (
        dict(entry["fee"]) if isinstance(entry.get("fee"), Mapping) else {}
    )
    audit: dict[str, object] = {
        "displayed_fingerprint": displayed_fingerprint,
        "bound_fingerprint": bound_fingerprint,
        "rotated": displayed_fingerprint
        != current_display_family_fingerprint,
    }
    if source_execution is not None:
        audit["payload_family"] = "heavy"
    payload: dict[str, object] = {
        "component_id": component_id,
        "market": dict(source_market if source_market is not None else entry["market"]),
        "execution": execution_payload,
        "fee": fee_payload,
        "execution_solution_fingerprint": bound_fingerprint,
        "audit": audit,
        "versions": {
            "contract_generation": int(contract["contract_generation"]),
            "qualification_policy_version": int(
                contract["qualification_policy_version"]
            ),
            "safety_config_version": int(contract["safety_config_version"]),
            "caps_configured_version": safety_version,
            "caps_fingerprint": fingerprint(dict(caps)),
            "mode": str(contract["mode"]),
            "capability": capability,
            "scope_id": scope_id,
            "scope_version": int(scope.get("scope_version") or 0),
            "enabled_execution_scope_version": [
                dict(item)
                for item in contract["enabled_execution_scope_version"]
            ],
        },
        "caps": caps,
        "projected_total_units": projected_units,
        "enqueued_at": now.isoformat(),
        # FIFO handoff facts for the queue-head driver (Slice 5): stable
        # family lineage (one real batch per opportunity family) and the
        # bound proof record for admission re-binding.
        "opportunity_episode_id": f"episode:{component_id}:{bound_fingerprint[-12:]}",
        "episode_lineage_id": f"lineage:{component_id}",
        "execution_batch_id": f"nleg-b-{now.strftime('%Y%m%d%H%M%S')}-{abs(hash(idempotency_key)) % 10**8:08d}",
    }
    if partial_fill_proof is not None:
        payload["partial_fill_proof"] = dict(partial_fill_proof)
    # Review round 2 (ruling 7): the frozen sequence baselines come from the
    # entry's solve-request snapshot; the queue-head preflight compares fresh
    # book sequences against them (SEQUENCE_REGRESSED when behind).
    sequences_block = entry.get("sequences")
    payload["sequences"] = (
        dict(sequences_block)
        if isinstance(sequences_block, Mapping)
        else {}
    )
    try:
        row = store.n_leg_request_enqueue(
            component_id=component_id,
            idempotency_key=idempotency_key,
            payload=payload,
            max_pending=QUEUE_LIMIT,
        )
    except ValueError as exc:
        # Review round 2 (ruling 3): the authoritative FIFO rules live inside
        # the enqueue transaction; a lost race surfaces here with the same
        # stable literals the pre-check above uses.
        message = str(exc)
        if message in (QUEUE_DUPLICATE, QUEUE_FULL):
            raise NLegConfirmRejected(message) from exc
        raise
    return _request_result(row)


def _request_result(row: Mapping[str, object]) -> dict[str, object]:
    payload = row.get("payload")
    audit: Mapping[str, object] = (
        payload.get("audit")
        if isinstance(payload, Mapping) and isinstance(payload.get("audit"), Mapping)
        else {}
    )
    return {
        "request_id": str(row["request_id"]),
        "fifo_index": int(row["fifo_index"]),
        "component_id": str(row["component_id"]),
        "state": str(row["state"]),
        "displayed_fingerprint": audit.get("displayed_fingerprint"),
        "bound_fingerprint": audit.get("bound_fingerprint"),
        "rotated": audit.get("rotated"),
    }
