"""Issue #60 phase-A N_LEG cutover: fence advance and ledger migration.

Two independent store-level entry points (no HTTP, no runtime):

- ``run_n_leg_cutover_migration(store, *, manifest)`` performs the whole
  ledger migration inside ONE transaction on the prediction arbitrage store:
  it prices every unsettled execution plus every reserved cross reservation
  into integer micro-USD capital units (ROUND_UP, conservative), initializes
  the ``n_leg_controls`` singleton from scratch, seeds the
  SAME_EVENT_SAME_VENUE scope at OBSERVE_ONLY, advances the
  ``minimum_reader_generation`` fence, and records one control_events audit
  row. Any failure rolls everything back: no controls singleton, fence
  unchanged, no audit row.
- ``activate_approved_relations(store)`` publishes all model-complete
  APPROVED relations through the relation-catalog v2 API as one atomic
  generation, in its OWN transaction, separate from the ledger migration.

The ``manifest`` maps execution_id -> explicit USD price (decimal string or
Decimal) used when an unsettled execution carries no priceable
``total_max_cost`` in its payload; every manifest entry must reference an
existing, unsettled execution.
"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal, InvalidOperation, ROUND_UP
from typing import Mapping

from open_trader.prediction_arbitrage_store import (
    N_LEG_READER_GENERATION,
    PredictionArbitrageStore,
    _utc_now,
)
from open_trader.prediction_n_leg_mode import (
    N_LEG_CONTRACT_GENERATION,
    N_LEG_CONTRACT_GENERATION_LABEL,
    SAME_EVENT_SAME_VENUE_MEMBERS,
    SAME_EVENT_SAME_VENUE_SCOPE_ID,
)
from open_trader.prediction_read_model import _NLEG_UNITS_PER_DOLLAR
from open_trader.relation_catalog import _stored_payload_complete
from open_trader.relation_catalog_v2 import RelationCatalogV2, SqliteCatalogStore

N_LEG_CUTOVER_BLOCKED_UNPRICED_EXECUTION = "N_LEG_CUTOVER_BLOCKED_UNPRICED_EXECUTION"
N_LEG_CUTOVER_BLOCKED_MANIFEST_MISMATCH = "N_LEG_CUTOVER_BLOCKED_MANIFEST_MISMATCH"
N_LEG_CUTOVER_BLOCKED_CATALOG_RECONCILIATION = (
    "N_LEG_CUTOVER_BLOCKED_CATALOG_RECONCILIATION"
)
N_LEG_CUTOVER_BLOCKED_INCONSISTENT_STATE = "N_LEG_CUTOVER_BLOCKED_INCONSISTENT_STATE"

#: Execution states whose capital is definitively settled, derived from the
#: execution state machine: ``both_rejected`` and ``complete`` are the only
#: states for which the store accepts a proven cross-reservation release
#: (``no_submit``/``both_rejected`` need state ``both_rejected`` with zero
#: positions observed, ``redeemed`` needs state ``complete`` with observed
#: redeemed collateral — ``release_cross_reservation``/``_cross_release_is_proven``
#: in prediction_arbitrage_store). ``no_submit`` itself is a reservation
#: release reason, not an execution state. Every other state — including the
#: lifecycle-terminal ``holding_to_resolution`` / incident states and the
#: in-flight validating/submitting/reconciling/remediating states — still
#: holds unsettled capital and must be priced or the cutover blocks.
N_LEG_SETTLED_EXECUTION_STATES = frozenset({"both_rejected", "complete"})

#: Cause reported for APPROVED relations whose stored model is not complete;
#: the facade's own diagnostic for this exact review state
#: (relation_catalog._approve_many_locked).
INCOMPLETE_MODEL_CAUSE = "INCOMPLETE_MODEL"


def _price_units(value: object) -> int | None:
    """USD decimal -> integer micro-USD units, ROUND_UP (conservative)."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite() or amount < 0:
        return None
    return int((amount * _NLEG_UNITS_PER_DOLLAR).to_integral_value(rounding=ROUND_UP))


def run_n_leg_cutover_migration(
    store: PredictionArbitrageStore, *, manifest: Mapping[str, object]
) -> dict[str, object]:
    """Migrate the legacy ledger into the N_LEG capital boundary, atomically."""
    prices = dict(manifest)
    now = _utc_now()
    with store._transaction() as connection:
        fence_row = connection.execute(
            "SELECT minimum_reader_generation FROM schema_metadata WHERE singleton=1"
        ).fetchone()
        if fence_row is None:
            raise ValueError("prediction minimum reader generation is missing")
        current_fence = int(fence_row[0])
        if current_fence >= N_LEG_READER_GENERATION:
            raw_controls = connection.execute(
                "SELECT * FROM n_leg_controls WHERE singleton=1"
            ).fetchone()
            control = store._n_leg_control_row(raw_controls)
            scope_row = connection.execute(
                "SELECT capability FROM n_leg_execution_scopes WHERE scope_id=?",
                (SAME_EVENT_SAME_VENUE_SCOPE_ID,),
            ).fetchone()
            problems: list[str] = []
            if raw_controls is None:
                problems.append("n_leg_controls singleton row is missing")
            elif int(control["contract_generation"]) != N_LEG_CONTRACT_GENERATION:
                problems.append(
                    f"n_leg_controls contract_generation is "
                    f"{control['contract_generation']}, expected "
                    f"{N_LEG_CONTRACT_GENERATION}"
                )
            if scope_row is None:
                problems.append(
                    f"n_leg_execution_scopes row for {SAME_EVENT_SAME_VENUE_SCOPE_ID} "
                    f"is missing"
                )
            if problems:
                raise ValueError(
                    f"{N_LEG_CUTOVER_BLOCKED_INCONSISTENT_STATE}: "
                    f"minimum reader generation {current_fence} is at the N_LEG "
                    f"fence but the migrated state is inconsistent: "
                    f"{'; '.join(problems)}"
                )
            return {
                "outcome": "already_migrated",
                "total_unsettled_capital_units": int(
                    control["total_unsettled_capital_units"]
                ),
                "minimum_reader_generation": current_fence,
                "contract_generation": int(control["contract_generation"]),
            }

        rows = connection.execute(
            "SELECT execution_id, state, payload FROM executions"
        ).fetchall()
        states: dict[str, str] = {}
        payloads: dict[str, dict[str, object]] = {}
        for row in rows:
            execution_id = str(row["execution_id"])
            states[execution_id] = str(row["state"])
            payloads[execution_id] = json.loads(str(row["payload"]))

        # Manifest integrity: every explicit price must reference an existing,
        # unsettled execution; anything else is a manifest mismatch and the
        # whole migration blocks.
        for execution_id in prices:
            if execution_id not in states:
                raise ValueError(
                    f"{N_LEG_CUTOVER_BLOCKED_MANIFEST_MISMATCH}: "
                    f"manifest references missing execution {execution_id}"
                )
            if states[execution_id] in N_LEG_SETTLED_EXECUTION_STATES:
                raise ValueError(
                    f"{N_LEG_CUTOVER_BLOCKED_MANIFEST_MISMATCH}: "
                    f"manifest references settled execution {execution_id} "
                    f"in state {states[execution_id]}"
                )

        reserved: dict[str, object] = {
            str(row["execution_id"]): row["amount"]
            for row in connection.execute(
                "SELECT execution_id, amount FROM cross_execution_reservations "
                "WHERE state='reserved'"
            ).fetchall()
        }

        total_units = 0
        priced_executions = 0
        source_states: dict[str, int] = {}
        # Reserved cross reservations carry the unsettled capital of their
        # execution (amount == payload total_max_cost); price those execution
        # ids exactly once, via the reservation.
        for execution_id, amount in sorted(reserved.items()):
            units = _price_units(amount)
            if units is None:
                raise ValueError(
                    f"{N_LEG_CUTOVER_BLOCKED_UNPRICED_EXECUTION}: "
                    f"reserved cross reservation for {execution_id}"
                )
            total_units += units
            priced_executions += 1
        for execution_id, state in sorted(states.items()):
            if execution_id in reserved or state in N_LEG_SETTLED_EXECUTION_STATES:
                continue
            price: object = payloads[execution_id].get("total_max_cost")
            if execution_id in prices:
                price = prices[execution_id]
            units = _price_units(price)
            if units is None:
                raise ValueError(
                    f"{N_LEG_CUTOVER_BLOCKED_UNPRICED_EXECUTION}: "
                    f"execution {execution_id} in state {state}"
                )
            total_units += units
            priced_executions += 1
            source_states[state] = source_states.get(state, 0) + 1

        _initialize_controls(connection, store, total_units=total_units, now=now)
        _ensure_same_event_same_venue_scope(connection, store, now=now)
        store.advance_minimum_reader_generation(
            N_LEG_READER_GENERATION, connection=connection
        )
        audit_event_id = store._insert_control_event(
            connection,
            action="n_leg_cutover_migration",
            target="n_leg_controls",
            outcome="succeeded",
            payload={
                "total_unsettled_capital_units": total_units,
                "priced_execution_count": priced_executions,
                "reserved_reservation_count": len(reserved),
                "source_states": source_states,
                "contract_generation": N_LEG_CONTRACT_GENERATION,
                "contract_generation_label": N_LEG_CONTRACT_GENERATION_LABEL,
                "minimum_reader_generation": N_LEG_READER_GENERATION,
            },
        )
        return {
            "outcome": "migrated",
            "total_unsettled_capital_units": total_units,
            "minimum_reader_generation": N_LEG_READER_GENERATION,
            "contract_generation": N_LEG_CONTRACT_GENERATION,
            "priced_execution_count": priced_executions,
            "reserved_reservation_count": len(reserved),
            "audit_event_id": audit_event_id,
        }


def activate_approved_relations(
    store: PredictionArbitrageStore,
    *,
    actor: str = "n_leg_cutover",
    git_sha: str = "n_leg_cutover_runtime",
) -> dict[str, object]:
    """Publish every model-complete APPROVED relation as one atomic generation.

    Uses the relation-catalog v2 activation API (never hand-rolled catalog
    SQL) in its own write transaction, separate from the ledger migration.
    Approval provenance defaults to the non-empty ``n_leg_cutover_runtime``
    sentinel ``git_sha``; an orchestrator slice passes the real deployed SHA.
    Returns the published/blocked counts with per-identity causes; when the
    accounting does not close (published + blocked != APPROVED considered) the
    generation publish is rejected with
    ``N_LEG_CUTOVER_BLOCKED_CATALOG_RECONCILIATION``.
    """
    if not git_sha:
        raise ValueError("git_sha must be a non-empty provenance string")
    catalog = RelationCatalogV2(SqliteCatalogStore(store.path))
    versions: Mapping[str, Mapping[str, object]] = catalog.store.get("versions", {})
    latest: Mapping[str, str] = catalog.store.get("latest", {})
    complete_payloads: list[dict[str, object]] = []
    blocked_causes: dict[str, str] = {}
    considered: dict[str, str] = {}
    for identity, version_id in latest.items():
        record = versions.get(version_id)
        if record is None or str(record.get("status")) != "APPROVED":
            continue
        payload = record["payload"]
        considered[str(identity)] = str(version_id)
        if _stored_payload_complete(payload):
            complete_payloads.append(dict(payload))
        else:
            blocked_causes[str(identity)] = INCOMPLETE_MODEL_CAUSE

    result = catalog.activate_many(
        complete_payloads,
        actor=actor,
        git_sha=git_sha,
    )
    published: set[str] = set()
    for identity, entry in result["results"].items():
        status = str(entry.get("status"))
        if status in {"APPROVED", "ALREADY_ACTIVE"}:
            published.add(str(identity))
        else:
            blocked_causes.setdefault(str(identity), str(entry.get("reason", status)))
    for item in result["blocked"]:
        blocked_causes.setdefault(str(item["identity"]), str(item.get("reason", "")))
        if not blocked_causes[str(item["identity"])]:
            blocked_causes[str(item["identity"])] = str(item.get("reason", "BLOCKED"))
    if published & set(blocked_causes):
        overlap = sorted(published & set(blocked_causes))
        raise ValueError(
            f"{N_LEG_CUTOVER_BLOCKED_CATALOG_RECONCILIATION}: "
            f"identities both published and blocked: {overlap}"
        )
    if published | set(blocked_causes) != set(considered):
        missing = sorted(set(considered) - (published | set(blocked_causes)))
        raise ValueError(
            f"{N_LEG_CUTOVER_BLOCKED_CATALOG_RECONCILIATION}: "
            f"published {len(published)} + blocked {len(blocked_causes)} != "
            f"APPROVED considered {len(considered)}; unaccounted: {missing}"
        )
    return {
        "published": len(published),
        "blocked": len(blocked_causes),
        "blocked_causes": dict(sorted(blocked_causes.items())),
        "approved_considered": len(considered),
    }


def _initialize_controls(
    connection: sqlite3.Connection,
    store: PredictionArbitrageStore,
    *,
    total_units: int,
    now: str,
) -> None:
    """Initialize the controls singleton from scratch — never from the legacy
    validation_mode or cross_auto_state singletons."""
    policy_row = connection.execute(
        "SELECT version FROM n_leg_qualification_policy ORDER BY version DESC LIMIT 1"
    ).fetchone()
    safety_row = connection.execute(
        "SELECT version FROM n_leg_safety_config ORDER BY version DESC LIMIT 1"
    ).fetchone()
    connection.execute(
        """
        INSERT INTO n_leg_controls(
            singleton, mode, breaker_open, breaker_reason, active_batch_id,
            total_unsettled_capital_units, contract_generation,
            qualification_policy_version, safety_config_version,
            enabled_execution_scope_version, updated_at
        ) VALUES (1, 'MANUAL', 0, NULL, NULL, ?, ?, ?, ?, '[]', ?)
        ON CONFLICT(singleton) DO UPDATE SET
            mode=excluded.mode,
            breaker_open=excluded.breaker_open,
            breaker_reason=excluded.breaker_reason,
            active_batch_id=excluded.active_batch_id,
            total_unsettled_capital_units=excluded.total_unsettled_capital_units,
            contract_generation=excluded.contract_generation,
            qualification_policy_version=excluded.qualification_policy_version,
            safety_config_version=excluded.safety_config_version,
            enabled_execution_scope_version=excluded.enabled_execution_scope_version,
            updated_at=excluded.updated_at
        """,
        (
            total_units,
            N_LEG_CONTRACT_GENERATION,
            int(policy_row["version"]) if policy_row is not None else 1,
            int(safety_row["version"]) if safety_row is not None else 1,
            now,
        ),
    )


def _ensure_same_event_same_venue_scope(
    connection: sqlite3.Connection,
    store: PredictionArbitrageStore,
    *,
    now: str,
) -> None:
    """Seed SAME_EVENT_SAME_VENUE at OBSERVE_ONLY when missing (existing seed
    semantics: an already-registered scope is left untouched)."""
    existing = connection.execute(
        "SELECT capability FROM n_leg_execution_scopes WHERE scope_id=?",
        (SAME_EVENT_SAME_VENUE_SCOPE_ID,),
    ).fetchone()
    if existing is not None:
        return
    connection.execute(
        "INSERT INTO n_leg_execution_scopes(scope_id, capability, scope_version, members, updated_at) VALUES (?, ?, ?, ?, ?)",
        (
            SAME_EVENT_SAME_VENUE_SCOPE_ID,
            "OBSERVE_ONLY",
            1,
            json.dumps(
                dict(SAME_EVENT_SAME_VENUE_MEMBERS),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            now,
        ),
    )
    store._insert_control_event(
        connection,
        action="n_leg_upsert_scope",
        target=f"n_leg_execution_scopes/{SAME_EVENT_SAME_VENUE_SCOPE_ID}",
        outcome="succeeded",
        payload={
            "scope_id": SAME_EVENT_SAME_VENUE_SCOPE_ID,
            "capability": "OBSERVE_ONLY",
            "scope_version": 1,
            "direction": "added",
            "action_word": "manual_confirm",
        },
    )
