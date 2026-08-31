"""Issue #60 phase A cutover: reader fence and single-transaction N_LEG migration.

Store-level seams only (no HTTP, no runtime): the cutover prices every
unsettled execution into micro-USD capital units, initializes the N_LEG
controls singleton from scratch (never inheriting legacy validation/auto
state), seeds the SAME_EVENT_SAME_VENUE scope at OBSERVE_ONLY, advances the
minimum reader generation fence, and audits the migration — all inside ONE
transaction so any failure leaves zero trace.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from open_trader.prediction_arbitrage_store import (
    PredictionArbitrageStore,
    read_minimum_reader_generation,
)
from open_trader.prediction_n_leg_cutover import (
    N_LEG_CUTOVER_BLOCKED_INCONSISTENT_STATE,
    N_LEG_CUTOVER_BLOCKED_MANIFEST_MISMATCH,
    N_LEG_CUTOVER_BLOCKED_UNPRICED_EXECUTION,
    activate_approved_relations,
    run_n_leg_cutover_migration,
)
from open_trader.prediction_n_leg_mode import SAME_EVENT_SAME_VENUE_SCOPE_ID
from open_trader.relation_catalog_v2 import RelationCatalogV2, SqliteCatalogStore
from test_prediction_arbitrage_store import cross_preview_payload
from test_relation_catalog import compiled_problem
from test_relation_catalog_v2 import _endpoint, _payload


def _store(tmp_path: Path) -> PredictionArbitrageStore:
    return PredictionArbitrageStore(tmp_path / "data")


def _data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


def _preview_expiry() -> str:
    moment = datetime.now(UTC) + timedelta(seconds=10)
    return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _create_plain_execution(
    store: PredictionArbitrageStore,
    market_id: str,
    total_max_cost: Decimal | None,
) -> str:
    payload: dict[str, object] = {
        "event_id": f"event-{market_id}",
        "market_id": market_id,
        "quantity": "20",
        "yes_max_price": "0.45",
        "no_max_price": "0.48",
    }
    if total_max_cost is not None:
        payload["total_max_cost"] = total_max_cost
    preview_id = store.create_preview(payload, expires_at=_preview_expiry())
    execution = store.consume_preview_and_create_execution(preview_id, f"key-{market_id}")
    return str(execution["execution_id"])


def _create_cross_execution(
    store: PredictionArbitrageStore,
    market_id: str,
    total_max_cost: Decimal,
) -> str:
    preview_id = store.create_preview(
        cross_preview_payload(market_id=market_id, total_max_cost=total_max_cost),
        expires_at=_preview_expiry(),
    )
    execution = store.consume_preview_and_create_execution(preview_id, f"key-{market_id}")
    return str(execution["execution_id"])


def test_cutover_prices_unsettled_capital_and_advances_fence(tmp_path: Path) -> None:
    store = _store(tmp_path)
    plain_id = _create_plain_execution(store, "market-plain", Decimal("12.34"))
    store.transition_execution(plain_id, state="holding_to_resolution", evidence={})
    cross_id = _create_cross_execution(store, "market-cross", Decimal("1.00"))
    store.transition_execution(cross_id, state="holding_to_resolution", evidence={})

    result = run_n_leg_cutover_migration(store, manifest={})

    assert result["outcome"] == "migrated"
    assert result["total_unsettled_capital_units"] == 13_340_000
    control = store.n_leg_control()
    assert control["total_unsettled_capital_units"] == 13_340_000
    assert control["mode"] == "MANUAL"
    assert control["contract_generation"] == 2
    scope = store.n_leg_scope(SAME_EVENT_SAME_VENUE_SCOPE_ID)
    assert scope is not None
    assert scope["capability"] == "OBSERVE_ONLY"
    assert read_minimum_reader_generation(_data_dir(tmp_path)) == 2


def test_cutover_blocked_on_unpriced_or_unknown_state(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # Unsettled execution with no priceable payload cost.
    unpriced_id = _create_plain_execution(store, "market-unpriced", None)
    store.transition_execution(
        unpriced_id, state="holding_to_resolution", evidence={}
    )
    # Synthetic unknown state: outside the settled allowlist, so unsettled;
    # it carries a price and must not silently pass as settled.
    weird_id = _create_plain_execution(store, "market-weird", Decimal("3.00"))
    store.transition_execution(weird_id, state="weird_state", evidence={})

    with pytest.raises(
        ValueError, match=N_LEG_CUTOVER_BLOCKED_UNPRICED_EXECUTION
    ):
        run_n_leg_cutover_migration(store, manifest={})

    # Logical zero-change: no controls singleton, fence untouched, no audit row.
    control = store.n_leg_control()
    assert control["mode"] == "MANUAL"
    assert control["total_unsettled_capital_units"] == 0
    assert control["contract_generation"] == 1
    assert read_minimum_reader_generation(_data_dir(tmp_path)) == 1
    assert (
        store.latest_control_event("n_leg_cutover_migration", "n_leg_controls")
        is None
    )


def test_cutover_no_auto_inheritance(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # Legacy automation is fully hot before the cutover.
    store.set_validation_mode("auto")
    store.set_cross_auto_mode("auto_submit", "legacy_owner")
    store.arm_cross_auto()
    state = store.cross_auto_state()
    assert state["configured_mode"] == "auto_submit"
    assert state["armed"] is True
    plain_id = _create_plain_execution(store, "market-plain", Decimal("1.00"))
    store.transition_execution(plain_id, state="holding_to_resolution", evidence={})

    run_n_leg_cutover_migration(store, manifest={})

    control = store.n_leg_control()
    assert control["mode"] == "MANUAL"
    scope = store.n_leg_scope(SAME_EVENT_SAME_VENUE_SCOPE_ID)
    assert scope is not None
    assert scope["capability"] == "OBSERVE_ONLY"
    db_path = (
        tmp_path / "data" / "prediction_arbitrage" / "prediction_arbitrage.sqlite3"
    )
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT mode, breaker_open, active_batch_id, enabled_execution_scope_version "
            "FROM n_leg_controls WHERE singleton=1"
        ).fetchone()
        assert row[0] == "MANUAL"
        assert row[1] == 0
        assert row[2] is None
        assert "AUTO" not in str(row[3])
        capabilities = [
            str(item[0])
            for item in connection.execute(
                "SELECT capability FROM n_leg_execution_scopes"
            ).fetchall()
        ]
        assert capabilities == ["OBSERVE_ONLY"]


def test_cutover_idempotent_rerun(tmp_path: Path) -> None:
    store = _store(tmp_path)
    plain_id = _create_plain_execution(store, "market-plain", Decimal("12.34"))
    store.transition_execution(plain_id, state="holding_to_resolution", evidence={})
    cross_id = _create_cross_execution(store, "market-cross", Decimal("1.00"))
    store.transition_execution(cross_id, state="holding_to_resolution", evidence={})

    first = run_n_leg_cutover_migration(store, manifest={})
    assert first["outcome"] == "migrated"

    second = run_n_leg_cutover_migration(store, manifest={})

    assert second["outcome"] == "already_migrated"
    assert second["total_unsettled_capital_units"] == 13_340_000
    assert store.n_leg_control()["total_unsettled_capital_units"] == 13_340_000
    assert read_minimum_reader_generation(_data_dir(tmp_path)) == 2
    db_path = (
        tmp_path / "data" / "prediction_arbitrage" / "prediction_arbitrage.sqlite3"
    )
    with sqlite3.connect(db_path) as connection:
        audit_rows = connection.execute(
            "SELECT COUNT(*) FROM control_events WHERE action='n_leg_cutover_migration'"
        ).fetchone()[0]
    assert audit_rows == 1


def test_cutover_fence_advanced_without_migration_blocks(tmp_path: Path) -> None:
    store = _store(tmp_path)
    plain_id = _create_plain_execution(store, "market-plain", Decimal("12.34"))
    store.transition_execution(plain_id, state="holding_to_resolution", evidence={})
    # The fence was advanced directly through the public store API without the
    # migration ever running: the idempotent short-circuit must not synthesize
    # a 0-unit already_migrated success from missing N_LEG state.
    store.advance_minimum_reader_generation(2)
    assert store.n_leg_control()["total_unsettled_capital_units"] == 0

    with pytest.raises(
        ValueError, match=N_LEG_CUTOVER_BLOCKED_INCONSISTENT_STATE
    ):
        run_n_leg_cutover_migration(store, manifest={})


def test_cutover_tampered_contract_generation_blocks(tmp_path: Path) -> None:
    store = _store(tmp_path)
    plain_id = _create_plain_execution(store, "market-plain", Decimal("12.34"))
    store.transition_execution(plain_id, state="holding_to_resolution", evidence={})

    assert run_n_leg_cutover_migration(store, manifest={})["outcome"] == "migrated"
    db_path = (
        tmp_path / "data" / "prediction_arbitrage" / "prediction_arbitrage.sqlite3"
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE n_leg_controls SET contract_generation=1 WHERE singleton=1"
        )

    with pytest.raises(
        ValueError, match=N_LEG_CUTOVER_BLOCKED_INCONSISTENT_STATE
    ):
        run_n_leg_cutover_migration(store, manifest={})


def test_cutover_idempotent_rerun_reports_real_migrated_state(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    plain_id = _create_plain_execution(store, "market-plain", Decimal("12.34"))
    store.transition_execution(plain_id, state="holding_to_resolution", evidence={})
    cross_id = _create_cross_execution(store, "market-cross", Decimal("1.00"))
    store.transition_execution(cross_id, state="holding_to_resolution", evidence={})

    first = run_n_leg_cutover_migration(store, manifest={})
    assert first["outcome"] == "migrated"

    second = run_n_leg_cutover_migration(store, manifest={})

    # The consistent short-circuit still reports success — with the REAL
    # migrated values read from the controls row, never synthesized defaults.
    assert second["outcome"] == "already_migrated"
    assert second["total_unsettled_capital_units"] == 13_340_000
    assert second["contract_generation"] == 2


def test_advance_minimum_reader_generation_only_up(tmp_path: Path) -> None:
    store = _store(tmp_path)
    data_dir = _data_dir(tmp_path)
    assert read_minimum_reader_generation(data_dir) == 1

    assert store.advance_minimum_reader_generation(2) == 2
    assert read_minimum_reader_generation(data_dir) == 2

    assert store.advance_minimum_reader_generation(2) == 2
    assert read_minimum_reader_generation(data_dir) == 2

    with pytest.raises(ValueError, match="cannot be lowered"):
        store.advance_minimum_reader_generation(1)
    assert read_minimum_reader_generation(data_dir) == 2


def _catalog_relation(
    round_tag: str, tag: str, *, complete: bool
) -> dict[str, object]:
    contracts = [f"t7-{round_tag}-{tag}-a", f"t7-{round_tag}-{tag}-b"]
    payload = _payload(
        relation_type="EXACTLY_ONE",
        endpoints=[
            dict(_endpoint(contract_id=contract, event_identity_basis="event-t7"))
            for contract in contracts
        ],
    )
    if complete:
        payload["problem"] = compiled_problem(
            contracts,
            {contract: "BUY_YES" for contract in contracts},
            as_of="2026-08-15T00:00:00Z",
            release_at="2026-12-31T17:00:00Z",
            rule={contract: f"rules-{contract}" for contract in contracts},
        )
    else:
        payload["terminal_states"] = []
        payload["payouts"] = {}
        payload["capital_release"] = None
        payload.pop("problem", None)
    return payload


def _force_approved(catalog: RelationCatalogV2, version_id: str) -> str:
    """Approve without publishing: status APPROVED, no generation membership
    (the COMPILED_PENDING_ACTIVATION / APPROVED_MODEL_INCOMPLETE states)."""
    catalog.store.begin_write()
    record = catalog.store["versions"][version_id]
    record["status"] = "APPROVED"
    catalog.store.commit_write()
    return str(record["identity"])


def _fixture_catalog(
    db_path: Path, round_tag: str
) -> tuple[RelationCatalogV2, list[str], list[str]]:
    catalog = RelationCatalogV2(SqliteCatalogStore(str(db_path)))
    complete_identities: list[str] = []
    incomplete_identities: list[str] = []
    for index in range(3):
        result = catalog.ingest(
            _catalog_relation(round_tag, f"ok{index}", complete=True)
        )
        complete_identities.append(_force_approved(catalog, str(result["version_id"])))
    for index in range(2):
        result = catalog.ingest(
            _catalog_relation(round_tag, f"bad{index}", complete=False)
        )
        incomplete_identities.append(_force_approved(catalog, str(result["version_id"])))
    return catalog, complete_identities, incomplete_identities


def test_activate_approved_relations_reconciles_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    db_path = (
        tmp_path / "data" / "prediction_arbitrage" / "prediction_arbitrage.sqlite3"
    )
    catalog, complete_identities, incomplete_identities = _fixture_catalog(
        db_path, "r1"
    )

    result = activate_approved_relations(store)

    assert result["published"] == 3
    assert result["blocked"] == 2
    assert result["blocked_causes"] == {
        identity: "INCOMPLETE_MODEL" for identity in incomplete_identities
    }
    fresh = RelationCatalogV2(SqliteCatalogStore(str(db_path)))
    assert set(fresh.store["generation"]) == set(complete_identities)
    assert len(fresh.store["generation"]) == 3

    # A fresh round of APPROVED relations: an injected publish failure inside
    # the catalog's write transaction must leave NO partial activation — the
    # generation stays exactly the first round's set.
    def _explode(*args: object, **kwargs: object) -> dict[str, object]:
        raise RuntimeError("publish exploded")

    round2_catalog, round2_complete, round2_incomplete = _fixture_catalog(
        db_path, "r2"
    )
    assert not (set(round2_complete) & set(complete_identities))
    monkeypatch.setattr(RelationCatalogV2, "_activate_many_locked", _explode)
    with pytest.raises(RuntimeError, match="publish exploded"):
        activate_approved_relations(store)
    monkeypatch.undo()
    reopened = RelationCatalogV2(SqliteCatalogStore(str(db_path)))
    assert set(reopened.store["generation"]) == set(complete_identities)
    for identity in round2_complete + round2_incomplete:
        version_id = reopened.store["latest"][identity]
        assert reopened.store["versions"][version_id]["status"] == "APPROVED"


def test_activate_approved_relations_records_provenance(tmp_path: Path) -> None:
    store = _store(tmp_path)
    db_path = (
        tmp_path / "data" / "prediction_arbitrage" / "prediction_arbitrage.sqlite3"
    )
    _fixture_catalog(db_path, "r1")

    result = activate_approved_relations(store)
    assert result["published"] == 3

    # Approval provenance must never persist an empty git_sha: the empty
    # string was the only approval path doing so.
    with sqlite3.connect(db_path) as connection:
        approval_rows = connection.execute(
            "SELECT actor, git_sha FROM catalog_v2_approvals"
        ).fetchall()
    assert approval_rows
    assert all(str(row[0]) == "n_leg_cutover" for row in approval_rows)
    assert all(str(row[1]) != "" for row in approval_rows)

    with pytest.raises(ValueError, match="git_sha"):
        activate_approved_relations(store, git_sha="")


def test_manifest_mismatch_blocks(tmp_path: Path) -> None:
    store = _store(tmp_path)
    settled_id = _create_plain_execution(store, "market-settled", Decimal("1.00"))
    store.transition_execution(settled_id, state="both_rejected", evidence={})

    # Manifest entry referencing a missing execution.
    with pytest.raises(
        ValueError, match=N_LEG_CUTOVER_BLOCKED_MANIFEST_MISMATCH
    ):
        run_n_leg_cutover_migration(store, manifest={"exec:missing": "5.00"})
    # Manifest entry referencing a terminal (settled) execution.
    with pytest.raises(
        ValueError, match=N_LEG_CUTOVER_BLOCKED_MANIFEST_MISMATCH
    ):
        run_n_leg_cutover_migration(store, manifest={settled_id: "5.00"})

    # Both blocks are logical zero-change: no singleton, fence 1, no audit row.
    control = store.n_leg_control()
    assert control["total_unsettled_capital_units"] == 0
    assert control["contract_generation"] == 1
    assert read_minimum_reader_generation(_data_dir(tmp_path)) == 1
    assert (
        store.latest_control_event("n_leg_cutover_migration", "n_leg_controls")
        is None
    )


def test_cutover_atomic_rollback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store(tmp_path)
    plain_id = _create_plain_execution(store, "market-plain", Decimal("12.34"))
    store.transition_execution(plain_id, state="holding_to_resolution", evidence={})

    def _explode(*args: object, **kwargs: object) -> int:
        raise RuntimeError("fence advance exploded after the controls write")

    monkeypatch.setattr(
        PredictionArbitrageStore, "advance_minimum_reader_generation", _explode
    )

    with pytest.raises(RuntimeError, match="fence advance exploded"):
        run_n_leg_cutover_migration(store, manifest={})

    monkeypatch.undo()
    # The controls write happened inside the failed transaction: everything
    # must roll back — no singleton, fence still 1, no audit row.
    control = store.n_leg_control()
    assert control["total_unsettled_capital_units"] == 0
    assert control["contract_generation"] == 1
    assert read_minimum_reader_generation(_data_dir(tmp_path)) == 1
    assert (
        store.latest_control_event("n_leg_cutover_migration", "n_leg_controls")
        is None
    )
