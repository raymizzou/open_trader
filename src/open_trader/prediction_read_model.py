from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Mapping, Sequence

from .predict_cross_venue import PredictCrossVenueMonitor
from .prediction_arbitrage import (
    MAX_CROSS_UNSETTLED_PRINCIPAL,
    MAX_EMERGENCY_LOSS,
    MAX_NORMAL_COST,
    MAX_WALLET_BALANCE,
    MIN_ESTIMATED_PROFIT,
    MIN_NET_EDGE,
    MIN_THRESHOLD_ANNUALIZED_YIELD,
)
from .prediction_arbitrage_store import PredictionArbitrageStore
from .prediction_n_leg_mode import n_leg_order_readiness
from .prediction_n_leg_read_model import project_n_leg_solution
from .prediction_title_translation import cached_prediction_title_zh


PREDICTION_HISTORY_KINDS = {"signals", "executions", "incidents"}


def _prediction_safe_value(value: object, *, key: str = "") -> object:
    """Convert monitor/store values to JSON without exposing credentials."""

    lowered = key.casefold()
    if (
        any(
            part in lowered
            for part in (
                "private",
                "secret",
                "password",
                "credential",
                "signature",
                "mnemonic",
                "seed",
            )
        )
        or lowered in {
            "api_key",
            "apikey",
            "api_token",
            "access_token",
            "refresh_token",
            "auth_token",
            "authorization",
            "bearer",
            "bearer_token",
            "token",
            "csrf_token",
        }
    ):
        return None
    if lowered in {"intent", "raw", "raw_markets", "raw_payload", "signed_order"}:
        return None
    if isinstance(value, datetime):
        return value.astimezone().isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if lowered in {"wallet_address", "wallet"} and isinstance(value, str) and value.startswith("0x"):
        return _prediction_mask_wallet(value)
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for name, item in value.items():
            field = str(name)
            safe = _prediction_safe_value(item, key=field)
            if safe is not None:
                result[field] = safe
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_prediction_safe_value(item, key=key) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _prediction_relation_safe_value(value: object) -> object:
    """Project relation discovery facts without raw rules, tokens, or errors."""

    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for name, item in value.items():
            lowered = str(name).casefold()
            if (
                lowered in {
                    "prompt",
                    "token_id",
                    "yes_token_id",
                    "no_token_id",
                    "raw_error",
                    "raw_rules",
                    "wallet_address",
                    "wallet",
                }
                or lowered.endswith("_token_id")
                or lowered.startswith("raw_")
            ):
                continue
            safe = _prediction_relation_safe_value(item)
            if safe is not None:
                result[str(name)] = safe
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_prediction_relation_safe_value(item) for item in value]
    return _prediction_safe_value(value)


def _prediction_mask_wallet(value: object) -> str:
    wallet = str(value or "").strip()
    if len(wallet) < 10:
        return ""
    return f"{wallet[:6]}…{wallet[-4:]}"


def _prediction_decimal_sort(value: object) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except Exception:
        return Decimal("-Infinity")
    return parsed if parsed.is_finite() else Decimal("-Infinity")


def _prediction_displayable(row: Mapping[str, object]) -> bool:
    reason = str(row.get("eligibility_reason") or "").strip()
    if reason in {"annualized_yield_below_minimum", "annualized_yield_unavailable"}:
        return False
    annualized = row.get("annualized_yield")
    if annualized not in (None, ""):
        try:
            value = Decimal(str(annualized))
        except Exception:
            value = None
        if (
            value is not None
            and value.is_finite()
            and value < MIN_THRESHOLD_ANNUALIZED_YIELD
        ):
            return False
    return True


def _prediction_annualized(item: Mapping[str, object]) -> Decimal:
    value = item.get("annualized_yield")
    if value not in (None, ""):
        try:
            parsed = Decimal(str(value))
        except Exception:
            parsed = None
        if parsed is not None and parsed.is_finite():
            return parsed
    opportunities = item.get("opportunities")
    if isinstance(opportunities, (list, tuple)):
        values = [
            _prediction_annualized(row)
            for row in opportunities
            if isinstance(row, Mapping)
        ]
        if values:
            return max(values)
    return Decimal("-Infinity")


def _prediction_remaining_days(item: Mapping[str, object]) -> Decimal:
    value = item.get("remaining_days")
    if value not in (None, ""):
        try:
            parsed = Decimal(str(value))
        except Exception:
            parsed = None
        if parsed is not None and parsed.is_finite():
            return parsed
    cutoff = item.get("resolution_at") or item.get("canonical_cutoff")
    if isinstance(cutoff, str):
        try:
            end = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
            days = Decimal(
                str(
                    (
                        end.astimezone(UTC)
                        - datetime.now(UTC).astimezone(UTC)
                    ).total_seconds()
                )
            ) / Decimal("86400")
            if days.is_finite():
                return days
        except Exception:
            pass
    opportunities = item.get("opportunities")
    if isinstance(opportunities, (list, tuple)):
        days = [
            _prediction_remaining_days(row)
            for row in opportunities
            if isinstance(row, Mapping)
        ]
        if days:
            return min(days)
    return Decimal("Infinity")


def _prediction_sort_key(
    item: Mapping[str, object],
) -> tuple[bool, Decimal, Decimal, Decimal, Decimal, str]:
    opportunities = item.get("opportunities")
    actionable = bool(item.get("actionable"))
    nested_profits: list[Decimal] = []
    nested_volumes: list[Decimal] = []
    if isinstance(opportunities, (list, tuple)):
        for row in opportunities:
            if not isinstance(row, Mapping):
                continue
            actionable = actionable or row.get("actionable") is True
            nested_profits.append(
                _prediction_decimal_sort(row.get("profit", row.get("minimum_profit")))
            )
            nested_volumes.append(_prediction_decimal_sort(row.get("volume_24h")))
    profit = item.get("profit", item.get("gross_upper_bound"))
    volume = item.get("volume_24h")
    if profit is None and nested_profits:
        profit = max(nested_profits)
    if volume is None and nested_volumes:
        volume = max(nested_volumes)
    return (
        not actionable,
        -_prediction_annualized(item),
        _prediction_remaining_days(item),
        -_prediction_decimal_sort(profit),
        -_prediction_decimal_sort(volume),
        str(item.get("event_id") or ""),
    )


def _prediction_first(row: Mapping[str, object], *keys: str) -> object | None:
    """Return the first present, non-empty value from a projected row."""

    for key in keys:
        value = row.get(key)
        if value is not None and value != "":
            return value
    return None


def _prediction_opportunity_aliases(value: object) -> object:
    """Project monitor/execution field names into the dashboard vocabulary."""

    if not isinstance(value, Mapping):
        return value
    result = dict(value)
    aliases = {
        "title": ("title", "question", "market_title", "event_title"),
        "event_title": ("event_title", "title", "question", "market_title"),
        "yes_price": ("yes_price", "yes_max_price", "yes_best_bid"),
        "no_price": ("no_price", "no_max_price", "no_best_bid"),
        "yes_cost": ("yes_cost", "yes_max_cost"),
        "no_cost": ("no_cost", "no_max_cost"),
        "max_cost": ("max_cost", "total_max_cost", "cost"),
        "profit": ("profit", "minimum_profit", "estimated_profit", "net_profit"),
    }
    for target, keys in aliases.items():
        if result.get(target) in (None, ""):
            source = _prediction_first(result, *keys)
            if source is not None:
                result[target] = source
    return result


def _prediction_event_aliases(value: object) -> object:
    """Keep event rows useful when the monitor returns nested market mappings."""

    if not isinstance(value, Mapping):
        return value
    result = dict(value)
    title = _prediction_first(result, "title", "event_title", "question", "market_title")
    if title is not None:
        result.setdefault("title", title)
        result.setdefault("event_title", title)
    markets = result.get("markets")
    if isinstance(markets, (list, tuple)):
        projected_markets = [
            _prediction_opportunity_aliases(item) for item in markets
        ]
        result["markets"] = projected_markets
        result.setdefault("market_count", len(projected_markets))
        result.setdefault(
            "actionable",
            any(
                isinstance(item, Mapping) and item.get("actionable") is True
                for item in projected_markets
            ),
        )
    opportunities = result.get("opportunities")
    if isinstance(opportunities, (list, tuple)):
        result["opportunities"] = [
            _prediction_opportunity_aliases(item) for item in opportunities
        ]
    return result


def _prediction_attach_cached_title(
    store: PredictionArbitrageStore | None,
    value: object,
    title_cache: dict[str, object] | None = None,
) -> object:
    if store is None or not isinstance(value, Mapping):
        return value
    result = dict(value)
    title = _prediction_first(
        result, "event_title", "title", "question", "market_title"
    )
    if title is not None:
        key = str(title)
        translated = None
        if title_cache is not None:
            translated = title_cache.get(key)
            if key not in title_cache:
                translated = cached_prediction_title_zh(
                    store, key, record_hit=False
                )
                title_cache[key] = translated
        else:
            translated = cached_prediction_title_zh(store, key, record_hit=False)
        if translated is not None:
            result["event_title_zh"] = translated
            result.setdefault("title_zh", translated)
    markets = result.get("markets")
    if isinstance(markets, (list, tuple)):
        result["markets"] = [
            _prediction_attach_cached_title(store, item, title_cache)
            for item in markets
        ]
    return result


def _prediction_attach_n_leg_shadow(
    store: PredictionArbitrageStore | None, value: object
) -> object:
    """Project the durable Episode summary without letting it change card economics."""

    if store is None or not isinstance(value, Mapping):
        return value
    signal_id = value.get("signal_episode_id")
    read_signal = getattr(store, "signal", None)
    if not isinstance(signal_id, str) or not signal_id or not callable(read_signal):
        return value
    try:
        signal = read_signal(signal_id)
    except Exception:
        return value
    shadow = signal.get("n_leg_shadow") if isinstance(signal, Mapping) else None
    safe_shadow = _prediction_safe_value(shadow)
    if not isinstance(safe_shadow, Mapping):
        return value
    result = dict(value)
    result["n_leg_shadow"] = dict(safe_shadow)
    return result


def _prediction_n_leg_shadow_summary(rows: list[Mapping[str, object]]) -> dict[str, object]:
    monitoring = completed = differences = failures = 0
    last_completed_at: str | None = None
    seen: set[str] = set()
    for row in rows:
        signal_id = row.get("signal_episode_id")
        shadow = row.get("n_leg_shadow")
        if not isinstance(signal_id, str) or signal_id in seen or not isinstance(shadow, Mapping):
            continue
        seen.add(signal_id)
        monitoring += 1
        completed += int(shadow.get("run_count") or 0)
        differences += int(shadow.get("difference_count") or 0)
        failures += int(shadow.get("failure_count") or 0)
        completed_at = shadow.get("last_run_at")
        if isinstance(completed_at, str) and (last_completed_at is None or completed_at > last_completed_at):
            last_completed_at = completed_at
    return {
        "monitoring": monitoring,
        "legacy_qualified": monitoring,
        "completed": completed,
        "differences": differences,
        "failures": failures,
        "last_completed_at": last_completed_at,
    }


def _prediction_evidence_value(row: Mapping[str, object], *keys: str) -> object | None:
    evidence = row.get("evidence")
    if isinstance(evidence, Mapping):
        return _prediction_first(evidence, *keys)
    if isinstance(evidence, (list, tuple)):
        for item in reversed(evidence):
            if isinstance(item, Mapping):
                value = _prediction_first(item, *keys)
                if value is not None:
                    return value
    return None


def _prediction_remediation_aliases(row: Mapping[str, object]) -> tuple[object | None, object | None]:
    """Derive a human-readable action and loss from persisted safety options."""

    options = row.get("remediation_options")
    if not isinstance(options, Mapping):
        evidence = row.get("evidence")
        if isinstance(evidence, Mapping):
            options = evidence.get("remediation_options")
        elif isinstance(evidence, (list, tuple)):
            for item in reversed(evidence):
                if isinstance(item, Mapping) and isinstance(item.get("remediation_options"), Mapping):
                    options = item["remediation_options"]
                    break
    if not isinstance(options, Mapping):
        return None, None
    candidates: list[tuple[Decimal, str, Mapping[str, object]]] = []
    for name, option in options.items():
        if not isinstance(option, Mapping):
            continue
        loss = _prediction_decimal_sort(_prediction_first(option, "loss", "estimated_loss", "expected_loss"))
        if not loss.is_finite():
            continue
        candidates.append((loss, str(name), option))
    if not candidates:
        return None, None
    loss, _, chosen = min(candidates, key=lambda item: item[0])
    side = str(chosen.get("side") or "").upper()
    leg = str(chosen.get("leg") or "").upper()
    quantity = _prediction_first(chosen, "quantity", "shares")
    if side == "SELL":
        action = "卖回"
    elif side == "BUY":
        action = "补买"
    else:
        action = "处置"
    parts = [action]
    if quantity is not None:
        parts.append(str(quantity))
    if leg:
        parts.append(leg)
    return " ".join(parts), format(-abs(loss), "f")


def _prediction_duration(start: object, end: object) -> str | None:
    if not isinstance(start, str) or not isinstance(end, str):
        return None
    try:
        started = datetime.fromisoformat(start.replace("Z", "+00:00"))
        ended = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        return None
    seconds = max(0, int((ended - started).total_seconds()))
    minutes, remainder = divmod(seconds, 60)
    if minutes:
        return f"{minutes}m {remainder}s"
    return f"{remainder}s"


def _prediction_history_epoch(value: object) -> float:
    if not isinstance(value, str):
        return float("-inf")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return float("-inf")


def _prediction_history_aliases(kind: str, value: object) -> object:
    """Normalize durable store rows without changing their audit fields."""

    if not isinstance(value, Mapping):
        return value
    result = dict(value)
    if kind == "signals":
        occurred_at = _prediction_first(
            result, "occurred_at", "started_at", "detected_at", "created_at"
        )
        event_title = _prediction_first(
            result, "event_title", "question", "title", "market_title"
        )
        duration = _prediction_first(result, "duration")
        if duration is None:
            duration = (
                "进行中"
                if result.get("started_at") and not result.get("ended_at")
                else _prediction_duration(result.get("started_at"), result.get("ended_at"))
            )
        aliases = {
            "occurred_at": occurred_at,
            "event_title": event_title,
            "duration": duration,
            "observed_duration_ms": _prediction_first(
                result, "observed_duration_ms"
            ),
            "first_positive_at": _prediction_first(result, "first_positive_at"),
            "last_positive_at": _prediction_first(result, "last_positive_at"),
            "initial_profit": _prediction_first(result, "initial_profit"),
            "peak_profit": _prediction_first(result, "peak_profit"),
            "final_profit": _prediction_first(result, "final_profit"),
            "ended_reason": _prediction_first(result, "ended_reason"),
            "signal_id": _prediction_first(result, "signal_id", "id"),
            "opportunity_id": _prediction_first(result, "opportunity_id"),
            "market_id": _prediction_first(result, "market_id"),
            "notification_state": _prediction_first(
                result, "notification_state", "notification_status"
            ),
            "book_timestamp_a": _prediction_first(result, "book_timestamp_a"),
            "book_timestamp_b": _prediction_first(result, "book_timestamp_b"),
            "book_received_at_a": _prediction_first(result, "book_received_at_a"),
            "book_received_at_b": _prediction_first(result, "book_received_at_b"),
            "peak_edge": _prediction_first(result, "peak_edge", "peak_net_edge", "net_edge"),
            "quantity": _prediction_first(result, "quantity", "peak_quantity"),
            "profit": _prediction_first(
                result,
                "profit",
                "peak_profit",
                "peak_estimated_profit",
                "estimated_profit",
                "minimum_profit",
            ),
        }
    elif kind == "executions":
        aliases = {
            "status": _prediction_first(result, "status", "state"),
            "completed_at": _prediction_first(
                result, "completed_at", "updated_at", "created_at"
            ),
            "event_title": _prediction_first(
                result, "event_title", "question", "title", "market_title"
            ),
            "quantity": _prediction_first(result, "quantity", "execution_quantity"),
            "actual_cost": _prediction_first(
                result, "actual_cost", "total_actual_cost"
            ),
            "merge_value": _prediction_first(
                result,
                "merge_value",
                "merged_value",
                "payout",
                "merge_amount",
            ),
            "realized_profit": _prediction_first(
                result,
                "realized_profit",
                "net_profit",
                "actual_profit",
            ),
        }
    else:
        derived_remediation, derived_loss = _prediction_remediation_aliases(result)
        aliases = {
            "status": _prediction_first(result, "status", "state"),
            "happened_at": _prediction_first(
                result, "happened_at", "created_at", "updated_at"
            ),
            "event_title": _prediction_first(
                result, "event_title", "question", "title", "market_title"
            ),
            "reason": _prediction_first(result, "reason")
            or _prediction_evidence_value(result, "reason", "error", "error_code"),
            "remediation": _prediction_first(result, "remediation")
            or _prediction_evidence_value(result, "remediation", "action")
            or derived_remediation,
            "loss": _prediction_first(result, "loss", "actual_loss")
            or _prediction_evidence_value(result, "loss", "actual_loss", "loss_amount")
            or derived_loss,
        }
    for key, item in aliases.items():
        if item is not None and result.get(key) in (None, ""):
            result[key] = item
    return result


def _prediction_unavailable_state(csrf_token: str, reason: str = "configuration_unavailable") -> dict[str, object]:
    return {
        "status": "unavailable",
        "health": {"status": "unavailable", "degraded_reasons": [reason]},
        "failure_reason": reason,
        "readiness": {"status": "unavailable", "reason": reason},
        "first_live_order": "待首单",
        "wallet": {"address": "", "masked_address": ""},
        "masked_wallet": "",
        "balances": {"p_usd": None, "allowance": None},
        "policy_limits": {
            "max_wallet_balance": format(MAX_WALLET_BALANCE, "f"),
            "max_normal_cost": format(MAX_NORMAL_COST, "f"),
            "min_estimated_profit": format(MIN_ESTIMATED_PROFIT, "f"),
            "min_net_edge": format(MIN_NET_EDGE, "f"),
            "max_emergency_loss": format(MAX_EMERGENCY_LOSS, "f"),
            "max_cross_unsettled_principal": format(MAX_CROSS_UNSETTLED_PRINCIPAL, "f"),
        },
        "heartbeat": None,
        "heartbeat_at": None,
        "stale": True,
        "events": [],
        "opportunities": [],
        "venues": [
            {
                "venue": "polymarket",
                "rest": "unavailable",
                "ws": "unavailable",
                "wallet": "",
                "balance": {"asset": "pUSD", "value": None},
                "mode": "只读",
                "last_success": None,
                "reason": reason,
            },
            {
                "venue": "predict.fun",
                "rest": "unavailable",
                "ws": "unavailable",
                "wallet": "",
                "balance": {"asset": "USDT", "value": None},
                "mode": "只读",
                "last_success": None,
                "reason": "cross_venue_unavailable",
            },
        ],
        "cross_venue": {
            "status": "unavailable",
            "mode": "observe_only",
            "reason": "cross_venue_unavailable",
            "funnel": {
                "matched_pairs": 0,
                "monitored_pairs": 0,
                "codex_approved_pairs": 0,
                "arbitrage_space_pairs": 0,
                "clear_signal_pairs": 0,
            },
            "events": [],
            "opportunities": [],
            "unsettled": {"current": None, "limit": format(MAX_CROSS_UNSETTLED_PRINCIPAL, "f")},
            "breaker": {"open": True, "scope": "cross_venue"},
        },
        "relation_discovery": {
            "status": "unavailable",
            "scan_logs": [],
            "codex_usage_24h": {},
            "annualized_distribution": {},
        },
        "event_count": 0,
        "market_count": 0,
        "token_count": 0,
        "signals_24h": 0,
        "current_execution": None,
        "breaker": {"open": True, "status": "locked", "incident": None},
        "csrf_token": csrf_token,
    }

_CROSS_VENUE_FUNNEL_FIELDS = (
    "matched_pairs",
    "monitored_pairs",
    "codex_approved_pairs",
    "arbitrage_space_pairs",
    "clear_signal_pairs",
    "manual_eligible_pairs",
    "manual_pending_pairs",
)


def _prediction_monitor_snapshot(monitor: object | None) -> Mapping[str, object]:
    if monitor is None:
        return {}
    try:
        value = monitor.snapshot()
    except Exception:
        return {}
    return value if isinstance(value, Mapping) else {}


def _prediction_cross_snapshot(monitor: object | None) -> dict[str, object]:
    snapshot = _prediction_safe_value(_prediction_monitor_snapshot(monitor))
    result = dict(snapshot) if isinstance(snapshot, Mapping) else {}
    funnel = result.get("funnel")
    result["funnel"] = {
        field: int(funnel.get(field) or 0) if isinstance(funnel, Mapping) else 0
        for field in _CROSS_VENUE_FUNNEL_FIELDS
    }
    result.setdefault("status", "unavailable")
    result.setdefault("mode", "observe_only")
    result["events"] = (
        [row for row in result.get("events", []) if isinstance(row, Mapping)]
        if isinstance(result.get("events"), (list, tuple))
        else []
    )
    result["opportunities"] = (
        [row for row in result.get("opportunities", []) if isinstance(row, Mapping)]
        if isinstance(result.get("opportunities"), (list, tuple))
        else []
    )
    return result


def _prediction_cross_unsettled(store: PredictionArbitrageStore | None) -> tuple[dict[str, object], Decimal | None]:
    current: Decimal | None = None
    method = getattr(store, "cross_unsettled_principal", None)
    if callable(method):
        try:
            value = method()
            parsed = Decimal(str(value))
            if parsed.is_finite() and parsed >= 0:
                current = parsed
        except Exception:
            current = None
    return {
        "current": format(current, "f") if current is not None else None,
        "limit": format(MAX_CROSS_UNSETTLED_PRINCIPAL, "f"),
    }, current


def _prediction_cross_opportunity_runtime(
    value: object,
    *,
    unsettled: Mapping[str, object],
    current: Decimal | None,
    cross_breaker_open: bool,
) -> object:
    if not isinstance(value, Mapping) or value.get("market_type") != "cross_venue_yes_no":
        return value
    result = dict(value)
    result["cross_breaker"] = {"open": cross_breaker_open, "scope": "cross_venue"}
    if current is None:
        return result
    try:
        total = Decimal(str(result.get("total_max_cost")))
    except Exception:
        return result
    if not total.is_finite() or total < 0:
        return result
    result["unsettled"] = {
        **unsettled,
        "after": format(current + total, "f"),
    }
    return result


_NLEG_STRATEGY_BY_MARKET = {
    "standard_binary": "yes_no",
    "cross_venue_yes_no": "yes_no",
    "threshold_hedge": "llm_hedge",
}
_NLEG_RELATION_BY_STRATEGY = {
    "yes_no": "NATIVE_COMPLEMENT",
    "llm_hedge": "IMPLIES",
}
_NLEG_SOURCE_BY_STRATEGY = {
    "yes_no": "VENUE_METADATA",
    "llm_hedge": "LLM",
}
_NLEG_SCOPE_LABELS = {
    ("same_venue", "same_event"): "同所 · 同事件",
    ("same_venue", "cross_event"): "同所 · 跨事件",
    ("cross_venue", "same_event"): "跨所 · 同事件",
    ("cross_venue", "cross_event"): "跨所 · 跨事件",
}
_NLEG_UNITS_PER_DOLLAR = Decimal("1000000")


def _prediction_strategy_type(row: Mapping[str, object]) -> str | None:
    strategy = row.get("strategy_type")
    if strategy in _NLEG_RELATION_BY_STRATEGY:
        return str(strategy)
    return _NLEG_STRATEGY_BY_MARKET.get(str(row.get("market_type") or ""))


def _prediction_n_leg_contract(execution: object | None) -> dict[str, object] | None:
    method = getattr(execution, "n_leg_mode_contract", None)
    if not callable(method):
        return None
    try:
        value = method()
    except Exception:
        return None
    safe = _prediction_safe_value(value)
    return dict(safe) if isinstance(safe, Mapping) else None


def _prediction_capital_usage(
    *,
    cross_unsettled: Mapping[str, object],
    current_execution: Mapping[str, object] | None,
    n_leg: Mapping[str, object] | None,
) -> dict[str, object]:
    safety = n_leg.get("safety_config") if isinstance(n_leg, Mapping) else None
    units = safety.get("max_total_unsettled_capital_units") if isinstance(safety, Mapping) else 0
    try:
        units = int(units or 0)
    except (TypeError, ValueError):
        units = 0
    max_usd = Decimal(units) / _NLEG_UNITS_PER_DOLLAR if units > 0 else Decimal("0")
    set_flag = units > 0
    current_raw = cross_unsettled.get("current")
    current = (
        _prediction_decimal(current_raw)
        if current_raw not in (None, "")
        else None
    )
    reserved = Decimal("0")
    if isinstance(current_execution, Mapping):
        reserved_value = _prediction_decimal_sort(
            current_execution.get("total_max_cost", current_execution.get("max_cost"))
        )
        if reserved_value.is_finite() and reserved_value > 0:
            reserved = reserved_value
    remaining = (max_usd - current - reserved) if set_flag and current is not None else None
    return {
        "max_total_unsettled_capital": format(max_usd, "f"),
        "max_total_unsettled_capital_set": set_flag,
        "current_conservative": format(current, "f") if current is not None else None,
        "active_batch_reserved": format(reserved, "f"),
        "remaining": format(remaining, "f") if remaining is not None else None,
    }


def _prediction_n_leg_solution_projection(
    solutions: Sequence[Mapping[str, object]],
    *,
    n_leg: Mapping[str, object] | None,
    total_unsettled_capital_units: object,
) -> list[dict[str, object]]:
    """Project serialized #84 solutions into dashboard n_leg_solutions."""
    if not solutions:
        return []
    n_leg = dict(n_leg) if isinstance(n_leg, Mapping) else {}
    try:
        readiness = n_leg_order_readiness(contract=n_leg)
    except Exception:
        readiness = {"scopes": {}}
    scope_rows = readiness.get("scopes")
    if not isinstance(scope_rows, Mapping):
        scope_rows = {}
    contract_scopes = n_leg.get("execution_scopes")
    if not isinstance(contract_scopes, Mapping):
        contract_scopes = {}
    try:
        max_units = int(
            (n_leg.get("safety_config") or {}).get("max_total_unsettled_capital_units") or 0
        )
    except (TypeError, ValueError):
        max_units = 0
    try:
        current = Decimal(str(total_unsettled_capital_units))
    except Exception:
        current = Decimal("0")
    current_units = int((current * _NLEG_UNITS_PER_DOLLAR).to_integral_value()) if current.is_finite() else 0
    projected: list[dict[str, object]] = []
    for entry in solutions:
        if not isinstance(entry, Mapping):
            continue
        scope_id = str(entry.get("scope_id") or "")
        scope = dict(contract_scopes.get(scope_id)) if scope_id in contract_scopes else {}
        scope_ready = scope_rows.get(scope_id)
        if isinstance(scope_ready, Mapping):
            scope.update(
                {
                    "order_ready": scope_ready.get("order_ready"),
                    "reason": scope_ready.get("reason"),
                    "action": scope_ready.get("action"),
                }
            )
        try:
            item = project_n_leg_solution(
                market=entry.get("market"),
                execution=entry.get("execution"),
                scope=scope,
                component_id=str(entry.get("component_id") or ""),
                max_total_unsettled_capital_units=max_units,
                total_unsettled_capital_units=current_units,
                legs=entry.get("legs"),
            )
        except Exception:
            item = None
        if item is not None:
            projected.append(item)
    return projected


def _prediction_nleg_labels(row: Mapping[str, object]) -> dict[str, object]:
    """Forward-project legacy current opportunities onto N_LEG taxonomy labels."""

    strategy = _prediction_strategy_type(row)
    legs = row.get("legs")
    buy_legs = row.get("buy_legs")
    leg_rows = legs if isinstance(legs, (list, tuple)) else buy_legs
    leg_count = len(leg_rows) if isinstance(leg_rows, (list, tuple)) and len(leg_rows) >= 2 else None
    if leg_count is None and strategy is not None:
        leg_count = 2
    exchanges = {
        str(item.get("exchange") or "").casefold()
        for item in leg_rows
        if isinstance(item, Mapping)
    } if isinstance(leg_rows, (list, tuple)) else set()
    cross_venue = (
        str(row.get("market_type") or "") == "cross_venue_yes_no"
        or ({"predict.fun", "polymarket"} <= exchanges)
    )
    event_id_a = row.get("event_id_a")
    event_id_b = row.get("event_id_b")
    cross_event = (
        event_id_a not in (None, "")
        and event_id_b not in (None, "")
        and str(event_id_a) != str(event_id_b)
    )
    venue_scope = "cross_venue" if cross_venue else "same_venue"
    event_scope = "cross_event" if cross_event else "same_event"
    labels: dict[str, object] = {
        "strategy_type": strategy,
        "engine_owner": strategy,
        "relation_type": (
            _NLEG_RELATION_BY_STRATEGY.get(str(strategy))
            if strategy is not None
            else None
        ),
        "discovery_source": (
            _NLEG_SOURCE_BY_STRATEGY.get(str(strategy))
            if strategy is not None
            else None
        ),
        "leg_count": leg_count,
        "scope": {"event": event_scope, "venue": venue_scope},
        "scope_label": _NLEG_SCOPE_LABELS[(venue_scope, event_scope)],
        "qualification_policy_version": str(
            row.get("qualification_policy_version") or "v1"
        ),
    }
    for field in ("contract_generation", "contract_version"):
        if row.get(field) not in (None, ""):
            labels[field] = row.get(field)
    return labels


def _prediction_approved(row: Mapping[str, object]) -> tuple[bool | None, bool]:
    codex = row.get("codex_approval")
    if isinstance(codex, Mapping):
        decision = str(codex.get("decision") or "").upper()
        return (decision == "APPROVE", decision != "")
    llm_status = row.get("llm_status")
    if llm_status not in (None, ""):
        return (str(llm_status) == "approved", True)
    approval = row.get("approved", row.get("approval"))
    if isinstance(approval, bool):
        return (approval, True)
    if isinstance(approval, str) and approval:
        return (str(approval).casefold() in {"true", "approved", "approve"}, True)
    return (None, False)


def _prediction_proof_complete(row: Mapping[str, object]) -> tuple[bool | None, bool]:
    if str(row.get("market_type") or "") == "threshold_hedge":
        return (False, True)
    codex = row.get("codex_approval")
    if isinstance(codex, Mapping):
        decision = str(codex.get("decision") or "").upper()
        return (decision == "APPROVE", decision != "")
    proof = row.get("proof_status", row.get("proof"))
    if isinstance(proof, bool):
        return (proof, True)
    if isinstance(proof, str) and proof:
        return (str(proof).casefold() in {"true", "complete", "verified", "approved"}, True)
    return (None, False)


def _prediction_net_edge(row: Mapping[str, object]) -> Decimal | None:
    profit = _prediction_decimal_sort(row.get("profit", row.get("minimum_profit")))
    payout = _prediction_decimal_sort(row.get("minimum_payout"))
    if not profit.is_finite() or not payout.is_finite() or payout <= 0:
        return None
    return profit / payout


def _prediction_remaining_days_known(row: Mapping[str, object]) -> Decimal | None:
    value = _prediction_decimal_sort(row.get("remaining_days"))
    if value.is_finite():
        return value
    cutoff = row.get("resolution_at") or row.get("canonical_cutoff")
    if not isinstance(cutoff, str):
        return None
    try:
        end = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
    except ValueError:
        return None
    days = Decimal(str((end.astimezone(UTC) - datetime.now(UTC).astimezone(UTC)).total_seconds())) / Decimal("86400")
    return days if days.is_finite() else None


def _prediction_balance_shortfall(
    row: Mapping[str, object], balances: Mapping[str, Decimal | None]
) -> bool:
    max_cost = _prediction_decimal_sort(row.get("max_cost", row.get("total_max_cost")))
    if not max_cost.is_finite() or max_cost < 0:
        return True
    legs = row.get("legs")
    if isinstance(legs, (list, tuple)) and str(row.get("market_type") or "") == "cross_venue_yes_no":
        required_predict = Decimal("0")
        required_poly = Decimal("0")
        for leg in legs:
            if not isinstance(leg, Mapping):
                continue
            cost = _prediction_decimal_sort(leg.get("max_cost"))
            if not cost.is_finite():
                continue
            if str(leg.get("exchange") or "").casefold() == "predict.fun":
                required_predict += cost
            else:
                required_poly += cost
        if required_predict > 0 or required_poly > 0:
            return (
                balances.get("predict_usdt") is None
                or balances.get("predict_usdt") < required_predict
                or balances.get("p_usd") is None
                or balances.get("p_usd") < required_poly
            )
    if str(row.get("market_type") or "") == "cross_venue_yes_no":
        return (
            balances.get("p_usd") is None
            or balances.get("p_usd") < max_cost
            or balances.get("predict_usdt") is None
            or balances.get("predict_usdt") < max_cost
        )
    return balances.get("p_usd") is None or balances.get("p_usd") < max_cost


def _prediction_relation_review_item(
    row: Mapping[str, object],
) -> dict[str, object] | None:
    """Map one v2 catalog row onto the six-state review vocabulary (no profit/preview)."""

    status = str(row.get("status") or "")
    activation = str(row.get("activation") or "")
    model = row.get("model")
    terminal_states = model.get("terminal_states") if isinstance(model, Mapping) else None
    compiled = bool(terminal_states)
    if status == "PENDING":
        key = "PENDING_APPROVAL"
    elif status == "APPROVED":
        if activation == "ACTIVE":
            key = "ACTIVATED"
        elif activation in {"ACTIVATION_BLOCKED_INCONSISTENT", "UNSUPPORTED_SIZE"}:
            key = "ACTIVATION_BLOCKED"
        elif activation == "SUPERSEDED":
            key = "SOURCE_CHANGED_REAPPROVAL"
        elif compiled:
            key = "COMPILED_PENDING_ACTIVATION"
        else:
            key = "APPROVED_MODEL_INCOMPLETE"
    else:
        return None
    item: dict[str, object] = {
        "version_id": row.get("version_id"),
        "title": row.get("statement") or row.get("title"),
        "relation_type": row.get("relation_type"),
        "discovery_source": row.get("discovery_source"),
        "status": key,
        "reason": "",
        "conflict_candidates": 0,
    }
    if key == "ACTIVATION_BLOCKED" and activation == "ACTIVATION_BLOCKED_INCONSISTENT":
        item["reason"] = "ACTIVATION_BLOCKED_INCONSISTENT"
        try:
            item["conflict_candidates"] = int(row.get("conflict_candidates") or 0)
        except (TypeError, ValueError):
            item["conflict_candidates"] = 0
    elif activation in {"ACTIVATION_BLOCKED_INCONSISTENT", "UNSUPPORTED_SIZE"}:
        item["reason"] = activation
    elif key == "SOURCE_CHANGED_REAPPROVAL":
        item["reason"] = "rules fingerprint 变化 · 保留批准不等于可交易"
    return item


def _prediction_relation_review(relation_catalog: object | None) -> dict[str, object]:
    review: dict[str, object] = {"pending_count": 0, "items": []}
    if relation_catalog is None:
        return review
    pending = getattr(relation_catalog, "pending_count", None)
    if callable(pending):
        try:
            review["pending_count"] = int(pending())
        except Exception:
            review["pending_count"] = 0
    rows = getattr(relation_catalog, "review_rows", None)
    if not callable(rows):
        return review
    try:
        raw_rows = rows()
    except Exception:
        return review
    items: list[dict[str, object]] = []
    for row in raw_rows:
        if not isinstance(row, Mapping):
            continue
        item = _prediction_relation_review_item(row)
        if item is not None:
            items.append(item)
    review["items"] = items
    return review


def _prediction_qualification(
    row: Mapping[str, object],
    *,
    balances: Mapping[str, Decimal | None],
    breaker_open: bool,
    cross_breaker_open: bool,
) -> dict[str, object]:
    approved, approved_known = _prediction_approved(row)
    proof, proof_known = _prediction_proof_complete(row)
    profit = _prediction_decimal_sort(row.get("profit", row.get("minimum_profit")))
    payout = _prediction_decimal_sort(row.get("minimum_payout"))
    annualized = _prediction_decimal_sort(row.get("annualized_yield"))
    remaining = _prediction_remaining_days_known(row)
    edge = _prediction_net_edge(row)
    profit_known = profit.is_finite()
    payout_known = payout.is_finite() and payout > 0
    annualized_known = annualized.is_finite()
    remaining_known = remaining is not None and remaining.is_finite()
    remaining_present = row.get("remaining_days") not in (None, "")
    tenor_value = format(remaining, "f") if remaining_known and remaining_present else None
    checks: list[dict[str, object]] = [
        {
            "key": "approved",
            "label": "已批准",
            "passed": approved,
            "value": (
                str((row.get("codex_approval") or {}).get("decision") or row.get("llm_status"))
                if isinstance(row.get("codex_approval"), Mapping) or row.get("llm_status")
                else row.get("approved", row.get("approval"))
            ),
            "threshold": "APPROVE",
        },
        {
            "key": "proof",
            "label": "证明完整",
            "passed": proof,
            "value": proof,
            "threshold": True,
        },
        {
            "key": "min_profit",
            "label": "最低利润 $1",
            "passed": (
                profit >= MIN_ESTIMATED_PROFIT
                if profit_known
                else None
            ),
            "value": format(profit, "f") if profit_known else None,
            "threshold": format(MIN_ESTIMATED_PROFIT, "f"),
        },
        {
            "key": "net_edge",
            "label": "1% 净边际",
            "passed": (
                edge >= MIN_NET_EDGE
                if edge is not None
                else None
            ),
            "value": format(edge, "f") if edge is not None else None,
            "threshold": format(MIN_NET_EDGE, "f"),
        },
        {
            "key": "annualized",
            "label": "15% 年化",
            "passed": (
                annualized >= MIN_THRESHOLD_ANNUALIZED_YIELD
                if annualized_known
                else None
            ),
            "value": format(annualized, "f") if annualized_known else None,
            "threshold": format(MIN_THRESHOLD_ANNUALIZED_YIELD, "f"),
        },
        {
            "key": "tenor",
            "label": "30 天资本释放",
            "passed": (
                Decimal("0") < remaining <= Decimal("30")
                if remaining_known
                else None
            ),
            "value": tenor_value,
            "threshold": "30",
        },
        {
            "key": "not_expired",
            "label": "未过期",
            "passed": (
                remaining > Decimal("0")
                if remaining_known
                else None
            ),
            "value": tenor_value,
            "threshold": ">0",
        },
    ]
    gate_checks = [
        item for item in checks if item["key"] not in {"approved", "proof"}
    ]
    gate_passed = all(item["passed"] is True for item in gate_checks)
    known = approved_known and proof_known and profit_known and payout_known and annualized_known and remaining_known
    if not known:
        status = "UNKNOWN"
    elif approved is True and proof is True and gate_passed:
        status = "QUALIFIED_VERIFIED"
    elif approved is True and gate_passed:
        status = "QUALIFIED_FEASIBLE"
    else:
        status = "NOT_QUALIFIED"
    order_ready = status in {"QUALIFIED_VERIFIED", "QUALIFIED_FEASIBLE"}
    reason = ""
    if not order_ready:
        reason = "资格数据未知" if status == "UNKNOWN" else "未通过资格门槛"
    elif (remaining_known and remaining <= Decimal("0")) or not gate_passed:
        order_ready = False
        reason = "已过期" if remaining_known and remaining <= Decimal("0") else "未通过资格门槛"
    elif breaker_open or cross_breaker_open:
        order_ready = False
        reason = "熔断中"
    elif _prediction_balance_shortfall(row, balances):
        order_ready = False
        reason = "余额不足"
    return {
        "status": status,
        "checks": checks,
        "order_ready": order_ready,
        "order_ready_reason": reason,
    }


def _prediction_predict_account_snapshot(execution: object | None) -> Mapping[str, object]:
    fresh = getattr(execution, "_fresh_predict_account_snapshot", None)
    method = fresh if callable(fresh) else getattr(
        getattr(execution, "_predict_trading", None), "account_snapshot", None
    )
    if not callable(method):
        return {}
    try:
        value = method()
    except Exception:
        return {}
    safe = _prediction_safe_value(value)
    if not isinstance(safe, Mapping):
        return {}
    normalized = _prediction_normalize_predict_account_snapshot(safe)
    return normalized if isinstance(normalized, Mapping) else {}


def _prediction_normalize_predict_account_snapshot(
    snapshot: Mapping[str, object],
) -> Mapping[str, object] | None:
    normalized = dict(snapshot)
    required = ("wallet_address", "available_usdt", "open_orders", "positions", "checked_at")
    if not all(name in normalized for name in required):
        return None
    has_current = all(
        name in normalized
        for name in ("allowance", "scope_ready", "gas_ready", "allowance_breaker")
    )
    if has_current:
        if (
            not isinstance(normalized.get("scope_ready"), bool)
            or not isinstance(normalized.get("gas_ready"), bool)
            or not isinstance(normalized.get("allowance_breaker"), bool)
            or normalized.get("allowance") in (None, "")
        ):
            return None
        return normalized
    if "allowance_ready" not in normalized:
        return None
    ready = normalized.get("allowance_ready") is True
    normalized["allowance"] = "0" if ready else ""
    normalized["scope_ready"] = ready
    normalized["gas_ready"] = ready
    normalized["allowance_breaker"] = not ready
    return normalized


def _prediction_predict_account_ready(snapshot: Mapping[str, object]) -> bool:
    return (
        snapshot.get("scope_ready") is True
        and snapshot.get("gas_ready") is True
        and _prediction_decimal(snapshot.get("minimum_top_up_bnb")) <= 0
        and snapshot.get("allowance_breaker") is False
        and snapshot.get("allowance") not in (None, "")
        and snapshot.get("available_usdt") not in (None, "")
    )


def _prediction_decimal(value: object) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except Exception:
        return Decimal("0")
    return parsed if parsed.is_finite() else Decimal("0")


def _prediction_venues_payload(
    *,
    snapshot: Mapping[str, object],
    readiness: Mapping[str, object],
    health: Mapping[str, object],
    masked_wallet: str,
    breaker_open: bool,
    execution: object | None,
    active_execution: Mapping[str, object] | None,
    cross_venue_monitor: object | None,
    cross_venue: Mapping[str, object],
) -> list[dict[str, object]]:
    status = str(snapshot.get("status") or health.get("status") or "unavailable")
    relation_discovery = snapshot.get("relation_discovery")
    websocket = (
        relation_discovery.get("websocket")
        if isinstance(relation_discovery, Mapping)
        else None
    )
    websocket_status = str(websocket.get("status") or "") if isinstance(websocket, Mapping) else ""
    rest = "ready" if status == "healthy" else status
    ws = "ready" if websocket_status == "connected" else "stale" if websocket_status else rest
    poly_reason = readiness.get("reason")
    if poly_reason in (None, ""):
        degraded = health.get("degraded_reasons")
        if isinstance(degraded, (list, tuple)) and degraded:
            poly_reason = degraded[0]
    poly_mode = "可以交易" if not breaker_open and rest == "ready" else "只读"

    source = getattr(cross_venue_monitor, "_predict", None)
    source_snapshot = _prediction_safe_value(_prediction_monitor_snapshot(source))
    source_snapshot = source_snapshot if isinstance(source_snapshot, Mapping) else {}
    predict_rest = str(source_snapshot.get("rest") or cross_venue.get("status") or "unavailable")
    predict_ws = str(source_snapshot.get("ws") or predict_rest)
    predict_account = (
        _prediction_predict_account_snapshot(execution)
        if predict_rest == "ready" and predict_ws == "ready"
        else {}
    )
    predict_wallet = str(source_snapshot.get("wallet") or "")
    if not predict_wallet:
        predict_wallet = str(predict_account.get("wallet_address") or "")
    if predict_wallet.startswith("0x") and "…" not in predict_wallet:
        predict_wallet = _prediction_mask_wallet(predict_wallet)
    predict_reason = source_snapshot.get("reason") or cross_venue.get("reason")
    cross_breaker_open = bool(getattr(execution, "_cross_breaker_open", True))
    predict_account_ready = _prediction_predict_account_ready(predict_account)
    residual_allowance = _prediction_decimal(predict_account.get("allowance"))
    gas_top_up = _prediction_decimal(predict_account.get("minimum_top_up_bnb"))
    if predict_rest == "pending" or predict_ws == "pending":
        predict_mode = "API Key 待分配"
        predict_reason = predict_reason or "api_key_pending"
    elif residual_allowance > 0 and active_execution is None:
        predict_mode = "熔断只读"
        predict_reason = predict_reason or "residual_predict_allowance"
    else:
        predict_mode = "可以交易" if predict_account_ready and not breaker_open and not cross_breaker_open else "只读"
        if gas_top_up > 0 and predict_reason in (None, ""):
            predict_reason = "insufficient_bnb"
        if predict_reason in (None, "") and not predict_account_ready and getattr(execution, "_predict_trading", None) is not None:
            predict_reason = "predict_account_unavailable"
        if predict_reason in (None, "") and predict_rest not in {"ready", "unknown"}:
            predict_reason = f"predict_{predict_rest}"
    predict_payload: dict[str, object] = {
        "venue": "predict.fun",
        "rest": predict_rest,
        "ws": predict_ws,
        "wallet": predict_wallet,
        "balance": {
            "asset": source_snapshot.get("settlement_asset", "USDT"),
            "value": predict_account.get("available_usdt", source_snapshot.get("balance")),
        },
        "mode": predict_mode,
        "last_success": source_snapshot.get("last_success"),
        "reason": predict_reason,
    }
    if predict_account:
        predict_payload.update(
            {
                "account": {
                    "role": "Predict Account · USDT/持仓/Allowance",
                    "address": _prediction_mask_wallet(
                        predict_account.get("predict_account")
                        or predict_account.get("wallet_address")
                        or predict_wallet
                    ),
                    "available_usdt": predict_account.get("available_usdt"),
                    "allowance": predict_account.get("allowance"),
                },
                "gas": {
                    "role": "Privy signer · BNB Gas",
                    "address": _prediction_mask_wallet(predict_account.get("gas_signer")),
                    "bnb_balance": predict_account.get("bnb_balance"),
                    "required_bnb": predict_account.get("required_bnb"),
                    "minimum_top_up": predict_account.get("minimum_top_up_bnb", "0"),
                },
                "reservation": {
                    "reserved_usdt": predict_account.get("reserved_usdt"),
                    "unsettled_usdt": predict_account.get("unsettled_usdt"),
                },
                "canary": predict_account.get("canary_mode"),
            }
        )
    return [
        {
            "venue": "polymarket",
            "rest": rest,
            "ws": ws,
            "wallet": masked_wallet,
            "balance": {
                "asset": "pUSD",
                "value": readiness.get("p_usd_balance", readiness.get("balance")),
            },
            "mode": poly_mode,
            "last_success": snapshot.get("heartbeat_at"),
            "reason": poly_reason,
        },
        predict_payload,
    ]


def prediction_state_payload(
    *,
    store: PredictionArbitrageStore | None,
    monitor: object | None,
    execution: object | None,
    csrf_token: str,
    cross_venue_monitor: PredictCrossVenueMonitor | None = None,
    relation_catalog: object | None = None,
    n_leg_solutions: Sequence[Mapping[str, object]] = (),
    n_leg_metrics: object = None,
) -> dict[str, object]:
    if monitor is None and store is None and execution is None:
        return _prediction_unavailable_state(csrf_token)
    snapshot = _prediction_monitor_snapshot(monitor)
    cross_venue = _prediction_cross_snapshot(cross_venue_monitor)
    safe_snapshot = _prediction_safe_value(snapshot)
    if not isinstance(safe_snapshot, Mapping):
        safe_snapshot = {}
    readiness = safe_snapshot.get("readiness")
    if not isinstance(readiness, Mapping):
        readiness = {"status": "unavailable", "reason": "readiness_unavailable"}
    else:
        readiness = dict(readiness)
        for field in ("wallet_address", "wallet"):
            value = readiness.get(field)
            if isinstance(value, str) and value.startswith("0x"):
                readiness[field] = _prediction_mask_wallet(value)
    raw_readiness = snapshot.get("readiness")
    wallet_address = ""
    if isinstance(raw_readiness, Mapping):
        wallet_address = str(
            raw_readiness.get("wallet_address")
            or raw_readiness.get("wallet")
            or ""
        )
    if not wallet_address and execution is not None:
        trading = getattr(execution, "_trading", None)
        config = getattr(trading, "config", None)
        wallet_address = str(getattr(config, "wallet_address", "") or "")
    masked_wallet = _prediction_mask_wallet(wallet_address)
    try:
        active = store.active_execution() if store is not None else None
    except Exception:
        active = None
    try:
        incident = store.unacknowledged_incident() if store is not None else None
    except Exception:
        incident = None
    breaker_open = True
    breaker_method = getattr(execution, "_breaker_is_open", None)
    if callable(breaker_method):
        try:
            breaker_open = bool(breaker_method())
        except Exception:
            breaker_open = True
    elif execution is not None:
        breaker_open = bool(getattr(execution, "_breaker_open", True))
    cross_breaker_open = bool(getattr(execution, "_cross_breaker_open", True))
    cross_unsettled, cross_unsettled_current = _prediction_cross_unsettled(store)
    cross_venue["unsettled"] = cross_unsettled
    cross_venue["breaker"] = {"open": cross_breaker_open, "scope": "cross_venue"}
    cross_venue["opportunities"] = [
        _prediction_cross_opportunity_runtime(
            row,
            unsettled=cross_unsettled,
            current=cross_unsettled_current,
            cross_breaker_open=cross_breaker_open,
        )
        for row in cross_venue["opportunities"]
    ]
    events = safe_snapshot.get("events")
    opportunities = safe_snapshot.get("opportunities")
    event_rows = [row for row in events if isinstance(row, Mapping)] if isinstance(events, (list, tuple)) else []
    opportunity_rows = [row for row in opportunities if isinstance(row, Mapping)] if isinstance(opportunities, (list, tuple)) else []
    event_rows = [
        _prediction_event_aliases(row)
        for row in event_rows
    ]
    opportunity_rows = [
        _prediction_attach_n_leg_shadow(
            store, _prediction_opportunity_aliases(row)
        )
        for row in opportunity_rows
    ]
    event_rows.extend(
        _prediction_attach_cached_title(store, _prediction_event_aliases(row))
        for row in cross_venue["events"]
    )
    opportunity_rows.extend(
        _prediction_attach_cached_title(
            store,
            _prediction_attach_n_leg_shadow(
                store, _prediction_opportunity_aliases(row)
            ),
        )
        for row in cross_venue["opportunities"]
    )
    opportunity_rows = [
        row for row in opportunity_rows if _prediction_displayable(row)
    ]
    for event in event_rows:
        markets = event.get("markets")
        if isinstance(markets, (list, tuple)):
            event["markets"] = [
                market
                for market in markets
                if isinstance(market, Mapping) and _prediction_displayable(market)
            ]
    event_rows = [
        event
        for event in event_rows
        if event.get("actionable") is True
        or "markets" not in event
        or event.get("markets")
    ]
    event_rows = sorted(
        (row for row in event_rows if isinstance(row, Mapping)), key=_prediction_sort_key
    )
    opportunity_rows = sorted(
        (row for row in opportunity_rows if isinstance(row, Mapping)), key=_prediction_sort_key
    )
    predict_snapshot = _prediction_predict_account_snapshot(execution)
    balances: dict[str, Decimal | None] = {
        "p_usd": _prediction_decimal_sort(
            readiness.get("p_usd_balance", readiness.get("balance"))
        ) if readiness.get("p_usd_balance", readiness.get("balance")) not in (None, "") else None,
        "predict_usdt": _prediction_decimal_sort(
            predict_snapshot.get("available_usdt")
        ) if predict_snapshot.get("available_usdt") not in (None, "") else None,
    }
    projected_opportunities: list[dict[str, object]] = []
    for row in opportunity_rows:
        projected = dict(row)
        projected.update(_prediction_nleg_labels(projected))
        projected["qualification"] = _prediction_qualification(
            projected,
            balances=balances,
            breaker_open=breaker_open,
            cross_breaker_open=cross_breaker_open,
        )
        projected_opportunities.append(projected)
    opportunity_rows = projected_opportunities
    qualified_opportunities = [
        row
        for row in opportunity_rows
        if row["qualification"]["status"] in {"QUALIFIED_VERIFIED", "QUALIFIED_FEASIBLE"}
    ]
    snapshot_status = str(safe_snapshot.get("status") or "unavailable")
    snapshot_stale = bool(safe_snapshot.get("stale")) or snapshot_status in {
        "degraded",
        "unavailable",
        "error",
    }
    opportunity_qualification: dict[str, object] = {
        "status": (
            "UNKNOWN"
            if snapshot_stale
            else "QUALIFIED"
            if qualified_opportunities
            else "NO_QUALIFIED_OPPORTUNITY"
        ),
        "no_arbitrage": False,
        "qualified_count": len(qualified_opportunities),
        "total_count": len(opportunity_rows),
        "verified_count": sum(
            row["qualification"]["status"] == "QUALIFIED_VERIFIED"
            for row in opportunity_rows
        ),
        "feasible_count": sum(
            row["qualification"]["status"] == "QUALIFIED_FEASIBLE"
            for row in opportunity_rows
        ),
        "unknown_count": sum(
            row["qualification"]["status"] == "UNKNOWN"
            for row in opportunity_rows
        ),
    }
    event_count = len(event_rows)
    market_count = 0
    token_ids: set[str] = set()
    for event in event_rows:
        markets = event.get("markets")
        if isinstance(markets, (list, tuple)):
            market_count += len(markets)
            for market in markets:
                if not isinstance(market, Mapping):
                    continue
                for key in ("yes_token_id", "no_token_id"):
                    token = market.get(key)
                    if token:
                        token_ids.add(str(token))
        elif isinstance(event.get("market_count"), int):
            market_count += int(event["market_count"])
    if not market_count:
        market_count = int(safe_snapshot.get("market_count") or 0)
    if not token_ids:
        for opportunity in opportunity_rows:
            for key in ("yes_token_id", "no_token_id"):
                token = opportunity.get(key)
                if token:
                    token_ids.add(str(token))
    token_count = len(token_ids)
    if not token_count:
        try:
            token_count = int(safe_snapshot.get("token_count") or market_count * 2)
        except (TypeError, ValueError):
            token_count = market_count * 2
    signals_24h = safe_snapshot.get("signals_24h", safe_snapshot.get("history_count_24h"))
    if signals_24h is None and store is not None:
        signal_history = getattr(store, "signal_history", None)
        if callable(signal_history):
            try:
                signals_24h = len(signal_history("24h"))
            except Exception:
                signals_24h = 0
    try:
        signals_24h = int(signals_24h or 0)
    except (TypeError, ValueError):
        signals_24h = 0
    first_live_order = readiness.get("first_live_order")
    if bool(getattr(execution, "_first_live_order_verified", False)):
        first_live_order = "已验证"
    elif first_live_order is None and store is not None:
        load_runtime = getattr(store, "load_runtime", None)
        if callable(load_runtime):
            try:
                runtime = load_runtime()
            except Exception:
                runtime = None
            if isinstance(runtime, Mapping):
                runtime_prediction = runtime.get("prediction_arbitrage")
                if isinstance(runtime_prediction, Mapping):
                    first_live_order = runtime_prediction.get("first_live_order")
                if first_live_order is None:
                    first_live_order = runtime.get("first_live_order")
    if first_live_order is not None:
        if str(first_live_order).casefold() in {"validated", "verified", "complete"}:
            first_live_order = "已验证"
        readiness["first_live_order"] = first_live_order
    current_execution = _prediction_safe_value(active)
    if isinstance(current_execution, Mapping):
        current_execution = dict(current_execution)
        state_value = current_execution.get("state")
        if state_value is not None:
            current_execution.setdefault("status", state_value)
        event_title = _prediction_first(
            current_execution, "question", "title", "market_title"
        )
        if event_title is not None:
            current_execution.setdefault("event_title", event_title)
    status = str(safe_snapshot.get("status") or "unavailable")
    if not snapshot:
        status = "unavailable"
    health = safe_snapshot.get("health")
    if not isinstance(health, Mapping):
        health = {"status": status, "degraded_reasons": []}
    else:
        health = dict(health)
    degraded_reasons = health.get("degraded_reasons")
    failure_reason = (
        degraded_reasons[0]
        if isinstance(degraded_reasons, (list, tuple)) and degraded_reasons
        else readiness.get("reason")
    )
    stale = bool(safe_snapshot.get("stale")) or status in {"degraded", "unavailable", "error"}
    policy = _prediction_unavailable_state(csrf_token)["policy_limits"]
    balances = {
        "p_usd": readiness.get("p_usd_balance", readiness.get("balance")),
        "allowance": readiness.get("p_usd_allowance", readiness.get("allowance")),
    }
    venues = _prediction_venues_payload(
        snapshot=safe_snapshot,
        readiness=readiness,
        health=health,
        masked_wallet=masked_wallet,
        breaker_open=breaker_open,
        execution=execution,
        active_execution=active,
        cross_venue_monitor=cross_venue_monitor,
        cross_venue=cross_venue,
    )
    validation_mode = "observe_only"
    auto_eat_stats: dict[str, object] = {}
    llm_usage_24h: dict[str, object] = {}
    cross_auto: dict[str, object] = {
        "configured_mode": "observe_only",
        "effective_mode": "observe_only",
        "armed": False,
        "pause_reason": "not_armed",
        "notification_ready": False,
        "daily_principal": {"current": "0", "limit": "100"},
        "latest_attempt": None,
    }
    if store is not None:
        get_mode = getattr(store, "get_validation_mode", None)
        if callable(get_mode):
            try:
                validation_mode = get_mode()
            except Exception:
                validation_mode = "observe_only"
        get_stats = getattr(store, "auto_eat_stats", None)
        if callable(get_stats):
            try:
                auto_eat_stats = get_stats()
            except Exception:
                auto_eat_stats = {}
        get_llm_usage = getattr(store, "llm_usage_24h", None)
        if callable(get_llm_usage):
            try:
                llm_value = _prediction_safe_value(get_llm_usage())
                if isinstance(llm_value, Mapping):
                    llm_usage_24h = dict(llm_value)
            except Exception:
                llm_usage_24h = {}
    get_cross_auto_status = getattr(execution, "cross_auto_status", None)
    if callable(get_cross_auto_status):
        try:
            status_value = _prediction_safe_value(get_cross_auto_status())
            if isinstance(status_value, Mapping):
                cross_auto = dict(status_value)
        except Exception:
            pass
    shadow_summary = _prediction_n_leg_shadow_summary(opportunity_rows)
    n_leg = _prediction_n_leg_contract(execution)
    n_leg_projections = _prediction_n_leg_solution_projection(
        n_leg_solutions,
        n_leg=n_leg,
        total_unsettled_capital_units=cross_unsettled_current,
    )
    if n_leg_projections:
        by_component = {
            str(item.get("component_id") or ""): item
            for item in n_leg_projections
            if item.get("component_id")
        }
        if by_component:
            for row in opportunity_rows:
                component_id = str(row.get("component_id") or "")
                if component_id in by_component:
                    row["n_leg_solution"] = by_component[component_id]
    capital_usage = _prediction_capital_usage(
        cross_unsettled=cross_unsettled,
        current_execution=current_execution,
        n_leg=n_leg,
    )
    result = {
        "status": status,
        "health": health,
        "failure_reason": failure_reason,
        "readiness": dict(readiness),
        "first_live_order": first_live_order,
        "wallet": {"address": "", "masked_address": masked_wallet},
        "masked_wallet": masked_wallet,
        "balances": balances,
        "policy_limits": policy,
        "heartbeat": safe_snapshot.get("heartbeat_at"),
        "heartbeat_at": safe_snapshot.get("heartbeat_at"),
        "stale": stale,
        "events": event_rows,
        "opportunities": opportunity_rows,
        "qualified_opportunities": qualified_opportunities,
        "opportunity_qualification": opportunity_qualification,
        "capital_usage": capital_usage,
        "venues": venues,
        "cross_venue": cross_venue,
        **({"n_leg": n_leg} if n_leg is not None else {}),
        "relation_discovery": _prediction_relation_safe_value(
            safe_snapshot.get("relation_discovery", {})
        ),
        "event_count": event_count,
        "market_count": market_count,
        "token_count": token_count,
        "signals_24h": signals_24h,
        "validation_mode": validation_mode,
        "auto_eat_stats": auto_eat_stats,
        "llm_usage_24h": llm_usage_24h,
        "cross_auto": cross_auto,
        "current_execution": current_execution,
        "breaker": {
            "open": breaker_open,
            "status": "locked" if breaker_open else "ready",
            "incident": _prediction_safe_value(incident),
        },
        "csrf_token": csrf_token,
    }
    if n_leg_projections:
        result["n_leg_solutions"] = n_leg_projections
    if isinstance(n_leg_metrics, Mapping):
        safe_metrics = _prediction_safe_value(n_leg_metrics)
        if isinstance(safe_metrics, Mapping):
            result["n_leg_metrics"] = dict(safe_metrics)
    if shadow_summary["monitoring"]:
        result["n_leg_shadow"] = shadow_summary
    result["relation_review"] = _prediction_relation_review(relation_catalog)
    return result


def prediction_history_payload(
    store: PredictionArbitrageStore | None,
    *,
    kind: str,
    limit: int,
    offset: int,
    monitor: object | None = None,
    execution: object | None = None,
    cross_venue_monitor: PredictCrossVenueMonitor | None = None,
) -> dict[str, object]:
    if kind not in PREDICTION_HISTORY_KINDS:
        raise ValueError("kind must be signals, executions, or incidents")
    if store is None:
        rows = []
    elif kind == "signals":
        rows = store.signal_history("30d")
    else:
        rows = store.histories(kind)
    title_cache: dict[str, object] = {}
    safe_rows = [
        _prediction_attach_cached_title(
            store,
            _prediction_history_aliases(kind, _prediction_safe_value(row)),
            title_cache,
        )
        for row in rows
    ]
    if kind == "signals":
        safe_rows = [
            row
            for row in safe_rows
            if not isinstance(row, Mapping) or _prediction_displayable(row)
        ]
        try:
            state = prediction_state_payload(
                store=store,
                monitor=monitor,
                execution=execution,
                csrf_token="",
                cross_venue_monitor=cross_venue_monitor,
            )
        except Exception:
            state = {}
        opportunities = state.get("opportunities") if isinstance(state, Mapping) else None
        opportunity_by_id: dict[str, Mapping[str, object]] = {}
        opportunity_by_market: dict[str, Mapping[str, object]] = {}
        if isinstance(opportunities, (list, tuple)):
            for value in opportunities:
                if not isinstance(value, Mapping):
                    continue
                opportunity_id = _prediction_first(value, "opportunity_id", "id")
                market_id = _prediction_first(value, "market_id")
                if opportunity_id not in (None, ""):
                    opportunity_by_id.setdefault(str(opportunity_id), value)
                if market_id not in (None, ""):
                    opportunity_by_market.setdefault(str(market_id), value)

        state_status = str(state.get("status") or "unavailable") if isinstance(state, Mapping) else "unavailable"
        state_stale = bool(state.get("stale")) if isinstance(state, Mapping) else True
        current_execution = state.get("current_execution") if isinstance(state, Mapping) else None
        breaker = state.get("breaker") if isinstance(state, Mapping) else None
        breaker_closed = isinstance(breaker, Mapping) and breaker.get("open") is False
        readiness = state.get("readiness") if isinstance(state, Mapping) else None
        accepted_readiness = {"ready", "allowed", "pass", "confirmed"}

        def _readiness_ok(value: object) -> bool:
            return value is True or (
                isinstance(value, str) and value.casefold() in accepted_readiness
            )
        readiness_status = str(readiness.get("status", "")).casefold() if isinstance(readiness, Mapping) else ""
        geoblock = readiness.get("geoblock") if isinstance(readiness, Mapping) else None
        relayer = (
            readiness.get("relayer")
            if isinstance(readiness, Mapping)
            else None
        )
        if relayer is None and isinstance(readiness, Mapping):
            relayer = readiness.get("relayer_readiness")
        readiness_usable = (
            isinstance(readiness, Mapping)
            and readiness_status not in {"unavailable", "blocked", "fail", "failed"}
            and _readiness_ok(geoblock)
            and _readiness_ok(relayer)
        )
        state_usable = (
            not state_stale
            and state_status not in {"degraded", "unavailable", "error"}
            and not current_execution
            and breaker_closed
            and readiness_usable
        )

        def _present(value: Mapping[str, object], *names: str) -> bool:
            return _prediction_first(value, *names) not in (None, "")

        def _complete(value: Mapping[str, object]) -> bool:
            market_type = str(value.get("market_type") or "standard_binary")
            if market_type == "threshold_hedge":
                required = (
                    ("opportunity_id", "relation_id", "id"),
                    ("market_type",),
                    ("event_id",),
                    ("market_id", "relation_id"),
                    ("question",),
                    ("question_a",),
                    ("question_b",),
                    ("relation",),
                    ("condition_id_a",),
                    ("condition_id_b",),
                    ("token_id_a",),
                    ("token_id_b",),
                    ("quantity",),
                    ("total_max_cost", "max_cost"),
                    ("maximum_fee",),
                    ("minimum_profit", "profit"),
                    ("minimum_payout",),
                    ("annualized_yield",),
                    ("remaining_days",),
                    ("resolution_at",),
                    ("confirmed_at",),
                    ("confirmed_age_seconds",),
                )
            elif market_type == "standard_binary":
                required = (
                    ("opportunity_id", "id"),
                    ("market_type",),
                    ("event_id",),
                    ("market_id",),
                    ("condition_id",),
                    ("yes_token_id",),
                    ("no_token_id",),
                    ("quantity",),
                    ("yes_max_price", "yes_price"),
                    ("no_max_price", "no_price"),
                    ("yes_max_cost",),
                    ("no_max_cost",),
                    ("total_max_cost", "max_cost"),
                    ("minimum_profit",),
                    ("net_edge",),
                    ("tick_size",),
                    ("confirmed_at",),
                    ("confirmed_age_seconds",),
                )
            elif market_type == "cross_venue_yes_no":
                required = (
                    ("opportunity_id", "id"),
                    ("market_type",),
                    ("legs",),
                    ("quantity",),
                    ("total_max_cost",),
                    ("minimum_profit",),
                    ("annualized_yield",),
                    ("resolution_at",),
                    ("clear_signal",),
                )
            else:
                return False
            return all(_present(value, *names) for names in required)

        projected_rows: list[object] = []
        for row in safe_rows:
            if not isinstance(row, Mapping):
                projected_rows.append(row)
                continue
            projected = dict(row)
            projected.setdefault("actionable_now", False)
            projected.setdefault("live_profit", None)
            opportunity_id = _prediction_first(projected, "opportunity_id")
            market_id = _prediction_first(projected, "market_id")
            current = None
            if opportunity_id not in (None, ""):
                current = opportunity_by_id.get(str(opportunity_id))
            if current is None and market_id not in (None, ""):
                current = opportunity_by_market.get(str(market_id))
            if isinstance(current, Mapping):
                row_market_type = str(projected.get("market_type") or "standard_binary")
                current_market_type = str(current.get("market_type") or "")
                same_market = row_market_type == "cross_venue_yes_no" or (
                    market_id not in (None, "")
                    and str(current.get("market_id") or "") == str(market_id)
                )
                if row_market_type != current_market_type or not same_market:
                    current = None
            if (
                isinstance(current, Mapping)
                and str(current.get("market_type") or "") == "cross_venue_yes_no"
            ):
                projected["execution_mode"] = current.get("execution_mode")
            is_open = not projected.get("ended_at")
            complete = isinstance(current, Mapping) and _complete(current)
            current_usable = bool(is_open and state_usable and complete)
            if current_usable and isinstance(current, Mapping):
                live_profit = _prediction_first(current, "estimated_profit", "profit")
                if live_profit is not None:
                    projected["live_profit"] = live_profit
                projected["actionable_now"] = current.get("actionable") is True
                if str(current.get("market_type") or "") == "cross_venue_yes_no":
                    projected["signal_live_now"] = True
                if str(current.get("market_type") or "") == "threshold_hedge":
                    for name in (
                        "annualized_yield",
                        "remaining_days",
                        "resolution_at",
                        "total_max_cost",
                        "maximum_fee",
                        "eligibility_reason",
                    ):
                        if current.get(name) not in (None, ""):
                            projected[name] = current[name]
            projected_rows.append(projected)
        safe_rows = sorted(
            projected_rows,
            key=lambda value: (
                not (isinstance(value, Mapping) and value.get("actionable_now") is True),
                -_prediction_history_epoch(value.get("started_at") if isinstance(value, Mapping) else None),
                str(value.get("signal_id") if isinstance(value, Mapping) else ""),
            ),
        )
    items = safe_rows[offset : offset + limit]
    return {
        "kind": kind,
        "items": items,
        "total": len(safe_rows),
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(items) < len(safe_rows),
    }
