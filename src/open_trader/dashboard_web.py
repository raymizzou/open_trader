from __future__ import annotations

import asyncio
import inspect
import json
import ipaddress
import os
import secrets
import subprocess
import threading
from datetime import date, datetime
from decimal import Decimal
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, quote, unquote, urlparse
from urllib.request import Request, urlopen

from .backtest import run_backtest
from .backtest_prices import DailyKlineProvider, normalize_backtest_symbol
from .dashboard import (
    DETAIL_FX_TO_HKD,
    DashboardConfig,
    _backtest_holding_detail,
    _latest_backtests_by_holding,
    load_historical_trend_report,
    load_dashboard_state,
    load_trend_report_history,
)
from .dashboard_quotes import SHANGHAI_TZ, load_published_quotes
from .futu_quote import FutuQuoteClient
from .polymarket_monitor import PolymarketMonitor
from .polymarket_relation_discovery import (
    CodexRelationValidator,
    discover_threshold_relation_catalog,
)
from .predict_cross_venue import (
    CodexCrossVenueEquivalenceValidator,
    PredictCrossVenueMonitor,
    validate_cross_execution_mode,
)
from .predict_source import PredictSource
from .predict_trading import PredictTradingClient

# Keep the old module attribute for downstream test fakes while production
# wiring uses the catalog result contract above.
discover_threshold_relations = discover_threshold_relation_catalog
from .polymarket_trading import PolymarketTradingClient, load_trading_config
from .daily_premarket import build_notifier
from .notifications import NullNotifier
from .prediction_arbitrage import (
    MAX_CROSS_UNSETTLED_PRINCIPAL,
    MAX_EMERGENCY_LOSS,
    MAX_NORMAL_COST,
    MAX_WALLET_BALANCE,
    MIN_ESTIMATED_PROFIT,
    MIN_NET_EDGE,
)
from .prediction_arbitrage_execution import PredictionExecutionService
from .prediction_arbitrage_store import PredictionArbitrageStore
from .prediction_title_translation import (
    CodexTitleTranslator,
    cached_prediction_title_zh,
)
from .research_chat import ResearchChatError, ResearchChatService
from .standard_strategies import strategy_catalog
from .statement_import import StatementImportService
from .strategy_backtest import (
    StandardBacktestRequest,
    run_standard_backtest,
    validate_standard_backtest_request,
)
from .trading_plan import load_trading_plan_rows
from .trend_simulate_positions import TrendSimulatePositionService


STATIC_DIR = Path(__file__).with_name("dashboard_static")
STANDARD_BACKTEST_RANGES = ("6M", "1Y", "3Y", "5Y", "CUSTOM")
STANDARD_BACKTEST_REQUEST_KEYS = {
    "market", "symbol", "strategy_id", "range_preset", "custom_start", "custom_end",
    "initial_cash", "max_strategy_weight", "commission_bps", "slippage_bps",
}
MAX_JSON_BODY_BYTES = 1024 * 1024
MAX_PDF_BODY_BYTES = 20 * 1024 * 1024


class RequestBodyTooLargeError(Exception):
    pass


class StandardBacktestExecutionError(RuntimeError):
    pass


PREDICTION_HISTORY_KINDS = {"signals", "executions", "incidents"}
PREDICTION_HISTORY_DEFAULT_LIMIT = 100
PREDICTION_HISTORY_MAX_LIMIT = 500


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


def _prediction_sort_key(item: Mapping[str, object]) -> tuple[bool, Decimal, Decimal, str]:
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
    store: PredictionArbitrageStore | None, value: object
) -> object:
    if store is None or not isinstance(value, Mapping):
        return value
    result = dict(value)
    title = _prediction_first(
        result, "event_title", "title", "question", "market_title"
    )
    if title is not None:
        translated = cached_prediction_title_zh(store, str(title), record_hit=False)
        if translated is not None:
            result["event_title_zh"] = translated
            result.setdefault("title_zh", translated)
    markets = result.get("markets")
    if isinstance(markets, (list, tuple)):
        result["markets"] = [
            _prediction_attach_cached_title(store, item) for item in markets
        ]
    return result


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


def _prediction_state_payload(
    *,
    store: PredictionArbitrageStore | None,
    monitor: object | None,
    execution: object | None,
    csrf_token: str,
    cross_venue_monitor: PredictCrossVenueMonitor | None = None,
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
        _prediction_opportunity_aliases(row)
        for row in opportunity_rows
    ]
    event_rows.extend(
        _prediction_attach_cached_title(store, _prediction_event_aliases(row))
        for row in cross_venue["events"]
    )
    opportunity_rows.extend(
        _prediction_attach_cached_title(store, _prediction_opportunity_aliases(row))
        for row in cross_venue["opportunities"]
    )
    event_rows = sorted(
        (row for row in event_rows if isinstance(row, Mapping)), key=_prediction_sort_key
    )
    opportunity_rows = sorted(
        (row for row in opportunity_rows if isinstance(row, Mapping)), key=_prediction_sort_key
    )
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
    return {
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
        "venues": venues,
        "cross_venue": cross_venue,
        "relation_discovery": _prediction_relation_safe_value(
            safe_snapshot.get("relation_discovery", {})
        ),
        "event_count": event_count,
        "market_count": market_count,
        "token_count": token_count,
        "signals_24h": signals_24h,
        "current_execution": current_execution,
        "breaker": {
            "open": breaker_open,
            "status": "locked" if breaker_open else "ready",
            "incident": _prediction_safe_value(incident),
        },
        "csrf_token": csrf_token,
    }


def _prediction_history_payload(
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
    rows = store.histories(kind) if store is not None else []
    safe_rows = [
        _prediction_attach_cached_title(
            store, _prediction_history_aliases(kind, _prediction_safe_value(row))
        )
        for row in rows
    ]
    if kind == "signals":
        try:
            state = _prediction_state_payload(
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


def build_standard_backtest_options_payload(config: DashboardConfig) -> dict[str, Any]:
    state = load_dashboard_state(config).to_dict()
    return {
        "strategies": [definition.to_dict() for definition in strategy_catalog()],
        "ranges": list(STANDARD_BACKTEST_RANGES),
        "defaults": {
            "range": "1Y", "initial_cash": "100000", "max_strategy_weight": "0.10",
            "commission_bps": "10", "slippage_bps": "5",
        },
        "universe": state["backtest_universe"],
        "benchmarks": {"US": "SPY", "HK": "HK.02800", "CN": "000300"},
    }


def _parse_decimal(request: dict[str, Any], key: str, default: str) -> Decimal:
    raw = str(request.get(key, default)).strip()
    percent = raw.endswith("%")
    if percent:
        raw = raw[:-1].strip()
    try:
        value = Decimal(raw)
    except Exception as exc:
        raise ValueError(f"{key} 必须是有效数字") from exc
    return value / Decimal("100") if percent else value


def _parse_iso_date(request: dict[str, Any], key: str) -> date | None:
    raw = str(request.get(key) or "").strip()
    if not raw:
        return None
    try:
        parsed = date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{key} 必须使用 YYYY-MM-DD 格式") from exc
    if parsed.isoformat() != raw:
        raise ValueError(f"{key} 必须使用 YYYY-MM-DD 格式")
    return parsed


def parse_standard_backtest_request(
    config: DashboardConfig, request: dict[str, Any]
) -> StandardBacktestRequest:
    unknown = set(request) - STANDARD_BACKTEST_REQUEST_KEYS
    if unknown:
        raise ValueError(f"不支持的请求字段：{', '.join(sorted(unknown))}")
    market = str(request.get("market") or "").strip().upper()
    symbol = str(request.get("symbol") or "").strip().upper()
    strategy_id = str(request.get("strategy_id") or "").strip()
    preset = str(request.get("range_preset") or "1Y").strip().upper()
    if preset not in STANDARD_BACKTEST_RANGES:
        raise ValueError(f"不支持的回测区间：{preset}")
    if strategy_id not in {item.strategy_id for item in strategy_catalog()}:
        raise ValueError(f"未知策略：{strategy_id}")
    custom_start = _parse_iso_date(request, "custom_start")
    custom_end = _parse_iso_date(request, "custom_end")
    if preset == "CUSTOM":
        if custom_start is None:
            raise ValueError("自定义区间必须提供开始日期")
        if custom_end is not None and custom_start >= custom_end:
            raise ValueError("开始日期必须早于结束日期")
    elif custom_start is not None or custom_end is not None:
        raise ValueError("预设区间不能同时提供自定义日期")
    options = build_standard_backtest_options_payload(config)
    universe = options["universe"]["holdings"] + options["universe"]["watchlist"]
    normalized = normalize_backtest_symbol(market, symbol)
    allowed = {
        (row["market"], row["symbol"].zfill(5) if row["market"] == "HK" and row["symbol"].isdigit() else row["symbol"])
        for row in universe
    }
    if (market, normalized) not in allowed:
        raise ValueError("所选标的不在可回测范围内")
    parsed = StandardBacktestRequest(
        data_dir=config.data_dir, reports_dir=config.reports_dir, market=market,
        symbol=normalized, strategy_id=strategy_id,
        range_preset=None if preset == "CUSTOM" else preset,
        custom_start=custom_start, custom_end=custom_end,
        initial_cash=_parse_decimal(request, "initial_cash", "100000"),
        max_strategy_weight=_parse_decimal(request, "max_strategy_weight", "0.10"),
        commission_bps=_parse_decimal(request, "commission_bps", "10"),
        slippage_bps=_parse_decimal(request, "slippage_bps", "5"),
    )
    validate_standard_backtest_request(parsed)
    return parsed


def build_standard_backtest_run_payload(
    config: DashboardConfig, request: dict[str, Any], *,
    provider: DailyKlineProvider | None = None,
) -> dict[str, Any]:
    if "adapter" in request:
        raise ValueError("不支持从界面选择回测执行工具")
    parsed = parse_standard_backtest_request(config, request)
    owned_provider = provider is None
    try:
        price_provider = provider or FutuQuoteClient(
            host=config.futu_host,
            port=config.futu_port,
        )
    except Exception as exc:
        raise StandardBacktestExecutionError(f"行情服务连接失败：{exc}") from exc
    result: dict[str, Any] | None = None
    primary_error: StandardBacktestExecutionError | None = None
    try:
        result = run_standard_backtest(parsed, price_provider=price_provider).to_dict()
    except Exception as exc:
        primary_error = StandardBacktestExecutionError(f"标准策略回测执行失败：{exc}")
        primary_error.__cause__ = exc
    if owned_provider and hasattr(price_provider, "close"):
        try:
            price_provider.close()
        except Exception as exc:
            if primary_error is None:
                raise StandardBacktestExecutionError(
                    f"行情服务关闭失败：{exc}"
                ) from exc
    if primary_error is not None:
        raise primary_error
    if result is None:  # pragma: no cover - defensive invariant
        raise StandardBacktestExecutionError("标准策略回测未返回结果")
    return result


def build_dashboard_payload(
    config: DashboardConfig,
) -> dict[str, Any]:
    return load_dashboard_state(config).to_dict()


def build_quotes_payload(
    config: DashboardConfig,
) -> dict[str, object]:
    return load_published_quotes(
        config.data_dir / "latest" / "quotes.json",
        now=datetime.now(SHANGHAI_TZ),
    )


def build_backtest_run_payload(
    config: DashboardConfig,
    request: dict[str, Any],
) -> dict[str, Any]:
    market = str(request.get("market") or "").strip().upper()
    symbol = str(request.get("symbol") or "").strip().upper()
    if not market or not symbol:
        raise ValueError("market and symbol are required")

    plan_path = _dashboard_backtest_plan_path(config.data_dir, market)
    plan = _latest_active_plan(plan_path, market=market, symbol=symbol)
    prices_path = config.data_dir / "prices" / market / f"{symbol}.csv"
    run_backtest(
        plan_path=plan_path,
        prices_path=prices_path,
        data_dir=config.data_dir,
        reports_dir=config.reports_dir,
        run_date=plan.run_date,
        symbol=symbol,
        market=market,
        initial_cash=_decimal_request_value(request, "initial_cash", "100000"),
        initial_position_quantity=_decimal_request_value(
            request, "initial_position_quantity", "0"
        ),
        commission_bps=_decimal_request_value(request, "commission_bps", "10"),
        slippage_bps=_decimal_request_value(request, "slippage_bps", "5"),
        adapter=str(request.get("adapter") or "backtrader"),
    )
    backtest = _dashboard_backtest_for_holding(config, market=market, symbol=symbol)
    return {
        "status": "ok",
        "market": market,
        "symbol": symbol,
        "backtest": backtest,
    }


def _dashboard_backtest_plan_path(data_dir: Path, market: str) -> Path:
    scoped_path = data_dir / "latest" / market / "trading_plan.csv"
    if scoped_path.exists():
        return scoped_path
    return data_dir / "latest" / "trading_plan.csv"


def _latest_active_plan(plan_path: Path, *, market: str, symbol: str) -> Any:
    plans = [
        plan
        for plan in load_trading_plan_rows(plan_path)
        if plan.status == "active"
        and plan.market.upper() == market
        and plan.symbol.upper() == symbol
    ]
    if not plans:
        raise ValueError(f"no active trading plan found for {market}.{symbol}")
    plans.sort(key=lambda plan: (plan.run_date, plan.symbol))
    return plans[-1]


def _decimal_request_value(
    request: dict[str, Any],
    key: str,
    default: str,
) -> Decimal:
    value = request.get(key, default)
    return Decimal(str(value))


def _dashboard_backtest_for_holding(
    config: DashboardConfig,
    *,
    market: str,
    symbol: str,
) -> dict[str, Any]:
    rows = _latest_backtests_by_holding(
        data_dir=config.data_dir, reports_dir=config.reports_dir, markets={market},
    )
    return _backtest_holding_detail(rows.get((market, symbol)))


def create_dashboard_server(
    config: DashboardConfig,
    host: str,
    port: int,
    research_chat_service: ResearchChatService | None = None,
    backtest_price_provider: DailyKlineProvider | None = None,
    statement_import_service: StatementImportService | None = None,
    trend_simulate_position_service: TrendSimulatePositionService | None = None,
    eastmoney_password: str = "",
    prediction_store: PredictionArbitrageStore | None = None,
    prediction_monitor: object | None = None,
    cross_venue_monitor: PredictCrossVenueMonitor | None = None,
    prediction_execution_service: object | None = None,
    prediction_session_token: str | None = None,
    prediction_csrf_token: str | None = None,
    runtime_metadata: Mapping[str, object] | None = None,
) -> ThreadingHTTPServer:
    chat_service = research_chat_service or ResearchChatService(data_dir=config.data_dir)
    import_service = statement_import_service or StatementImportService(
        data_dir=config.data_dir,
        reports_dir=config.reports_dir,
        eastmoney_password=eastmoney_password,
    )
    portfolio_update_lock = threading.Lock()
    prediction_session = prediction_session_token or secrets.token_urlsafe(32)
    prediction_csrf = prediction_csrf_token or secrets.token_urlsafe(32)
    health_runtime = dict(
        runtime_metadata
        if runtime_metadata is not None
        else _dashboard_runtime_metadata()
    )

    class DashboardRequestHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/":
                self._send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
                return
            if path == "/static/dashboard.css":
                self._send_file(STATIC_DIR / "dashboard.css", "text/css; charset=utf-8")
                return
            if path == "/static/dashboard.js":
                self._send_file(
                    STATIC_DIR / "dashboard.js",
                    "application/javascript; charset=utf-8",
                )
                return
            if path == "/healthz":
                self._send_json(
                    {
                        "schema_version": "open_trader.legacy_dashboard.health.v1",
                        "module": "legacy_dashboard",
                        **health_runtime,
                    }
                )
                return
            if path == "/api/dashboard":
                try:
                    self._send_json(
                        build_dashboard_payload(config)
                    )
                except Exception as exc:
                    self._send_error_json(exc)
                return
            if path == "/api/prediction-arbitrage/state":
                self._send_prediction_state()
                return
            if path == "/api/prediction-arbitrage/history":
                try:
                    query = parse_qs(parsed.query, keep_blank_values=True)
                    kind = str(query.get("kind", [""])[0]).strip()
                    limit = self._prediction_query_int(query, "limit", PREDICTION_HISTORY_DEFAULT_LIMIT)
                    offset = self._prediction_query_int(query, "offset", 0)
                    self._send_json(
                        _prediction_history_payload(
                            prediction_store,
                            kind=kind,
                            limit=limit,
                            offset=offset,
                            monitor=prediction_monitor,
                            execution=prediction_execution_service,
                            cross_venue_monitor=cross_venue_monitor,
                        )
                    )
                except Exception as exc:
                    self._send_error_json(exc)
                return
            if path == "/api/backtests/options":
                try:
                    self._send_json(build_standard_backtest_options_payload(config))
                except Exception as exc:
                    self._send_error_json(exc)
                return
            if path == "/api/quotes":
                try:
                    self._send_json(build_quotes_payload(config))
                except Exception as exc:
                    self._send_error_json(exc)
                return
            trend_reports_prefix = "/api/trend-reports/"
            if path.startswith(trend_reports_prefix):
                route = path.removeprefix(trend_reports_prefix).split("/", 2)
                try:
                    if len(route) == 2 and route[1] == "history":
                        self._send_json(
                            load_trend_report_history(
                                config.reports_dir, broker=route[0]
                            )
                        )
                        return
                    if len(route) == 3 and route[1] == "history":
                        try:
                            report = load_historical_trend_report(
                                config,
                                broker=route[0],
                                artifact=unquote(route[2]),
                            )
                        except FileNotFoundError as exc:
                            self._send_error_json(exc, HTTPStatus.NOT_FOUND)
                            return
                        self._send_json(report)
                        return
                except Exception as exc:
                    self._send_error_json(exc)
                    return
            trend_simulate_prefix = "/api/trend-simulate-positions/"
            if path.startswith(trend_simulate_prefix):
                broker = path.removeprefix(trend_simulate_prefix)
                if broker and "/" not in broker:
                    try:
                        if trend_simulate_position_service is None:
                            raise RuntimeError(
                                "trend simulate position service is unavailable"
                            )
                        self._send_json(
                            trend_simulate_position_service.load(broker)
                        )
                    except Exception as exc:
                        self._send_error_json(exc)
                    return
            session_id = self._research_chat_session_id(path)
            if session_id is not None:
                try:
                    self._send_json(chat_service.get_session(session_id))
                except Exception as exc:
                    self._send_error_json(exc)
                return
            self._send_not_found()

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            try:
                if path in {
                    "/api/prediction-arbitrage/preview",
                    "/api/prediction-arbitrage/executions",
                    "/api/prediction-arbitrage/circuit-breaker/reset",
                    "/api/prediction-arbitrage/predict-allowance/cleanup",
                }:
                    self._require_prediction_mutation()
                    payload = self._read_json_body()
                    if prediction_execution_service is None:
                        raise RuntimeError("prediction execution service is unavailable")
                    if path.endswith("/preview"):
                        self._require_prediction_schema(payload, {"opportunity_id"})
                        opportunity_id = self._required_prediction_string(payload, "opportunity_id")
                        result = prediction_execution_service.preview(opportunity_id)
                    elif path.endswith("/executions"):
                        self._require_prediction_schema(payload, {"preview_id", "idempotency_key"})
                        preview_id = self._required_prediction_string(payload, "preview_id")
                        idempotency_key = self._required_prediction_string(payload, "idempotency_key")
                        result = prediction_execution_service.confirm(preview_id, idempotency_key)
                    elif path.endswith("/circuit-breaker/reset"):
                        self._require_prediction_schema(payload, {"incident_id"})
                        incident_id = self._required_prediction_string(payload, "incident_id")
                        result = prediction_execution_service.reset_breaker(incident_id)
                    else:
                        self._require_prediction_schema(payload, {"confirm"})
                        if payload.get("confirm") is not True:
                            raise ValueError("confirm must be true")
                        result = prediction_execution_service.cleanup_predict_allowance(confirm=True)
                    self._send_json(_prediction_safe_value(result))
                    return
                if path in {
                    "/api/statements/phillips",
                    "/api/statements/eastmoney",
                }:
                    if not _is_loopback_address(self.client_address[0]):
                        raise PermissionError("结单上传仅允许从本机访问")
                    broker = path.rsplit("/", 1)[-1]
                    with portfolio_update_lock:
                        self._send_json(
                            import_service.import_pdf(broker, self._read_pdf_body())
                        )
                    return
                if path == "/api/research-chat/sessions":
                    payload = self._read_json_body()
                    market = str(payload.get("market") or "")
                    symbol = str(payload.get("symbol") or "")
                    if not market or not symbol:
                        raise ResearchChatError("market and symbol are required")
                    self._send_json(
                        chat_service.create_session(
                            market=market,
                            symbol=symbol,
                        )
                    )
                    return
                if path == "/api/backtests/run":
                    self._send_json(
                        build_backtest_run_payload(config, self._read_json_body())
                    )
                    return
                if path == "/api/backtests/standard/run":
                    self._send_json(
                        build_standard_backtest_run_payload(
                            config, self._read_json_body(), provider=backtest_price_provider,
                        )
                    )
                    return
                if path.startswith("/api/research-chat/sessions/"):
                    route = self._research_chat_session_action(path)
                    if route is None:
                        self._send_not_found()
                        return
                    session_id, action = route
                    if action == "messages":
                        payload = self._read_json_body()
                        self._send_json(
                            chat_service.append_message(
                                session_id=session_id,
                                content=str(payload.get("content") or ""),
                            )
                        )
                        return
                    if action == "finalize":
                        self._read_json_body()
                        self._send_json(
                            chat_service.finalize_session(session_id=session_id)
                        )
                        return
            except Exception as exc:
                self._send_error_json(exc)
                return
            self._send_not_found()

        def _send_prediction_state(self) -> None:
            payload = _prediction_state_payload(
                store=prediction_store,
                monitor=prediction_monitor,
                execution=prediction_execution_service,
                csrf_token=prediction_csrf,
                cross_venue_monitor=cross_venue_monitor,
            )
            self.send_response(HTTPStatus.OK)
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header(
                "Set-Cookie",
                f"ot_prediction_session={prediction_session}; SameSite=Strict; HttpOnly; Path=/",
            )
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                return

        @staticmethod
        def _prediction_query_int(
            query: Mapping[str, list[str]], key: str, default: int
        ) -> int:
            raw = str(query.get(key, [str(default)])[0] or str(default))
            try:
                value = int(raw)
            except ValueError as exc:
                raise ValueError(f"{key} must be a non-negative integer") from exc
            if value < 0 or (key == "limit" and value > PREDICTION_HISTORY_MAX_LIMIT):
                raise ValueError(f"{key} is outside the allowed range")
            if key == "limit" and value == 0:
                raise ValueError("limit must be positive")
            return value

        @staticmethod
        def _require_prediction_schema(
            payload: dict[str, Any], expected: set[str]
        ) -> None:
            if set(payload) != expected:
                raise ValueError("prediction request fields are invalid")

        @staticmethod
        def _required_prediction_string(payload: dict[str, Any], key: str) -> str:
            value = payload.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{key} is required")
            return value.strip()

        def _prediction_listener_host_header(self) -> str:
            address = self.server.server_address
            # Preserve the configured loopback name (notably ``localhost``)
            # so browser Host/Origin headers match the URL the operator used.
            bound_host = str(host)
            bound_port = int(address[1])
            if ":" in bound_host and not bound_host.startswith("["):
                bound_host = f"[{bound_host}]"
            return f"{bound_host}:{bound_port}"

        def _require_prediction_mutation(self) -> None:
            try:
                if not _is_loopback_address(str(self.client_address[0])):
                    raise PermissionError("prediction mutations require loopback")
            except ValueError as exc:
                raise PermissionError("prediction mutations require loopback") from exc
            expected_host = self._prediction_listener_host_header()
            if self.headers.get("Host", "") != expected_host:
                raise PermissionError("prediction mutation Host is invalid")
            if self.headers.get("Origin", "") != f"http://{expected_host}":
                raise PermissionError("prediction mutation Origin is invalid")
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get("Cookie", ""))
            except Exception as exc:
                raise PermissionError("prediction session is invalid") from exc
            provided_session = cookie.get("ot_prediction_session")
            if provided_session is None or not secrets.compare_digest(
                provided_session.value, prediction_session
            ):
                raise PermissionError("prediction session is invalid")
            provided_csrf = self.headers.get("X-CSRF-Token", "")
            if not secrets.compare_digest(provided_csrf, prediction_csrf):
                raise PermissionError("prediction CSRF token is invalid")

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _read_json_body(self) -> dict[str, Any]:
            raw_content_length = self.headers.get("Content-Length") or "0"
            try:
                content_length = int(raw_content_length)
            except ValueError as exc:
                raise ValueError("Content-Length 必须是非负整数") from exc
            if content_length < 0:
                raise ValueError("Content-Length 必须是非负整数")
            if content_length > MAX_JSON_BODY_BYTES:
                raise RequestBodyTooLargeError("请求正文不能超过 1 MiB")
            body = self.rfile.read(content_length) if content_length else b"{}"
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ValueError("请求正文必须是有效的 JSON 对象") from exc
            if not isinstance(payload, dict):
                raise ValueError("请求正文必须是有效的 JSON 对象")
            return payload

        def _read_pdf_body(self) -> bytes:
            if (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower() != "application/pdf":
                raise ValueError("请求正文必须是 PDF")
            raw_content_length = self.headers.get("Content-Length")
            if raw_content_length is None:
                raise ValueError("Content-Length 必须是非负整数")
            try:
                content_length = int(raw_content_length)
            except ValueError as exc:
                raise ValueError("Content-Length 必须是非负整数") from exc
            if content_length < 0:
                raise ValueError("Content-Length 必须是非负整数")
            if content_length > MAX_PDF_BODY_BYTES:
                raise RequestBodyTooLargeError("PDF 不能超过 20 MiB")
            body = self.rfile.read(content_length)
            if not body.startswith(b"%PDF-"):
                raise ValueError("请求正文必须是有效的 PDF")
            return body

        def _research_chat_session_id(self, path: str) -> str | None:
            parts = path.strip("/").split("/")
            if len(parts) == 4 and parts[:3] == ["api", "research-chat", "sessions"]:
                return parts[3]
            return None

        def _research_chat_session_action(self, path: str) -> tuple[str, str] | None:
            parts = path.strip("/").split("/")
            if (
                len(parts) == 5
                and parts[:3] == ["api", "research-chat", "sessions"]
                and parts[3]
                and parts[4] in {"messages", "finalize"}
            ):
                return parts[3], parts[4]
            return None

        def _send_json(
            self,
            payload: Any,
            status: HTTPStatus = HTTPStatus.OK,
        ) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                return

        def _send_error_json(
            self, error: Exception, status: HTTPStatus | None = None
        ) -> None:
            if status is None:
                status = HTTPStatus.INTERNAL_SERVER_ERROR
                if isinstance(error, RequestBodyTooLargeError):
                    status = HTTPStatus.REQUEST_ENTITY_TOO_LARGE
                elif isinstance(error, PermissionError):
                    status = HTTPStatus.FORBIDDEN
                elif isinstance(error, ValueError):
                    status = HTTPStatus.BAD_REQUEST
                elif isinstance(error, StandardBacktestExecutionError):
                    status = HTTPStatus.BAD_GATEWAY
            self._send_json(
                {
                    "status": "error",
                    "error_type": type(error).__name__,
                    "message": str(error),
                },
                status=status,
            )

        def _send_file(self, path: Path, content_type: str) -> None:
            if not path.is_file():
                self._send_not_found()
                return

            body = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_not_found(self) -> None:
            body = b"not found"
            self.send_response(HTTPStatus.NOT_FOUND)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return ThreadingHTTPServer((host, port), DashboardRequestHandler)


def _is_loopback_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_loopback


def _require_loopback_host(value: str) -> None:
    if value == "localhost":
        return
    try:
        if _is_loopback_address(value):
            return
    except ValueError:
        pass
    raise ValueError("dashboard dual runtime requires a loopback host")


def _dashboard_runtime_metadata() -> dict[str, object]:
    cwd = Path.cwd().resolve()
    try:
        git_sha = subprocess.check_output(
            ["git", "-C", str(cwd), "rev-parse", "HEAD"], text=True
        ).strip()
        source_status = subprocess.check_output(
            [
                "git", "-C", str(cwd), "status", "--porcelain",
                "--untracked-files=all", "--", "src/open_trader",
            ],
            text=True,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        git_sha = ""
        source_status = "unavailable"
    return {
        "pid": os.getpid(),
        "started_at": datetime.now().astimezone().isoformat(),
        "cwd": str(cwd),
        "git_sha": git_sha,
        "source_state": "clean" if not source_status else "dirty",
    }


class _UnavailableCrossVenueMonitor:
    def __init__(self, reason: str) -> None:
        self._reason = reason

    def snapshot(self) -> dict[str, object]:
        return {
            "status": "degraded",
            "mode": "observe_only",
            "reason": self._reason,
            "funnel": {},
            "events": [],
            "opportunities": [],
        }


class _CrossVenueRuntime:
    def __init__(self, monitor: PredictCrossVenueMonitor) -> None:
        self._monitor = monitor
        self._predict = getattr(monitor, "_predict", None)
        self._started = threading.Event()
        self._stop_requested = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self) -> None:
        if self._thread is not None:
            return

        async def run() -> None:
            self._loop = asyncio.get_running_loop()
            try:
                try:
                    result = self._monitor.start()
                    if inspect.isawaitable(result):
                        await result
                finally:
                    self._started.set()
                await asyncio.to_thread(self._stop_requested.wait)
                result = self._monitor.stop()
                if inspect.isawaitable(result):
                    await result
            finally:
                self._loop = None

        self._thread = threading.Thread(
            target=lambda: asyncio.run(run()),
            name="predict-cross-venue-monitor",
            daemon=True,
        )
        self._thread.start()
        self._started.wait(timeout=5)

    def snapshot(self) -> dict[str, object]:
        loop = self._loop
        if (
            loop is None
            or loop.is_closed()
            or self._thread is threading.current_thread()
        ):
            return self._monitor.snapshot()
        coroutine = self._snapshot_on_loop()
        try:
            future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        except Exception:
            coroutine.close()
            return {}
        try:
            return future.result(timeout=1)
        except Exception:
            return {}

    async def _snapshot_on_loop(self) -> dict[str, object]:
        return self._monitor.snapshot()

    def refresh_opportunity(self, opportunity_id: str) -> dict[str, object] | None:
        loop = self._loop
        if (
            loop is None
            or loop.is_closed()
            or self._thread is threading.current_thread()
        ):
            return None
        coroutine = self._refresh_on_loop(opportunity_id)
        try:
            future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        except Exception:
            coroutine.close()
            return None
        try:
            return future.result(timeout=15)
        except Exception:
            return None

    async def _refresh_on_loop(
        self, opportunity_id: str
    ) -> dict[str, object] | None:
        refresh = getattr(self._monitor, "refresh_opportunity", None)
        if not callable(refresh):
            return None
        value = refresh(opportunity_id)
        if inspect.isawaitable(value):
            value = await value
        return dict(value) if isinstance(value, Mapping) else None

    def stop(self) -> None:
        self._stop_requested.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)


def _cross_venue_gamma_lookup(
    condition_ids: tuple[str, ...], *, closed: bool
) -> tuple[object, ...]:
    from polymarket import PublicClient

    client = PublicClient()
    try:
        paginator = client.list_markets(condition_ids=condition_ids, closed=closed)
        iter_items = getattr(paginator, "iter_items", None)
        return tuple(iter_items()) if callable(iter_items) else tuple(paginator)
    finally:
        client.close()


def _cross_venue_clob_lookup(condition_id: str) -> object:
    request = Request(
        f"https://clob.polymarket.com/markets/{quote(condition_id, safe='')}",
        headers={"User-Agent": "OpenTrader/1.0"},
    )
    with urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _build_cross_venue_monitor(
    *,
    trading_config: object,
    prediction_monitor: PolymarketMonitor,
    store: PredictionArbitrageStore,
    execution: PredictionExecutionService,
    codex_model: str,
    predict_trading: object | None = None,
) -> PredictCrossVenueMonitor | _UnavailableCrossVenueMonitor:
    predict_config = getattr(trading_config, "predict", None)
    if predict_config is None:
        return _UnavailableCrossVenueMonitor("predict_not_configured")
    if predict_trading is None:
        return _UnavailableCrossVenueMonitor("predict_construction_failed")
    try:
        return PredictCrossVenueMonitor(
            predict_source=PredictSource(predict_config),
            polymarket_monitor=prediction_monitor,
            validator=CodexCrossVenueEquivalenceValidator(store, model=codex_model),
            gamma_lookup=_cross_venue_gamma_lookup,
            clob_lookup=_cross_venue_clob_lookup,
            predict_quote_fn=getattr(predict_trading, "quote_market_buy", None),
            store=store,
            ready_observer=execution.notify_ready_opportunity,
            holding_reconciler=execution.reconcile_cross_holdings_once,
            execution_mode=validate_cross_execution_mode(
                os.environ.get("OPEN_TRADER_CROSS_EXECUTION_MODE")
            ),
        )
    except Exception:
        return _UnavailableCrossVenueMonitor("predict_construction_failed")


def serve_dashboard(
    config: DashboardConfig,
    *,
    host: str,
    port: int,
    eastmoney_password: str = "",
    prediction_notifier: object | None = None,
    public_url: str = "",
    cross_venue_monitor: PredictCrossVenueMonitor | None = None,
) -> None:
    if public_url.strip():
        _require_loopback_host(host)
    resolved_public_url = public_url.strip() or f"http://{host}:{port}/"
    if not resolved_public_url.endswith("/"):
        resolved_public_url += "/"
    runtime_metadata = _dashboard_runtime_metadata()
    print(f"dashboard_runtime: {json.dumps(runtime_metadata)}", flush=True)
    trend_simulate_position_service = TrendSimulatePositionService(
        host=config.futu_host,
        port=config.futu_port,
        account_ids={
            "eastmoney": config.trend_review_cn_simulate_acc_id,
            "tiger": config.trend_review_us_simulate_acc_id,
            "phillips": config.trend_review_hk_simulate_acc_id,
        },
        fx_to_hkd=DETAIL_FX_TO_HKD,
        data_dir=config.data_dir,
        reports_dir=config.reports_dir,
    )
    trend_simulate_position_service.prewarm()
    prediction_store: PredictionArbitrageStore | None = None
    prediction_monitor: object | None = None
    prediction_execution: object | None = None
    prediction_trading: object | None = None
    predict_trading: object | None = None
    cross_runtime: _CrossVenueRuntime | None = None
    if config.prediction_config_path is not None:
        prediction_path = config.prediction_config_path.expanduser()
        try:
            prediction_store = PredictionArbitrageStore(config.data_dir)
            trading_config = load_trading_config(prediction_path)
            prediction_trading = PolymarketTradingClient.from_keychain(trading_config)
            try:
                predict_trading = PredictTradingClient.from_keychain(trading_config)
            except Exception:
                predict_trading = None
            codex_model = os.environ.get("OPEN_TRADER_CODEX_MODEL", "gpt-5.6-sol").strip()
            relation_validator = CodexRelationValidator(
                prediction_store,
                model=codex_model,
            )
            title_translator = CodexTitleTranslator(prediction_store)
            prediction_monitor = PolymarketMonitor(
                store=prediction_store,
                trading=prediction_trading,
                relation_discovery=discover_threshold_relation_catalog,
                relation_validator=relation_validator,
                title_translator=title_translator,
            )
            prediction_execution = PredictionExecutionService(
                store=prediction_store,
                monitor=prediction_monitor,
                trading=prediction_trading,
                notifier=prediction_notifier or NullNotifier(),
                lock_path=config.data_dir / "prediction_arbitrage" / "execution.lock",
                dashboard_url=resolved_public_url,
                predict_trading=predict_trading,
            )
            prediction_monitor.set_ready_observer(
                prediction_execution.notify_ready_opportunity
            )
            prediction_monitor.set_failure_observer(
                prediction_execution.notify_monitor_failure
            )
            if cross_venue_monitor is None:
                cross_venue_monitor = _build_cross_venue_monitor(
                    trading_config=trading_config,
                    prediction_monitor=prediction_monitor,
                    store=prediction_store,
                    execution=prediction_execution,
                    codex_model=codex_model,
                    predict_trading=predict_trading,
                )
            if cross_venue_monitor is not None and not isinstance(
                cross_venue_monitor, _UnavailableCrossVenueMonitor
            ):
                cross_runtime = _CrossVenueRuntime(cross_venue_monitor)
            set_cross_venue_monitor = getattr(
                prediction_execution, "set_cross_venue_monitor", None
            )
            if callable(set_cross_venue_monitor):
                set_cross_venue_monitor(cross_runtime or cross_venue_monitor)
        except Exception:
            # A missing Keychain/config must leave a visible, schema-valid locked
            # Dashboard rather than aborting the existing portfolio surface.
            if prediction_monitor is not None:
                try:
                    prediction_monitor.stop()
                except Exception:
                    pass
            for resource in (prediction_execution, prediction_trading, predict_trading, prediction_store):
                close = getattr(resource, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass
            prediction_store = None
            prediction_monitor = None
            prediction_execution = None
            prediction_trading = None
            cross_venue_monitor = None
        else:
            try:
                # Reconcile authenticated state before any public monitor heartbeat.
                prediction_execution.reconcile_startup()
            except Exception:
                # Keep the service visible and locked so the state API can expose
                # the failed startup rather than silently dropping its ledger.
                pass
            else:
                prediction_monitor.start()
                if cross_runtime is not None:
                    cross_runtime.start()
    server = create_dashboard_server(
        config=config,
        host=host,
        port=port,
        trend_simulate_position_service=trend_simulate_position_service,
        eastmoney_password=eastmoney_password,
        prediction_store=prediction_store,
        prediction_monitor=prediction_monitor,
        cross_venue_monitor=cross_runtime or cross_venue_monitor,
        prediction_execution_service=prediction_execution,
        runtime_metadata=runtime_metadata,
    )
    _, actual_port = server.server_address
    try:
        print(f"dashboard_url: {resolved_public_url}", flush=True)
        print(f"portfolio: {config.portfolio_path}")
        print(f"futu: {config.futu_host}:{config.futu_port}")
        print(f"poll_seconds: {config.poll_seconds}")
        server.serve_forever()
    finally:
        if cross_runtime is not None:
            cross_runtime.stop()
        if prediction_monitor is not None:
            try:
                prediction_monitor.stop()
            except Exception:
                pass
        for resource in (prediction_execution, prediction_trading, predict_trading, prediction_store):
            close = getattr(resource, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        server.server_close()
