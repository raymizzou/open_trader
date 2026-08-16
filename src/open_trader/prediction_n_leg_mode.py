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
    if mode == "AUTO":
        _require_auto_enable(store, control, incident_id)
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


def _require_auto_enable(store: object, control: Mapping[str, object], incident_id: object) -> None:
    if control["breaker_open"]:
        raise ValueError("N_LEG_BREAKER_OPEN")
    if control["active_batch_id"] is not None:
        raise ValueError("N_LEG_ACTIVE_BATCH_EXISTS")
    if store.unacknowledged_incident() is not None:
        raise ValueError("N_LEG_INCIDENT_ACTIVE")
    for item in control["enabled_execution_scope_version"]:
        scope = store.n_leg_scope(str(item["scope_id"]))
        if scope is None or int(scope["scope_version"]) != int(item["scope_version"]):
            raise ValueError("N_LEG_SCOPE_VERSION_DRIFT")
    incidents = store.histories("incidents")
    if incidents:
        if not isinstance(incident_id, str) or not incident_id:
            raise ValueError("N_LEG_AUTO_REQUIRES_RESOLVED_INCIDENT")
        resolved = next(
            (
                item
                for item in incidents
                if item.get("incident_id") == incident_id and item.get("acknowledged") is True
            ),
            None,
        )
        if resolved is None:
            raise ValueError("N_LEG_AUTO_REQUIRES_RESOLVED_INCIDENT")


def n_leg_set_enabled_scope(
    store: object,
    *,
    scope_id: str,
    enable: bool,
    base_contract_generation: int,
    audit: object = None,
) -> dict[str, object]:
    if not isinstance(scope_id, str) or not scope_id:
        raise ValueError("scope id must be non-empty text")
    if type(enable) is not bool:
        raise ValueError("enable must be a boolean")
    control = store.n_leg_control()
    if int(control["contract_generation"]) != base_contract_generation:
        raise NLegVersionConflict("n-leg contract generation mismatch")
    enabled = list(control["enabled_execution_scope_version"])
    current = next((item for item in enabled if item["scope_id"] == scope_id), None)
    if enable:
        scope = store.n_leg_scope(scope_id)
        if scope is None:
            raise ValueError("N_LEG_SCOPE_NOT_REGISTERED")
        if current is not None and int(current["scope_version"]) == int(scope["scope_version"]):
            return n_leg_mode_contract(store)
        enabled = [item for item in enabled if item["scope_id"] != scope_id]
        enabled.append({"scope_id": scope_id, "scope_version": int(scope["scope_version"])})
        downgrade = current is None
    else:
        enabled = [item for item in enabled if item["scope_id"] != scope_id]
        downgrade = False
    next_mode = (
        "MANUAL"
        if downgrade and control["mode"] == "AUTO"
        else str(control["mode"])
    )
    store.n_leg_mode_control_write(
        mode=next_mode,
        contract_generation=int(control["contract_generation"]),
        qualification_policy_version=int(control["qualification_policy_version"]),
        safety_config_version=int(control["safety_config_version"]),
        enabled_execution_scope_version=enabled,
    )
    if downgrade:
        store.record_control_event(
            action="n_leg_auto_downgrade",
            target="n_leg_controls",
            outcome="failed",
            payload={
                "reason": "SCOPE_ENABLED_EXPANSION",
                "scope_id": scope_id,
                "mode": "MANUAL",
                "action_word": "manual_confirm",
                **_audit_dict(audit),
            },
        )
    store.record_control_event(
        action="n_leg_set_enabled_scope",
        target=f"n_leg_execution_scopes/{scope_id}",
        outcome="succeeded",
        payload={
            "scope_id": scope_id,
            "enable": enable,
            "action_word": _write_word(store.n_leg_control()["mode"]),
            **_audit_dict(audit),
        },
    )
    return n_leg_mode_contract(store)


def n_leg_enforce_auto_scope_versions(
    store: object, *, audit: object = None
) -> dict[str, object]:
    """AUTO runtime admission: fail closed and drop to MANUAL on scope drift."""
    control = store.n_leg_control()
    if control["mode"] != "AUTO":
        return {"ok": True, "mode": "MANUAL", "downgraded": False}
    drift = []
    for item in control["enabled_execution_scope_version"]:
        scope = store.n_leg_scope(str(item["scope_id"]))
        if scope is None or int(scope["scope_version"]) != int(item["scope_version"]):
            drift.append(str(item["scope_id"]))
    if not drift:
        return {"ok": True, "mode": "AUTO", "downgraded": False}
    _downgrade(store, "SCOPE_VERSION_DRIFT", audit)
    store.record_control_event(
        action="n_leg_auto_downgrade",
        target="n_leg_controls",
        outcome="failed",
        payload={
            "reason": "SCOPE_VERSION_DRIFT",
            "scope_ids": drift,
            "mode": "MANUAL",
            "action_word": "manual_confirm",
            **_audit_dict(audit),
        },
    )
    return {"ok": False, "mode": "MANUAL", "downgraded": True, "scope_ids": drift}


def _policy_direction(before: Mapping[str, object], after: Mapping[str, object]) -> str:
    """Loosen when any threshold admits riskier economics."""
    if (
        _money(after["min_profit_usd"], "min_profit_usd")
        < _money(before["min_profit_usd"], "min_profit_usd")
        or _money(after["min_net_margin"], "min_net_margin")
        < _money(before["min_net_margin"], "min_net_margin")
        or _money(after["min_annualized_return"], "min_annualized_return")
        < _money(before["min_annualized_return"], "min_annualized_return")
        or after["max_capital_release_days"] > before["max_capital_release_days"]
    ):
        return "loosen"
    if before == after:
        return "same"
    return "tighten"


def _safety_direction(before: Mapping[str, object], after: Mapping[str, object]) -> str:
    """Loosen when a cap grows or the rearm gap shrinks."""
    if (
        after["episode_rearm_gap_seconds"] < before["episode_rearm_gap_seconds"]
        or after["max_total_unsettled_capital_units"]
        > before["max_total_unsettled_capital_units"]
        or after["max_partial_fill_loss_units"]
        > before["max_partial_fill_loss_units"]
        or after["max_auto_repair_loss_units"]
        > before["max_auto_repair_loss_units"]
    ):
        return "loosen"
    if before == after:
        return "same"
    return "tighten"


def _downgrade(
    store: object,
    reason: str,
    audit: object,
    *,
    qualification_policy_version: int | None = None,
    safety_config_version: int | None = None,
) -> None:
    control = store.n_leg_control()
    store.n_leg_mode_control_write(
        mode="MANUAL",
        contract_generation=int(control["contract_generation"]),
        qualification_policy_version=(
            int(control["qualification_policy_version"])
            if qualification_policy_version is None
            else qualification_policy_version
        ),
        safety_config_version=(
            int(control["safety_config_version"])
            if safety_config_version is None
            else safety_config_version
        ),
        enabled_execution_scope_version=list(
            control["enabled_execution_scope_version"]
        ),
    )
    store.record_control_event(
        action="n_leg_auto_downgrade",
        target="n_leg_controls",
        outcome="failed",
        payload={
            "reason": reason,
            "mode": "MANUAL",
            "action_word": "manual_confirm",
            **_audit_dict(audit),
        },
    )


def n_leg_update_qualification_policy(
    store: object,
    *,
    policy: object,
    base_version: int,
    audit: object = None,
) -> dict[str, object]:
    validated = _validated_policy(policy)
    current = _current_policy(store)
    if current["version"] != base_version:
        raise NLegVersionConflict("qualification policy version mismatch")
    direction = _policy_direction(current["policy"], validated)
    next_version = current["version"] + 1
    store.n_leg_qualification_policy_write(next_version, validated)
    control = store.n_leg_control()
    if direction == "loosen" and control["mode"] == "AUTO":
        _downgrade(
            store,
            "QUALIFICATION_POLICY_LOOSENED",
            audit,
            qualification_policy_version=next_version,
        )
    else:
        store.n_leg_mode_control_write(
            qualification_policy_version=next_version,
            contract_generation=int(control["contract_generation"]),
            safety_config_version=int(control["safety_config_version"]),
            enabled_execution_scope_version=list(
                control["enabled_execution_scope_version"]
            ),
        )
    store.record_control_event(
        action="n_leg_update_qualification_policy",
        target="n_leg_controls",
        outcome="succeeded",
        payload={
            "qualification_policy_version": next_version,
            "direction": direction,
            "action_word": _write_word(store.n_leg_control()["mode"]),
            **_audit_dict(audit),
        },
    )
    return n_leg_mode_contract(store)


def n_leg_update_safety_config(
    store: object,
    *,
    config: object,
    base_version: int,
    audit: object = None,
) -> dict[str, object]:
    validated = _validated_safety_config(config)
    current = _current_safety_config(store)
    if current["version"] != base_version:
        raise NLegVersionConflict("safety config version mismatch")
    direction = _safety_direction(current["config"], validated)
    next_version = current["version"] + 1
    store.n_leg_safety_config_write(next_version, validated)
    control = store.n_leg_control()
    if direction == "loosen" and control["mode"] == "AUTO":
        _downgrade(
            store,
            "SAFETY_CONFIG_LOOSENED",
            audit,
            safety_config_version=next_version,
        )
    else:
        store.n_leg_mode_control_write(
            safety_config_version=next_version,
            contract_generation=int(control["contract_generation"]),
            qualification_policy_version=int(control["qualification_policy_version"]),
            enabled_execution_scope_version=list(
                control["enabled_execution_scope_version"]
            ),
        )
    store.record_control_event(
        action="n_leg_update_safety_config",
        target="n_leg_controls",
        outcome="succeeded",
        payload={
            "safety_config_version": next_version,
            "direction": direction,
            "action_word": _write_word(store.n_leg_control()["mode"]),
            **_audit_dict(audit),
        },
    )
    return n_leg_mode_contract(store)


def n_leg_upsert_scope(
    store: object,
    *,
    scope_id: str,
    capability: str,
    members: object,
    base_scope_version: int | None = None,
    audit: object = None,
) -> dict[str, object]:
    if not isinstance(scope_id, str) or not scope_id:
        raise ValueError("scope id must be non-empty text")
    if capability not in CAPABILITIES:
        raise ValueError("scope capability is invalid")
    validated_members = _validated_members(members)
    existing = store.n_leg_scope(scope_id)
    if existing is None:
        if capability != "OBSERVE_ONLY":
            raise ValueError("N_LEG_SCOPE_STARTS_OBSERVE_ONLY")
        scope_version = 1
        direction = "added"
    else:
        if int(existing["scope_version"]) != base_scope_version:
            raise NLegVersionConflict("scope version mismatch")
        scope_version = int(existing["scope_version"]) + 1
        if capability != existing["capability"]:
            direction = "loosen" if capability > existing["capability"] else "tighten"
        elif validated_members != existing["members"]:
            direction = "changed"
        else:
            direction = "same"
    store.n_leg_scope_write(
        scope_id, capability=capability, scope_version=scope_version, members=validated_members
    )
    control = store.n_leg_control()
    if direction in {"loosen", "changed", "added"} and control["mode"] == "AUTO":
        reason = {
            "loosen": "SCOPE_CAPABILITY_RAISED",
            "changed": "SCOPE_MEMBERS_CHANGED",
            "added": "SCOPE_ADDED",
        }[direction]
        _downgrade(store, reason, audit)
    store.record_control_event(
        action="n_leg_upsert_scope",
        target=f"n_leg_execution_scopes/{scope_id}",
        outcome="succeeded",
        payload={
            "scope_id": scope_id,
            "capability": capability,
            "scope_version": scope_version,
            "direction": direction,
            "action_word": _write_word(store.n_leg_control()["mode"]),
            **_audit_dict(audit),
        },
    )
    return n_leg_mode_contract(store)


def n_leg_order_readiness(
    store: object | None = None, *, contract: Mapping[str, object] | None = None
) -> dict[str, object]:
    """would-submit envelope: per-scope readiness without touching orders."""
    current = (
        n_leg_mode_contract(store)
        if contract is None
        else dict(contract)
    )
    mode = str(current["mode"])
    gates = dict(current["execution_gates"])
    enabled = {
        str(item["scope_id"]): int(item["scope_version"])
        for item in current["enabled_execution_scope_version"]
    }
    scopes: dict[str, dict[str, object]] = {}
    for scope_id, scope in current["execution_scopes"].items():
        capability = str(scope["capability"])
        if capability == "OBSERVE_ONLY":
            ready, reason, action = False, "SCOPE_OBSERVE_ONLY", None
        elif capability == "MANUAL_CANARY":
            if mode == "MANUAL":
                ready, reason, action = True, "MANUAL_CANARY", "manual_confirm"
            else:
                ready, reason, action = False, "MANUAL_CANARY_REQUIRES_MANUAL", None
        else:
            if mode == "AUTO" and scope_id in enabled and enabled[scope_id] == int(scope["scope_version"]):
                ready, reason, action = True, "AUTO_ELIGIBLE", "auto_submit"
            elif mode == "MANUAL":
                ready, reason, action = True, "MANUAL_CONFIRM_ALLOWED", "manual_confirm"
            else:
                ready, reason, action = False, "SCOPE_NOT_ENABLED", None
        scopes[scope_id] = {
            "scope_id": scope_id,
            "order_ready": ready,
            "reason": reason,
            "action": action,
        }
    gate_reason = (
        "GLOBAL_BREAKER_OPEN"
        if gates["breaker_open"]
        else "EXECUTION_INCIDENT_ACTIVE"
        if gates["incident_active"]
        else "EXECUTION_BATCH_ACTIVE"
        if gates["batch_active"]
        else None
    )
    if gate_reason is not None:
        for entry in scopes.values():
            entry["order_ready"] = False
            entry["reason"] = gate_reason
            entry["action"] = None
    return {
        "order_ready": any(entry["order_ready"] for entry in scopes.values()),
        "gates": gates,
        "scopes": scopes,
    }
