from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from open_trader.prediction_n_leg_mode import (
    DEFAULT_QUALIFICATION_POLICY,
    DEFAULT_SAFETY_CONFIG,
    NLegVersionConflict,
    n_leg_mode_contract,
    n_leg_set_mode,
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
