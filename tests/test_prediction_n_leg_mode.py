from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from open_trader.prediction_n_leg_mode import (
    DEFAULT_QUALIFICATION_POLICY,
    DEFAULT_SAFETY_CONFIG,
    NLegVersionConflict,
    ensure_same_event_same_venue_scope,
    n_leg_enforce_auto_scope_versions,
    n_leg_mode_contract,
    n_leg_order_readiness,
    n_leg_set_enabled_scope,
    n_leg_set_mode,
    n_leg_update_qualification_policy,
    n_leg_update_safety_config,
    n_leg_caps_gate,
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


def test_ensure_same_event_same_venue_scope_is_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)

    assert ensure_same_event_same_venue_scope(store) is True

    scope = n_leg_mode_contract(store)["execution_scopes"]["SAME_EVENT_SAME_VENUE"]
    assert scope["capability"] == "OBSERVE_ONLY"
    assert scope["scope_version"] == 1
    assert scope["members"] == {
        "relation_type": "complement",
        "same_event": True,
        "same_venue": True,
        "venues": ["polymarket"],
    }
    event = store.latest_control_event(
        "n_leg_upsert_scope", "n_leg_execution_scopes/SAME_EVENT_SAME_VENUE"
    )
    assert event is not None
    assert event["outcome"] == "succeeded"

    with sqlite3.connect(_db_path(tmp_path)) as connection:
        before = connection.execute(
            "SELECT COUNT(*) FROM control_events WHERE action='n_leg_upsert_scope'"
        ).fetchone()[0]

    assert ensure_same_event_same_venue_scope(store) is False

    scope = n_leg_mode_contract(store)["execution_scopes"]["SAME_EVENT_SAME_VENUE"]
    assert scope["scope_version"] == 1
    with sqlite3.connect(_db_path(tmp_path)) as connection:
        after = connection.execute(
            "SELECT COUNT(*) FROM control_events WHERE action='n_leg_upsert_scope'"
        ).fetchone()[0]
    assert before == 1
    assert after == 1


def test_n_leg_solution_projection_passes_policy_and_balances(tmp_path: Path) -> None:
    from datetime import UTC, datetime, timedelta

    from open_trader.prediction_market_solution import MarketSolution
    from open_trader.prediction_n_leg import (
        ActionQuantity,
        canonical_payload,
        fingerprint,
    )
    from open_trader.prediction_read_model import _prediction_n_leg_solution_projection

    now = datetime(2026, 9, 1, tzinfo=UTC)
    market = canonical_payload(
        MarketSolution(
            component_id="c1",
            structure_fingerprint="sha256:struct",
            quote_fingerprint="sha256:quote",
            quantities=(ActionQuantity("a-yes", 2), ActionQuantity("a-no", 2)),
            guaranteed_profit_units=1_500_000,
            bounded_cost_units=98_500_000,
            bounded_payout_units=100_000_000,
            capital_release_at=now + timedelta(days=30),
            global_search_closed=True,
            verification_fingerprint="sha256:verify",
        )
    )
    execution = {
        "market_solution_fingerprint": fingerprint(canonical_payload(market)),
        "quantities": market["quantities"],
        "capital_use_units": 98_500_000,
        "reason": "EXECUTABLE",
        "order_ready": False,
        "partial_fill_proof": "PARTIAL_FILL_SAFE",
    }
    policy = dict(DEFAULT_QUALIFICATION_POLICY)
    policy["min_net_margin"] = "0.02"
    n_leg = {
        "schema_version": "open_trader.prediction_n_leg.mode_contract.v1",
        "contract_generation": 1,
        "mode": "MANUAL",
        "qualification_policy_version": 2,
        "qualification_policy": {"version": 2, "policy": policy},
        "safety_config_version": 1,
        "safety_config": DEFAULT_SAFETY_CONFIG,
        "execution_scopes": {
            "s1": {"scope_id": "s1", "capability": "MANUAL_CANARY", "scope_version": 1},
        },
        "enabled_execution_scope_version": [{"scope_id": "s1", "scope_version": 1}],
        "execution_gates": {
            "breaker_open": False,
            "incident_active": False,
            "batch_active": False,
        },
    }

    items = _prediction_n_leg_solution_projection(
        [
            {
                "component_id": "c1",
                "scope_id": "s1",
                "market": market,
                "execution": execution,
                "fee": {
                    "status": "fee_free",
                    "charging_contracts": [],
                    "unknown_contracts": [],
                },
                "legs": [
                    {
                        "action_id": "a-yes",
                        "venue": "polymarket",
                        "max_cost": "50.00",
                    },
                    {
                        "action_id": "a-no",
                        "venue": "predict.fun",
                        "max_cost": "48.50",
                    },
                ],
            }
        ],
        n_leg=n_leg,
        total_unsettled_capital_units=0,
        now=now,
        balance_snapshot={
            "polymarket": {"available": "100.00", "allowance": "100.00"},
            "predict.fun": {"available": "100.00", "allowance": "100.00"},
        },
    )

    assert len(items) == 1
    item = items[0]
    # The 1.5% margin fails the contract's 2% floor: policy comes from the
    # contract, not from hardcoded defaults.
    assert item["qualification"]["policy"]["min_net_margin"] == "0.02"
    assert item["qualification"]["status"] == "NOT_QUALIFIED"
    checks = {row["key"]: row for row in item["qualification"]["checks"]}
    assert checks["net_margin"]["passed"] is False
    assert item["funding"]["status"] == "SUFFICIENT"
    assert set(item["funding"]["venues"]) == {"polymarket", "predict.fun"}
    assert item["execution"]["reason"] == "MANUAL_CANARY"


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


# ---------------------------------------------------------------------------
# Issue #64 Slice 1: the four caps must be written in one explicit shot; the
# stored config JSON carries a caps_configured marker and the order gate reads
# only that marker. A1/A2/A3 are the approved acceptance cases.
# ---------------------------------------------------------------------------


def _caps_config_override() -> dict[str, object]:
    return {
        "episode_rearm_gap_seconds": 300,
        "max_per_trade_cost_units": 25_000_000,
        "max_total_unsettled_capital_units": 100_000_000,
        "max_partial_fill_loss_units": 1_000_000,
        "max_auto_repair_loss_units": 1_000_000,
    }


def test_a1_full_caps_write_marks_caps_configured_and_gate_opens(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    assert n_leg_caps_gate(store) == (False, "CAPS_NOT_CONFIGURED")

    contract = n_leg_update_safety_config(
        store, config=_caps_config_override(), base_version=1
    )

    assert contract["safety_config"]["caps_configured"] is True
    stored = store.n_leg_safety_config_latest()
    assert stored is not None
    assert stored["config"]["caps_configured"] is True
    ok, reason = n_leg_caps_gate(store)
    assert ok is True
    assert reason == "CAPS_CONFIGURED"


def test_a2_partial_caps_write_is_rejected_and_config_unchanged(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    partial = {
        "episode_rearm_gap_seconds": 300,
        "max_per_trade_cost_units": 25_000_000,
        "max_total_unsettled_capital_units": 100_000_000,
    }

    with pytest.raises(ValueError):
        n_leg_update_safety_config(store, config=partial, base_version=1)

    assert store.n_leg_safety_config_latest() is None
    assert n_leg_mode_contract(store)["safety_config_version"] == 1
    assert n_leg_caps_gate(store) == (False, "CAPS_NOT_CONFIGURED")


def test_a3_default_v1_all_zero_caps_gate_stays_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)

    ok, reason = n_leg_caps_gate(store)

    assert ok is False
    assert reason == "CAPS_NOT_CONFIGURED"
    assert n_leg_mode_contract(store)["safety_config"] == DEFAULT_SAFETY_CONFIG


def test_a4_preflight_threshold_defaults_are_versioned_config(tmp_path: Path) -> None:
    """Ruling 7: the quote-age and skew thresholds ride the versioned safety
    config with defaults 10/5; a write without them reads as the defaults."""
    store = _store(tmp_path)
    contract = n_leg_update_safety_config(
        store,
        config={
            "episode_rearm_gap_seconds": 300,
            "max_per_trade_cost_units": 25_000_000,
            "max_total_unsettled_capital_units": 100_000_000,
            "max_partial_fill_loss_units": 1_000_000,
            "max_auto_repair_loss_units": 1_000_000,
        },
        base_version=1,
    )
    assert contract["safety_config"]["max_quote_age_seconds"] == 10
    assert contract["safety_config"]["max_cross_leg_skew_seconds"] == 5


def test_p3_submit_timeout_and_reconciliation_window_are_versioned_config(
    tmp_path: Path,
) -> None:
    """Ruling 8 (review round 2): ``max_leg_submit_seconds`` (default 15) and
    ``reconciliation_timeout_seconds`` (default 60) ride the versioned safety
    config as NON-cap keys — writable alongside the four caps without
    extending the four-together rule, marker semantics unchanged, defaults
    when not written, and a non-positive value is rejected."""
    store = _store(tmp_path)
    contract = n_leg_update_safety_config(
        store,
        config={
            "episode_rearm_gap_seconds": 300,
            "max_per_trade_cost_units": 25_000_000,
            "max_total_unsettled_capital_units": 100_000_000,
            "max_partial_fill_loss_units": 1_000_000,
            "max_auto_repair_loss_units": 1_000_000,
            "max_leg_submit_seconds": 2,
            "reconciliation_timeout_seconds": 90,
        },
        base_version=1,
    )
    assert contract["safety_config"]["max_leg_submit_seconds"] == 2
    assert contract["safety_config"]["reconciliation_timeout_seconds"] == 90
    # the caps marker rides the four caps, exactly as before
    assert contract["safety_config"]["caps_configured"] is True
    assert n_leg_caps_gate(store) == (True, "CAPS_CONFIGURED")

    # a write without them reads as the defaults (15 / 60)
    fresh = _store(tmp_path / "defaults")
    contract = n_leg_update_safety_config(
        fresh,
        config={
            "episode_rearm_gap_seconds": 300,
            "max_per_trade_cost_units": 0,
            "max_total_unsettled_capital_units": 0,
            "max_partial_fill_loss_units": 0,
            "max_auto_repair_loss_units": 0,
        },
        base_version=1,
    )
    assert contract["safety_config"]["max_leg_submit_seconds"] == 15
    assert contract["safety_config"]["reconciliation_timeout_seconds"] == 60

    # non-positive values are rejected and change nothing
    with pytest.raises(ValueError):
        n_leg_update_safety_config(
            store,
            config={
                "episode_rearm_gap_seconds": 300,
                "max_per_trade_cost_units": 25_000_000,
                "max_total_unsettled_capital_units": 100_000_000,
                "max_partial_fill_loss_units": 1_000_000,
                "max_auto_repair_loss_units": 1_000_000,
                "max_leg_submit_seconds": 0,
            },
            base_version=2,
        )
    assert (
        store.n_leg_safety_config_latest()["config"]["max_leg_submit_seconds"]
        == 2
    )
