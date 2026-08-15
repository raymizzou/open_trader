from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from open_trader.prediction_n_leg_mode import (
    DEFAULT_QUALIFICATION_POLICY,
    DEFAULT_SAFETY_CONFIG,
    NLegVersionConflict,
    n_leg_enforce_auto_scope_versions,
    n_leg_mode_contract,
    n_leg_order_readiness,
    n_leg_set_enabled_scope,
    n_leg_set_mode,
    n_leg_update_qualification_policy,
    n_leg_update_safety_config,
    n_leg_upsert_scope,
)


def _store(tmp_path: Path) -> PredictionArbitrageStore:
    return PredictionArbitrageStore(tmp_path / "data")


def _db_path(tmp_path: Path) -> Path:
    return (
        tmp_path
        / "data"
        / "prediction_arbitrage"
        / "prediction_arbitrage.sqlite3"
    )


def _scope_members(extra: object = None) -> dict[str, object]:
    members = {
        "relation_type": "complement",
        "same_event": True,
        "same_venue": False,
        "venues": ["predict", "polymarket"],
    }
    if extra is not None:
        members.update(extra)
    return members


def test_fresh_contract_defaults_to_manual_and_never_inherits_legacy_mode(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.set_cross_auto_mode("auto_submit", "legacy_owner")

    contract = n_leg_mode_contract(store)

    assert contract["schema_version"] == "open_trader.prediction_n_leg.mode_contract.v1"
    assert contract["contract_generation"] == 1
    assert contract["mode"] == "MANUAL"
    assert contract["qualification_policy_version"] == 1
    assert contract["qualification_policy"] == DEFAULT_QUALIFICATION_POLICY
    assert contract["safety_config_version"] == 1
    assert contract["safety_config"] == DEFAULT_SAFETY_CONFIG
    assert contract["execution_scopes"] == {}
    assert contract["enabled_execution_scope_version"] == []
    assert contract["execution_gates"] == {
        "breaker_open": False,
        "incident_active": False,
        "batch_active": False,
    }


def test_set_mode_persists_and_audits_write_word(tmp_path: Path) -> None:
    store = _store(tmp_path)

    contract = n_leg_set_mode(
        store, mode="AUTO", base_contract_generation=1, audit={"actor": "test"}
    )

    assert contract["mode"] == "AUTO"
    assert _store(tmp_path).n_leg_control()["mode"] == "AUTO"
    event = store.latest_control_event("n_leg_set_mode", "n_leg_controls")
    assert event is not None
    assert event["outcome"] == "succeeded"
    assert event["payload"]["action_word"] == "auto_submit"


def test_set_mode_version_mismatch_rejects_without_state_change(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)

    with pytest.raises(NLegVersionConflict, match="generation mismatch"):
        n_leg_set_mode(store, mode="AUTO", base_contract_generation=99)

    assert n_leg_mode_contract(store)["mode"] == "MANUAL"


def test_malformed_stored_enabled_list_reads_fail_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.n_leg_mode_control_write(enabled_execution_scope_version=[])
    with sqlite3.connect(_db_path(tmp_path)) as connection:
        connection.execute(
            "UPDATE n_leg_controls SET enabled_execution_scope_version='not-json' WHERE singleton=1"
        )

    assert n_leg_mode_contract(store)["enabled_execution_scope_version"] == []


def test_upsert_scope_starts_observe_only_and_bumps_version(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="OBSERVE_ONLY"):
        n_leg_upsert_scope(
            store,
            scope_id="s1",
            capability="MANUAL_CANARY",
            members=_scope_members(),
        )

    contract = n_leg_upsert_scope(
        store, scope_id="s1", capability="OBSERVE_ONLY", members=_scope_members()
    )
    assert contract["execution_scopes"]["s1"]["scope_version"] == 1

    contract = n_leg_upsert_scope(
        store,
        scope_id="s1",
        capability="MANUAL_CANARY",
        members=_scope_members(),
        base_scope_version=1,
    )
    assert contract["execution_scopes"]["s1"]["capability"] == "MANUAL_CANARY"
    assert contract["execution_scopes"]["s1"]["scope_version"] == 2

    with pytest.raises(NLegVersionConflict, match="scope version mismatch"):
        n_leg_upsert_scope(
            store,
            scope_id="s1",
            capability="OBSERVE_ONLY",
            members=_scope_members(),
            base_scope_version=1,
        )


def test_scope_members_change_downgrades_auto_to_manual(tmp_path: Path) -> None:
    store = _store(tmp_path)
    n_leg_upsert_scope(
        store, scope_id="s1", capability="OBSERVE_ONLY", members=_scope_members()
    )
    n_leg_set_mode(store, mode="AUTO", base_contract_generation=1)

    contract = n_leg_upsert_scope(
        store,
        scope_id="s1",
        capability="OBSERVE_ONLY",
        members=_scope_members({"venues": ["predict"]}),
        base_scope_version=1,
    )

    assert contract["mode"] == "MANUAL"
    assert contract["execution_scopes"]["s1"]["scope_version"] == 2
    assert (
        store.latest_control_event("n_leg_auto_downgrade", "n_leg_controls")["payload"][
            "reason"
        ]
        == "SCOPE_MEMBERS_CHANGED"
    )


def test_policy_tighten_keeps_auto_and_loosen_downgrades(tmp_path: Path) -> None:
    store = _store(tmp_path)
    n_leg_set_mode(store, mode="AUTO", base_contract_generation=1)
    tightened = dict(DEFAULT_QUALIFICATION_POLICY)
    tightened["min_profit_usd"] = "5.00"

    contract = n_leg_update_qualification_policy(
        store, policy=tightened, base_version=1
    )

    assert contract["mode"] == "AUTO"
    assert contract["qualification_policy_version"] == 2

    loosened = dict(DEFAULT_QUALIFICATION_POLICY)
    loosened["min_net_margin"] = "0.001"
    contract = n_leg_update_qualification_policy(
        store, policy=loosened, base_version=2
    )

    assert contract["mode"] == "MANUAL"
    assert contract["qualification_policy_version"] == 3
    assert (
        store.latest_control_event("n_leg_auto_downgrade", "n_leg_controls")["payload"][
            "reason"
        ]
        == "QUALIFICATION_POLICY_LOOSENED"
    )


def test_policy_version_mismatch_rejects_without_change(tmp_path: Path) -> None:
    store = _store(tmp_path)
    changed = dict(DEFAULT_QUALIFICATION_POLICY)
    changed["min_profit_usd"] = "2.00"

    with pytest.raises(NLegVersionConflict, match="policy version mismatch"):
        n_leg_update_qualification_policy(store, policy=changed, base_version=99)

    assert n_leg_mode_contract(store)["qualification_policy_version"] == 1


def test_safety_config_direction_downgrades_on_loosen_only(tmp_path: Path) -> None:
    store = _store(tmp_path)
    n_leg_set_mode(store, mode="AUTO", base_contract_generation=1)
    tightened = dict(DEFAULT_SAFETY_CONFIG)
    tightened["episode_rearm_gap_seconds"] = 600

    contract = n_leg_update_safety_config(store, config=tightened, base_version=1)
    assert contract["mode"] == "AUTO"
    assert contract["safety_config_version"] == 2

    loosened = dict(DEFAULT_SAFETY_CONFIG)
    loosened["max_total_unsettled_capital_units"] = 1000
    contract = n_leg_update_safety_config(store, config=loosened, base_version=2)
    assert contract["mode"] == "MANUAL"
    assert contract["safety_config_version"] == 3


def test_readiness_reflects_capability_and_mode(tmp_path: Path) -> None:
    store = _store(tmp_path)
    n_leg_upsert_scope(
        store, scope_id="observe", capability="OBSERVE_ONLY", members=_scope_members()
    )
    n_leg_upsert_scope(
        store, scope_id="canary", capability="OBSERVE_ONLY", members=_scope_members()
    )
    n_leg_upsert_scope(
        store,
        scope_id="canary",
        capability="MANUAL_CANARY",
        members=_scope_members(),
        base_scope_version=1,
    )
    n_leg_upsert_scope(
        store, scope_id="auto", capability="OBSERVE_ONLY", members=_scope_members()
    )
    n_leg_upsert_scope(
        store,
        scope_id="auto",
        capability="AUTO_ELIGIBLE",
        members=_scope_members(),
        base_scope_version=1,
    )

    readiness = n_leg_order_readiness(store)
    assert readiness["scopes"]["observe"] == {
        "scope_id": "observe",
        "order_ready": False,
        "reason": "SCOPE_OBSERVE_ONLY",
        "action": None,
    }
    assert readiness["scopes"]["canary"]["action"] == "manual_confirm"
    assert readiness["scopes"]["auto"]["order_ready"] is True
    assert readiness["scopes"]["auto"]["reason"] == "MANUAL_CONFIRM_ALLOWED"
    assert readiness["scopes"]["auto"]["action"] == "manual_confirm"

    n_leg_set_mode(store, mode="AUTO", base_contract_generation=1)
    readiness = n_leg_order_readiness(store)
    assert readiness["order_ready"] is False
    assert readiness["scopes"]["auto"]["reason"] == "SCOPE_NOT_ENABLED"


def test_gates_block_readiness_even_when_auto_ready(tmp_path: Path) -> None:
    store = _store(tmp_path)
    n_leg_upsert_scope(
        store, scope_id="auto", capability="OBSERVE_ONLY", members=_scope_members()
    )
    n_leg_upsert_scope(
        store,
        scope_id="auto",
        capability="AUTO_ELIGIBLE",
        members=_scope_members(),
        base_scope_version=1,
    )
    n_leg_set_enabled_scope(store, scope_id="auto", enable=True, base_contract_generation=1)
    n_leg_set_mode(store, mode="AUTO", base_contract_generation=1)
    with sqlite3.connect(_db_path(tmp_path)) as connection:
        connection.execute(
            "UPDATE n_leg_controls SET breaker_open=1, breaker_reason='TEST' WHERE singleton=1"
        )

    readiness = n_leg_order_readiness(store)

    assert readiness["order_ready"] is False
    assert readiness["scopes"]["auto"]["reason"] == "GLOBAL_BREAKER_OPEN"


def test_auto_enable_requires_closed_gates_and_resolved_incident(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.n_leg_mode_control_write(enabled_execution_scope_version=[])
    with sqlite3.connect(_db_path(tmp_path)) as connection:
        connection.execute(
            "UPDATE n_leg_controls SET breaker_open=1, breaker_reason='TEST' WHERE singleton=1"
        )
    with pytest.raises(ValueError, match="N_LEG_BREAKER_OPEN"):
        n_leg_set_mode(store, mode="AUTO", base_contract_generation=1)
    with sqlite3.connect(_db_path(tmp_path)) as connection:
        connection.execute(
            "UPDATE n_leg_controls SET breaker_open=0, breaker_reason=NULL WHERE singleton=1"
        )

    now = "2026-08-16T00:00:00Z"
    with sqlite3.connect(_db_path(tmp_path)) as connection:
        connection.execute(
            "INSERT INTO previews(preview_id, payload, created_at, expires_at, consumed_at) VALUES ('p1', '{}', ?, ?, NULL)",
            (now, now),
        )
        connection.execute(
            "INSERT INTO executions(execution_id, preview_id, idempotency_key, singleton, state, payload, evidence, created_at, updated_at) VALUES ('e1', 'p1', 'k1', 1, 'complete', '{}', '{}', ?, ?)",
            (now, now),
        )
        connection.execute(
            "INSERT INTO incidents(incident_id, execution_id, payload, acknowledgement, acknowledged_at, created_at, updated_at) VALUES ('inc-1', 'e1', '{}', '\"operator\"', ?, ?, ?)",
            (now, now, now),
        )

    with pytest.raises(ValueError, match="N_LEG_AUTO_REQUIRES_RESOLVED_INCIDENT"):
        n_leg_set_mode(store, mode="AUTO", base_contract_generation=1)
    with pytest.raises(ValueError, match="N_LEG_AUTO_REQUIRES_RESOLVED_INCIDENT"):
        n_leg_set_mode(
            store,
            mode="AUTO",
            base_contract_generation=1,
            incident_id="inc-wrong",
        )

    contract = n_leg_set_mode(
        store,
        mode="AUTO",
        base_contract_generation=1,
        incident_id="inc-1",
    )
    assert contract["mode"] == "AUTO"


def test_enable_scope_expansion_downgrades_and_go_existing_is_idempotent(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    n_leg_upsert_scope(
        store, scope_id="s1", capability="OBSERVE_ONLY", members=_scope_members()
    )
    n_leg_set_enabled_scope(store, scope_id="s1", enable=True, base_contract_generation=1)
    n_leg_set_mode(store, mode="AUTO", base_contract_generation=1)

    n_leg_upsert_scope(
        store,
        scope_id="s2",
        capability="OBSERVE_ONLY",
        members=_scope_members({"relation_type": "threshold"}),
    )
    contract = n_leg_set_enabled_scope(
        store, scope_id="s2", enable=True, base_contract_generation=1
    )
    assert contract["mode"] == "MANUAL"

    re_go = n_leg_set_enabled_scope(
        store, scope_id="s2", enable=True, base_contract_generation=1
    )
    assert re_go["mode"] == "MANUAL"
    assert re_go["enabled_execution_scope_version"] == contract[
        "enabled_execution_scope_version"
    ]

    contract = n_leg_set_enabled_scope(
        store, scope_id="s2", enable=False, base_contract_generation=1
    )
    assert all(item["scope_id"] != "s2" for item in contract["enabled_execution_scope_version"])


def test_enforce_auto_scope_versions_downgrades_on_drift(tmp_path: Path) -> None:
    store = _store(tmp_path)
    n_leg_upsert_scope(
        store, scope_id="s1", capability="OBSERVE_ONLY", members=_scope_members()
    )
    n_leg_set_enabled_scope(store, scope_id="s1", enable=True, base_contract_generation=1)
    n_leg_set_mode(store, mode="AUTO", base_contract_generation=1)
    with sqlite3.connect(_db_path(tmp_path)) as connection:
        connection.execute(
            "UPDATE n_leg_execution_scopes SET scope_version=99 WHERE scope_id='s1'"
        )

    result = n_leg_enforce_auto_scope_versions(store, audit={"actor": "runtime"})

    assert result == {
        "ok": False,
        "mode": "MANUAL",
        "downgraded": True,
        "scope_ids": ["s1"],
    }
    assert n_leg_mode_contract(store)["mode"] == "MANUAL"


def test_invalid_policy_and_safety_payloads_fail_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="fields are invalid"):
        n_leg_update_qualification_policy(
            store, policy={"min_profit_usd": "1.00"}, base_version=1
        )
    with pytest.raises(ValueError, match="non-negative"):
        n_leg_update_safety_config(
            store,
            config={**DEFAULT_SAFETY_CONFIG, "max_auto_repair_loss_units": -1},
            base_version=1,
        )
    assert n_leg_mode_contract(store)["qualification_policy_version"] == 1
    assert n_leg_mode_contract(store)["safety_config_version"] == 1
