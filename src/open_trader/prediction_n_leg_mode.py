"""Versioned N_LEG mode contract: mode, scope capability, policy, and gates.

Issue #58 backend contract. This layer persists and audits configuration only;
qualification evaluation stays in the #51 solver/verifier chain and real order
submission stays outside this module until the #60 owner cutover.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Mapping


SCHEMA_VERSION = "open_trader.prediction_n_leg.mode_contract.v1"
CAPABILITIES = ("OBSERVE_ONLY", "MANUAL_CANARY", "AUTO_ELIGIBLE")

DEFAULT_QUALIFICATION_POLICY = {
    "min_profit_usd": "1.00",
    "min_net_margin": "0.01",
    "min_annualized_return": "0.15",
    "max_capital_release_days": 30,
}

DEFAULT_SAFETY_CONFIG = {
    "episode_rearm_gap_seconds": 300,
    "max_total_unsettled_capital_units": 0,
    "max_partial_fill_loss_units": 0,
    "max_auto_repair_loss_units": 0,
}


class NLegVersionConflict(Exception):
    """The mutation base version does not match the stored contract version."""


def _audit_dict(audit: object) -> dict[str, object]:
    if not isinstance(audit, Mapping):
        return {}
    return {str(key): value for key, value in audit.items()}


def _write_word(mode: str) -> str:
    return "auto_submit" if mode == "AUTO" else "manual_confirm"


def _positive_int(value: object, name: str) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _nonnegative_int(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _money(value: object, name: str) -> Decimal:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a decimal string")
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{name} must be a decimal string") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"{name} must be non-negative")
    return amount


def _validated_policy(policy: object) -> dict[str, object]:
    expected = set(DEFAULT_QUALIFICATION_POLICY)
    if not isinstance(policy, dict) or set(policy) != expected:
        raise ValueError("qualification policy fields are invalid")
    return {
        "min_profit_usd": str(_money(policy["min_profit_usd"], "min_profit_usd")),
        "min_net_margin": str(_money(policy["min_net_margin"], "min_net_margin")),
        "min_annualized_return": str(
            _money(policy["min_annualized_return"], "min_annualized_return")
        ),
        "max_capital_release_days": _positive_int(
            policy["max_capital_release_days"], "max_capital_release_days"
        ),
    }


def _validated_safety_config(config: object) -> dict[str, object]:
    expected = set(DEFAULT_SAFETY_CONFIG)
    if not isinstance(config, dict) or set(config) != expected:
        raise ValueError("safety config fields are invalid")
    return {
        "episode_rearm_gap_seconds": _positive_int(
            config["episode_rearm_gap_seconds"], "episode_rearm_gap_seconds"
        ),
        "max_total_unsettled_capital_units": _nonnegative_int(
            config["max_total_unsettled_capital_units"],
            "max_total_unsettled_capital_units",
        ),
        "max_partial_fill_loss_units": _nonnegative_int(
            config["max_partial_fill_loss_units"], "max_partial_fill_loss_units"
        ),
        "max_auto_repair_loss_units": _nonnegative_int(
            config["max_auto_repair_loss_units"], "max_auto_repair_loss_units"
        ),
    }


def _validated_members(members: object) -> dict[str, object]:
    if not isinstance(members, dict) or not members:
        raise ValueError("scope members must be a non-empty object")
    return {str(key): value for key, value in members.items()}


def _current_policy(store: object) -> dict[str, object]:
    stored = store.n_leg_qualification_policy_latest()
    if stored is None:
        return {"version": 1, "policy": dict(DEFAULT_QUALIFICATION_POLICY)}
    return {
        "version": _positive_int(stored["version"], "policy version"),
        "policy": _validated_policy(stored["policy"]),
    }


def _current_safety_config(store: object) -> dict[str, object]:
    stored = store.n_leg_safety_config_latest()
    if stored is None:
        return {"version": 1, "config": dict(DEFAULT_SAFETY_CONFIG)}
    return {
        "version": _positive_int(stored["version"], "safety version"),
        "config": _validated_safety_config(stored["config"]),
    }


def n_leg_mode_contract(store: object) -> dict[str, object]:
    """Compose the full versioned N_LEG mode contract for one store."""
    control = store.n_leg_control()
    policy = _current_policy(store)
    safety = _current_safety_config(store)
    scopes = store.n_leg_scopes()
    return {
        "schema_version": SCHEMA_VERSION,
        "contract_generation": _positive_int(
            control["contract_generation"], "contract_generation"
        ),
        "mode": str(control["mode"]),
        "qualification_policy_version": _positive_int(
            control["qualification_policy_version"], "qualification_policy_version"
        ),
        "qualification_policy": policy["policy"],
        "safety_config_version": _positive_int(
            control["safety_config_version"], "safety_config_version"
        ),
        "safety_config": safety["config"],
        "execution_scopes": scopes,
        "enabled_execution_scope_version": list(
            control["enabled_execution_scope_version"]
        ),
        "execution_gates": {
            "breaker_open": bool(control["breaker_open"]),
            "incident_active": store.unacknowledged_incident() is not None,
            "batch_active": control["active_batch_id"] is not None,
        },
    }


def n_leg_set_mode(
    store: object,
    *,
    mode: str,
    base_contract_generation: int,
    incident_id: object = None,
    audit: object = None,
) -> dict[str, object]:
    if mode not in {"MANUAL", "AUTO"}:
        raise ValueError("n-leg mode must be MANUAL or AUTO")
    control = store.n_leg_control()
    if int(control["contract_generation"]) != base_contract_generation:
        raise NLegVersionConflict("n-leg contract generation mismatch")
    store.n_leg_mode_control_write(
        mode=mode,
        contract_generation=int(control["contract_generation"]),
        qualification_policy_version=int(control["qualification_policy_version"]),
        safety_config_version=int(control["safety_config_version"]),
        enabled_execution_scope_version=list(
            control["enabled_execution_scope_version"]
        ),
    )
    store.record_control_event(
        action="n_leg_set_mode",
        target="n_leg_controls",
        outcome="succeeded",
        payload={
            "mode": mode,
            "action_word": _write_word(mode),
            **_audit_dict(audit),
        },
    )
    return n_leg_mode_contract(store)
