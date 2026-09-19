"""Serialized, server-owned execution of one prediction-market pair."""

from __future__ import annotations

import fcntl
import hashlib
import inspect
import importlib.metadata
import re
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from pathlib import Path
from typing import Any, Mapping

from .daily_premarket import send_notification_with_results
from .notifications import (
    Notifier,
    beijing_clock,
    render_prediction_opportunity_notification,
    render_yes_no_signal_notification,
)
from .polymarket_trading import (
    LegResult,
    PairSubmission,
    PolymarketTradingClient,
    ThresholdHedgeSubmission,
    ThresholdLegResult,
    _submit_error_detail,
)
from .polymarket_lp_risk import TERMINAL_ORDER_STATES, evaluate_lp_exposure
from .prediction_arbitrage import (
    MAX_CROSS_UNSETTLED_PRINCIPAL,
    MAX_EMERGENCY_LOSS,
    MAX_NORMAL_COST,
    MAX_WALLET_BALANCE,
    MIN_ESTIMATED_PROFIT,
    MIN_THRESHOLD_ANNUALIZED_YIELD,
    PROTECTED_BUY_SHARE_PRECISION,
    PairIntent,
    ThresholdHedgeIntent,
    ThresholdHedgeLeg,
    protected_buy_quantity,
)
from .predict_cross_venue import (
    CrossVenueIntent,
    CrossVenueLeg,
    canonical_cutoff_is_future,
    cross_venue_notification_dedupe_identity,
    parse_canonical_cutoff,
    validate_cross_execution_mode,
)
from .predict_trading import PREDICT_BASE_UNITS
from .prediction_arbitrage_store import PredictionArbitrageStore
from .prediction_title_translation import cached_prediction_title_zh
from .validation_eat_policy import should_eat as _validation_should_eat


PREVIEW_TTL = timedelta(seconds=10)
LP_REWARD_PERCENTAGE_CACHE_SECONDS = 10.0
LP_REWARD_SHARE_STALE_SECONDS = 30.0
LP_SHARE_WATCH_DURATION_SECONDS = 60.0
LP_REWARD_REFERENCE_PERCENTAGE = Decimal("5")
LP_REWARD_SHARE_TARGET_MAX = Decimal("8")
_LP_REWARD_CACHE_SECONDS = 60.0
_LP_TRADES_CACHE_SECONDS = 60.0

_THRESHOLD_ERROR_HINTS = {
    "auth": "签名或钱包身份校验未通过",
    "geoblock_blocked": "当前网络被地区限制拦截",
    "preflight_required": "提交前预检未通过",
    "preflight_failed": "提交前预检未通过",
    "rejected": "订单被交易场所拒绝",
    "order_amount_mismatch": "下单数量与预期不一致",
    "order_shape_mismatch": "签名订单结构与预期不一致",
    "account_insufficient": "账户余额或授权额度不足",
    "invalid": "订单参数校验失败",
    "network": "网络请求失败",
    "timeout": "请求超时",
    "unavailable": "服务不可用",
    "sdk_error": "SDK 调用失败",
    "signing": "订单签名失败",
    "opportunity_unavailable": "机会已失效或不可用",
    "rule_hash_changed": "规则指纹已变化，机会不再一致",
    "cache_fingerprint_changed": "市场快照指纹已变化",
    "threshold_preflight_unavailable": "预检通道不可用",
    "threshold_submission_unavailable": "提交通道不可用",
}


def _threshold_error_hint(code: str) -> str:
    hint = _THRESHOLD_ERROR_HINTS.get(str(code).strip())
    return hint if hint is not None else "详见执行日志"


_THRESHOLD_EVIDENCE_LABELS = (
    ("signer_match", "签名者"),
    ("wallet_match", "钱包"),
    ("account_reads", "账户读取"),
    ("geoblock", "地区限制"),
    ("fok_pair_signed_not_submitted", "FOK 双腿签名"),
    ("equal_requested_shares", "数量一致"),
)

_PREFLIGHT_EVIDENCE_PASS_VALUES = frozenset({"yes", "pass", "allowed"})


def _preflight_evidence_mark(value: object) -> str:
    text = str(value).strip().lower()
    if text in _PREFLIGHT_EVIDENCE_PASS_VALUES:
        return "✅"
    return f"❌（{value}）"


BOOK_FRESHNESS_SECONDS = Decimal("10")
LP_OBSERVATION_FRESHNESS_SECONDS = Decimal("120")
MAX_RECONCILIATION_SECONDS = 30
TERMINAL_STATES = {
    "both_rejected",
    "complete",
    "holding_to_resolution",
    "neutralized_incident",
    "directional_incident",
    "merge_incident",
}
_PROCESS_LOCK = threading.Lock()
ExecutionIntent = PairIntent | ThresholdHedgeIntent | CrossVenueIntent


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _canonical_decimal(value: Decimal) -> Decimal:
    """Drop insignificant trailing zeros without exponent notation (issue #140).

    Observation decimals persist through the JSON store, so the same numeric
    value must not surface as both "0.25" and "0.2500" depending on operand
    shapes.  Canonical form keeps the numeric semantics untouched.
    """

    normalized = value.normalize()
    if normalized == normalized.to_integral_value():
        return normalized.quantize(Decimal("1"))
    return normalized


def _lp_min_scoring_size(
    minimum_order_size: Decimal | None, reward_min_size: Decimal | None
) -> Decimal | None:
    """Smallest quantity that can score rewards, legalised to the 0.01 grid."""

    if (
        minimum_order_size is None
        or reward_min_size is None
        or minimum_order_size <= 0
        or reward_min_size <= 0
    ):
        return None
    quantity = max(minimum_order_size, reward_min_size)
    return (quantity / Decimal("0.01")).to_integral_value(
        rounding=ROUND_CEILING
    ) * Decimal("0.01")


def _lp_order_purpose(
    side: object, quantity: Decimal | None, min_scoring_size: Decimal | None
) -> str | None:
    """Derive the order purpose chip with zero storage.

    A BUY whose original size equals the market's minimum scoring quantity is
    a trial order; a larger BUY is a formal order.  SELL orders and markets
    with unknown rules stay unlabelled instead of guessing.
    """

    if str(side or "").strip().upper() != "BUY":
        return None
    if quantity is None or min_scoring_size is None or min_scoring_size <= 0:
        return None
    if quantity == min_scoring_size:
        return "trial"
    if quantity > min_scoring_size:
        return "formal"
    return None


def _normalize_lp_recommendations(value: object) -> list[dict[str, object]]:
    """Copy candidate rows and attach the existing reference reward fields."""

    if not isinstance(value, (list, tuple)):
        return []
    normalized: list[dict[str, object]] = []
    for row in value:
        if not isinstance(row, Mapping):
            continue
        recommendation = dict(row)
        recommendation["reference_share_percentage"] = LP_REWARD_REFERENCE_PERCENTAGE
        pool = _decimal(recommendation.get("daily_pool_usd"))
        recommendation["reference_daily_reward_usd"] = (
            None
            if pool is None or pool < Decimal("0")
            else pool * LP_REWARD_REFERENCE_PERCENTAGE / Decimal("100")
        )
        normalized.append(recommendation)
    return normalized


def _safe_decimal(value: object) -> str | None:
    parsed = _decimal(value)
    return None if parsed is None else format(parsed, "f")


def _age_seconds(value: object) -> float | None:
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max(0.0, (_utc_now() - moment.astimezone(UTC)).total_seconds())


def _lp_share_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _lp_share_severity(value: Decimal) -> str:
    if value > LP_REWARD_SHARE_TARGET_MAX:
        return "warning"
    return "normal"


def _lp_share_target(value: Decimal) -> tuple[str, Decimal]:
    if value < LP_REWARD_REFERENCE_PERCENTAGE:
        return "space", LP_REWARD_REFERENCE_PERCENTAGE - value
    if value > LP_REWARD_SHARE_TARGET_MAX:
        return "excess", value - LP_REWARD_SHARE_TARGET_MAX
    return "target", Decimal("0")


def _lp_reward_record_today(entry: object, reward_date: str) -> bool:
    """True only when a market carries actual reward evidence for the date."""

    if not isinstance(entry, Mapping) or str(entry.get("reward_date") or "") != reward_date:
        return False
    if entry.get("state") == "known":
        return True
    if _decimal(entry.get("market_amount")) is not None:
        return True
    if _decimal(entry.get("market_amount_raw")) is not None:
        return True
    accruals = entry.get("market_accruals_raw")
    return isinstance(accruals, (list, tuple)) and bool(accruals)


def _lp_market_reward_known(observation: object) -> bool:
    """Market is paying rewards today per a complete stored observation."""

    return (
        isinstance(observation, Mapping) and observation.get("state") == "known"
    )


def _lp_market_reward_missing(
    observation: object,
    *,
    day_start: datetime,
    day_end: datetime,
) -> bool:
    """Complete reward-rate read found no reward market for the day.

    The negative signal only counts when the observation was read inside the
    current reward day (its ``checked_at``/``last_success_at`` falls on
    ``[day_start, day_end)``); an older or undated read is unknown and must
    fail open instead of excluding today's LP rows.
    """

    if not (
        isinstance(observation, Mapping)
        and observation.get("stale") is not True
        and observation.get("reason") == "reward_market_missing"
    ):
        return False
    for key in ("checked_at", "last_success_at"):
        moment = _aware_datetime(observation.get(key))
        if moment is not None and day_start <= moment < day_end:
            return True
    return False


def _lp_aggregate_fills_by_order(
    trades_by_market: Mapping[str, object],
    *,
    day_start: datetime,
) -> dict[str, dict[str, object]]:
    """Group one reward day's account fills by our order id.

    The trades adapter keeps only maker orders provably ours, so maker fills
    attribute ``matched_amount`` to each remaining maker order; only a trade
    we placed as taker attributes its size to ``taker_order_id`` — when we
    are the maker that id belongs to the counterparty and must never count.
    Fills whose ``matched_at`` precedes ``day_start`` (the reward day's UTC
    00:00, i.e. Beijing 08:00) do not participate in the aggregation.
    """

    fills: dict[str, dict[str, object]] = {}
    for raw_condition, market_trades in trades_by_market.items():
        condition_id = str(raw_condition or "")
        if not condition_id or not isinstance(market_trades, (list, tuple)):
            continue
        for trade in market_trades:
            if not isinstance(trade, Mapping):
                continue
            # FAILED trades never land on chain and must never become shown
            # volume; landed-ness of the remaining statuses is decided by the
            # settlement precedents upstream, not here.
            if str(trade.get("status") or "").upper() == "FAILED":
                continue
            matched_at = _aware_datetime(trade.get("matched_at"))
            if matched_at is None or matched_at < day_start:
                continue
            size = _decimal(trade.get("size"))
            if size is None or size <= 0:
                continue
            price = _decimal(trade.get("price"))
            trader_side = str(trade.get("trader_side") or "").upper()
            taker_order_id = str(trade.get("taker_order_id") or "")
            contributions: list[tuple[str, Decimal, str]] = []
            if trader_side == "TAKER":
                if taker_order_id:
                    contributions.append(
                        (taker_order_id, size, str(trade.get("side") or "").upper())
                    )
            else:
                makers = trade.get("maker_orders")
                for maker in makers if isinstance(makers, (list, tuple)) else ():
                    if not isinstance(maker, Mapping):
                        continue
                    maker_id = str(maker.get("order_id") or "")
                    if not maker_id:
                        continue
                    amount = _decimal(maker.get("matched_amount"))
                    if amount is None or amount <= 0:
                        amount = size
                    contributions.append(
                        (
                            maker_id,
                            amount,
                            str(maker.get("side") or "").upper(),
                        )
                    )
            for order_id, amount, side in contributions:
                entry = fills.get(order_id)
                if entry is None:
                    entry = {
                        "condition_id": condition_id,
                        "token_id": str(trade.get("token_id") or ""),
                        "side": side or None,
                        "filled_quantity": Decimal("0"),
                        "notional": Decimal("0"),
                        "priced": False,
                        "last_fill": matched_at,
                    }
                    fills[order_id] = entry
                entry["filled_quantity"] = entry["filled_quantity"] + amount
                if price is not None:
                    entry["notional"] = entry["notional"] + amount * price
                    entry["priced"] = True
                if side:
                    entry["side"] = side
                if matched_at > entry["last_fill"]:
                    entry["last_fill"] = matched_at
    result: dict[str, dict[str, object]] = {}
    for order_id, entry in fills.items():
        filled_quantity = entry["filled_quantity"]
        result[order_id] = {
            "order_id": order_id,
            "condition_id": entry["condition_id"],
            "token_id": entry["token_id"],
            "side": entry["side"],
            "price": (
                entry["notional"] / filled_quantity
                if entry["priced"] and filled_quantity > 0
                else None
            ),
            "filled_quantity": filled_quantity,
            "last_fill_at": _timestamp(entry["last_fill"]),
        }
    return result

def _aware_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if moment.tzinfo is None:
        return None
    return moment.astimezone(UTC)


def _lp_exposure_signature(
    orders: object,
    positions: object,
    condition_id: str,
) -> str:
    """Hash the current market rows that determine measured LP exposure."""

    facts: list[tuple[str, ...]] = []
    for row in orders if isinstance(orders, (list, tuple)) else ():
        if not isinstance(row, Mapping) or str(row.get("condition_id") or "") != condition_id:
            continue
        facts.append(
            (
                "order",
                str(row.get("token_id") or ""),
                str(row.get("outcome") or ""),
                str(row.get("side") or ""),
                str(row.get("status") or ""),
                _safe_decimal(row.get("price")) or "",
                _safe_decimal(row.get("quantity")) or "",
                _safe_decimal(row.get("filled_quantity")) or "",
                _safe_decimal(row.get("remaining_quantity")) or "",
            )
        )
    for row in positions if isinstance(positions, (list, tuple)) else ():
        if not isinstance(row, Mapping) or str(row.get("condition_id") or "") != condition_id:
            continue
        facts.append(
            (
                "position",
                str(row.get("token_id") or ""),
                str(row.get("outcome") or ""),
                _safe_decimal(row.get("size")) or "",
                _safe_decimal(row.get("average_price")) or "",
            )
        )
    encoded = repr(sorted(facts, key=repr)).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _mask_lp_observation(
    saved: Mapping[str, object],
    reason: str,
) -> dict[str, object]:
    """Hide current measurements while retaining trial and alert history."""

    return {
        **saved,
        "state": "unknown",
        "stale": True,
        "reason": reason,
        "current_hourly_reward_usd": None,
        "occupied_capital_usd": None,
        "exposure_quantity": None,
        "current_yield_pct_per_hour": None,
        "qualified": None,
        "qualification_basis": None,
        "risk_state": "unknown",
        "risk_warning": None,
        "risk_directions": [],
        "add_room": {"available": False, "reason": reason},
    }


def _project_lp_observations(
    saved_observations: object,
    *,
    orders: object,
    positions: object,
    account_checked_at: object,
    account_complete: bool,
    now: datetime,
) -> dict[str, dict[str, object]]:
    if not isinstance(saved_observations, Mapping):
        return {}
    account_moment = _aware_datetime(account_checked_at)
    account_age = (
        Decimal(str((now - account_moment).total_seconds()))
        if account_moment is not None
        else None
    )
    account_fresh = (
        account_complete
        and account_age is not None
        and Decimal("0") <= account_age <= LP_OBSERVATION_FRESHNESS_SECONDS
    )
    projected: dict[str, dict[str, object]] = {}
    for raw_condition_id, saved in saved_observations.items():
        condition_id = str(raw_condition_id)
        if not isinstance(saved, Mapping):
            continue
        reason: str | None = None
        if not account_fresh:
            reason = "account_facts_stale"
        else:
            observed_at = _aware_datetime(saved.get("checked_at"))
            if observed_at is None:
                reason = "observation_stale"
            else:
                observation_age = Decimal(str((now - observed_at).total_seconds()))
                if (
                    observation_age < 0
                    or observation_age > LP_OBSERVATION_FRESHNESS_SECONDS
                ):
                    reason = "observation_stale"
            if reason is None:
                expected_signature = _lp_exposure_signature(
                    orders, positions, condition_id
                )
                if saved.get("exposure_fingerprint") != expected_signature:
                    reason = "exposure_changed"
        projected[condition_id] = (
            _mask_lp_observation(saved, reason)
            if reason is not None
            else dict(saved)
        )
    return projected


def _normalize_predict_account_snapshot(
    snapshot: Mapping[str, object],
) -> dict[str, object] | None:
    normalized = dict(snapshot)
    required = (
        "wallet_address",
        "available_usdt",
        "available_usdt_raw",
        "open_orders",
        "positions",
        "checked_at",
    )
    if not all(name in normalized for name in required):
        return None
    has_current = all(
        name in normalized
        for name in (
            "allowance",
            "allowance_raw",
            "scope_ready",
            "gas_ready",
            "allowance_breaker",
        )
    )
    if has_current:
        if (
            not isinstance(normalized.get("scope_ready"), bool)
            or not isinstance(normalized.get("gas_ready"), bool)
            or not isinstance(normalized.get("allowance_breaker"), bool)
            or normalized.get("allowance") in (None, "")
            or normalized.get("allowance_raw") in (None, "")
        ):
            return None
        return normalized
    if "allowance_ready" not in normalized:
        return None
    ready = normalized.get("allowance_ready") is True
    normalized["allowance"] = "0" if ready else ""
    normalized["allowance_raw"] = "0" if ready else ""
    normalized["scope_ready"] = ready
    normalized["gas_ready"] = ready
    normalized["allowance_breaker"] = not ready
    return normalized


def _predict_account_snapshot_ready(snapshot: Mapping[str, object]) -> bool:
    return (
        snapshot.get("scope_ready") is True
        and snapshot.get("gas_ready") is True
        and snapshot.get("allowance_breaker") is False
        and snapshot.get("allowance") not in (None, "")
        and snapshot.get("available_usdt") not in (None, "")
    )


def _call(method: object, *args: object, **kwargs: object) -> object:
    if not callable(method):
        return None
    # Inspect the callable before invoking it.  Retrying a TypeError from a
    # mutation-capable method could submit a second batch after a POST began.
    if kwargs:
        try:
            signature = inspect.signature(method)
            accepts_kwargs = any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in signature.parameters.values()
            )
            if not accepts_kwargs:
                kwargs = {
                    name: value
                    for name, value in kwargs.items()
                    if name in signature.parameters
                    and signature.parameters[name].kind
                    is not inspect.Parameter.POSITIONAL_ONLY
                }
        except (TypeError, ValueError):
            pass
    value = method(*args, **kwargs)
    if inspect.isawaitable(value):
        raise TypeError("execution collaborators must be synchronous")
    return value


class PredictionExecutionService:
    """One process/file-serialized pair execution state machine."""

    def __init__(
        self,
        *,
        store: PredictionArbitrageStore,
        monitor: object,
        trading: object,
        notifier: Notifier,
        lock_path: Path,
        dashboard_url: str = "http://127.0.0.1:8766/",
        predict_trading: object | None = None,
        legacy_retired: bool = False,
        lp: object | None = None,
    ) -> None:
        self._store = store
        self._monitor = monitor
        self._trading = trading
        self._predict_trading = predict_trading
        # The LP session uses the same durable store and process/file mutex as
        # the existing execution path.  Keep the collaborator optional so
        # shadow and legacy test fixtures retain their read-only surface.
        self._lp = lp
        self._lp_dashboard_lock = threading.RLock()
        self._lp_reward_percentage_lock = threading.Lock()
        self._lp_share_watch_notification_lock = threading.Lock()
        self._lp_share_watch_seen: set[str] = set()
        self._lp_dashboard_cache: dict[str, object] | None = None
        self._lp_reward_percentage_cache: dict[str, object] | None = None
        self._lp_reward_share_observations: dict[str, dict[str, object]] = {}
        self._lp_reward_share_active: dict[str, bool] = {}
        self._lp_reward_cache: dict[tuple[str, str], dict[str, object]] = {}
        self._lp_reward_cached_at: dict[tuple[str, str], float] = {}
        self._lp_reward_refresh_pending: dict[str, set[str]] = {}
        self._lp_reward_refresh_active: set[tuple[str, str]] = set()
        self._lp_reward_refresh_thread: threading.Thread | None = None
        # Issue #140: process-memory record of the last successful official
        # scoring read per order id, so a failed poll can still show when the
        # order was last confirmed scoring (or explicitly not scoring).
        self._lp_scoring_observations: dict[str, dict[str, str]] = {}
        # 当天 LP 委托：按奖励日缓存的成交聚合（我方订单 id → 汇总成交量），
        # 由后台刷新线程补全，lp_dashboard() 读取缓存做 fail-open 装配。
        self._lp_orders_today_fills: dict[str, dict[str, dict[str, object]]] = {}
        self._lp_orders_today_fills_complete: dict[str, bool] = {}
        self._lp_orders_today_trades_read_at: dict[tuple[str, str], float] = {}
        self._lp_orders_today_pending: dict[str, set[str]] = {}
        self._lp_orders_today_refresh_thread: threading.Thread | None = None
        self._cross_venue_monitor: object | None = None
        self._notifier = notifier
        self._lock_path = Path(lock_path)
        self._dashboard_url = str(dashboard_url)
        # Issue #60: once the reader fence reaches the N_LEG generation the
        # legacy cross-auto submit path is observe-only regardless of the
        # stored configured mode / armed flag.
        self._legacy_strategy_retired = bool(legacy_retired)
        self._n_leg_paused = False
        self._process_lock = _PROCESS_LOCK
        # A newly constructed process has not reconciled its dedicated wallet;
        # only a clean startup/reset path may clear this lock.
        self._breaker_open = True
        self._cross_breaker_open = False
        self._first_live_order_verified = False
        set_lp_guard = getattr(self._lp, "set_mutation_guard", None)
        if callable(set_lp_guard):
            set_lp_guard(self.lp_mutation_allowed)
        self._threads: dict[str, threading.Thread] = {}
        self._clock = time.monotonic
        self._sleep = time.sleep
        self._predict_snapshot_lock = threading.RLock()
        self._predict_snapshot_cache: dict[str, object] | None = None
        self._last_zero_landing_summary: dict[str, object] | None = None

    def lp_preview(self, request: Mapping[str, object]) -> dict[str, object]:
        """Run the LP read-only preflight through the production collaborator."""

        service = self._lp
        preview = getattr(service, "preview", None)
        if not callable(preview):
            return {"state": "rejected", "reason": "lp_unavailable"}
        return preview(request)

    def lp_candidate_preview(
        self, candidate: Mapping[str, object]
    ) -> dict[str, object]:
        """Create a candidate preview with price and size fixed by live facts."""

        service = self._lp
        preview = getattr(service, "preview_candidate", None)
        if not callable(preview):
            return {"state": "rejected", "reason": "lp_unavailable"}
        return preview(candidate)

    def lp_status(self, session_id: str | None = None) -> dict[str, object]:
        service = self._lp
        status = getattr(service, "status", None)
        if not callable(status):
            return {"state": "none", "session_id": None, "reason": "lp_unavailable"}
        return status(session_id)

    def _lp_account_id(self) -> str | None:
        config = getattr(self._trading, "config", None)
        wallet = getattr(config, "wallet_address", None)
        if not isinstance(wallet, str) or not wallet.strip():
            return None
        return hashlib.sha256(wallet.strip().casefold().encode("utf-8")).hexdigest()

    def lp_share_watch_state(self) -> dict[str, dict[str, object]]:
        account_id = self._lp_account_id()
        reader = getattr(self._store, "lp_observations", None)
        if account_id is None or not callable(reader):
            return {}
        try:
            observations = reader(account_id)
        except Exception:
            return {}
        result: dict[str, dict[str, object]] = {}
        if not isinstance(observations, Mapping):
            return result
        for condition_id, observation in observations.items():
            if not isinstance(observation, Mapping):
                continue
            share_alert = observation.get("share_alert")
            if isinstance(share_alert, Mapping):
                result[str(condition_id)] = dict(share_alert)
        return result

    def _lp_open_orders_snapshot(
        self, *, stop_event: threading.Event | None = None
    ) -> Mapping[str, object] | None:
        if stop_event is not None and stop_event.is_set():
            return None
        reader = getattr(self._trading, "lp_open_orders_snapshot", None)
        if callable(reader):
            try:
                result = _call(reader)
            except Exception:
                return None
            if stop_event is not None and stop_event.is_set():
                return None
            return result if isinstance(result, Mapping) else None
        # Compatibility for read-only collaborators that predate the narrow
        # adapter; the production client supplies the lightweight method above.
        reader = getattr(self._trading, "lp_account_snapshot", None)
        if not callable(reader):
            return None
        try:
            result = _call(reader)
        except Exception:
            return None
        if stop_event is not None and stop_event.is_set():
            return None
        if not isinstance(result, Mapping):
            return None
        return {
            "authenticated": result.get("authenticated") is True,
            "open_orders": result.get("open_orders", ()),
            "open_orders_complete": result.get("open_orders_complete") is True,
            "checked_at": result.get("checked_at"),
            "wallet_address": result.get("wallet_address"),
        }

    def _lp_open_orders_snapshot_valid(
        self, snapshot: object, *, now: datetime | None = None
    ) -> bool:
        if not isinstance(snapshot, Mapping):
            return False
        checked_at = _lp_share_datetime(snapshot.get("checked_at"))
        current = now or _utc_now()
        age = (
            (current - checked_at.astimezone(UTC)).total_seconds()
            if checked_at is not None
            else None
        )
        expected_wallet = getattr(self._trading, "wallet_address", None)
        if not isinstance(expected_wallet, str) or not expected_wallet.strip():
            config = getattr(self._trading, "config", None)
            expected_wallet = getattr(config, "wallet_address", None)
        snapshot_wallet = snapshot.get("wallet_address")
        wallet_matches = (
            snapshot_wallet is None
            or (
                isinstance(expected_wallet, str)
                and bool(expected_wallet.strip())
                and isinstance(snapshot_wallet, str)
                and bool(snapshot_wallet.strip())
                and snapshot_wallet.strip().casefold()
                == expected_wallet.strip().casefold()
            )
        )
        return (
            snapshot.get("authenticated") is True
            and snapshot.get("open_orders_complete") is True
            and isinstance(snapshot.get("open_orders"), (list, tuple))
            and checked_at is not None
            and age is not None
            and 0 <= age < LP_REWARD_SHARE_STALE_SECONDS
            and wallet_matches
        )

    @staticmethod
    def _lp_active_order_conditions(snapshot: Mapping[str, object]) -> set[str] | None:
        if (
            snapshot.get("authenticated") is not True
            or snapshot.get("open_orders_complete") is not True
            or not isinstance(snapshot.get("open_orders"), (list, tuple))
        ):
            return None
        active: set[str] = set()
        for order in snapshot["open_orders"]:
            if not isinstance(order, Mapping):
                return None
            condition_id = str(order.get("condition_id") or "").strip()
            if not condition_id:
                return None
            status = str(order.get("status") or "").upper()
            remaining = _decimal(order.get("remaining_quantity"))
            if remaining is None:
                remaining = _decimal(order.get("remaining_size"))
            if remaining is None:
                remaining = _decimal(order.get("size"))
            if remaining is None:
                original = _decimal(order.get("original_size"))
                matched = _decimal(order.get("size_matched"))
                if original is not None and matched is not None:
                    remaining = original - matched
            if remaining is None or remaining < 0:
                return None
            if status not in TERMINAL_ORDER_STATES and remaining > 0:
                active.add(condition_id)
        return active

    def set_lp_share_watch(
        self, condition_id: str, *, enabled: bool
    ) -> dict[str, object]:
        market = str(condition_id).strip()
        if not market:
            raise ValueError("condition_id is required")
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        account_id = self._lp_account_id()
        updater = getattr(self._store, "update_lp_observation", None)
        if account_id is None or not callable(updater):
            raise RuntimeError("LP share watch persistence is unavailable")
        existing = self.lp_share_watch_state().get(market, {})
        if not enabled:
            saved = updater(
                account_id,
                market,
                {
                    "share_alert": {
                        "enabled": False,
                        "paused": False,
                        "breach_started_at": None,
                        "notification_pending": False,
                    }
                },
            )
            return {
                "state": "disabled",
                "condition_id": market,
                "enabled": False,
                "share_alert": saved.get("share_alert", {}),
            }
        snapshot = self._lp_open_orders_snapshot()
        active_conditions = (
            None
            if not self._lp_open_orders_snapshot_valid(snapshot)
            else self._lp_active_order_conditions(snapshot)
        )
        if active_conditions is None:
            return {
                "state": "rejected",
                "reason": "account_facts_unknown",
                "condition_id": market,
                "enabled": False,
            }
        if market not in active_conditions:
            return {
                "state": "rejected",
                "reason": "no_active_orders",
                "condition_id": market,
                "enabled": False,
            }
        title = existing.get("market_title")
        observations_reader = getattr(self._store, "lp_observations", None)
        if callable(observations_reader):
            try:
                observations = observations_reader(account_id)
            except Exception:
                observations = {}
            observation = observations.get(market) if isinstance(observations, Mapping) else None
            if isinstance(observation, Mapping):
                root_title = observation.get("market_title")
                if isinstance(root_title, str) and root_title.strip():
                    title = root_title.strip()
        for order in snapshot.get("open_orders", ()):
            if isinstance(order, Mapping) and str(order.get("condition_id") or "").strip() == market:
                title = order.get("market_title") or order.get("title") or title
                break
        # Repeating an already-enabled selection is an idempotent no-op.  In
        # particular, do not clear a breach start or pending delivery and
        # accidentally extend the continuous-breach window.
        share_update = {
            "enabled": True,
            "paused": False,
            "market_title": title or market,
        }
        if existing.get("enabled") is not True:
            share_update.update(
                {
                    "breach_started_at": None,
                    "notification_pending": False,
                }
            )
        saved = updater(
            account_id,
            market,
            {"share_alert": share_update},
        )
        return {
            "state": "enabled",
            "condition_id": market,
            "enabled": True,
            "share_alert": saved.get("share_alert", {}),
        }

    def refresh_lp_share_watch(
        self, *, stop_event: threading.Event | None = None
    ) -> dict[str, object]:
        """Refresh selected LP share watches and deliver Xiaoai alerts."""

        account_id = self._lp_account_id()
        observations_reader = getattr(self._store, "lp_observations", None)
        update_observation = getattr(self._store, "update_lp_observation", None)
        if (
            account_id is None
            or not callable(observations_reader)
            or not callable(update_observation)
        ):
            return {
                "state": "unknown",
                "reason": "lp_observation_store_unavailable",
                "observations": {},
            }
        try:
            saved_observations = observations_reader(account_id)
        except Exception:
            return {
                "state": "unknown",
                "reason": "lp_observation_store_unavailable",
                "observations": {},
            }
        if not isinstance(saved_observations, Mapping):
            saved_observations = {}
        selected = {
            str(condition_id): dict(alert)
            for condition_id, observation in saved_observations.items()
            if isinstance(observation, Mapping)
            and isinstance(observation.get("share_alert"), Mapping)
            and observation["share_alert"].get("enabled") is True
            for alert in (observation["share_alert"],)
        }
        if stop_event is not None and stop_event.is_set():
            return {"state": "cancelled", "observations": dict(saved_observations)}
        first_seen = {
            condition_id: condition_id not in self._lp_share_watch_seen
            for condition_id in selected
        }
        self._lp_share_watch_seen.update(selected)

        def current_alert(condition_id: str) -> dict[str, object]:
            current = self.lp_share_watch_state().get(condition_id, {})
            return dict(current) if isinstance(current, Mapping) else {}

        def save_watch_fields(
            condition_id: str, fields: Mapping[str, object]
        ) -> dict[str, object] | None:
            current = current_alert(condition_id)
            if current.get("enabled") is not True:
                return None
            return update_observation(
                account_id,
                condition_id,
                {"share_alert": dict(fields)},
            )

        now = _utc_now()
        if not selected:
            return {
                "state": "ready",
                "checked_at": _timestamp(now),
                "observations": dict(saved_observations),
            }

        snapshot = self._lp_open_orders_snapshot(stop_event=stop_event)
        # The authenticated reader stamps its result after the network call;
        # compare against a clock read taken after that external operation.
        now = _utc_now()
        snapshot_checked_at = (
            _lp_share_datetime(snapshot.get("checked_at"))
            if isinstance(snapshot, Mapping)
            else None
        )
        snapshot_valid = self._lp_open_orders_snapshot_valid(snapshot, now=now)
        active_conditions = (
            self._lp_active_order_conditions(snapshot)
            if snapshot_valid and isinstance(snapshot, Mapping)
            else None
        )
        if active_conditions is None:
            for condition_id in selected:
                save_watch_fields(
                    condition_id,
                    {
                        "paused": False,
                        "breach_started_at": None,
                        "notification_pending": False,
                        "last_attempt_at": _timestamp(now),
                    },
                )
            return {
                "state": "unknown",
                "reason": "account_facts_unknown",
                "checked_at": _timestamp(now),
                "observations": observations_reader(account_id),
            }

        active_selected = tuple(
            condition_id for condition_id in selected if condition_id in active_conditions
        )
        if not active_selected:
            for condition_id in selected:
                save_watch_fields(
                    condition_id,
                    {
                        "paused": True,
                        "breach_started_at": None,
                        "notification_pending": False,
                        "last_attempt_at": _timestamp(now),
                    },
                )
            return {
                "state": "ready",
                "checked_at": _timestamp(now),
                "observations": observations_reader(account_id),
            }

        if stop_event is not None and stop_event.is_set():
            return {"state": "cancelled", "observations": observations_reader(account_id)}
        projections = self._lp_reward_share_projection(active_selected)
        if stop_event is not None and stop_event.is_set():
            return {"state": "cancelled", "observations": observations_reader(account_id)}
        now = _utc_now()
        observation_titles: dict[str, str] = {}
        for condition_id, observation in saved_observations.items():
            if not isinstance(observation, Mapping):
                continue
            title = observation.get("market_title")
            if isinstance(title, str) and title.strip():
                observation_titles[str(condition_id)] = title.strip()
        order_titles: dict[str, str] = {}
        if isinstance(snapshot, Mapping):
            for raw_order in snapshot.get("open_orders", ()):
                if not isinstance(raw_order, Mapping):
                    continue
                condition_id = str(raw_order.get("condition_id") or "").strip()
                title = str(
                    raw_order.get("market_title")
                    or raw_order.get("title")
                    or ""
                ).strip()
                if condition_id and title and condition_id not in order_titles:
                    order_titles[condition_id] = title

        pending_notifications: list[tuple[str, str, str, str, datetime]] = []
        for condition_id in selected:
            alert = current_alert(condition_id)
            if alert.get("enabled") is not True:
                continue
            if condition_id not in active_conditions:
                save_watch_fields(
                    condition_id,
                    {
                        "paused": True,
                        "breach_started_at": None,
                        "notification_pending": False,
                        "last_attempt_at": _timestamp(now),
                    },
                )
                continue
            projection = projections.get(condition_id, {})
            percentage = _decimal(projection.get("percentage"))
            source_checked_at = _lp_share_datetime(projection.get("checked_at"))
            valid = (
                projection.get("state") == "known"
                and percentage is not None
                and source_checked_at is not None
            )
            if not valid:
                save_watch_fields(
                    condition_id,
                    {
                        "paused": False,
                        "breach_started_at": None,
                        "notification_pending": False,
                        "last_attempt_at": _timestamp(now),
                    },
                )
                continue
            assert percentage is not None
            source_text = _timestamp(source_checked_at)
            previous_source = _lp_share_datetime(alert.get("last_share_checked_at"))
            if previous_source is not None and source_checked_at < previous_source:
                save_watch_fields(
                    condition_id,
                    {
                        "paused": False,
                        "breach_started_at": None,
                        "notification_pending": False,
                        "last_attempt_at": _timestamp(now),
                    },
                )
                continue
            source_advanced = previous_source is None or source_checked_at > previous_source
            source_gap = (
                source_advanced
                and previous_source is not None
                and (source_checked_at - previous_source).total_seconds()
                >= LP_REWARD_SHARE_STALE_SECONDS
            )
            update: dict[str, object] = {
                "paused": False,
                "last_share_checked_at": source_text,
                "last_share_percentage": percentage,
                "market_title": order_titles.get(condition_id)
                or observation_titles.get(condition_id)
                or alert.get("market_title")
                or condition_id,
                "last_attempt_at": _timestamp(now),
            }
            if first_seen.get(condition_id):
                # An unfinished breach, including a pending unsent delivery,
                # belongs to the prior service process; restart must establish
                # a new source-timestamp window. A successful event remains
                # represented by notification_sent below.
                alert = {
                    **alert,
                    "breach_started_at": None,
                    "notification_pending": False,
                }
            if percentage <= LP_REWARD_SHARE_TARGET_MAX:
                update.update(
                    {
                        "breach_started_at": None,
                        "notification_pending": False,
                        "notification_sent": False,
                    }
                )
            else:
                breach_started_at = _lp_share_datetime(alert.get("breach_started_at"))
                if source_gap:
                    breach_started_at = None
                if breach_started_at is None:
                    breach_started_at = source_checked_at
                update["breach_started_at"] = _timestamp(breach_started_at)
                if alert.get("notification_sent") is True:
                    update["notification_pending"] = False
                elif source_advanced and (
                    source_checked_at - breach_started_at
                ).total_seconds() >= LP_SHARE_WATCH_DURATION_SECONDS:
                    update["notification_pending"] = True
                    title = "LP 份额预警"
                    market_title = str(
                        order_titles.get(condition_id)
                        or observation_titles.get(condition_id)
                        or alert.get("market_title")
                        or condition_id
                    )
                    message = "\n".join(
                        (
                            f"市场：{market_title}。",
                            f"当前奖励份额 {percentage.normalize():f}%，目标区间 5%–8%，已连续超过 8% 至少一分钟。",
                            f"数据时间：北京时间 {beijing_clock(source_checked_at, seconds=True) or '未知'}。",
                        )
                    )
                    pending_notifications.append(
                        (condition_id, title, message, source_text, source_checked_at)
                    )
            save_watch_fields(condition_id, update)

        for (
            condition_id,
            title,
            message,
            source_text,
            source_checked_at,
        ) in pending_notifications:
            if stop_event is not None and stop_event.is_set():
                break
            latest = self.lp_share_watch_state().get(condition_id, {})
            if (
                latest.get("enabled") is not True
                or latest.get("paused") is True
                or latest.get("notification_pending") is not True
                or latest.get("last_share_checked_at") != source_text
            ):
                continue
            with self._lp_share_watch_notification_lock:
                latest = self.lp_share_watch_state().get(condition_id, {})
                if (
                    latest.get("enabled") is not True
                    or latest.get("paused") is True
                    or latest.get("notification_pending") is not True
                    or latest.get("last_share_checked_at") != source_text
                ):
                    continue
                if stop_event is not None and stop_event.is_set():
                    break
                send_now = _utc_now()
                source_age = (
                    send_now - source_checked_at.astimezone(UTC)
                ).total_seconds()
                order_age = (
                    send_now - snapshot_checked_at.astimezone(UTC)
                ).total_seconds()
                if not (
                    0 <= source_age < LP_REWARD_SHARE_STALE_SECONDS
                    and 0 <= order_age < LP_REWARD_SHARE_STALE_SECONDS
                ):
                    save_watch_fields(
                        condition_id,
                        {
                            "paused": False,
                            "breach_started_at": None,
                            "notification_pending": False,
                            "last_attempt_at": _timestamp(send_now),
                        },
                    )
                    continue
                if stop_event is not None and stop_event.is_set():
                    break
                attempts = send_notification_with_results(
                    self._notifier,
                    title,
                    message,
                    channels={"xiaoai"},
                )
            succeeded = any(
                getattr(attempt, "channel", "") == "xiaoai"
                and getattr(attempt, "success", False) is True
                for attempt in attempts
            )
            attempt = next(
                (
                    attempt
                    for attempt in attempts
                    if getattr(attempt, "channel", "") == "xiaoai"
                ),
                None,
            )
            attempt_payload = {
                "success": succeeded,
                "suppressed": bool(
                    getattr(attempt, "suppressed", False) if attempt is not None else False
                ),
                "error_type": str(
                    getattr(attempt, "error_type", "not_attempted")
                    if attempt is not None
                    else "not_attempted"
                ),
                "error": str(getattr(attempt, "error", "") if attempt is not None else ""),
                    "checked_at": _timestamp(send_now),
                }
            save_watch_fields(
                condition_id,
                {
                    "notification_pending": not succeeded,
                    "notification_sent": succeeded
                    or latest.get("notification_sent") is True,
                    "notification_channels": {"xiaoai": attempt_payload},
                    "last_notification_attempt": attempt_payload,
                    "last_notification_attempt_at": _timestamp(send_now),
                    "last_notification_success_at": (
                        _timestamp(send_now)
                        if succeeded
                        else latest.get("last_notification_success_at")
                    ),
                },
            )
        return {
            "state": "ready",
            "checked_at": _timestamp(now),
            "observations": observations_reader(account_id),
        }

    def _update_lp_risk_alerts(
        self,
        *,
        condition_id: str,
        market_title: str,
        risk_directions: list[dict[str, object]],
        previous: object,
        now: datetime,
    ) -> dict[str, dict[str, object]]:
        timestamp = _timestamp(now)
        alerts = {
            str(token): dict(alert)
            for token, alert in previous.items()
            if isinstance(previous, Mapping) and isinstance(alert, Mapping)
        } if isinstance(previous, Mapping) else {}
        current_directions = {
            str(direction.get("outcome") or "未知方向")
            for direction in risk_directions
        }
        for outcome in set(alerts) - current_directions:
            if alerts[outcome].get("active") is True:
                alerts[outcome] = {
                    **alerts[outcome],
                    "active": False,
                    "recovered_at": timestamp,
                    "recovery_reason": "exposure_zero",
                }

        targets = getattr(self._notifier, "_notifiers", None)
        candidates = list(targets) if isinstance(targets, (list, tuple)) else [self._notifier]
        supported_channels = {"feishu", "feishu_app", "xiaoai"}
        configured = {
            channel
            for target in candidates
            if (channel := self._notification_channel(target)) in supported_channels
        }

        for direction in risk_directions:
            outcome = str(direction.get("outcome") or "未知方向")
            alert = alerts.get(outcome, {})
            if direction.get("state") != "known" or not isinstance(direction.get("warning"), bool):
                continue
            if direction.get("warning") is False:
                if alert.get("active") is True:
                    alerts[outcome] = {
                        **alert,
                        "active": False,
                        "recovered_at": timestamp,
                        "recovery_reason": "risk_below_threshold",
                    }
                continue

            is_new_event = alert.get("active") is not True
            channels_value = alert.get("channels")
            channels = (
                {
                    str(channel): dict(result)
                    for channel, result in channels_value.items()
                    if isinstance(result, Mapping)
                }
                if not is_new_event and isinstance(channels_value, Mapping)
                else {}
            )
            title = "LP 风险警告"
            loss_ratio = _decimal(direction.get("loss_ratio"))
            ratio_text = (
                format((loss_ratio * Decimal("100")).normalize(), "f")
                if loss_ratio is not None
                else "未知"
            )
            principal = _decimal(direction.get("risk_principal"))
            stress_loss = _decimal(direction.get("stress_loss"))
            principal_text = f"{principal:.2f}" if principal is not None else "未知"
            loss_text = f"{stress_loss:.2f}" if stress_loss is not None else "未知"
            order_note = (
                "未成交买单按假设成交计入。"
                if direction.get("unfilled_buy_orders") is True
                else "按当前已确认持仓及未成交买单计算。"
            )
            message = "\n".join(
                (
                    f"标的：{market_title}（{condition_id}），方向：{direction.get('outcome') or '未知'}。",
                    f"当前风险本金 {principal_text} 美元，预计压力退出损失 {loss_text} 美元，损失率 {ratio_text}%，达到 10% 警戒线。",
                    order_note,
                    "请检查未成交买单和现有持仓；本告警不代表已执行止损。",
                    f"数据时间：北京时间 {beijing_clock(now) or '未知'}。",
                )
            )
            pending = {
                channel
                for channel in configured
                if not (
                    isinstance(channels.get(channel), Mapping)
                    and channels[channel].get("success") is True
                )
            }
            attempts_result = (
                send_notification_with_results(
                    self._notifier, title, message, channels=pending
                )
                if pending
                else []
            )
            attempted: set[str] = set()
            for attempt in attempts_result:
                channel = str(getattr(attempt, "channel", ""))
                if channel not in pending:
                    continue
                attempted.add(channel)
                channels[channel] = {
                    "success": getattr(attempt, "success", False) is True,
                    "suppressed": getattr(attempt, "suppressed", False) is True,
                    "error_type": str(getattr(attempt, "error_type", "") or ""),
                    "error": "delivery_failed" if getattr(attempt, "error", "") else "",
                    "checked_at": timestamp,
                }
            for channel in pending - attempted:
                channels[channel] = {
                    "success": False,
                    "suppressed": False,
                    "error_type": "not_attempted",
                    "error": "",
                    "checked_at": timestamp,
                }
            if "feishu" not in configured and "feishu_app" not in configured:
                channels.setdefault(
                    "feishu",
                    {"success": False, "suppressed": False, "error_type": "not_configured"},
                )
            if "xiaoai" not in configured:
                channels.setdefault(
                    "xiaoai",
                    {"success": False, "suppressed": False, "error_type": "not_configured"},
                )
            alerts[outcome] = {
                "active": True,
                "triggered_at": alert.get("triggered_at") if not is_new_event else timestamp,
                "last_checked_at": timestamp,
                "last_attempt_at": timestamp if pending else alert.get("last_attempt_at"),
                "outcome": outcome,
                "risk_principal": direction.get("risk_principal"),
                "stress_loss": direction.get("stress_loss"),
                "loss_ratio": direction.get("loss_ratio"),
                "title": title,
                "message": message,
                "channels": channels,
            }
        return alerts

    def _lp_reward_share_projection(
        self, condition_ids: tuple[str, ...]
    ) -> dict[str, dict[str, object]]:
        with self._lp_reward_percentage_lock:
            return self._lp_reward_share_projection_locked(condition_ids)

    def _lp_reward_share_projection_locked(
        self, condition_ids: tuple[str, ...]
    ) -> dict[str, dict[str, object]]:
        reader = getattr(self._trading, "lp_reward_percentages", None)
        if not callable(reader):
            raw_result: Mapping[str, object] = {}
        else:
            sampled_at = self._clock()
            cached = self._lp_reward_percentage_cache
            if (
                isinstance(cached, Mapping)
                and sampled_at - float(cached.get("sampled_at", 0.0))
                < LP_REWARD_PERCENTAGE_CACHE_SECONDS
            ):
                cached_result = cached.get("result")
                raw_result = cached_result if isinstance(cached_result, Mapping) else {}
            else:
                try:
                    result = _call(reader)
                except Exception:
                    result = None
                raw_result = result if isinstance(result, Mapping) else {}
                self._lp_reward_percentage_cache = {
                    "sampled_at": self._clock(),
                    "result": raw_result,
                }

        percentages = raw_result.get("percentages")
        result_checked_at = _lp_share_datetime(raw_result.get("checked_at"))
        expected_maker = getattr(self._trading, "wallet_address", None)
        if not isinstance(expected_maker, str) or not expected_maker.strip():
            config = getattr(self._trading, "config", None)
            expected_maker = getattr(config, "wallet_address", None)
        maker_address = raw_result.get("maker_address")
        maker_matches = (
            isinstance(expected_maker, str)
            and bool(expected_maker.strip())
            and isinstance(maker_address, str)
            and bool(maker_address.strip())
            and maker_address.strip().casefold() == expected_maker.strip().casefold()
        )
        result_is_known = (
            raw_result.get("state") == "known"
            and raw_result.get("scope") == "account"
            and isinstance(percentages, Mapping)
            and result_checked_at is not None
            and isinstance(maker_address, str)
            and bool(maker_address.strip())
            and maker_matches
        )
        now = _utc_now()
        result_age = (
            (now - result_checked_at.astimezone(UTC)).total_seconds()
            if result_checked_at is not None
            else None
        )
        result_is_fresh = (
            result_is_known
            and result_age is not None
            and 0 <= result_age < LP_REWARD_SHARE_STALE_SECONDS
        )
        result_checked_text = (
            _timestamp(result_checked_at) if result_checked_at is not None else None
        )
        projections: dict[str, dict[str, object]] = {}
        persisted_loaded = False
        persisted_observations: Mapping[str, object] = {}
        for condition_id in condition_ids:
            previous = self._lp_reward_share_observations.get(condition_id)
            raw_percentage = percentages.get(condition_id) if isinstance(percentages, Mapping) else None
            percentage = _decimal(raw_percentage)
            valid = (
                result_is_fresh
                and percentage is not None
                and Decimal("0") <= percentage <= Decimal("100")
            )
            older_replay = False
            replay_same_after_failure = False
            if valid:
                assert percentage is not None
                active = self._lp_reward_share_active.get(condition_id, False)
                previous_checked_at = _lp_share_datetime(
                    previous.get("checked_at") if isinstance(previous, Mapping) else None
                )
                older_replay = (
                    previous_checked_at is not None
                    and result_checked_at is not None
                    and result_checked_at < previous_checked_at
                )
                same_source = (
                    previous_checked_at is not None
                    and result_checked_at is not None
                    and result_checked_at == previous_checked_at
                )
                replay_same_after_failure = same_source and not active
                if not older_replay and not replay_same_after_failure:
                    same_observation = (
                        previous is not None
                        and active
                        and previous.get("checked_at") == result_checked_text
                    )
                    if not same_observation:
                        delta = (
                            None
                            if previous is None or previous.get("percentage") is None
                            else percentage - previous["percentage"]
                        )
                        target_status, target_delta = _lp_share_target(percentage)
                        observation = {
                            "condition_id": condition_id,
                            "percentage": percentage,
                            "reference_share_percentage": LP_REWARD_REFERENCE_PERCENTAGE,
                            "target_share_percentage": LP_REWARD_SHARE_TARGET_MAX,
                            "target_status": target_status,
                            "target_delta_percentage_points": target_delta,
                            "delta_percentage_points": delta,
                            "checked_at": result_checked_text,
                            "last_success_at": result_checked_text,
                            "severity": _lp_share_severity(percentage),
                        }
                        self._lp_reward_share_observations[condition_id] = observation
                    else:
                        observation = previous
                    self._lp_reward_share_active[condition_id] = True
                    projections[condition_id] = {
                        **observation,
                        "state": "known",
                        "historical": False,
                        "stale": False,
                    }
                    continue

            self._lp_reward_share_active[condition_id] = False
            historical = dict(previous) if isinstance(previous, Mapping) else {}
            if not historical and not persisted_loaded:
                persisted_loaded = True
                account_id = self._lp_account_id()
                observations_reader = getattr(self._store, "lp_observations", None)
                if account_id is not None and callable(observations_reader):
                    try:
                        saved = observations_reader(account_id)
                    except Exception:
                        saved = {}
                    if isinstance(saved, Mapping):
                        persisted_observations = saved
            if not historical:
                saved_observation = persisted_observations.get(condition_id)
                saved_share = (
                    saved_observation.get("share_alert")
                    if isinstance(saved_observation, Mapping)
                    else None
                )
                saved_percentage = (
                    _decimal(saved_share.get("last_share_percentage"))
                    if isinstance(saved_share, Mapping)
                    else None
                )
                saved_checked_at = (
                    _lp_share_datetime(saved_share.get("last_share_checked_at"))
                    if isinstance(saved_share, Mapping)
                    else None
                )
                if (
                    saved_percentage is not None
                    and Decimal("0") <= saved_percentage <= Decimal("100")
                    and saved_checked_at is not None
                ):
                    saved_checked_text = _timestamp(saved_checked_at)
                    historical = {
                        "percentage": saved_percentage,
                        "checked_at": saved_checked_text,
                        "last_success_at": saved_checked_text,
                    }
            reason = (
                "reward_share_replay_older"
                if older_replay
                else "reward_share_replay_same"
                if replay_same_after_failure
                else "reward_share_stale"
                if result_is_known and not result_is_fresh
                else "reward_share_unknown"
            )
            projections[condition_id] = {
                "condition_id": condition_id,
                "state": "unknown",
                "percentage": historical.get("percentage"),
                "reference_share_percentage": LP_REWARD_REFERENCE_PERCENTAGE,
                "target_share_percentage": LP_REWARD_SHARE_TARGET_MAX,
                "target_status": "unknown",
                "target_delta_percentage_points": None,
                "delta_percentage_points": None,
                "checked_at": historical.get("checked_at"),
                "last_success_at": historical.get("last_success_at"),
                "severity": "unknown",
                "historical": bool(historical),
                "stale": True,
                "last_attempt_at": _timestamp(now),
                "reason": reason,
            }
        return projections

    def lp_dashboard(self) -> dict[str, object]:
        """Read official account orders and holdings without managing them."""

        with self._lp_dashboard_lock:
            reader = getattr(self._trading, "lp_account_snapshot", None)
            try:
                if not callable(reader):
                    raise RuntimeError("lp_account_reader_unavailable")
                snapshot = _call(reader)
                if (
                    not isinstance(snapshot, Mapping)
                    or snapshot.get("authenticated") is not True
                    or not isinstance(snapshot.get("open_orders"), (list, tuple))
                    or not isinstance(snapshot.get("positions"), (list, tuple))
                ):
                    raise RuntimeError("lp_account_snapshot_unknown")
                checked = snapshot.get("checked_at")
                if isinstance(checked, str):
                    text = checked.strip()
                    if text.endswith("Z"):
                        text = text[:-1] + "+00:00"
                    try:
                        checked = datetime.fromisoformat(text)
                    except ValueError as exc:
                        raise RuntimeError("lp_account_timestamp_unknown") from exc
                if not isinstance(checked, datetime) or checked.tzinfo is None:
                    raise RuntimeError("lp_account_timestamp_unknown")
                checked_at = _timestamp(checked)

                session: dict[str, object]
                try:
                    raw_session = self.lp_status()
                    session = dict(raw_session) if isinstance(raw_session, Mapping) else {"state": "unknown"}
                except Exception:
                    session = {"state": "unknown"}
                managed_ids = {
                    str(session.get(key) or "")
                    for key in (
                        "entry_order_id",
                        "passive_exit_order_id",
                        "protected_exit_order_id",
                    )
                } - {""}
                raw_owned = session.get("owned_order_ids")
                if isinstance(raw_owned, (list, tuple, set, frozenset)):
                    managed_ids.update(str(order_id) for order_id in raw_owned if order_id)
                managed_token = str(session.get("token_id") or "")

                orders: list[dict[str, object]] = []
                for raw_order in snapshot["open_orders"]:
                    if not isinstance(raw_order, Mapping):
                        continue
                    order_id = str(raw_order.get("order_id", raw_order.get("id", "")) or "")
                    filled = _decimal(raw_order.get("size_matched", raw_order.get("filled_quantity")))
                    remaining = _decimal(raw_order.get("remaining_size", raw_order.get("remaining_quantity")))
                    quantity = _decimal(raw_order.get("original_size", raw_order.get("quantity")))
                    if quantity is None and filled is not None and remaining is not None:
                        quantity = filled + remaining
                    if remaining is None and quantity is not None and filled is not None:
                        remaining = max(Decimal("0"), quantity - filled)
                    managed = order_id in managed_ids
                    minimum_order_size = _decimal(raw_order.get("minimum_order_size"))
                    reward_min_size = _decimal(raw_order.get("reward_min_size"))
                    min_scoring_size = _lp_min_scoring_size(
                        minimum_order_size, reward_min_size
                    )
                    orders.append(
                        {
                            "order_id": order_id,
                            "market_id": raw_order.get("market_id"),
                            "condition_id": raw_order.get("condition_id", raw_order.get("market")),
                            "market_title": raw_order.get("market_title", raw_order.get("title")),
                            "market_url": raw_order.get("market_url"),
                            "token_id": raw_order.get("token_id", raw_order.get("asset_id")),
                            "outcome": raw_order.get("outcome"),
                            "side": raw_order.get("side"),
                            "status": raw_order.get("status"),
                            "price": _decimal(raw_order.get("price")),
                            "quantity": quantity,
                            "filled_quantity": filled,
                            "remaining_quantity": remaining,
                            "minimum_order_size": minimum_order_size,
                            "reward_min_size": reward_min_size,
                            "min_scoring_size": min_scoring_size,
                            "purpose": _lp_order_purpose(
                                raw_order.get("side"), quantity, min_scoring_size
                            ),
                            "reward_max_spread": _decimal(raw_order.get("reward_max_spread")),
                            "fees_enabled": raw_order.get("fees_enabled"),
                            "fee_exponent": _decimal(raw_order.get("fee_exponent")),
                            "taker_fee_rate": _decimal(raw_order.get("taker_fee_rate")),
                            "management": "system_managed" if managed else "manual_read_only",
                            "read_only": not managed,
                        }
                    )

                positions: list[dict[str, object]] = []
                for raw_position in snapshot["positions"]:
                    if not isinstance(raw_position, Mapping):
                        continue
                    token_id = str(raw_position.get("token_id", raw_position.get("asset_id", "")) or "")
                    managed = bool(managed_token and token_id == managed_token)
                    positions.append(
                        {
                            "market_id": raw_position.get("market_id"),
                            "condition_id": raw_position.get("condition_id", raw_position.get("market")),
                            "market_title": raw_position.get("market_title", raw_position.get("title")),
                            "market_url": raw_position.get("market_url"),
                            "token_id": token_id or None,
                            "outcome": raw_position.get("outcome"),
                            "size": _decimal(raw_position.get("size", raw_position.get("quantity"))),
                            "average_price": _decimal(raw_position.get("average_price")),
                            "current_value": _decimal(raw_position.get("current_value")),
                            "reward_min_size": _decimal(raw_position.get("reward_min_size")),
                            "reward_max_spread": _decimal(raw_position.get("reward_max_spread")),
                            "fees_enabled": raw_position.get("fees_enabled"),
                            "fee_exponent": _decimal(raw_position.get("fee_exponent")),
                            "taker_fee_rate": _decimal(raw_position.get("taker_fee_rate")),
                            "management": "system_managed" if managed else "manual_read_only",
                            "read_only": not managed,
                        }
                    )

                scoring_reader = getattr(self._trading, "get_order_scoring", None)
                scoring_by_order: dict[str, tuple[str, str | None, str | None]] = {}
                for order in orders:
                    order_id = str(order.get("order_id") or "")
                    if order.get("management") != "manual_read_only" or not order_id:
                        continue
                    if order_id not in scoring_by_order:
                        attempted_at = _timestamp(datetime.now(UTC))
                        status = "unknown"
                        # Deliberately not named `checked_at`: that name is
                        # already the dashboard's account snapshot timestamp.
                        scoring_read_at: str | None = attempted_at
                        last_success_at = (
                            self._lp_scoring_observations.get(order_id, {}).get(
                                "scoring_last_success_at"
                            )
                        )
                        if callable(scoring_reader):
                            try:
                                scoring = _call(scoring_reader, order_id)
                            except Exception:
                                scoring = None
                            if isinstance(scoring, bool):
                                status = "true" if scoring else "false"
                                scoring_read_at = attempted_at
                                last_success_at = attempted_at
                                self._lp_scoring_observations[order_id] = {
                                    "scoring_status": status,
                                    "scoring_last_success_at": attempted_at,
                                }
                        scoring_by_order[order_id] = (
                            status,
                            scoring_read_at,
                            last_success_at,
                        )
                    order["scoring_status"], order["scoring_checked_at"], order[
                        "scoring_last_success_at"
                    ] = scoring_by_order[order_id]
                live_manual_order_ids = {
                    str(order.get("order_id") or "")
                    for order in orders
                    if order.get("management") == "manual_read_only"
                    and order.get("order_id")
                }
                self._lp_scoring_observations = {
                    order_id: record
                    for order_id, record in self._lp_scoring_observations.items()
                    if order_id in live_manual_order_ids
                }

                candidate_reader = getattr(self._lp, "candidate_snapshot", None)
                try:
                    raw_candidates = _call(candidate_reader) if callable(candidate_reader) else {}
                    candidate_snapshot = (
                        dict(raw_candidates)
                        if isinstance(raw_candidates, Mapping)
                        else {}
                    )
                except Exception:
                    candidate_snapshot = {}
                raw_candidates = candidate_snapshot.get("candidates")
                candidates = [
                    dict(candidate)
                    for candidate in raw_candidates
                    if isinstance(candidate, Mapping)
                ] if isinstance(raw_candidates, (list, tuple)) else []
                raw_recommendations = candidate_snapshot.get("recommendations")
                recommendations = _normalize_lp_recommendations(raw_recommendations)
                raw_selected_results = candidate_snapshot.get("selected_results")
                selected_results = _normalize_lp_recommendations(raw_selected_results)
                preparation_value = candidate_snapshot.get("preparation")
                if not isinstance(preparation_value, Mapping):
                    preparation_reader = getattr(self._lp, "preparation_snapshot", None)
                    try:
                        preparation_value = (
                            _call(preparation_reader)
                            if callable(preparation_reader)
                            else {}
                        )
                    except Exception:
                        preparation_value = {}
                preparation = (
                    dict(preparation_value)
                    if isinstance(preparation_value, Mapping)
                    else {}
                )
                raw_market_rewards = candidate_snapshot.get("market_rewards")
                market_rewards = (
                    dict(raw_market_rewards)
                    if isinstance(raw_market_rewards, Mapping)
                    else {}
                )
                manual_conditions = tuple(
                    dict.fromkeys(
                        str(row.get("condition_id") or "").strip()
                        for row in (*orders, *positions)
                        if row.get("management") == "manual_read_only"
                        and str(row.get("condition_id") or "").strip()
                    )
                )
                share_conditions = tuple(
                    dict.fromkeys(
                        str(row.get("condition_id") or "").strip()
                        for row in (*orders, *positions)
                        if str(row.get("condition_id") or "").strip()
                    )
                )
                reward_shares = self._lp_reward_share_projection(share_conditions)
                previous_dashboard = self._lp_dashboard_cache
                previous_rewards_value = (
                    previous_dashboard.get("market_rewards")
                    if isinstance(previous_dashboard, Mapping)
                    else None
                )
                previous_rewards = (
                    previous_rewards_value
                    if isinstance(previous_rewards_value, Mapping)
                    else {}
                )
                reward_date = checked.astimezone(UTC).date().isoformat()
                cached_rewards, refresh_ids = self._lp_reward_cache_snapshot(
                    reward_date,
                    manual_conditions,
                    previous_rewards=previous_rewards,
                    candidate_rewards=market_rewards,
                )
                market_rewards.update(cached_rewards)
                account_id = self._lp_account_id()
                stored_observations = (
                    self._store.lp_observations(account_id)
                    if account_id is not None
                    and callable(getattr(self._store, "lp_observations", None))
                    else {}
                )
                with self._lp_dashboard_lock:
                    cached_fills = dict(
                        self._lp_orders_today_fills.get(reward_date, {})
                    )
                lp_orders_today, non_lp_row_count, trade_condition_ids = (
                    self._lp_orders_today_snapshot(
                        orders=orders,
                        positions=positions,
                        observations=stored_observations,
                        market_rewards=market_rewards,
                        reward_date=reward_date,
                        managed_order_ids=managed_ids,
                        managed_token=managed_token,
                    )
                )
                for order_id, fill in cached_fills.items():
                    if any(
                        str(row.get("order_id") or "") == order_id
                        for row in lp_orders_today
                    ):
                        continue
                    # Cached fills face the same explicit-negative gate as
                    # snapshot rows; a market excluded as non-LP today must
                    # not re-enter the table through its fills.
                    if self._lp_market_explicitly_negative(
                        str(fill.get("condition_id") or ""),
                        observations=stored_observations,
                        market_rewards=market_rewards,
                        reward_date=reward_date,
                    ):
                        continue
                    fill_managed = (
                        str(order_id) in managed_ids
                        or bool(managed_token)
                        and str(fill.get("token_id") or "") == managed_token
                    )
                    lp_orders_today.append(
                        {
                            "order_id": order_id,
                            "condition_id": fill.get("condition_id"),
                            "market_id": fill.get("market_id"),
                            "market_title": fill.get("market_title"),
                            "market_url": fill.get("market_url"),
                            "token_id": fill.get("token_id"),
                            "outcome": fill.get("outcome"),
                            "side": fill.get("side"),
                            "status": "MATCHED",
                            "price": fill.get("price"),
                            "quantity": None,
                            "filled_quantity": fill.get("filled_quantity"),
                            "remaining_quantity": Decimal("0"),
                            "last_fill_at": fill.get("last_fill_at"),
                            "state": "filled",
                            "scoring_status": "unknown",
                            "scoring_checked_at": None,
                            "scoring_last_success_at": None,
                            "min_scoring_size": None,
                            "purpose": None,
                            "management": (
                                "system_managed"
                                if fill_managed
                                else "manual_read_only"
                            ),
                            "read_only": not fill_managed,
                        }
                    )
                result = {
                    "state": "ready",
                    "orders": orders,
                    "positions": positions,
                    "lp_orders_today": lp_orders_today,
                    "non_lp_row_count": non_lp_row_count,
                    "candidates": candidates,
                    "recommendations": recommendations,
                    "selected_results": selected_results,
                    "preparation": preparation,
                    "funnel": (
                        dict(candidate_snapshot.get("funnel"))
                        if isinstance(candidate_snapshot.get("funnel"), Mapping)
                        else {}
                    ),
                    "selected_market_ids": candidate_snapshot.get(
                        "selected_market_ids", []
                    ),
                    "reward_shares": reward_shares,
                    "market_rewards": market_rewards,
                    "candidate_state": candidate_snapshot.get("state", "unknown"),
                    "complete": candidate_snapshot.get("complete") is True,
                    "scanning": candidate_snapshot.get("scanning") is True,
                    "candidate_stale": (
                        candidate_snapshot.get("stale") is True
                        or candidate_snapshot.get("state") == "stale"
                    ),
                    "candidate_checked_at": candidate_snapshot.get("checked_at"),
                    "candidate_last_success_at": candidate_snapshot.get("last_success_at"),
                    "candidate_last_attempt_at": candidate_snapshot.get("last_attempt_at"),
                    "missing_metadata_condition_ids": candidate_snapshot.get(
                        "missing_metadata_condition_ids", []
                    ),
                    "missing_book_token_ids": candidate_snapshot.get(
                        "missing_book_token_ids", []
                    ),
                    "catalog_complete": candidate_snapshot.get("catalog_complete") is True,
                    "candidate_retention_reason": candidate_snapshot.get(
                        "retention_reason"
                    ),
                    "checked_at": checked_at,
                    "last_success_at": checked_at,
                    "stale": False,
                    "authenticated": True,
                    "open_orders_complete": snapshot.get("open_orders_complete") is True,
                    "positions_complete": snapshot.get("positions_complete") is True,
                    "lp_share_watch_state": self.lp_share_watch_state(),
                    "lp_observations": _project_lp_observations(
                        stored_observations,
                        orders=orders,
                        positions=positions,
                        account_checked_at=checked_at,
                        account_complete=(
                            snapshot.get("open_orders_complete") is True
                            and snapshot.get("positions_complete") is True
                        ),
                        now=_utc_now(),
                    ),
                    "lp_session": session,
                }
                self._lp_dashboard_cache = result
                self._schedule_lp_reward_refresh(reward_date, refresh_ids)
                self._schedule_lp_orders_today_refresh(
                    reward_date, trade_condition_ids
                )
                return result
            except Exception:
                cached = self._lp_dashboard_cache
                try:
                    raw_candidate_snapshot = _call(
                        getattr(self._lp, "candidate_snapshot")
                    )
                    candidate_snapshot = (
                        dict(raw_candidate_snapshot)
                        if isinstance(raw_candidate_snapshot, Mapping)
                        else {}
                    )
                except Exception:
                    candidate_snapshot = {}
                candidate_rows = candidate_snapshot.get("candidates")
                recommendations = candidate_snapshot.get("recommendations")
                selected_results = candidate_snapshot.get("selected_results")
                candidate_funnel = candidate_snapshot.get("funnel")
                candidate_state = candidate_snapshot.get("state", "unknown")
                preparation_value = candidate_snapshot.get("preparation")
                if not isinstance(preparation_value, Mapping):
                    preparation_reader = getattr(self._lp, "preparation_snapshot", None)
                    try:
                        preparation_value = (
                            _call(preparation_reader)
                            if callable(preparation_reader)
                            else {}
                        )
                    except Exception:
                        preparation_value = {}
                preparation = (
                    dict(preparation_value)
                    if isinstance(preparation_value, Mapping)
                    else {}
                )
                candidate_projection = {
                    "candidates": [
                        dict(row)
                        for row in candidate_rows
                        if isinstance(row, Mapping)
                    ] if isinstance(candidate_rows, (list, tuple)) else [],
                    "recommendations": [
                        dict(row)
                        for row in _normalize_lp_recommendations(recommendations)
                    ],
                    "selected_results": _normalize_lp_recommendations(selected_results),
                    "preparation": preparation,
                    "funnel": (
                        dict(candidate_funnel)
                        if isinstance(candidate_funnel, Mapping)
                        else {}
                    ),
                    "selected_market_ids": candidate_snapshot.get(
                        "selected_market_ids", []
                    ),
                    "candidate_state": candidate_state,
                    "complete": candidate_snapshot.get("complete") is True,
                    "scanning": candidate_snapshot.get("scanning") is True,
                    "candidate_stale": (
                        candidate_snapshot.get("stale") is True
                        or candidate_state == "stale"
                    ),
                    "candidate_checked_at": candidate_snapshot.get("checked_at"),
                    "candidate_last_success_at": candidate_snapshot.get("last_success_at"),
                    "candidate_last_attempt_at": candidate_snapshot.get("last_attempt_at"),
                    "missing_metadata_condition_ids": candidate_snapshot.get(
                        "missing_metadata_condition_ids", []
                    ),
                    "missing_book_token_ids": candidate_snapshot.get(
                        "missing_book_token_ids", []
                    ),
                    "catalog_complete": candidate_snapshot.get("catalog_complete") is True,
                    "candidate_retention_reason": candidate_snapshot.get(
                        "retention_reason"
                    ),
                }
                if cached is not None:
                    cached_orders = cached.get("orders")
                    degraded_orders = (
                        [
                            {
                                **order,
                                # Issue #140: a stale snapshot carries no fresh
                                # official scoring; keep the last success time
                                # instead of replaying the old status verbatim.
                                "scoring_status": "unknown",
                                "scoring_last_success_at": (
                                    order.get("scoring_checked_at")
                                    if order.get("scoring_checked_at") is not None
                                    else order.get("scoring_last_success_at")
                                ),
                            }
                            for order in cached_orders
                            if isinstance(order, Mapping)
                        ]
                        if isinstance(cached_orders, (list, tuple))
                        else cached_orders
                    )
                    cached_today_orders = cached.get("lp_orders_today")
                    degraded_today_orders = (
                        [
                            {
                                **order,
                                # Issue #140: the 当天 LP 委托 table renders from
                                # lp_orders_today, so stale scoring must be
                                # downgraded here exactly like `orders`.
                                "scoring_status": "unknown",
                                "scoring_last_success_at": (
                                    order.get("scoring_checked_at")
                                    if order.get("scoring_checked_at") is not None
                                    else order.get("scoring_last_success_at")
                                ),
                            }
                            for order in cached_today_orders
                            if isinstance(order, Mapping)
                        ]
                        if isinstance(cached_today_orders, (list, tuple))
                        else cached_today_orders
                    )
                    return {
                        **cached,
                        **candidate_projection,
                        "orders": degraded_orders,
                        "lp_orders_today": degraded_today_orders,
                        "state": "stale",
                        "stale": True,
                        "lp_share_watch_state": self.lp_share_watch_state(),
                    }
                return {
                    "state": "unknown",
                    "orders": [],
                    "positions": [],
                    "lp_orders_today": [],
                    "non_lp_row_count": None,
                    **candidate_projection,
                    "reward_shares": {},
                    "market_rewards": {},
                    "checked_at": None,
                    "authenticated": False,
                    "open_orders_complete": False,
                    "positions_complete": False,
                    "last_success_at": None,
                    "stale": True,
                    "lp_share_watch_state": self.lp_share_watch_state(),
                    "lp_observations": {},
                    "lp_session": self.lp_status(),
                }

    def refresh_lp_observations(
        self, *, stop_event: threading.Event | None = None
    ) -> dict[str, object]:
        """Refresh durable, read-only LP yield and risk observations."""

        account_id = self._lp_account_id()
        observations_reader = getattr(self._store, "lp_observations", None)
        save_observation = getattr(self._store, "save_lp_observation", None)
        if account_id is None or not callable(observations_reader) or not callable(save_observation):
            return {"state": "unknown", "reason": "lp_observation_store_unavailable", "observations": {}}

        previous = observations_reader(account_id)
        now = _utc_now()

        def mark_stale(reason: str) -> dict[str, dict[str, object]]:
            stale_rows: dict[str, dict[str, object]] = {}
            for condition_id, saved in previous.items():
                stale = {
                    **_mask_lp_observation(saved, reason),
                    "checked_at": now,
                    "last_attempt_at": now,
                }
                stale_rows[condition_id] = save_observation(
                    account_id, condition_id, stale
                )
            with self._lp_dashboard_lock:
                cached = self._lp_dashboard_cache
                if cached is not None:
                    self._lp_dashboard_cache = {
                        **cached,
                        "state": "stale",
                        "stale": True,
                        "lp_observations": stale_rows,
                    }
            return stale_rows

        if stop_event is not None and stop_event.is_set():
            return {"state": "cancelled", "observations": previous}
        dashboard = self.lp_dashboard()
        now = _utc_now()
        if dashboard.get("state") != "ready" or dashboard.get("stale") is True:
            return {
                "state": "unknown",
                "reason": "account_snapshot_stale",
                "observations": mark_stale("account_snapshot_stale"),
            }

        orders = dashboard.get("orders")
        positions = dashboard.get("positions")
        if not isinstance(orders, (list, tuple)) or not isinstance(positions, (list, tuple)):
            return {
                "state": "unknown",
                "reason": "account_facts_unknown",
                "observations": mark_stale("account_facts_unknown"),
            }
        account_checked_at = _aware_datetime(dashboard.get("checked_at"))
        account_complete = (
            dashboard.get("authenticated") is True
            and dashboard.get("open_orders_complete") is True
            and dashboard.get("positions_complete") is True
            and account_checked_at is not None
            and Decimal("0") <= Decimal(str((now - account_checked_at).total_seconds())) <= Decimal("120")
        )
        if not account_complete:
            return {
                "state": "unknown",
                "reason": "account_facts_unknown",
                "observations": mark_stale("account_facts_unknown"),
            }

        orders = [row for row in orders if isinstance(row, Mapping)]
        positions = [row for row in positions if isinstance(row, Mapping)]
        orders_by_market: dict[str, list[Mapping[str, object]]] = {}
        positions_by_market: dict[str, list[Mapping[str, object]]] = {}
        for row in orders:
            condition_id = str(row.get("condition_id") or "").strip()
            if condition_id:
                orders_by_market.setdefault(condition_id, []).append(row)
        for row in positions:
            condition_id = str(row.get("condition_id") or "").strip()
            if condition_id:
                positions_by_market.setdefault(condition_id, []).append(row)
        active_market_ids = set(orders_by_market) | set(positions_by_market)
        market_ids = active_market_ids | set(previous)
        if not market_ids:
            return {"state": "ready", "checked_at": _timestamp(now), "observations": {}}

        rates: Mapping[str, object] = {}
        rate_reader = getattr(self._trading, "lp_reward_rates", None)
        rate_result: object = None
        if active_market_ids and callable(rate_reader):
            try:
                rate_result = _call(rate_reader, stop_event=stop_event)
            except Exception:
                rate_result = None
        if isinstance(rate_result, Mapping):
            raw_markets = rate_result.get("markets")
            if isinstance(raw_markets, Mapping):
                rates = raw_markets
        rate_now = _utc_now()
        rates_checked_at = _aware_datetime(
            rate_result.get("checked_at") if isinstance(rate_result, Mapping) else None
        )
        rates_complete = (
            isinstance(rate_result, Mapping)
            and rate_result.get("state") == "known"
            and rate_result.get("complete") is True
            and rates_checked_at is not None
            and Decimal("0") <= Decimal(str((rate_now - rates_checked_at).total_seconds())) <= Decimal("120")
        )

        account_facts = {
            "authenticated": True,
            "checked_at": account_checked_at,
            "open_orders_complete": True,
            "positions_complete": True,
            "open_orders": orders,
            "positions": positions,
        }
        tokens = tuple(
            dict.fromkeys(
                str(row.get("token_id") or "").strip()
                for row in (*orders, *positions)
                if row.get("condition_id") in active_market_ids
                and str(row.get("token_id") or "").strip()
            )
        )
        books: Mapping[str, object] = {}
        book_reader = getattr(self._trading, "lp_order_books", None)
        if tokens and callable(book_reader):
            try:
                raw_books = _call(book_reader, tokens, stop_event=stop_event)
                if isinstance(raw_books, Mapping):
                    books = raw_books
            except Exception:
                books = {}

        now = _utc_now()
        if account_checked_at is None or not (
            Decimal("0")
            <= Decimal(str((now - account_checked_at).total_seconds()))
            <= Decimal("120")
        ):
            return {
                "state": "unknown",
                "reason": "account_facts_unknown",
                "observations": mark_stale("account_facts_unknown"),
            }
        if rates_checked_at is None or not (
            Decimal("0")
            <= Decimal(str((now - rates_checked_at).total_seconds()))
            <= Decimal("120")
        ):
            rates_complete = False

        result: dict[str, dict[str, object]] = {}
        for condition_id in sorted(market_ids):
            market_orders = orders_by_market.get(condition_id, [])
            market_positions = positions_by_market.get(condition_id, [])
            previous_market = previous.get(condition_id, {})
            if not market_orders and not market_positions:
                # Only a complete, fresh account snapshot can confirm a new trial cycle.
                flat = {
                    "state": "known",
                    "stage": "flat",
                    "stale": False,
                    "reason": "no_exposure",
                    "current_hourly_reward_usd": None,
                    "occupied_capital_usd": Decimal("0"),
                    "exposure_quantity": Decimal("0"),
                    "current_yield_pct_per_hour": None,
                    "trial_reference": None,
                    "trial_baseline": None,
                    "risk_state": "known",
                    "risk_warning": False,
                    "risk_directions": [],
                    "add_room": {"available": False, "reason": "no_exposure"},
                    "exposure_fingerprint": _lp_exposure_signature(
                        market_orders, market_positions, condition_id
                    ),
                    "checked_at": now,
                    "last_success_at": now,
                    "last_attempt_at": now,
                }
                flat["risk_alerts"] = self._update_lp_risk_alerts(
                    condition_id=condition_id,
                    market_title=condition_id,
                    risk_directions=[],
                    previous=previous_market.get("risk_alerts"),
                    now=now,
                )
                result[condition_id] = save_observation(account_id, condition_id, flat)
                continue

            raw_rate = rates.get(condition_id)
            reward_known = (
                rates_complete
                and isinstance(raw_rate, Mapping)
                and raw_rate.get("state") == "known"
                and raw_rate.get("currency") == "USD"
            )
            rate_reason: str | None = None
            if not rates_complete:
                rate_reason = (
                    "reward_rates_stale"
                    if rates_checked_at is not None
                    and (now - rates_checked_at).total_seconds() > 120
                    else "reward_rates_unknown"
                )
            elif not isinstance(raw_rate, Mapping):
                rate_reason = "reward_market_missing"
            elif raw_rate.get("state") != "known" or raw_rate.get("currency") != "USD":
                rate_reason = str(raw_rate.get("reason") or "reward_rate_unknown")
            hourly_reward = _decimal(raw_rate.get("hourly_reward_usd")) if reward_known else None
            if hourly_reward is None or hourly_reward < 0:
                reward_known = False
                rate_reason = rate_reason or "reward_rate_unknown"
            elif reward_known:
                hourly_reward = _canonical_decimal(hourly_reward)

            exposure_quantity = Decimal("0")
            occupied_capital = Decimal("0")
            exposure_known = True
            buy_orders: list[Mapping[str, object]] = []
            for position in market_positions:
                size = _decimal(position.get("size"))
                if size is None or size < 0:
                    exposure_known = False
                    continue
                if size == 0:
                    continue
                price = _decimal(position.get("average_price"))
                if price is None or price <= 0 or price > 1:
                    exposure_known = False
                    continue
                exposure_quantity += size
                occupied_capital += size * price
            for order in market_orders:
                status = str(order.get("status") or "").upper()
                if status in TERMINAL_ORDER_STATES:
                    continue
                side = str(order.get("side") or "").upper()
                if side not in {"BUY", "SELL"}:
                    exposure_known = False
                    continue
                if side != "BUY":
                    continue
                remaining = _decimal(order.get("remaining_quantity"))
                price = _decimal(order.get("price"))
                if remaining is None or remaining < 0 or price is None or price <= 0 or price > 1:
                    exposure_known = False
                    continue
                if remaining > 0:
                    buy_orders.append(order)
                    exposure_quantity += remaining
                    occupied_capital += remaining * price

            market_rows = (*market_orders, *market_positions)
            min_sizes = {
                parsed
                for row in market_rows
                if (parsed := _decimal(row.get("reward_min_size"))) is not None and parsed > 0
            }
            minimum_size = next(iter(min_sizes)) if len(min_sizes) == 1 else None
            if len(min_sizes) > 1:
                exposure_known = False

            source_rows = (
                raw_rate.get("sources")
                if isinstance(raw_rate, Mapping)
                else None
            )
            source_names = (
                tuple(source_rows)
                if isinstance(source_rows, Mapping)
                else tuple(source_rows)
                if isinstance(source_rows, (list, tuple))
                else ()
            )
            reward_share_positive = False
            for source in source_names:
                source_data = raw_rate.get(str(source)) if isinstance(raw_rate, Mapping) else None
                if isinstance(source_data, Mapping):
                    percentage = _decimal(source_data.get("earning_percentage"))
                    reward_share_positive = reward_share_positive or (
                        percentage is not None and percentage > 0
                    )
            # Issue #140: qualification is three-state and carries its basis.
            # Official per-order scoring decides when buy orders exist; a
            # contradictory mix (explicit false vs positive reward share) or
            # any unknown keeps the state explicitly UNKNOWN instead of
            # guessing from the reward share alone.
            orders_qualified: bool | None
            qualification_basis: str | None = None
            if buy_orders:
                buy_scoring = {
                    str(order.get("scoring_status")) for order in buy_orders
                }
                if buy_scoring == {"true"}:
                    orders_qualified = True
                    qualification_basis = "orders_scoring"
                elif buy_scoring == {"false"} and not reward_share_positive:
                    orders_qualified = False
                    qualification_basis = "orders_not_scoring"
                else:
                    orders_qualified = None
            else:
                orders_qualified = True if reward_share_positive else None
                if orders_qualified is True:
                    qualification_basis = "reward_share_positive"
            qualified = orders_qualified

            rate_pct = (
                hourly_reward / occupied_capital * Decimal("100")
                if reward_known and exposure_known and occupied_capital > 0
                else None
            )
            if rate_pct is not None:
                rate_pct = _canonical_decimal(rate_pct)
            market_metadata = next(
                (row for row in market_rows if row.get("token_id")), {}
            )
            tokens_by_market = tuple(
                dict.fromkeys(
                    str(row.get("token_id") or "")
                    for row in market_rows
                    if str(row.get("token_id") or "")
                )
            )
            risk_directions: list[dict[str, object]] = []
            for token_id in tokens_by_market:
                direction = next(
                    (row for row in market_rows if str(row.get("token_id") or "") == token_id),
                    market_metadata,
                )
                risk = evaluate_lp_exposure(
                    books.get(token_id),
                    market={
                        "condition_id": condition_id,
                        "token_id": token_id,
                        "fees_enabled": direction.get("fees_enabled"),
                        "taker_fee_rate": direction.get("taker_fee_rate"),
                        "fee_exponent": direction.get("fee_exponent"),
                    },
                    account=account_facts,
                    now=now,
                )
                risk_directions.append(
                    {
                        "outcome": str(direction.get("outcome") or "未知方向"),
                        "token_id": token_id,
                        "unfilled_buy_orders": any(
                            str(order.get("token_id") or "") == token_id
                            for order in buy_orders
                        ),
                        "state": risk.get("state"),
                        "warning": risk.get("warning"),
                        "risk_quantity": risk.get("risk_quantity"),
                        "risk_principal": risk.get("risk_principal"),
                        "stress_loss": risk.get("stress_loss"),
                        "loss_ratio": risk.get("loss_ratio"),
                        "threshold": risk.get("threshold"),
                        "reason_codes": risk.get("reason_codes", []),
                        "checked_at": risk.get("checked_at"),
                    }
                )
            if not tokens_by_market:
                risk_state = "known"
                risk_warning: bool | None = False
            elif any(row.get("state") != "known" for row in risk_directions):
                risk_state = "unknown"
                risk_warning = None
            else:
                risk_warning = any(row.get("warning") is True for row in risk_directions)
                risk_state = "warning" if risk_warning else "known"

            risk_alerts = self._update_lp_risk_alerts(
                condition_id=condition_id,
                market_title=str(
                    market_metadata.get("market_title")
                    or market_metadata.get("title")
                    or condition_id
                ),
                risk_directions=risk_directions,
                previous=previous_market.get("risk_alerts"),
                now=now,
            )

            previous_quantity = _decimal(previous_market.get("exposure_quantity")) or Decimal("0")
            previous_stage = str(previous_market.get("stage") or "")
            trial_reference = previous_market.get("trial_reference")
            baseline = previous_market.get("trial_baseline")
            if not isinstance(trial_reference, Mapping):
                trial_reference = None
            if not isinstance(baseline, Mapping):
                baseline = None

            if exposure_quantity == 0:
                trial_reference = None
                baseline = None
            else:
                added_quantity = exposure_quantity > previous_quantity
                if baseline is None and added_quantity and trial_reference is not None:
                    baseline = dict(trial_reference)
                    baseline = {**baseline, "captured_at": now}
                is_trial = (
                    minimum_size is not None
                    and exposure_quantity == minimum_size
                    and occupied_capital > 0
                    and reward_known
                    and qualified
                    and exposure_known
                    and (
                        previous_quantity == 0
                        or previous_stage in {"trial", "trial_unknown"}
                    )
                )
                if baseline is None and is_trial and rate_pct is not None:
                    trial_reference = {
                        "yield_pct_per_hour": rate_pct,
                        "checked_at": now,
                        "quantity": exposure_quantity,
                        "price": occupied_capital / exposure_quantity,
                        "occupied_capital_usd": occupied_capital,
                    }

            stage_reason: str | None = None
            if exposure_quantity == 0:
                stage = "flat"
                stage_reason = "no_exposure"
            elif baseline is not None:
                stage = "added"
                stage_reason = None if rate_pct is not None else "current_yield_unknown"
            elif (
                minimum_size is not None
                and exposure_quantity == minimum_size
            ):
                stage = "trial" if trial_reference is not None else "trial_unknown"
                stage_reason = None if rate_pct is not None else rate_reason or "current_yield_unknown"
            else:
                stage = "added_untracked"
                stage_reason = "trial_baseline_unrecorded"

            add_room_reason = None
            if rate_pct is None:
                add_room_reason = "current_yield_unknown"
            elif rate_pct < Decimal("0.1"):
                add_room_reason = "yield_below_threshold"
            elif not qualified:
                add_room_reason = "reward_qualification_unknown"
            elif risk_state == "unknown":
                add_room_reason = "risk_unknown"
            elif risk_warning is True:
                add_room_reason = "risk_warning"
            elif exposure_quantity <= 0 or occupied_capital <= 0:
                add_room_reason = "no_exposure"
            add_room = {"available": add_room_reason is None, "reason": add_room_reason}

            observation = {
                "state": "known" if rate_pct is not None else "unknown",
                "stage": stage,
                "stale": bool(rate_pct is None and rate_reason == "reward_rates_stale"),
                "reason": rate_reason or stage_reason,
                "current_hourly_reward_usd": hourly_reward if reward_known else None,
                "occupied_capital_usd": occupied_capital if exposure_known else None,
                "exposure_quantity": exposure_quantity if exposure_known else None,
                "current_yield_pct_per_hour": rate_pct,
                "trial_reference": trial_reference,
                "trial_baseline": baseline,
                "qualified": qualified,
                "qualification_basis": qualification_basis,
                "risk_state": risk_state,
                "risk_warning": risk_warning,
                "risk_directions": risk_directions,
                "risk_alerts": risk_alerts,
                "add_room": add_room,
                "exposure_fingerprint": _lp_exposure_signature(
                    market_orders, market_positions, condition_id
                ),
                "checked_at": now,
                "last_success_at": now if rate_pct is not None else previous_market.get("last_success_at"),
                "last_attempt_at": now,
            }
            result[condition_id] = save_observation(account_id, condition_id, observation)

        return {"state": "ready", "checked_at": _timestamp(now), "observations": result}

    def _lp_market_explicitly_negative(
        self,
        condition_id: str,
        *,
        observations: Mapping[str, object],
        market_rewards: Mapping[str, object],
        reward_date: str,
    ) -> bool:
        """Every LP signal explicitly negative for this market today.

        Reused by the 当天 LP 委托 snapshot and by the cached-fill append so
        both exclusions share one day-bounded negative signal.
        """

        observation = observations.get(condition_id)
        day_start = datetime.fromisoformat(f"{reward_date}T00:00:00+00:00")
        return _lp_market_reward_missing(
            observation,
            day_start=day_start,
            day_end=day_start + timedelta(days=1),
        ) and not (
            _lp_reward_record_today(market_rewards.get(condition_id), reward_date)
            or _lp_market_reward_known(observation)
        )

    def _lp_orders_today_snapshot(
        self,
        *,
        orders: list[dict[str, object]],
        positions: list[dict[str, object]],
        observations: Mapping[str, object],
        market_rewards: Mapping[str, object],
        reward_date: str,
        managed_order_ids: set[str],
        managed_token: str,
    ) -> tuple[list[dict[str, object]], int, tuple[str, ...]]:
        """Assemble 当天 LP 委托 rows with fail-open LP attribution.

        Open orders stay when any LP signal is positive or any signal is
        unknown; a row is excluded only when every signal is explicitly
        negative and the row is not managed by the LP session.  Positions
        never become rows here; they only feed the non-LP counter.
        """

        def reward_record_today(condition_id: str) -> bool:
            return _lp_reward_record_today(
                market_rewards.get(condition_id), reward_date
            )

        def market_negative(condition_id: str) -> bool:
            return self._lp_market_explicitly_negative(
                condition_id,
                observations=observations,
                market_rewards=market_rewards,
                reward_date=reward_date,
            )

        def row_positive(condition_id: str, scoring: object) -> bool:
            observation = observations.get(condition_id)
            return (
                scoring == "true"
                or _lp_market_reward_known(observation)
                or reward_record_today(condition_id)
            )

        today_rows: list[dict[str, object]] = []
        non_lp_rows = 0
        for row in orders:
            condition_id = str(row.get("condition_id") or "")
            managed = str(row.get("order_id") or "") in managed_order_ids
            scoring = row.get("scoring_status")
            excluded = (
                not managed
                and not row_positive(condition_id, scoring)
                and scoring == "false"
                and market_negative(condition_id)
            )
            if excluded:
                non_lp_rows += 1
                continue
            today_rows.append({**row, "state": "open", "last_fill_at": None})
        for row in positions:
            condition_id = str(row.get("condition_id") or "")
            managed = bool(
                managed_token and str(row.get("token_id") or "") == managed_token
            )
            if (
                not managed
                and not row_positive(condition_id, None)
                and market_negative(condition_id)
            ):
                non_lp_rows += 1

        trade_conditions = {
            condition_id
            for condition_id in market_rewards
            if _lp_reward_record_today(market_rewards.get(condition_id), reward_date)
        }
        for condition_id, observation in observations.items():
            if _lp_market_reward_known(observation):
                trade_conditions.add(str(condition_id))
                continue
            exposure = _decimal(observation.get("exposure_quantity")) if isinstance(observation, Mapping) else None
            stage = (
                str(observation.get("stage") or "")
                if isinstance(observation, Mapping)
                else ""
            )
            if (exposure is not None and exposure > 0) or stage not in {"", "flat"}:
                trade_conditions.add(str(condition_id))
        return today_rows, non_lp_rows, tuple(sorted(trade_conditions))

    def _schedule_lp_orders_today_refresh(
        self, reward_date: str, condition_ids: tuple[str, ...]
    ) -> None:
        if not condition_ids:
            return
        now = self._clock()
        with self._lp_dashboard_lock:
            # TTL throttle: markets already read successfully within the
            # window are not re-queued, so every dashboard poll does not hit
            # the authenticated trades API for the whole market set.
            read_at = self._lp_orders_today_trades_read_at
            due = tuple(
                condition_id
                for condition_id in condition_ids
                if now - read_at.get((reward_date, condition_id), float("-inf"))
                >= _LP_TRADES_CACHE_SECONDS
            )
            if not due:
                return
            pending = self._lp_orders_today_pending.setdefault(reward_date, set())
            pending.update(due)
            running = self._lp_orders_today_refresh_thread
            if running is not None and running.is_alive():
                return
            thread = threading.Thread(
                target=self._refresh_lp_orders_today,
                name="lp-orders-today-refresh",
                daemon=True,
            )
            self._lp_orders_today_refresh_thread = thread
        thread.start()

    def _refresh_lp_orders_today(self) -> None:
        try:
            while True:
                with self._lp_dashboard_lock:
                    if not self._lp_orders_today_pending:
                        if (
                            self._lp_orders_today_refresh_thread
                            is threading.current_thread()
                        ):
                            self._lp_orders_today_refresh_thread = None
                        return
                    reward_date, pending = next(
                        iter(self._lp_orders_today_pending.items())
                    )
                    condition_ids = tuple(pending)
                    self._lp_orders_today_pending.pop(reward_date, None)
                try:
                    self._refresh_lp_orders_today_batch(reward_date, condition_ids)
                except Exception:
                    pass
        finally:
            with self._lp_dashboard_lock:
                if self._lp_orders_today_refresh_thread is threading.current_thread():
                    self._lp_orders_today_refresh_thread = None

    def _refresh_lp_orders_today_batch(
        self, reward_date: str, condition_ids: tuple[str, ...]
    ) -> None:
        """Refresh one reward date's fill aggregation without blocking reads."""

        reader = getattr(self._trading, "lp_account_trades", None)
        if not callable(reader):
            return
        result = _call(reader, condition_ids)
        if not isinstance(result, Mapping):
            return
        day_start = datetime.fromisoformat(f"{reward_date}T00:00:00+00:00")
        raw_trades = result.get("trades")
        fills = (
            _lp_aggregate_fills_by_order(raw_trades, day_start=day_start)
            if isinstance(raw_trades, Mapping)
            else {}
        )
        fills = self._lp_enrich_fill_rows(fills)
        with self._lp_dashboard_lock:
            previous = self._lp_orders_today_fills.get(reward_date, {})
            merged = {**previous, **fills}
            self._lp_orders_today_fills.clear()
            self._lp_orders_today_fills[reward_date] = merged
            self._lp_orders_today_fills_complete[reward_date] = (
                result.get("complete") is True
            )
            # Record per-market read success so the TTL throttle only skips
            # markets whose trades were actually read back.
            if isinstance(raw_trades, Mapping):
                read_at = self._lp_orders_today_trades_read_at
                for stale_key in [
                    key for key in read_at if key[0] != reward_date
                ]:
                    del read_at[stale_key]
                now = self._clock()
                for condition_id in condition_ids:
                    if condition_id in raw_trades:
                        read_at[(reward_date, condition_id)] = now

    def _lp_enrich_fill_rows(
        self, fills: dict[str, dict[str, object]]
    ) -> dict[str, dict[str, object]]:
        """Best-effort market metadata for orders that already left the book."""

        missing = tuple(
            dict.fromkeys(
                str(fill.get("condition_id") or "")
                for fill in fills.values()
                if fill.get("condition_id")
                and (not fill.get("market_title") or not fill.get("outcome"))
            )
        )
        if not missing:
            return fills
        metadata_reader = getattr(self._trading, "lp_market_metadata", None)
        if not callable(metadata_reader):
            return fills
        try:
            metadata = _call(metadata_reader, missing)
        except Exception:
            return fills
        if not isinstance(metadata, Mapping):
            return fills
        for fill in fills.values():
            market = metadata.get(str(fill.get("condition_id") or ""))
            if isinstance(market, Mapping):
                for key in ("market_id", "market_title", "market_url"):
                    if not fill.get(key) and market.get(key) is not None:
                        fill[key] = market.get(key)
                # Resolve the traded outcome from the metadata outcome map so
                # filled rows render their real outcome instead of UNKNOWN.
                if not fill.get("outcome"):
                    token_id = str(fill.get("token_id") or "")
                    outcomes = market.get("outcomes")
                    if token_id and isinstance(outcomes, Mapping):
                        for outcome in outcomes.values():
                            if (
                                isinstance(outcome, Mapping)
                                and str(outcome.get("token_id") or "") == token_id
                                and outcome.get("label") is not None
                            ):
                                fill["outcome"] = outcome.get("label")
                                break
        return fills

    def _lp_reward_cache_snapshot(
        self,
        reward_date: str,
        condition_ids: tuple[str, ...],
        *,
        previous_rewards: Mapping[str, object],
        candidate_rewards: Mapping[str, object],
    ) -> tuple[dict[str, dict[str, object]], tuple[str, ...]]:
        cached: dict[str, dict[str, object]] = {}
        refresh_ids: list[str] = []
        now = self._clock()
        for condition_id in condition_ids:
            key = (reward_date, condition_id)
            reward = self._lp_reward_cache.get(key)
            if reward is None:
                candidate = candidate_rewards.get(condition_id)
                if (
                    isinstance(candidate, Mapping)
                    and str(candidate.get("reward_date") or "") == reward_date
                    and str(candidate.get("condition_id") or "") == condition_id
                ):
                    reward = dict(candidate)
                    if reward.get("stale") is not False:
                        reward["stale"] = True
                        if not reward.get("reason"):
                            reward["reason"] = "reward_candidate_stale"
                else:
                    previous = previous_rewards.get(condition_id)
                    reward = self._lp_reward_unknown(
                        reward_date,
                        condition_id,
                        previous if isinstance(previous, Mapping) else {},
                        attempted_at=None,
                        reason="reward_read_unknown",
                    )
                self._lp_reward_cache[key] = dict(reward)
                if reward.get("stale") is False:
                    self._lp_reward_cached_at[key] = now
            elif reward.get("stale") is False:
                cached_at = self._lp_reward_cached_at.setdefault(key, now)
                if now - cached_at >= _LP_REWARD_CACHE_SECONDS:
                    reward = {**reward, "stale": True, "reason": "reward_cache_expired"}
                    self._lp_reward_cache[key] = reward
            cached[condition_id] = dict(reward)
            if reward.get("stale") is True:
                refresh_ids.append(condition_id)
        return cached, tuple(refresh_ids)


    def _schedule_lp_reward_refresh(
        self, reward_date: str, condition_ids: tuple[str, ...]
    ) -> None:
        if not condition_ids:
            return
        pending = self._lp_reward_refresh_pending.get(reward_date)
        running = self._lp_reward_refresh_thread
        attempted_at = _timestamp(datetime.now(UTC))
        for condition_id in condition_ids:
            key = (reward_date, condition_id)
            if key in self._lp_reward_refresh_active:
                continue
            if pending is None:
                pending = self._lp_reward_refresh_pending.setdefault(reward_date, set())
            pending.add(condition_id)
            reward = self._lp_reward_cache.get(key)
            if reward is None:
                reward = self._lp_reward_unknown(
                    reward_date,
                    condition_id,
                    {},
                    attempted_at=attempted_at,
                    reason="reward_read_pending",
                )
                self._lp_reward_cache[key] = reward
            if reward.get("stale") is True:
                updated = dict(reward)
                updated["last_attempt_at"] = attempted_at
                self._lp_reward_cache[key] = updated
        if running is not None and running.is_alive():
            return
        if pending is None or not pending:
            self._lp_reward_refresh_pending.pop(reward_date, None)
            return
        thread = threading.Thread(
            target=self._refresh_lp_rewards,
            name="lp-reward-refresh",
            daemon=True,
        )
        self._lp_reward_refresh_thread = thread
        thread.start()


    def _refresh_lp_rewards(self) -> None:
        try:
            while True:
                with self._lp_dashboard_lock:
                    if not self._lp_reward_refresh_pending:
                        if (
                            self._lp_reward_refresh_thread
                            is threading.current_thread()
                        ):
                            self._lp_reward_refresh_thread = None
                        return
                    reward_date, pending = next(
                        iter(self._lp_reward_refresh_pending.items())
                    )
                    condition_ids = tuple(pending)
                    self._lp_reward_refresh_pending.pop(reward_date, None)
                    active = {
                        (reward_date, condition_id) for condition_id in condition_ids
                    }
                    self._lp_reward_refresh_active.update(active)
                    attempted_at = _timestamp(datetime.now(UTC))
                try:
                    self._refresh_lp_reward_batch(
                        reward_date, condition_ids, attempted_at
                    )
                except Exception:
                    pass
                finally:
                    with self._lp_dashboard_lock:
                        self._lp_reward_refresh_active.difference_update(active)
        finally:
            with self._lp_dashboard_lock:
                if self._lp_reward_refresh_thread is threading.current_thread():
                    self._lp_reward_refresh_thread = None


    def _refresh_lp_reward_batch(
        self,
        reward_date: str,
        condition_ids: tuple[str, ...],
        attempted_at: str,
    ) -> None:
        observed_by_condition: dict[str, object] = {}
        try:
            batch_reader = getattr(self._trading, "lp_reward_snapshots", None)
            if callable(batch_reader):
                observed = _call(batch_reader, reward_date, condition_ids)
                if isinstance(observed, Mapping):
                    observed_by_condition = {
                        str(condition_id): value
                        for condition_id, value in observed.items()
                    }
            else:
                reader = getattr(self._trading, "lp_reward_snapshot", None)
                if callable(reader):
                    for condition_id in condition_ids:
                        try:
                            observed_by_condition[condition_id] = _call(
                                reader, reward_date, condition_id
                            )
                        except Exception:
                            observed_by_condition[condition_id] = None
        except Exception:
            observed_by_condition = {}

        observed_at = _timestamp(datetime.now(UTC))
        updated_rewards: dict[str, dict[str, object]] = {}
        with self._lp_dashboard_lock:
            for condition_id in condition_ids:
                key = (reward_date, condition_id)
                previous = self._lp_reward_cache.get(key, {})
                updated = self._lp_reward_entry(
                    observed_by_condition.get(condition_id),
                    reward_date=reward_date,
                    condition_id=condition_id,
                    previous=previous,
                    attempted_at=attempted_at,
                    observed_at=observed_at,
                )
                self._lp_reward_cache[key] = updated
                if updated.get("stale") is False:
                    self._lp_reward_cached_at[key] = self._clock()
                updated_rewards[condition_id] = updated
            cached = self._lp_dashboard_cache
            if isinstance(cached, Mapping):
                raw_rewards = cached.get("market_rewards")
                market_rewards = (
                    dict(raw_rewards) if isinstance(raw_rewards, Mapping) else {}
                )
                for condition_id, updated in updated_rewards.items():
                    current = market_rewards.get(condition_id)
                    if (
                        isinstance(current, Mapping)
                        and str(current.get("reward_date") or "") == reward_date
                    ):
                        market_rewards[condition_id] = updated
                if market_rewards != raw_rewards:
                    self._lp_dashboard_cache = {
                        **cached,
                        "market_rewards": market_rewards,
                    }


    @staticmethod
    def _lp_reward_entry(
        observed: object,
        *,
        reward_date: str,
        condition_id: str,
        previous: Mapping[str, object],
        attempted_at: str,
        observed_at: str,
    ) -> dict[str, object]:
        raw_market: Mapping[str, object] = (
            observed if isinstance(observed, Mapping) else {}
        )
        accruals = raw_market.get("market_accruals_raw")
        has_raw_amount = _decimal(raw_market.get("market_amount_raw")) is not None or (
            isinstance(accruals, (list, tuple)) and bool(accruals)
        )
        identity_matches = (
            str(raw_market.get("reward_date") or "") == reward_date
            and str(raw_market.get("condition_id") or "") == condition_id
        )
        read_complete = identity_matches and (
            raw_market.get("state") == "known"
            or (
                raw_market.get("state") == "unknown"
                and raw_market.get("reason") == "usd_value_unknown"
                and has_raw_amount
            )
        )
        if read_complete:
            return {
                "state": raw_market.get("state", "unknown"),
                "usd_state": raw_market.get("usd_state", "unknown"),
                "reward_date": reward_date,
                "condition_id": condition_id,
                "market_amount": raw_market.get("market_amount"),
                "market_amount_raw": raw_market.get("market_amount_raw"),
                "market_asset": raw_market.get("market_asset"),
                "market_accruals_raw": accruals,
                "checked_at": observed_at,
                "last_success_at": observed_at,
                "last_attempt_at": observed_at,
                "stale": False,
                "source": "platform_earnings",
                "currency": "USD",
                "paid": False,
                "reason": raw_market.get("reason"),
            }
        return PredictionExecutionService._lp_reward_unknown(
            reward_date,
            condition_id,
            previous,
            attempted_at=attempted_at,
            reason=str(raw_market.get("reason") or "reward_read_unknown"),
        )


    @staticmethod
    def _lp_reward_unknown(
        reward_date: str,
        condition_id: str,
        previous: Mapping[str, object],
        *,
        attempted_at: str | None,
        reason: str,
    ) -> dict[str, object]:
        retained = (
            previous
            if str(previous.get("reward_date") or "") == reward_date
            and str(previous.get("condition_id") or "") == condition_id
            else {}
        )
        return {
            "state": "unknown",
            "usd_state": "unknown",
            "reward_date": reward_date,
            "condition_id": condition_id,
            "market_amount": retained.get("market_amount"),
            "market_amount_raw": retained.get("market_amount_raw"),
            "market_asset": retained.get("market_asset"),
            "market_accruals_raw": retained.get("market_accruals_raw"),
            "checked_at": retained.get("checked_at"),
            "last_success_at": retained.get("last_success_at"),
            "last_attempt_at": attempted_at,
            "stale": True,
            "source": "platform_earnings",
            "currency": "USD",
            "paid": False,
            "reason": reason,
        }


    def lp_report(self, report_date: str) -> dict[str, object] | None:
        """Read one immutable stored LP daily report."""

        reader = getattr(self._store, "lp_daily_report", None)
        if not callable(reader):
            return None
        report = reader(report_date)
        return dict(report) if isinstance(report, Mapping) else None

    def lp_start(self, preview_id: str, idempotency_key: str) -> dict[str, object]:
        """Start one LP session under the shared execution mutex."""

        service = self._lp
        start = getattr(service, "start", None)
        if not callable(start):
            return {"state": "rejected", "reason": "lp_unavailable"}
        key = str(idempotency_key).strip()
        if not key:
            return {"state": "rejected", "reason": "idempotency_key_required"}
        existing = self._store.lp_session_by_idempotency(key)
        if existing is not None:
            return self.lp_status(str(existing["session_id"]))
        if self._breaker_is_open():
            return {"state": "locked", "reason": "circuit_breaker_open"}
        active_lp = self._store.lp_active_session()
        if active_lp is not None:
            return {
                "state": "busy",
                "reason": "active_lp_session",
                "session_id": active_lp.get("session_id"),
            }
        active = self._store.active_execution()
        if active is not None:
            return {
                "state": "busy",
                "reason": "active_execution",
                "execution_id": active.get("execution_id"),
            }
        lock = self._acquire_global_lock()
        if lock is None:
            active_lp = self._store.lp_active_session()
            if active_lp is not None:
                return {
                    "state": "busy",
                    "reason": "active_lp_session",
                    "session_id": active_lp.get("session_id"),
                }
            return {"state": "busy", "reason": "execution_lock"}
        try:
            existing = self._store.lp_session_by_idempotency(key)
            if existing is not None:
                return self.lp_status(str(existing["session_id"]))
            active_lp = self._store.lp_active_session()
            if active_lp is not None:
                return {
                    "state": "busy",
                    "reason": "active_lp_session",
                    "session_id": active_lp.get("session_id"),
                }
            active = self._store.active_execution()
            if active is not None:
                return {
                    "state": "busy",
                    "reason": "active_execution",
                    "execution_id": active.get("execution_id"),
                }
            return start(str(preview_id), key)
        finally:
            self._release_global_lock(lock)

    def lp_stop(self, session_id: str | None = None) -> dict[str, object]:
        """Cancel only this LP session's open orders under the shared mutex."""

        service = self._lp
        stop = getattr(service, "stop", None)
        if not callable(stop):
            return {"state": "none", "session_id": None, "reason": "lp_unavailable"}
        lock = self._acquire_global_lock()
        if lock is None:
            return {"state": "busy", "reason": "execution_lock"}
        try:
            return stop(session_id)
        finally:
            self._release_global_lock(lock)

    def lp_tick(self) -> dict[str, object]:
        """Run one LP reconciliation iteration under the shared mutex."""

        service = self._lp
        tick = getattr(service, "tick", None)
        if not callable(tick):
            return {"state": "none", "session_id": None, "reason": "lp_unavailable"}
        lock = self._acquire_global_lock()
        if lock is None:
            return {"state": "busy", "reason": "execution_lock"}
        try:
            return tick()
        finally:
            self._release_global_lock(lock)

    def set_cross_venue_monitor(self, monitor: object) -> None:
        self._cross_venue_monitor = monitor

    def _predict_canary_fingerprint(self) -> str | None:
        snapshot = self._live_predict_account_snapshot()
        if snapshot is None:
            return None
        values = [
            snapshot.get("sdk_version", snapshot.get("predict_sdk_version")) or self._predict_sdk_version(),
            snapshot.get("predict_account"),
            snapshot.get("gas_signer"),
            snapshot.get("chain", snapshot.get("chain_id")) or "BNB_MAINNET",
            snapshot.get("approval_step_id"),
            "set_exact_buy_allowance:v1/clear_buy_allowance:v1",
        ]
        if any(not isinstance(value, str) or not value.strip() for value in values):
            return None
        return "|".join(str(value).strip() for value in values)

    @staticmethod
    def _predict_sdk_version() -> str:
        try:
            return importlib.metadata.version("predict-sdk")
        except importlib.metadata.PackageNotFoundError:
            return "predict-sdk-installed"

    def _cross_canary_verified(self, fingerprint: str) -> bool:
        for row in self._store.histories("executions"):
            evidence = row.get("evidence")
            if not isinstance(evidence, (list, tuple)):
                continue
            for item in evidence:
                allowance = item.get("predict_allowance") if isinstance(item, Mapping) else None
                if (
                    isinstance(item, Mapping)
                    and item.get("canary_verified") is True
                    and item.get("canary_fingerprint") == fingerprint
                    and isinstance(allowance, Mapping)
                    and allowance.get("zero_verified") is True
                ):
                    return True
        return False

    def preview(
        self,
        opportunity_id: str,
        *,
        auto_eat: bool = False,
        auto_submit: bool = False,
    ) -> dict[str, object]:
        """Freshly validate one server-issued opportunity and persist a preview."""

        cross_preview = str(opportunity_id).startswith("cross:")
        cross_fingerprint = self._predict_canary_fingerprint() if cross_preview else None
        cross_cap = (
            Decimal("20")
            if cross_fingerprint is not None
            and self._cross_canary_verified(cross_fingerprint)
            else Decimal("5")
        )
        prepared = self._prepare_opportunity(
            str(opportunity_id),
            cross_max_total_cost=cross_cap if cross_preview else None,
            cross_prefer_smallest=cross_preview,
            cross_canary_fingerprint=cross_fingerprint,
            cross_canary_cap=cross_cap if cross_preview else None,
            cross_auto_submit=auto_submit,
        )
        if isinstance(prepared, dict):
            return prepared
        opportunity, intent, account = prepared
        if (
            cross_preview
            and isinstance(intent, CrossVenueIntent)
            and intent.total_max_cost > cross_cap
        ):
            return {
                "state": "rejected",
                "reason": "cross_venue_minimum_exceeds_canary",
                "current": format(intent.total_max_cost, "f"),
                "limit": format(cross_cap, "f"),
            }
        if (auto_eat or auto_submit) and isinstance(intent, CrossVenueIntent) and intent.manual_only:
            return {"state": "rejected", "reason": "manual_only_requires_approval"}

        now = _utc_now()
        expires_at = now + PREVIEW_TTL
        payload = self._preview_payload(
            opportunity, intent, account=account, expires_at=expires_at
        )
        if auto_eat:
            payload["auto_eat"] = True
        if auto_submit:
            payload["auto_submit"] = True
        preview_id = self._store.create_preview(
            payload, expires_at=_timestamp(expires_at)
        )
        result = dict(payload)
        result.update(
            {
                "id": preview_id,
                "preview_id": preview_id,
                "state": "previewed",
            }
        )
        if not isinstance(intent, CrossVenueIntent):
            result["expires_at"] = _timestamp(expires_at)
        return result

    def _prepare_opportunity(
        self,
        opportunity_id: str,
        *,
        cross_target_quantity: Decimal | None = None,
        cross_max_total_cost: Decimal | None = None,
        cross_prefer_smallest: bool = False,
        cross_canary_fingerprint: str | None = None,
        cross_canary_cap: Decimal | None = None,
        cross_auto_submit: bool = False,
    ) -> tuple[dict[str, object], ExecutionIntent, dict[str, object]] | dict[str, object]:
        """Run the shared read-only admission checks used by preview and notify."""

        if self._breaker_is_open():
            return {"state": "locked", "reason": "circuit_breaker_open"}
        active = self._store.active_execution()
        if active is not None:
            return {
                "state": "busy",
                "reason": "active_execution",
                "execution_id": active["execution_id"],
            }
        probe = self._acquire_global_lock()
        if probe is None:
            return {"state": "busy", "reason": "execution_lock"}
        self._release_global_lock(probe)

        opportunity = self._fresh_opportunity(
            str(opportunity_id),
            target_quantity=cross_target_quantity,
            max_total_cost=cross_max_total_cost,
            prefer_smallest=cross_prefer_smallest,
        )
        if (
            opportunity is not None
            and opportunity.get("market_type") == "cross_venue_yes_no"
            and self._cross_breaker_open
        ):
            return {"state": "locked", "reason": "cross_circuit_breaker_open"}
        intent = self._intent_from_opportunity(opportunity)
        if opportunity is None or intent is None:
            return {"state": "rejected", "reason": "opportunity_unavailable"}
        reason = self._validate_opportunity(
            opportunity, intent, auto_submit=cross_auto_submit
        )
        if reason is not None:
            return {"state": "rejected", "reason": reason}
        account, reason = self._volatile_checks(intent)
        if account is None:
            return {"state": "rejected", "reason": reason or "readiness_unavailable"}
        if isinstance(intent, CrossVenueIntent):
            account["canary_fingerprint"] = (
                cross_canary_fingerprint or self._predict_canary_fingerprint()
            )
            account["canary_cap"] = cross_canary_cap or Decimal("5")
        return opportunity, intent, account

    def notify_ready_opportunity(
        self, opportunity_id: str, signal_id: str
    ) -> dict[str, object]:
        """Deliver one observation-only alert after a fresh no-submit proof."""

        signal = self._store.signal(str(signal_id))
        if signal is None:
            return {"state": "ignored", "reason": "signal_unavailable"}
        if (
            signal.get("market_type") == "threshold_hedge"
            and self._store.get_validation_mode() == "auto"
        ):
            return {"state": "ignored", "reason": "mode_auto"}
        if signal.get("market_type") == "cross_venue_yes_no":
            if self._configured_cross_execution_mode() == "auto_submit":
                return self.auto_submit_cross_venue(
                    str(opportunity_id), str(signal_id)
                )
            return self._notify_cross_venue_signal(
                str(opportunity_id), str(signal_id), signal
            )
        if signal.get("market_type") == "standard_binary":
            return self._notify_yes_no_signal(str(signal_id), signal)
        if signal.get("ended_at") is not None:
            return {"state": "ignored", "reason": "signal_closed"}
        if signal.get("notification_state") == "sent":
            return {"state": "ignored", "reason": "already_sent"}
        attempts = _decimal(signal.get("notification_attempts")) or Decimal("0")
        if attempts >= 3:
            return {"state": "ignored", "reason": "notification_attempts_exhausted"}

        prepared = self._prepare_opportunity(str(opportunity_id))
        if isinstance(prepared, dict):
            return {"state": "failed", "reason": prepared.get("reason", "not_ready")}
        opportunity, intent, _account = prepared
        if not isinstance(intent, ThresholdHedgeIntent):
            return {"state": "failed", "reason": "unsupported_intent"}
        if opportunity.get("rules_verified_at") in (None, ""):
            return {"state": "failed", "reason": "rules_unverified"}
        if not self._codex_approved(opportunity):
            return {"state": "failed", "reason": "codex_not_approved"}
        if opportunity.get("remediation_safe") is False:
            return {"state": "failed", "reason": "emergency_unwind_unavailable"}
        if intent.minimum_profit <= 0 or intent.net_edge <= 0:
            return {"state": "failed", "reason": "threshold_economics"}

        preflight = getattr(self._trading, "no_submit_threshold_preflight", None)
        if not callable(preflight):
            return {"state": "failed", "reason": "threshold_preflight_unavailable"}
        try:
            preflight_result = _call(preflight, intent)
        except Exception:
            preflight_result = None
        if not self._preflight_passed(preflight_result):
            return {
                "state": "failed",
                "reason": self._preflight_error_code(preflight_result)
                or "preflight_failed",
            }

        final = self._fresh_opportunity(str(opportunity_id))
        final_intent = self._intent_from_opportunity(final)
        if final is None or not isinstance(final_intent, ThresholdHedgeIntent):
            return {"state": "failed", "reason": "opportunity_changed"}
        if self._intent_payload(final_intent) != self._intent_payload(intent):
            return {"state": "failed", "reason": "opportunity_changed"}
        final_reason = self._validate_opportunity(final, final_intent)
        if final_reason is not None:
            return {"state": "failed", "reason": final_reason}
        final_age = _decimal(final.get("confirmed_age_seconds"))
        if final_age is None or final_age > BOOK_FRESHNESS_SECONDS:
            return {"state": "failed", "reason": "books_stale"}
        if final_intent.minimum_profit <= 0 or final_intent.net_edge <= 0:
            return {"state": "failed", "reason": "threshold_economics"}
        if final.get("rules_verified_at") in (None, ""):
            return {"state": "failed", "reason": "opportunity_changed"}
        if not self._codex_approved(final):
            return {"state": "failed", "reason": "opportunity_changed"}
        if final.get("remediation_safe") is False:
            return {"state": "failed", "reason": "opportunity_changed"}
        for key in ("rules_hash_a", "rules_hash_b", "cache_key", "rules_fingerprint"):
            if opportunity.get(key) != final.get(key):
                return {"state": "failed", "reason": "opportunity_changed"}

        order_ready_at = _timestamp(_utc_now())
        reservation = self._store.reserve_notification_attempt(
            str(signal_id),
            max_attempts=3,
            lease_seconds=60.0,
            order_ready_at=order_ready_at,
        )
        reservation_state = reservation.get("state")
        if reservation_state in {"missing", "closed"}:
            return {"state": "ignored", "reason": "signal_closed" if reservation_state == "closed" else "signal_unavailable"}
        if reservation_state == "sent":
            return {"state": "ignored", "reason": "already_sent"}
        if reservation_state == "in_flight":
            return {"state": "ignored", "reason": "notification_in_flight"}
        if reservation_state == "exhausted":
            return {"state": "ignored", "reason": "notification_attempts_exhausted"}
        lease_id = reservation.get("lease_id")
        current = reservation.get("signal")
        if not isinstance(lease_id, str) or not isinstance(current, Mapping):
            return {"state": "failed", "reason": "notification_state_unavailable"}

        final = dict(final)
        final["order_ready_at"] = current.get("order_ready_at", order_ready_at)
        try:
            title, message = render_prediction_opportunity_notification(
                final, current, dashboard_url=self._dashboard_url
            )
            feishu_success = self._deliver_feishu_notification(title, message)
        except Exception:
            feishu_success = False
        completion = self._store.complete_notification_attempt(
            str(signal_id),
            lease_id,
            success=feishu_success,
            error_code="delivery_failed",
        )
        if completion.get("state") == "sent":
            return {"state": "sent", "signal_id": str(signal_id)}
        if completion.get("state") == "closed":
            return {"state": "ignored", "reason": "signal_closed"}
        return {"state": "failed", "reason": "notification_failed"}

    def set_validation_mode(
        self, mode: str, *, audit: Mapping[str, object] | None = None
    ) -> dict[str, object]:
        ranks = {"observe_only": 0, "manual": 1, "auto": 2}
        current = self._store.get_validation_mode()
        if mode in ranks and ranks[mode] > ranks[current]:
            if self._breaker_is_open():
                return {"state": "locked", "reason": "circuit_breaker_open"}
            if self._store.active_execution() is not None:
                return {"state": "busy", "reason": "active_execution"}
            lock = self._acquire_global_lock()
            if lock is None:
                return {"state": "busy", "reason": "control_in_progress"}
            try:
                if self._breaker_is_open():
                    return {"state": "locked", "reason": "circuit_breaker_open"}
                if self._store.active_execution() is not None:
                    return {"state": "busy", "reason": "active_execution"}
                value = self._store.set_validation_mode(mode, audit=audit)
            except ValueError as exc:
                return {"state": "rejected", "reason": str(exc)}
            finally:
                self._release_global_lock(lock)
            return {"state": "ok", "mode": value}
        try:
            value = self._store.set_validation_mode(mode, audit=audit)
        except ValueError as exc:
            return {"state": "rejected", "reason": str(exc)}
        return {"state": "ok", "mode": value}

    def n_leg_mode_contract(self) -> dict[str, object]:
        from .prediction_n_leg_mode import n_leg_mode_contract

        return n_leg_mode_contract(self._store)

    def n_leg_set_mode(
        self,
        mode: str,
        *,
        base_contract_generation: int,
        incident_id: object = None,
        audit: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        from .prediction_n_leg_mode import n_leg_set_mode

        return n_leg_set_mode(
            self._store,
            mode=mode,
            base_contract_generation=base_contract_generation,
            incident_id=incident_id,
            audit=audit,
        )

    def n_leg_update_qualification_policy(
        self,
        policy: object,
        *,
        base_version: int,
        audit: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        from .prediction_n_leg_mode import n_leg_update_qualification_policy

        return n_leg_update_qualification_policy(
            self._store, policy=policy, base_version=base_version, audit=audit
        )

    def n_leg_update_safety_config(
        self,
        config: object,
        *,
        base_version: int,
        audit: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        from .prediction_n_leg_mode import n_leg_update_safety_config

        return n_leg_update_safety_config(
            self._store, config=config, base_version=base_version, audit=audit
        )

    def n_leg_upsert_scope(
        self,
        scope_id: str,
        *,
        capability: str,
        members: object,
        base_scope_version: int | None = None,
        audit: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        from .prediction_n_leg_mode import n_leg_upsert_scope

        return n_leg_upsert_scope(
            self._store,
            scope_id=scope_id,
            capability=capability,
            members=members,
            base_scope_version=base_scope_version,
            audit=audit,
        )

    def n_leg_set_enabled_scope(
        self,
        scope_id: str,
        *,
        enable: bool,
        base_contract_generation: int,
        audit: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        from .prediction_n_leg_mode import n_leg_set_enabled_scope

        return n_leg_set_enabled_scope(
            self._store,
            scope_id=scope_id,
            enable=enable,
            base_contract_generation=base_contract_generation,
            audit=audit,
        )

    def n_leg_order_readiness(self) -> dict[str, object]:
        from .prediction_n_leg_mode import n_leg_order_readiness

        return n_leg_order_readiness(self._store)

    def n_leg_enforce_auto_scope_versions(
        self, *, audit: Mapping[str, object] | None = None
    ) -> dict[str, object]:
        from .prediction_n_leg_mode import n_leg_enforce_auto_scope_versions

        return n_leg_enforce_auto_scope_versions(self._store, audit=audit)

    def auto_eat_threshold(
        self, opportunity_id: str, signal_id: str
    ) -> dict[str, object]:
        signal = self._store.signal(str(signal_id))
        if signal is None:
            return {"state": "ignored", "reason": "signal_unavailable"}
        if self._store.get_validation_mode() != "auto":
            return {"state": "ignored", "reason": "mode_not_auto"}
        market_id = str(signal.get("market_id") or "")
        prepared = self._prepare_opportunity(str(opportunity_id))
        if isinstance(prepared, dict):
            reason = str(prepared.get("reason") or "not_ready")
            if reason != "circuit_breaker_open":
                self._store.record_auto_eat_attempt(
                    signal_id=str(signal_id), market_id=market_id,
                    decision="rejected", reason=reason,
                )
            return prepared
        opportunity, intent, account = prepared
        if not isinstance(intent, ThresholdHedgeIntent):
            self._store.record_auto_eat_attempt(
                signal_id=str(signal_id), market_id=market_id,
                decision="rejected", reason="unsupported_intent",
            )
            return {"state": "rejected", "reason": "unsupported_intent"}
        annualized = _decimal(opportunity.get("annualized_yield"))
        if annualized is None or annualized <= MIN_THRESHOLD_ANNUALIZED_YIELD:
            self._store.record_auto_eat_attempt(
                signal_id=str(signal_id), market_id=market_id,
                decision="rejected", reason="annualized_below_minimum",
            )
            return {"state": "rejected", "reason": "annualized_below_minimum"}
        if intent.minimum_profit <= 0 or intent.net_edge <= 0:
            self._store.record_auto_eat_attempt(
                signal_id=str(signal_id), market_id=market_id,
                decision="rejected", reason="threshold_economics",
            )
            return {"state": "rejected", "reason": "threshold_economics"}
        balance = _decimal(account.get("p_usd_balance")) or Decimal("0")
        allowed, reason = _validation_should_eat(
            store=self._store, signal=signal, intent=intent,
            balance=balance, now=datetime.now(UTC),
        )
        if not allowed:
            self._store.record_auto_eat_attempt(
                signal_id=str(signal_id), market_id=market_id,
                decision="rejected", reason=reason,
            )
            return {"state": "rejected", "reason": reason}
        preview_result = self.preview(str(opportunity_id), auto_eat=True)
        if not isinstance(preview_result, Mapping) or preview_result.get("state") != "previewed":
            reason = str(preview_result.get("reason") or "preview_failed")
            self._store.record_auto_eat_attempt(
                signal_id=str(signal_id), market_id=market_id,
                decision="rejected", reason=reason,
            )
            return preview_result
        preview_id = str(preview_result["preview_id"])
        confirm_result = self.confirm(preview_id, str(signal_id))
        execution_id = str(confirm_result.get("execution_id") or "")
        confirm_state = str(confirm_result.get("state") or "")
        if execution_id or confirm_state in {
            "validating", "submitting", "reconciling", "holding_to_resolution",
        }:
            self._store.record_auto_eat_attempt(
                signal_id=str(signal_id), market_id=market_id,
                decision="submitted", preview_id=preview_id,
                execution_id=execution_id, total_cost=intent.total_max_cost,
            )
            return confirm_result
        reason = str(confirm_result.get("reason") or "confirm_failed")
        self._store.record_auto_eat_attempt(
            signal_id=str(signal_id), market_id=market_id,
            decision="rejected", reason=reason, preview_id=preview_id,
        )
        return confirm_result

    def _notify_threshold_submitted(
        self, leg: ThresholdHedgeLeg, result: ThresholdLegResult
    ) -> None:
        try:
            title = "预测套利单已提交"
            message = (
                f"{leg.label}｜{leg.outcome}\n"
                f"限价：${leg.max_price:.4f}\n"
                f"数量：{format(leg.quantity, 'f')}\n"
                f"订单号：{result.order_id}"
            )
            self._deliver_feishu_notification(title, message)
        except Exception:
            return

    def _notify_threshold_filled(
        self, leg: ThresholdHedgeLeg, order_id: str, quantity: Decimal
    ) -> None:
        try:
            title = "预测套利单已吃"
            message = (
                f"{leg.label}｜{leg.outcome}\n"
                f"成交数量：{format(quantity, 'f')}\n"
                f"订单号：{order_id}"
            )
            self._deliver_feishu_notification(title, message)
        except Exception:
            return

    def _notify_threshold_rejected(
        self, leg: ThresholdHedgeLeg, result: ThresholdLegResult
    ) -> None:
        try:
            title = "预测套利单提交失败"
            message = (
                f"{leg.label}｜{leg.outcome}\n"
                f"失败原因：{result.error_code}\n"
                f"{_threshold_error_hint(result.error_code)}"
            )
            self._deliver_feishu_notification(title, message)
        except Exception:
            return

    def _notify_threshold_order_failed(
        self,
        execution_id: str,
        reason: str,
        *,
        preflight: Mapping[str, object] | None = None,
    ) -> None:
        payload = self._store.execution_payload(execution_id)
        if not isinstance(payload, Mapping) or not isinstance(
            self._intent_from_payload(payload.get("intent")),
            ThresholdHedgeIntent,
        ):
            return
        try:
            title = "预测套利单提交失败"
            message = f"原因：{reason}\n{_threshold_error_hint(reason)}"
            if preflight:
                lines = [
                    f"{label} {_preflight_evidence_mark(preflight[key])}"
                    for key, label in _THRESHOLD_EVIDENCE_LABELS
                    if key in preflight
                ]
                if lines:
                    message = "\n".join([message, *lines])
            self._deliver_feishu_notification(title, message)
        except Exception:
            return

    def _notify_threshold_settlement(
        self,
        execution_id: str,
        intent: ThresholdHedgeIntent,
        quantity: Decimal,
        proof: Mapping[str, object],
    ) -> None:
        expected = intent.minimum_profit
        actual = quantity - (
            intent.total_max_cost / intent.quantity * quantity
        )
        try:
            title = "预测套利单结算"
            message = (
                f"关系 {intent.relation_id}\n"
                f"成交数量 {quantity}\n"
                f"预计利润 ${expected:.4f}\n"
                f"实际锁定利润 ${actual:.4f}\n"
                f"证明已验证: {proof.get('verified') is True}"
            )
            self._deliver_feishu_notification(title, message)
        except Exception:
            return

    def notify_observation(
        self,
        opportunity: Mapping[str, object],
        signal_id: str,
        lease_id: str,
    ) -> dict[str, object]:
        """Deliver the immediate observation alert reserved by the monitor."""

        signal = self._store.signal(str(signal_id))
        if signal is None:
            return {"state": "ignored", "reason": "signal_unavailable"}
        if signal.get("observation_state") == "sent":
            return {"state": "ignored", "reason": "already_sent"}
        attempts = _decimal(signal.get("observation_attempts")) or Decimal("0")
        if attempts >= 3:
            return {"state": "ignored", "reason": "notification_attempts_exhausted"}
        market_id = str(signal.get("market_id", "")).strip()
        if self._store.notification_sent_since(
            market_id, _utc_now() - timedelta(minutes=30), kind="observation"
        ):
            self._store.update_signal(
                signal_id,
                {
                    "observation_state": "suppressed",
                    "observation_suppressed_reason": "market_cooldown",
                },
            )
            return {"state": "ignored", "reason": "market_cooldown"}
        try:
            title, message = render_prediction_opportunity_notification(
                opportunity,
                signal,
                dashboard_url=self._dashboard_url,
                kind="observation",
            )
            feishu_success = self._deliver_feishu_notification(title, message)
        except Exception:
            feishu_success = False
        completion = self._store.complete_notification_attempt(
            str(signal_id),
            str(lease_id),
            kind="observation",
            success=feishu_success,
            error_code="delivery_failed",
        )
        if completion.get("state") == "sent":
            return {"state": "sent", "signal_id": str(signal_id)}
        if completion.get("state") == "closed":
            return {"state": "ignored", "reason": "signal_closed"}
        return {"state": "failed", "reason": "notification_failed"}

    def _notify_cross_venue_signal(
        self,
        opportunity_id: str,
        signal_id: str,
        signal: Mapping[str, object],
    ) -> dict[str, object]:
        """Recheck a cross signal without creating an execution preview."""

        prepared = self._prepare_opportunity(opportunity_id)
        if isinstance(prepared, dict):
            reason = str(prepared.get("reason", "not_ready"))
            if reason == "insufficient_bnb":
                fresh = self._fresh_opportunity(opportunity_id)
                if (
                    not isinstance(fresh, Mapping)
                    or cross_venue_notification_dedupe_identity(fresh)
                    != signal.get("notification_dedupe_identity")
                ):
                    return {"state": "failed", "reason": "opportunity_changed"}
                return self._notify_cross_gas_blocked_signal(signal_id, signal)
            return {"state": "failed", "reason": reason}
        opportunity, intent, _account = prepared
        if not isinstance(intent, CrossVenueIntent):
            return {"state": "failed", "reason": "unsupported_intent"}
        identity = cross_venue_notification_dedupe_identity(opportunity)
        if (
            identity is None
            or identity != signal.get("notification_dedupe_identity")
        ):
            return {"state": "failed", "reason": "opportunity_changed"}
        return self._notify_yes_no_signal(
            signal_id, signal, rendered_signal={**opportunity, "signal_id": signal_id}
        )

    _CROSS_AUTO_REASONS: dict[str, tuple[str, str, bool, str]] = {
        "configured_mode_not_auto_submit": (
            "当前配置模式不是自动下单",
            "系统",
            True,
            "prediction-arb cross-auto status --url http://127.0.0.1:8769",
        ),
        "cross_auto_paused": (
            "自动下单已暂停",
            "系统",
            True,
            "prediction-arb cross-auto status --url http://127.0.0.1:8769",
        ),
        "cross_auto_daily_principal_cap": ("当日自动本金已达到上限", "跨市场", False, ""),
        "cross_pair_unsettled": ("同一市场对仍有未结算仓位", "跨市场", True, ""),
        "active_execution": ("已有执行正在进行", "系统", False, ""),
        "execution_lock": ("已有执行正在进行", "系统", False, ""),
        "books_stale": ("盘口数据已过期", "行情", False, ""),
        "insufficient_bnb": ("BNB Gas 不足", "Predict.fun", True, ""),
        "account_insufficient": ("账户可用余额不足", "账户", True, ""),
        "notification_config_unavailable": ("飞书通知配置不可用", "通知", True, ""),
        "manual_only_requires_approval": ("该机会需要人工审查，自动模式不执行", "规则", True, ""),
        "cross_venue_minimum_exceeds_canary": ("场所最小可执行金额高于当前试探额度", "跨市场", True, ""),
    }

    def auto_submit_cross_venue(
        self, opportunity_id: str, signal_id: str
    ) -> dict[str, object]:
        """Claim one stage-5 episode, then reuse the normal submit state machine."""

        if not self._notification_channels_ready():
            return self._claim_and_finish_cross_auto_rejection(
                signal_id, opportunity_id, {"reason": "notification_config_unavailable"}
            )
        preview = self.preview(opportunity_id, auto_submit=True)
        if preview.get("state") != "previewed":
            return self._claim_and_finish_cross_auto_rejection(
                signal_id, opportunity_id, preview
            )
        result = self.confirm(str(preview["preview_id"]), signal_id)
        if result.get("state") not in {
            "busy", "locked", "rejected", "failed", "ignored"
        }:
            return self._record_cross_auto_result(signal_id, preview, result)
        return self._finish_cross_auto_rejection(signal_id, opportunity_id, result)

    def _claim_and_finish_cross_auto_rejection(
        self,
        signal_id: str,
        opportunity_id: str,
        result: Mapping[str, object],
    ) -> dict[str, object]:
        claim = self._store.claim_cross_auto_attempt(signal_id, opportunity_id)
        if claim["state"] == "signal_already_attempted":
            return {"state": "ignored", "reason": "signal_already_attempted"}
        if claim["state"] == "rejected":
            result = claim
        return self._finish_cross_auto_rejection(signal_id, opportunity_id, result)

    def _cross_auto_facts(
        self,
        reason: str,
        *,
        signal_id: str,
        opportunity_id: str,
    ) -> dict[str, object]:
        reason_zh, venue, required, action = self._CROSS_AUTO_REASONS.get(
            reason, ("自动下单条件不满足", "系统", False, "")
        )
        current: object = None
        limit: object = None
        if reason == "cross_auto_paused":
            current, limit = "paused", "armed"
        elif reason == "configured_mode_not_auto_submit":
            current = self._configured_cross_execution_mode()
            limit = "auto_submit"
        elif reason == "cross_auto_daily_principal_cap":
            current, limit = (
                format(self._store.cross_auto_daily_principal(), "f"),
                "100",
            )
        elif reason == "cross_pair_unsettled":
            current, limit = "1", "1"
        elif reason in {"active_execution", "execution_lock"}:
            current, limit = "1", "1"
        elif reason == "books_stale":
            current, limit = ">10s", "10s"
        elif reason == "insufficient_bnb":
            account = self._live_predict_account_snapshot() or {}
            current = _safe_decimal(account.get("bnb_balance"))
            limit = _safe_decimal(account.get("required_bnb"))
        elif reason == "account_insufficient":
            account = self._live_predict_account_snapshot() or {}
            current = _safe_decimal(account.get("available_usdt"))
            limit = "required"
        elif reason == "notification_config_unavailable":
            current, limit = "unavailable", "ready"
        return {
            "reason_code": reason,
            "reason_zh": reason_zh,
            "current": current,
            "limit": limit,
            "venue": venue,
            "operator_action_required": required,
            "operator_action": action.format(data_dir=self._store.data_dir),
            "signal_id": signal_id,
            "opportunity_id": opportunity_id,
        }

    def _finish_cross_auto_rejection(
        self,
        signal_id: str,
        opportunity_id: str,
        result: Mapping[str, object],
    ) -> dict[str, object]:
        reason = str(result.get("reason", "opportunity_unavailable"))
        if reason == "execution_lock":
            reason = "active_execution"
        facts = self._cross_auto_facts(
            reason, signal_id=signal_id, opportunity_id=opportunity_id
        )
        if result.get("current") is not None:
            facts["current"] = result["current"]
        if result.get("limit") is not None:
            facts["limit"] = result["limit"]
        try:
            self._store.finish_cross_auto_attempt(
                signal_id,
                decision="rejected",
                reason=reason,
                reason_zh=str(facts["reason_zh"]),
                current=facts["current"],
                limit=facts["limit"],
                venue=str(facts["venue"]),
                operator_action_required=bool(facts["operator_action_required"]),
                operator_action=str(facts["operator_action"]),
            )
        except KeyError:
            return {"state": "ignored", "reason": "signal_already_attempted"}
        if facts["operator_action_required"]:
            self._notify_cross_auto_rejection_once(signal_id, facts)
        return {"state": "rejected", "reason": reason, "facts": facts}

    def _record_cross_auto_result(
        self,
        signal_id: str,
        preview: Mapping[str, object],
        result: Mapping[str, object],
    ) -> dict[str, object]:
        try:
            self._store.finish_cross_auto_attempt(
                signal_id,
                decision="submitted",
                reason="submitted",
                reason_zh="已提交双边订单，等待对账",
                venue="跨市场",
                preview_id=str(preview.get("preview_id", "")),
                execution_id=str(result.get("execution_id", "")),
                total_cost=preview.get("total_max_cost"),
            )
        except KeyError:
            return {"state": "ignored", "reason": "signal_already_attempted"}
        return dict(result)

    def _notify_cross_auto_rejection_once(
        self, signal_id: str, facts: Mapping[str, object]
    ) -> None:
        reservation = self._store.reserve_notification_attempt(
            signal_id, max_attempts=1, lease_seconds=60.0
        )
        if reservation.get("state") != "reserved" or not isinstance(
            reservation.get("lease_id"), str
        ):
            return
        lines = [
            f"原因：{facts['reason_zh']}（{facts['reason_code']}）",
            f"场所：{facts['venue']}",
            f"当前/限制：{facts['current']} / {facts['limit']}",
        ]
        if facts.get("operator_action"):
            lines.append(f"操作：{facts['operator_action']}")
        lines.extend(
            (f"机会编号：{facts['signal_id']}", f"Dashboard：{self._dashboard_url}")
        )
        message = "\n".join(lines)
        try:
            sent = self._deliver_feishu_notification("自动下单已拒绝", message)
        except Exception:
            sent = False
        self._store.complete_notification_attempt(
            signal_id,
            str(reservation["lease_id"]),
            success=sent,
            error_code="delivery_failed",
        )

    def _configured_cross_execution_mode(self) -> str:
        if self._legacy_strategy_retired:
            return "observe_only"
        try:
            return validate_cross_execution_mode(
                self._store.cross_auto_state().get("configured_mode")
            )
        except Exception:
            return "observe_only"

    def _cross_auto_monitor_ready(self) -> bool:
        snapshot = getattr(self._cross_venue_monitor, "snapshot", None)
        try:
            current = _call(snapshot) if callable(snapshot) else None
        except Exception:
            return False
        if not isinstance(current, Mapping) or current.get("status") != "ready":
            return False
        readiness = current.get("readiness")
        if isinstance(readiness, Mapping) and readiness.get("status") != "ready":
            return False

        primary_snapshot = getattr(self._monitor, "snapshot", None)
        try:
            primary = _call(primary_snapshot) if callable(primary_snapshot) else None
        except Exception:
            return False
        if not isinstance(primary, Mapping):
            return False
        primary_status = str(primary.get("status", "")).casefold()
        if primary_status not in {"healthy", "ready"}:
            return False
        primary_readiness = primary.get("readiness")
        if not isinstance(primary_readiness, Mapping):
            return False
        accepted = (True, "ready", "allowed", "pass", "confirmed")
        for key in ("wallet", "wallet_ready", "geoblock", "relayer", "relayer_readiness"):
            if key in primary_readiness and primary_readiness[key] not in accepted:
                return False
        if "geoblock" not in primary_readiness:
            return False
        if "relayer" not in primary_readiness and "relayer_readiness" not in primary_readiness:
            return False
        if str(primary_readiness.get("status", "ready")).casefold() in {
            "unavailable", "blocked", "fail", "failed"
        }:
            return False
        return "balance" in primary_readiness or "p_usd_balance" in primary_readiness

    def cross_auto_status(self) -> dict[str, object]:
        try:
            state = self._store.cross_auto_state()
        except Exception:
            state = {
                "configured_mode": "observe_only",
                "armed": False,
                "reason": "not_armed",
            }
        configured = validate_cross_execution_mode(state.get("configured_mode"))
        armed = state.get("armed") is True
        notification_ready = self._notification_channels_ready()
        ready = self._cross_auto_monitor_ready() and notification_ready
        latest = self._store.cross_auto_attempts(limit=1)
        effective = configured
        if configured == "auto_submit" and not (armed and ready):
            effective = "observe_only"
        if self._legacy_strategy_retired:
            # Issue #60: N_LEG era — the legacy cross-auto effective mode is
            # observe-only no matter what is configured or armed.
            effective = "observe_only"
        return {
            "configured_mode": configured,
            "effective_mode": effective,
            "armed": armed,
            "pause_reason": "" if armed else str(state.get("reason", "not_armed")),
            "notification_ready": notification_ready,
            "daily_principal": {
                "current": format(self._store.cross_auto_daily_principal(), "f"),
                "limit": "100",
            },
            "latest_attempt": latest[0] if latest else None,
        }

    def pause_cross_auto(
        self,
        reason: str = "operator_paused",
        *,
        audit: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        return self._store.pause_cross_auto(reason, audit=audit)

    def _notify_cross_gas_blocked_signal(
        self, signal_id: str, signal: Mapping[str, object]
    ) -> dict[str, object]:
        reservation = self._store.reserve_notification_attempt(
            signal_id, max_attempts=3, lease_seconds=60.0
        )
        state = reservation.get("state")
        if state in {"missing", "closed"}:
            return {"state": "ignored", "reason": "signal_closed" if state == "closed" else "signal_unavailable"}
        if state == "sent":
            return {"state": "ignored", "reason": "already_sent"}
        if state == "in_flight":
            return {"state": "ignored", "reason": "notification_in_flight"}
        if state == "exhausted":
            return {"state": "ignored", "reason": "notification_attempts_exhausted"}
        lease_id = reservation.get("lease_id")
        if not isinstance(lease_id, str):
            return {"state": "failed", "reason": "notification_state_unavailable"}
        account = self._live_predict_account_snapshot() or {}
        top_up = str(account.get("minimum_top_up_bnb", ""))
        required = str(account.get("required_bnb", ""))
        balance = str(account.get("bnb_balance", ""))
        message = "\n".join(
            (
                "Predict stage-5 signal blocked by BNB gas.",
                f"BNB balance: {balance}",
                f"Required BNB: {required}",
                f"Minimum manual top-up: {top_up}",
                f"机会编号：{signal_id}",
                f"Dashboard：{self._dashboard_url}",
            )
        )
        try:
            sent = self._deliver_feishu_notification("Predict BNB gas top-up required", message)
        except Exception:
            sent = False
        completion = self._store.complete_notification_attempt(
            signal_id, lease_id, success=sent, error_code="delivery_failed"
        )
        if completion.get("state") == "sent":
            return {"state": "sent", "signal_id": signal_id, "reason": "insufficient_bnb"}
        if completion.get("state") == "closed":
            return {"state": "ignored", "reason": "signal_closed"}
        return {"state": "failed", "reason": "notification_failed"}

    def _notify_yes_no_signal(
        self,
        signal_id: str,
        signal: Mapping[str, object],
        *,
        rendered_signal: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        if signal.get("ended_at") is not None:
            return {"state": "ignored", "reason": "signal_closed"}
        notification_state = str(signal.get("notification_state", "pending"))
        if notification_state == "sent":
            return {"state": "ignored", "reason": "already_sent"}
        if notification_state == "suppressed":
            return {
                "state": "ignored",
                "reason": str(
                    signal.get("notification_suppressed_reason", "suppressed")
                ),
            }
        attempts = _decimal(signal.get("notification_attempts")) or Decimal("0")
        if attempts >= 3:
            return {"state": "ignored", "reason": "notification_attempts_exhausted"}

        market_id = str(signal.get("market_id", "")).strip()
        if signal.get("market_type") != "cross_venue_yes_no" and self._store.notification_sent_since(
            market_id, _utc_now() - timedelta(minutes=30)
        ):
            self._store.update_signal(
                signal_id,
                {
                    "notification_state": "suppressed",
                    "notification_suppressed_reason": "market_cooldown",
                },
            )
            return {"state": "ignored", "reason": "market_cooldown"}

        reservation = self._store.reserve_notification_attempt(
            signal_id, max_attempts=3, lease_seconds=60.0
        )
        reservation_state = reservation.get("state")
        if reservation_state in {"missing", "closed"}:
            return {
                "state": "ignored",
                "reason": (
                    "signal_closed"
                    if reservation_state == "closed"
                    else "signal_unavailable"
                ),
            }
        if reservation_state == "sent":
            return {"state": "ignored", "reason": "already_sent"}
        if reservation_state == "in_flight":
            return {"state": "ignored", "reason": "notification_in_flight"}
        if reservation_state == "exhausted":
            return {"state": "ignored", "reason": "notification_attempts_exhausted"}
        lease_id = reservation.get("lease_id")
        current = reservation.get("signal")
        if not isinstance(lease_id, str) or not isinstance(current, Mapping):
            return {"state": "failed", "reason": "notification_state_unavailable"}

        current = dict(current)
        if rendered_signal is None:
            event_title = str(
                current.get("event_title", current.get("question", "")) or ""
            ).strip()
            translated_title = cached_prediction_title_zh(self._store, event_title)
            if translated_title is not None:
                current["event_title_zh"] = translated_title
        else:
            current.update(rendered_signal)
        try:
            title, message = render_yes_no_signal_notification(current)
            feishu_success = self._deliver_feishu_notification(title, message)
        except Exception:
            feishu_success = False
        completion = self._store.complete_notification_attempt(
            signal_id,
            lease_id,
            success=feishu_success,
            error_code="delivery_failed",
        )
        if completion.get("state") == "sent":
            return {"state": "sent", "signal_id": signal_id}
        if completion.get("state") == "closed":
            return {"state": "ignored", "reason": "signal_closed"}
        return {"state": "failed", "reason": "notification_failed"}

    def notify_lp_preparation_failure(
        self, failure: Mapping[str, object]
    ) -> dict[str, object]:
        """Alert once when the durable LP preparation task is paused."""

        paused_market_count = failure.get("paused_market_count")
        if type(paused_market_count) is int and paused_market_count > 0:
            raw_samples = failure.get("alert_error_samples")
            if not isinstance(raw_samples, (list, tuple)):
                raw_samples = failure.get("paused_error_samples")
            samples = (
                [sample for sample in raw_samples if isinstance(sample, Mapping)]
                if isinstance(raw_samples, (list, tuple))
                else []
            )
            sample_text = "、".join(
                f"{str(sample.get('condition_id') or '未知标的')}"
                f"({str(sample.get('stage') or 'unknown_stage')}/"
                f"{str(sample.get('error') or 'unknown_error')})"
                for sample in samples[:5]
            ) or "未知"
            if failure.get("alert_error_samples_truncated") is True or (
                not isinstance(failure.get("alert_error_samples"), (list, tuple))
                and failure.get("paused_error_samples_truncated") is True
            ):
                sample_text += "、…"
            alert_count = failure.get("alert_condition_count")
            cohort_count = (
                alert_count
                if type(alert_count) is int and alert_count > 0
                else paused_market_count
            )
            if cohort_count == paused_market_count:
                headline = f"部分标的补全暂停：共 {paused_market_count} 个市场。"
            else:
                headline = (
                    f"部分标的补全暂停：本次新增 {cohort_count} 个市场（累计暂停 "
                    f"{paused_market_count} 个）。"
                )
            message = "\n".join(
                (
                    headline,
                    f"失败样例：{sample_text}。",
                    "其他标的仍可继续准备和筛选；请在 Dashboard 使用“恢复准备”继续。",
                    f"Dashboard：{self._dashboard_url}",
                )
            )
            now_clock = beijing_clock(datetime.now(UTC)) or "未知"
            if self._deliver_feishu_notification(
                f"❌ 部分 LP 标的补全暂停（{now_clock}）", message
            ):
                return {"state": "sent"}
            return {"state": "failed", "reason": "notification_unavailable"}

        raw_stage = str(failure.get("stage") or "unknown_stage")
        stage = (
            raw_stage
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", raw_stage)
            else "unknown_stage"
        )
        raw_error = str(failure.get("last_error") or "unknown_error")
        error = (
            raw_error
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", raw_error)
            else "unknown_error"
        )
        completed = failure.get("completed_count")
        total = failure.get("total_count")
        completed_text = str(completed) if type(completed) is int and completed >= 0 else "未知"
        total_text = str(total) if type(total) is int and total >= 0 else "未知"
        last_failure = beijing_clock(failure.get("last_failure_at")) or "未知"
        last_success = beijing_clock(failure.get("last_success_at")) or "从未成功"
        message = "\n".join(
            (
                f"LP 准备已暂停：阶段 {stage} · 错误 {error}。",
                f"进度 {completed_text}/{total_text}；失败时间 {last_failure}；上次成功 {last_success}。",
                "请检查交易所连接后，在 Dashboard 使用“恢复准备”继续。",
                f"Dashboard：{self._dashboard_url}",
            )
        )
        now_clock = beijing_clock(datetime.now(UTC)) or "未知"
        if self._deliver_feishu_notification(
            f"❌ LP 准备已暂停（{now_clock}）", message
        ):
            return {"state": "sent"}
        return {"state": "failed", "reason": "notification_unavailable"}

    def notify_monitor_failure(
        self, failure: Mapping[str, object]
    ) -> dict[str, object]:
        """Alert operators when monitor threads or refresh loops break."""

        component = failure.get("component")
        if component == "monitor_thread":
            return self._notify_monitor_thread_event(failure)

        if component == "llm_validation":
            reason_codes = failure.get("reason_codes") or []
            reason_text = "、".join(
                str(code) for code in reason_codes if str(code).strip()
            )
            summary = str(failure.get("summary") or "").strip() or (
                "当前选中的 LLM 校验引擎不可用，无法校验新关系"
            )
            body = summary if not reason_text else f"{summary}（{reason_text}）"
            message = "\n".join(
                (
                    f"{body}。",
                    "引擎恢复后自动重新校验，可在看板一键切换引擎。",
                    f"Dashboard：{self._dashboard_url}",
                )
            )
            now_clock = beijing_clock(datetime.now(UTC)) or "未知"
            if self._deliver_feishu_notification(
                f"⚠️ LLM 校验不可用，暂停自动下单（{now_clock}）", message
            ):
                return {"state": "sent"}
            return {"state": "failed", "reason": "notification_failed"}

        raw_error_type = str(failure.get("error_type") or "")
        error_type = (
            raw_error_type
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", raw_error_type)
            else "unknown_error"
        )
        last_success_clock = beijing_clock(failure.get("last_success_at"))
        attempts = failure.get("attempts")
        attempts_text = (
            str(attempts)
            if isinstance(attempts, int) and not isinstance(attempts, bool)
            else "5"
        )
        message = "\n".join(
            (
                f"连续 {attempts_text} 次刷新失败，自动重试已停止。",
                f"最后错误 {error_type} · 上次成功刷新 {last_success_clock or '从未成功'}",
                "请重启 Dashboard 服务并检查 Polymarket 连接。",
            )
        )
        now_clock = beijing_clock(datetime.now(UTC)) or "未知"
        if self._deliver_feishu_notification(
            f"❌ 行情刷新连续失败，需人工干预（{now_clock}）", message
        ):
            return {"state": "sent"}
        return {"state": "failed", "reason": "notification_failed"}

    def _notify_monitor_thread_event(
        self, failure: Mapping[str, object]
    ) -> dict[str, object]:
        raw_error_type = str(failure.get("error_type") or "")
        error_type = (
            raw_error_type
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", raw_error_type)
            else "unknown_error"
        )
        crashed_clock_seconds = (
            beijing_clock(failure.get("crashed_at"), seconds=True) or "未知"
        )
        crashed_clock = beijing_clock(failure.get("crashed_at")) or "未知"
        recovered_clock_seconds = (
            beijing_clock(failure.get("recovered_at"), seconds=True) or "未知"
        )
        recovered_clock = beijing_clock(failure.get("recovered_at")) or "未知"
        now_clock = beijing_clock(datetime.now(UTC)) or "未知"
        event = str(failure.get("event") or "crashed")
        restarts = failure.get("restarts")
        restarts_text = (
            str(restarts)
            if isinstance(restarts, int) and not isinstance(restarts, bool)
            else "未知"
        )
        consecutive = failure.get("consecutive")
        consecutive_text = (
            str(consecutive)
            if isinstance(consecutive, int) and not isinstance(consecutive, bool)
            else "未知"
        )
        if event == "recovered":
            downtime = failure.get("downtime_seconds")
            try:
                downtime_text = str(int(round(float(downtime))))
            except (TypeError, ValueError):
                downtime_text = "未知"
            title = f"✅ 预测监控线程已自动恢复（{recovered_clock}）"
            message = "\n".join(
                (
                    (
                        f"{crashed_clock_seconds} 崩溃 → {recovered_clock_seconds} 恢复，"
                        f"停摆 {downtime_text} 秒，累计重启 {restarts_text} 次。"
                    ),
                    "行情刷新已恢复正常。",
                )
            )
        elif event == "gave_up":
            title = f"❌ 预测监控线程已停止重启，等待人工处理（{crashed_clock}）"
            message = "\n".join(
                (
                    "连续 10 次崩溃，已停止自动重启。",
                    f"最后错误 {error_type} · 最后崩溃 {crashed_clock_seconds}",
                    f"累计重启 {restarts_text} 次 · Dashboard：{self._dashboard_url}",
                )
            )
        else:
            retry_in_seconds = failure.get("retry_in_seconds")
            retry_text = (
                str(retry_in_seconds)
                if isinstance(retry_in_seconds, int)
                and not isinstance(retry_in_seconds, bool)
                else "未知"
            )
            title = f"❌ 预测监控线程崩溃，将自动重启（{crashed_clock}）"
            message = "\n".join(
                (
                    f"崩溃时间 {crashed_clock_seconds} · 错误 {error_type}",
                    (
                        f"连续第 {consecutive_text} 次 · 累计 {restarts_text} 次，"
                        f"{retry_text} 秒后重试；期间行情扫描暂停，恢复后自动通知。"
                    ),
                    f"Dashboard：{self._dashboard_url}",
                )
            )
        if self._deliver_feishu_notification(title, message):
            return {"state": "sent"}
        return {"state": "failed", "reason": "notification_failed"}

    def _deliver_feishu_notification(self, title: str, message: str) -> bool:
        fallback_target: object | None = None
        try:
            attempts_result = send_notification_with_results(
                self._notifier,
                title,
                message,
                channels={"feishu", "feishu_app"},
            )
            if not attempts_result:
                # Test doubles and custom notifiers may expose an explicit
                # channel without being one of the built-in Feishu classes.
                fallback_target = self._feishu_target()
                if fallback_target is not None:
                    attempts_result = send_notification_with_results(
                        fallback_target, title, message, channels=None
                    )
        except Exception:
            attempts_result = []
        feishu_success = any(
            getattr(item, "channel", "") in {"feishu", "feishu_app"}
            and getattr(item, "success", False)
            for item in attempts_result
        )
        if not feishu_success and fallback_target is not None:
            feishu_success = any(
                getattr(item, "success", False) for item in attempts_result
            )
        return feishu_success

    def _feishu_target(self) -> object | None:
        targets = getattr(self._notifier, "_notifiers", None)
        candidates = list(targets) if isinstance(targets, (list, tuple)) else [self._notifier]
        for target in candidates:
            if self._notification_channel(target) in {"feishu", "feishu_app"}:
                return target
        return None

    @staticmethod
    def _codex_approved(opportunity: Mapping[str, object]) -> bool:
        validation = opportunity.get("relation_validation")
        if isinstance(validation, Mapping):
            return str(validation.get("status", "")).strip().lower() == "approved"
        return str(opportunity.get("llm_status", "")).strip().lower() == "approved"

    def confirm(self, preview_id: str, idempotency_key: str) -> dict[str, object]:
        """Consume one preview and start exactly one daemon execution thread."""

        key = str(idempotency_key).strip()
        existing = self._execution_for_idempotency(key)
        if existing is not None:
            return existing
        if self._breaker_is_open():
            existing = self._execution_for_idempotency(key)
            if existing is not None:
                return existing
            return {"state": "locked", "reason": "circuit_breaker_open"}
        lp_active_reader = getattr(self._store, "lp_active_session", None)
        lp_active = lp_active_reader() if callable(lp_active_reader) else None
        if lp_active is not None:
            return {
                "state": "busy",
                "reason": "active_lp_session",
                "session_id": lp_active.get("session_id"),
            }
        active = self._store.active_execution()
        if active is not None:
            return {
                "state": "busy",
                "reason": "active_execution",
                "execution_id": active["execution_id"],
            }
        lock = self._acquire_global_lock()
        if lock is None:
            active = self._store.active_execution()
            lp_active_reader = getattr(self._store, "lp_active_session", None)
            lp_active = lp_active_reader() if callable(lp_active_reader) else None
            if lp_active is not None:
                return {
                    "state": "busy",
                    "reason": "active_lp_session",
                    "session_id": lp_active.get("session_id"),
                }
            if active is not None:
                if str(active.get("idempotency_key", "")) == key:
                    return self._decorate_execution(active)
                return {
                    "state": "busy",
                    "reason": "active_execution",
                    "execution_id": active["execution_id"],
                }
            existing = self._wait_for_idempotency(key)
            if existing is not None:
                return existing
            return {"state": "busy", "reason": "execution_lock"}
        try:
            existing = self._execution_for_idempotency(key)
            if existing is not None:
                self._release_global_lock(lock)
                return existing
            active = self._store.active_execution()
            lp_active_reader = getattr(self._store, "lp_active_session", None)
            lp_active = lp_active_reader() if callable(lp_active_reader) else None
            if lp_active is not None:
                self._release_global_lock(lock)
                return {
                    "state": "busy",
                    "reason": "active_lp_session",
                    "session_id": lp_active.get("session_id"),
                }
            if active is not None:
                self._release_global_lock(lock)
                if str(active.get("idempotency_key", "")) == key:
                    return self._decorate_execution(active)
                return {
                    "state": "busy",
                    "reason": "active_execution",
                    "execution_id": active["execution_id"],
                }
            try:
                execution = self._store.consume_preview_and_create_execution(
                    str(preview_id), key
                )
            except ValueError as exc:
                message = str(exc)
                if "active execution" in message:
                    active = self._store.active_execution()
                    self._release_global_lock(lock)
                    return {
                        "state": "busy",
                        "reason": "active_execution",
                        **({"execution_id": active["execution_id"]} if active else {}),
                    }
                self._release_global_lock(lock)
                return {"state": "rejected", "reason": message}
            except sqlite3.IntegrityError:
                existing = self._execution_for_idempotency(key)
                active = self._store.active_execution()
                self._release_global_lock(lock)
                if existing is not None:
                    return existing
                return {
                    "state": "busy",
                    "reason": "active_execution",
                    **({"execution_id": active["execution_id"]} if active else {}),
                }
            if execution.get("state") in {"rejected", "ignored"}:
                self._release_global_lock(lock)
                return execution
            execution_id = str(execution["execution_id"])
            # A store implementation may return an idempotency hit instead of
            # raising.  Never start a second worker for that durable row.
            if (
                str(execution.get("state", "")) != "validating"
                or str(execution.get("preview_id", "")) != str(preview_id)
            ):
                self._release_global_lock(lock)
                return self._decorate_execution(execution)
            thread = threading.Thread(
                target=self._run_execution,
                args=(execution_id, lock),
                name=f"prediction-execution-{execution_id[:8]}",
                daemon=True,
            )
            self._threads[execution_id] = thread
            thread.start()
            return self._decorate_execution(execution)
        except Exception:
            self._release_global_lock(lock)
            raise

    def execution(self, execution_id: str) -> dict[str, object]:
        for row in self._store.histories("executions"):
            if str(row.get("execution_id")) == str(execution_id):
                return self._decorate_execution(row)
        raise KeyError(execution_id)

    def reconcile_cross_holdings_once(self) -> dict[str, int]:
        """Observe automatic settlement; this method never submits redemption."""

        counts = {"complete": 0, "pending": 0, "unknown": 0}
        try:
            counts["complete"] += len(self._store.release_proven_cross_completions())
        except Exception:
            pass
        for row in self._store.histories("executions"):
            if row.get("state") != "holding_to_resolution":
                continue
            intent = self._intent_from_payload(row.get("intent"))
            execution_id = str(row.get("execution_id", ""))
            if not isinstance(intent, CrossVenueIntent) or not execution_id:
                continue
            by_exchange = {leg.exchange: leg for leg in intent.legs}
            polymarket_leg = by_exchange.get("polymarket")
            predict_leg = by_exchange.get("predict.fun")
            polymarket = self._fresh_account_snapshot()
            predict = self._live_predict_account_snapshot()
            if (
                polymarket_leg is None
                or predict_leg is None
                or polymarket is None
                or predict is None
            ):
                self._cross_redemption_unknown(execution_id, row, "account_unavailable")
                counts["unknown"] += 1
                continue
            positions = {
                "polymarket": self._cross_position_quantity(polymarket, polymarket_leg),
                "predict.fun": self._cross_position_quantity(predict, predict_leg),
            }
            if any(quantity is None for quantity in positions.values()):
                self._cross_redemption_unknown(execution_id, row, "position_unknown")
                counts["unknown"] += 1
                continue
            concrete = {venue: quantity for venue, quantity in positions.items() if quantity is not None}
            if any(quantity > 0 for quantity in concrete.values()):
                winners = tuple(
                    winner
                    for snapshot, leg in ((polymarket, polymarket_leg), (predict, predict_leg))
                    if (winner := self._cross_redeemable_winner(snapshot, leg)) is not None
                )
                evidence: dict[str, object] = {
                    "phase": "redemption_pending",
                    "status_text": "待兑付",
                    "positions": {venue: format(quantity, "f") for venue, quantity in concrete.items()},
                }
                if len(winners) == 1:
                    evidence["settlement"] = {"winner": winners[0]}
                elif len(winners) > 1:
                    self._cross_redemption_unknown(execution_id, row, "winner_ambiguous")
                    counts["unknown"] += 1
                    continue
                self._transition(
                    execution_id,
                    "holding_to_resolution",
                    evidence,
                )
                counts["pending"] += 1
                continue
            winner = self._cross_settlement_winner(row.get("evidence"), intent)
            if winner is None:
                self._cross_redemption_unknown(execution_id, row, "winner_not_observed")
                counts["unknown"] += 1
                continue
            baseline = self._cross_settlement_baseline(row.get("evidence"))
            poly_after = _decimal(polymarket.get("p_usd_balance"))
            predict_after = _decimal(predict.get("available_usdt"))
            if (
                baseline is None
                or poly_after is None
                or predict_after is None
            ):
                self._cross_redemption_unknown(execution_id, row, "collateral_unknown")
                counts["unknown"] += 1
                continue
            redeemed = {
                "polymarket": max(Decimal("0"), poly_after - baseline["polymarket"]),
                "predict.fun": max(Decimal("0"), predict_after - baseline["predict.fun"]),
            }
            winner_venue = str(winner["venue"])
            winner_quantity = _decimal(winner.get("quantity"))
            if (
                winner_quantity is None
                or redeemed.get(winner_venue, Decimal("0")) < winner_quantity
            ):
                self._cross_redemption_unknown(execution_id, row, "winning_collateral_not_observed")
                counts["unknown"] += 1
                continue
            self._transition(
                execution_id,
                "complete",
                {
                    "phase": "redemption_observed",
                    "positions": {venue: "0" for venue in concrete},
                    "redemption": {
                        "observed": True,
                        "winner": winner,
                        "redeemed_collateral": {
                            venue: format(amount, "f") for venue, amount in redeemed.items()
                        },
                    },
                    "settlement_baseline": {
                        venue: format(amount, "f") for venue, amount in baseline.items()
                    },
                },
            )
            try:
                self._store.release_cross_reservation(execution_id, reason="redeemed")
            except Exception:
                self._finish_cross_incident(
                    execution_id,
                    "cross_reservation_release_failed",
                    evidence={"release_reason": "redeemed"},
                )
                counts["unknown"] += 1
                continue
            counts["complete"] += 1
        return counts

    @staticmethod
    def _cross_position_quantity(
        snapshot: Mapping[str, object], leg: CrossVenueLeg
    ) -> Decimal | None:
        positions = snapshot.get("positions")
        if not isinstance(positions, (list, tuple)):
            return None
        total = Decimal("0")
        for position in positions:
            if not isinstance(position, Mapping):
                return None
            token = position.get("token_id", position.get("tokenId", position.get("asset_id", "")))
            if token != leg.token_id:
                continue
            quantity = _decimal(
                position.get("size", position.get("quantity", position.get("shares", position.get("amount"))))
            )
            if quantity is None or quantity < 0:
                return None
            total += quantity
        return total

    @staticmethod
    def _cross_settlement_baseline(evidence: object) -> dict[str, Decimal] | None:
        if not isinstance(evidence, (list, tuple)):
            return None
        for item in reversed(evidence):
            if not isinstance(item, Mapping) or item.get("phase") != "holding_to_resolution":
                continue
            raw = item.get("settlement_baseline")
            if not isinstance(raw, Mapping):
                continue
            values: dict[str, Decimal] = {}
            for venue in ("polymarket", "predict.fun"):
                balance = _decimal(raw.get(venue))
                if balance is None or balance < 0:
                    break
                values[venue] = balance
            if len(values) == 2:
                return values
        return None

    @staticmethod
    def _cross_redeemable_winner(
        snapshot: Mapping[str, object], leg: CrossVenueLeg
    ) -> dict[str, object] | None:
        positions = snapshot.get("positions")
        if not isinstance(positions, (list, tuple)):
            return None
        quantity = Decimal("0")
        for position in positions:
            if not isinstance(position, Mapping):
                continue
            token_id = position.get("token_id", position.get("tokenId", position.get("asset_id", "")))
            if token_id != leg.token_id:
                continue
            condition_id = position.get("condition_id", position.get("conditionId", position.get("market", "")))
            if condition_id not in (None, "", leg.condition_id):
                continue
            outcome = position.get("outcome", position.get("outcome_name", ""))
            if outcome not in (None, "") and str(outcome).upper() != leg.outcome:
                continue
            redeemable = position.get("redeemable")
            if redeemable is not True and str(redeemable).casefold() != "true":
                continue
            amount = _decimal(
                position.get("size", position.get("quantity", position.get("shares", position.get("amount"))))
            )
            if amount is None or amount <= 0:
                continue
            quantity += amount
        if quantity != leg.net_quantity:
            return None
        return {
            "venue": leg.exchange,
            "condition_id": leg.condition_id,
            "outcome": leg.outcome,
            "token_id": leg.token_id,
            "quantity": quantity,
        }

    @staticmethod
    def _cross_settlement_winner(
        evidence: object, intent: CrossVenueIntent
    ) -> dict[str, object] | None:
        if not isinstance(evidence, (list, tuple)):
            return None
        by_exchange = {leg.exchange: leg for leg in intent.legs}
        for item in reversed(evidence):
            if not isinstance(item, Mapping):
                continue
            settlement = item.get("settlement")
            winner = settlement.get("winner") if isinstance(settlement, Mapping) else None
            if not isinstance(winner, Mapping):
                continue
            venue = winner.get("venue")
            leg = by_exchange.get(venue) if isinstance(venue, str) else None
            quantity = _decimal(winner.get("quantity"))
            if (
                leg is None
                or winner.get("condition_id") != leg.condition_id
                or winner.get("outcome") != leg.outcome
                or winner.get("token_id") != leg.token_id
                or quantity is None
                or quantity <= 0
                or quantity != leg.net_quantity
            ):
                continue
            return {
                "venue": venue,
                "condition_id": leg.condition_id,
                "outcome": leg.outcome,
                "token_id": leg.token_id,
                "quantity": quantity,
            }
        return None

    def _cross_redemption_unknown(
        self, execution_id: str, row: Mapping[str, object], reason: str
    ) -> None:
        evidence = row.get("evidence")
        alerted = isinstance(evidence, (list, tuple)) and any(
            isinstance(item, Mapping) and item.get("redemption_alerted") is True
            for item in evidence
        )
        payload: dict[str, object] = {
            "phase": "redemption_pending",
            "status_text": "待兑付",
            "reason": reason,
            "redemption_alerted": alerted,
        }
        if not alerted:
            self._notify_incident("cross_redemption_unknown")
            payload["redemption_alerted"] = True
        self._transition(execution_id, "holding_to_resolution", payload)

    def reconcile_startup(self) -> dict[str, object]:
        """Reconcile live account state before allowing a new opportunity."""
        self._breaker_open = True
        snapshot = self._fresh_account_snapshot()
        if snapshot is None:
            return {"state": "locked", "reason": "account_unavailable"}
        if self._store.unacknowledged_incident() is not None:
            return {"state": "locked", "reason": "unacknowledged_incident"}

        # An LP session owns its target order and inventory across process
        # restarts. Reconcile it before the legacy execution/account checks so
        # its session-specific recovery runs first.
        lp_active_reader = getattr(self._store, "lp_active_session", None)
        lp_active = lp_active_reader() if callable(lp_active_reader) else None
        if lp_active is not None:
            lp_tick = getattr(self._lp, "tick", None)
            if not callable(lp_tick):
                return {
                    "state": "locked",
                    "reason": "active_lp_service_unavailable",
                    "session_id": lp_active.get("session_id"),
                }
            try:
                lp_result = lp_tick()
            except Exception:
                return {
                    "state": "locked",
                    "reason": "active_lp_reconciliation_failed",
                    "session_id": lp_active.get("session_id"),
                }
            refreshed_lp = lp_active_reader() if callable(lp_active_reader) else lp_active
            lp_state = str((refreshed_lp or lp_result).get("state", ""))
            if lp_state not in {"complete", "entry_rejected"}:
                if not self._relayer_ready():
                    return {"state": "locked", "reason": "readiness_unavailable"}
                if not self._notification_channels_ready():
                    return {
                        "state": "locked",
                        "reason": "notification_config_unavailable",
                    }
                self._breaker_open = False
                return {
                    "state": "ready",
                    "readiness": "lp_active",
                    "lp": lp_result,
                }

        if self._n_leg_paused:
            if not self._relayer_ready(require_merge=False):
                return {"state": "locked", "reason": "readiness_unavailable"}
            if not self._notification_channels_ready():
                return {
                    "state": "locked",
                    "reason": "notification_config_unavailable",
                }
            self._breaker_open = False
            return {"state": "ready", "readiness": "n_leg_paused"}

        active = self._store.active_execution()
        active_id = str(active.get("execution_id", "")) if active else ""
        predict = self._live_predict_account_snapshot()
        residual_allowance = _decimal(predict.get("allowance")) if predict else None
        residual_allowance_raw = _decimal(predict.get("allowance_raw")) if predict else None
        if active is None and (
            residual_allowance is not None
            and residual_allowance_raw is not None
            and (residual_allowance > 0 or residual_allowance_raw > 0)
        ):
            self._cross_breaker_open = True
            evidence = {
                "phase": "startup_residual_predict_allowance",
                "allowance": _safe_decimal(residual_allowance),
                "usdt_moved": False,
            }
            self._startup_incident("", "residual_predict_allowance", evidence)
            return {"state": "locked", "reason": "residual_predict_allowance", **evidence}
        open_orders = self._order_ids(snapshot.get("open_order_ids", ()))
        if active is not None and open_orders:
            evidence = {
                "phase": "startup_open_orders",
                "open_orders": open_orders,
                "canceled": [],
                "remaining": open_orders,
            }
            self._startup_incident(
                active_id,
                "startup_open_orders",
                evidence,
            )
            return {"state": "locked", "reason": "open_orders", **evidence}

        active_intent = self._intent_from_payload(active.get("intent")) if active else None
        totals = self._position_totals(
            snapshot,
            active_intent,
            known_tokens=self._known_holding_tokens(),
        )
        if totals["unknown"]:
            evidence = {"phase": "startup_unknown_state", "positions": totals["unknown"]}
            self._startup_incident(active_id, "unknown_external_state", evidence)
            return {"state": "locked", "reason": "unknown_external_state", **evidence}
        if isinstance(active_intent, ThresholdHedgeIntent):
            if totals["yes"] > 0 and totals["no"] > 0 and totals["yes"] == totals["no"]:
                try:
                    self._transition(
                        active_id,
                        "holding_to_resolution",
                        {
                            "phase": "startup_threshold_holding",
                            "merge": "not_applicable",
                            "quantity": totals["yes"],
                        },
                    )
                except Exception:
                    return {"state": "locked", "reason": "startup_reconcile_persistence_failed"}
                active = None
                active_id = ""
            elif totals["yes"] > 0 or totals["no"] > 0:
                evidence = {
                    "phase": "startup_threshold_directional_imbalance",
                    "leg_a_quantity": _safe_decimal(totals["yes"]),
                    "leg_b_quantity": _safe_decimal(totals["no"]),
                }
                self._startup_incident(active_id, "startup_directional_imbalance", evidence)
                return {"state": "locked", "reason": "directional_imbalance", **evidence}
        if totals["yes"] > 0 and totals["no"] > 0 and totals["yes"] == totals["no"]:
            merge = self._startup_merge(
                active,
                active_intent,
                totals["yes"],
                before=snapshot,
            )
            evidence = {
                "phase": "startup_equal_pair",
                "yes_quantity": _safe_decimal(totals["yes"]),
                "no_quantity": _safe_decimal(totals["no"]),
                **merge,
            }
            incident_state = (
                "merge_incident"
                if merge.get("reconciled") is not True
                else "directional_incident"
            )
            self._startup_incident(
                active_id,
                "startup_equal_pair",
                evidence,
                state=incident_state,
            )
            return {"state": "locked", "reason": "equal_pair", **evidence}
        if totals["yes"] > 0 or totals["no"] > 0:
            evidence = {
                "phase": "startup_directional_imbalance",
                "yes_quantity": _safe_decimal(totals["yes"]),
                "no_quantity": _safe_decimal(totals["no"]),
            }
            self._startup_incident(active_id, "startup_directional_imbalance", evidence)
            return {"state": "locked", "reason": "directional_imbalance", **evidence}
        confirmed_merge = self._confirmed_merge_evidence(active)
        if active is not None and confirmed_merge is not None:
            try:
                self._transition(
                    active_id,
                    "complete",
                    {
                        "phase": "startup_merge_reconciled",
                        "merge": "already_confirmed",
                        "merge_result": confirmed_merge,
                        "checked_at": snapshot.get("checked_at"),
                        "positions": snapshot.get("positions", ()),
                    },
                )
            except Exception:
                return {
                    "state": "locked",
                    "reason": "startup_reconcile_persistence_failed",
                    "execution_id": active_id,
                }
            if not self._relayer_ready():
                return {"state": "locked", "reason": "readiness_unavailable"}
            if not self._notification_channels_ready():
                return {"state": "locked", "reason": "notification_config_unavailable"}
            self._breaker_open = False
            return {
                "state": "ready",
                "readiness": "reconciled",
                "execution_id": active_id,
            }
        if active is not None:
            evidence = {"phase": "startup_local_pending", "execution_id": active_id}
            self._startup_incident(active_id, "stale_local_execution", evidence)
            return {"state": "locked", "reason": "stale_local_execution", **evidence}
        if not self._relayer_ready():
            return {"state": "locked", "reason": "readiness_unavailable"}
        if not self._notification_channels_ready():
            return {"state": "locked", "reason": "notification_config_unavailable"}
        self._breaker_open = False
        return {"state": "ready", "readiness": "fresh"}

    def reset_breaker(
        self,
        incident_id: str,
        *,
        audit: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        lock = self._acquire_global_lock()
        if lock is None:
            return {"state": "busy", "reason": "control_in_progress"}
        try:
            event_id: str | None = None
            if audit is not None:
                latest = self._store.latest_control_event(
                    "reset_breaker", str(incident_id)
                )
                if latest is not None and latest.get("outcome") == "started":
                    event_id = str(latest["event_id"])
                else:
                    try:
                        event_id = self._store.begin_control_event(
                            action="reset_breaker",
                            target=str(incident_id),
                            payload=dict(audit),
                        )
                    except Exception:
                        return {
                            "state": "locked",
                            "reason": "audit_persistence_failed",
                        }
            result = self._reset_breaker(incident_id)
            if event_id is not None:
                try:
                    self._store.finish_control_event(
                        event_id,
                        outcome=(
                            "succeeded" if result.get("state") == "ready" else "rejected"
                        ),
                        payload={"result": result},
                    )
                except Exception:
                    self._breaker_open = True
                    return {
                        "state": "locked",
                        "reason": "audit_persistence_failed",
                    }
            return result
        finally:
            self._release_global_lock(lock)

    def _reset_breaker(self, incident_id: str) -> dict[str, object]:
        incident = next(
            (
                row
                for row in self._store.histories("incidents")
                if str(row.get("incident_id", "")) == str(incident_id)
            ),
            None,
        )
        if incident is not None and incident.get("acknowledged") is True:
            acknowledgement = incident.get("acknowledgement")
            if (
                isinstance(acknowledgement, Mapping)
                and acknowledgement.get("reconciliation") == "fresh_clean"
            ):
                self._breaker_open = False
                return {
                    "state": "ready",
                    "reason": "reset_confirmed",
                    "incident_id": str(incident_id),
                }
            incident = None
        if incident is None:
            self._breaker_open = True
            return {"state": "locked", "reason": "incident_not_found", "incident_id": str(incident_id)}
        snapshot = self._account_snapshot()
        reasons: list[str] = []
        holding_imbalances: dict[str, dict[str, object]] = {}
        incident_execution = next(
            (
                row
                for row in self._store.histories("executions")
                if str(row.get("execution_id", ""))
                == str(incident.get("execution_id", ""))
            ),
            None,
        )
        if self._pending_merge_for_incident(incident, incident_execution):
            reasons.append("pending_merge")
        if snapshot is None:
            reasons.append("account_unavailable")
        else:
            checked_age = _age_seconds(snapshot.get("checked_at"))
            if checked_age is None or checked_age > 60:
                reasons.append("account_stale")
            if "open_order_ids" not in snapshot or "positions" not in snapshot:
                reasons.append("account_malformed")
            elif not self._snapshot_collections_valid(snapshot):
                reasons.append("account_malformed")
            open_orders = self._order_ids(snapshot.get("open_order_ids", ()))
            if open_orders:
                reasons.append("open_orders")
            active = self._store.active_execution()
            intent = self._intent_from_payload(active.get("intent")) if active else None
            totals = self._position_totals(
                snapshot, intent, known_tokens=self._known_holding_tokens()
            )
            holding_imbalances = self._holding_imbalances(snapshot)
            if totals["unknown"]:
                reasons.append("unknown_external_state")
            if (totals["yes"] != 0 or totals["no"] != 0) or holding_imbalances:
                reasons.append("directional_imbalance")
            if self._pending_merge(active) or self._pending_merge_for_incident(incident, incident_execution):
                reasons.append("pending_merge")
        if not self._relayer_ready():
            reasons.append("readiness_unavailable")
        if not self._notification_channels_ready():
            reasons.append("notification_config_unavailable")
        if reasons:
            self._breaker_open = True
            reason = reasons[0]
            update_incident = getattr(self._store, "update_incident", None)
            if callable(update_incident):
                denial: dict[str, object] = {
                    "reason": reason,
                    "blocking_reasons": reasons,
                    "at": _timestamp(_utc_now()),
                }
                if holding_imbalances:
                    denial["holding_imbalances"] = holding_imbalances
                try:
                    update_incident(
                        str(incident_id),
                        {"last_reset_denial": denial},
                    )
                except Exception:
                    pass
            return {
                "state": "locked",
                "reason": reason,
                "blocking_reasons": reasons,
                "incident_id": str(incident_id),
            }
        payload = {
            "incident_id": str(incident_id),
            "acknowledged_by": "operator",
            "acknowledged_at": _timestamp(_utc_now()),
            "reconciliation": "fresh_clean",
        }
        try:
            self._store.acknowledge_incident(str(incident_id), payload)
        except Exception:
            self._breaker_open = True
            return {"state": "locked", "reason": "acknowledgement_failed", "incident_id": str(incident_id)}
        active = self._store.active_execution()
        if active is not None and str(active.get("execution_id", "")) == str(incident.get("execution_id", "")):
            try:
                self._transition(
                    str(active["execution_id"]),
                    "directional_incident",
                    {"phase": "reset_acknowledged", "incident_id": str(incident_id)},
                )
            except Exception:
                self._breaker_open = True
                return {"state": "locked", "reason": "execution_close_failed", "incident_id": str(incident_id)}
        self._breaker_open = False
        return {"state": "ready", "reason": "reset_confirmed", "incident_id": str(incident_id)}

    def cleanup_predict_allowance(
        self,
        *,
        confirm: bool,
        audit: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        if confirm is not True:
            return {"state": "locked", "reason": "confirmation_required"}
        lock = self._acquire_global_lock()
        if lock is None:
            return {"state": "busy", "reason": "control_in_progress"}
        try:
            return self._cleanup_predict_allowance(audit=audit)
        finally:
            self._release_global_lock(lock)

    def _cleanup_predict_allowance(
        self, *, audit: Mapping[str, object] | None = None
    ) -> dict[str, object]:
        if self._store.active_execution() is not None:
            return {"state": "locked", "reason": "active_execution"}
        before = self._live_predict_account_snapshot()
        if before is None:
            return {"state": "locked", "reason": "account_unavailable"}
        allowance = _decimal(before.get("allowance"))
        allowance_raw = _decimal(before.get("allowance_raw"))
        if allowance is None or allowance_raw is None:
            return {"state": "locked", "reason": "allowance_unavailable"}
        event_id: str | None = None
        prior_allowance = allowance
        if audit is not None:
            latest = self._store.latest_control_event(
                "cleanup_predict_allowance", "predict_allowance"
            )
            if latest is not None and latest.get("outcome") == "started":
                event_id = str(latest["event_id"])
                payload = latest.get("payload")
                if isinstance(payload, Mapping):
                    prior_allowance = (
                        _decimal(payload.get("before_allowance")) or allowance
                    )
        if allowance == 0 and allowance_raw == 0:
            if audit is not None and event_id is None:
                try:
                    event_id = self._store.begin_control_event(
                        action="cleanup_predict_allowance",
                        target="predict_allowance",
                        payload={
                            **dict(audit),
                            "confirm": True,
                            "before_allowance": _safe_decimal(allowance),
                        },
                    )
                except Exception:
                    return {
                        "state": "locked",
                        "reason": "audit_persistence_failed",
                    }
            return self._complete_predict_allowance_cleanup(
                prior_allowance, event_id=event_id
            )
        if (
            before.get("gas_ready") is not True
            or (_decimal(before.get("minimum_top_up_bnb")) or Decimal("0")) > 0
        ):
            return {
                "state": "locked",
                "reason": "insufficient_bnb",
                "minimum_top_up_bnb": str(before.get("minimum_top_up_bnb", "")),
            }
        market_id = self._current_predict_market_id()
        if not market_id:
            return {"state": "locked", "reason": "predict_market_unavailable"}
        if audit is not None and event_id is None:
            try:
                event_id = self._store.begin_control_event(
                    action="cleanup_predict_allowance",
                    target="predict_allowance",
                    payload={
                        **dict(audit),
                        "confirm": True,
                        "before_allowance": _safe_decimal(allowance),
                    },
                )
            except Exception:
                return {"state": "locked", "reason": "audit_persistence_failed"}
        proof = self._clear_predict_allowance_zero(market_id)
        after = self._live_predict_account_snapshot()
        if (
            proof is None
            or after is None
            or _decimal(after.get("allowance")) != 0
            or _decimal(after.get("allowance_raw")) != 0
            or self._predict_approval_identity(before) != self._predict_approval_identity(after)
        ):
            self._cross_breaker_open = True
            self._startup_incident(
                "",
                "predict_allowance_cleanup_failed",
                {
                    "phase": "operator_allowance_cleanup_failed",
                    "before_allowance": _safe_decimal(allowance),
                    "after_allowance": _safe_decimal(after.get("allowance")) if after else None,
                    "usdt_moved": False,
                },
            )
            return {"state": "locked", "reason": "predict_allowance_cleanup_failed"}
        return self._complete_predict_allowance_cleanup(
            prior_allowance, event_id=event_id
        )

    def _complete_predict_allowance_cleanup(
        self, before_allowance: Decimal, *, event_id: str | None
    ) -> dict[str, object]:
        result = {
            "state": "ready",
            "before_allowance": _safe_decimal(before_allowance),
            "after_allowance": "0",
            "usdt_moved": False,
        }
        if event_id is not None:
            try:
                self._store.finish_control_event(
                    event_id, outcome="succeeded", payload=result
                )
            except Exception:
                self._cross_breaker_open = True
                return {"state": "locked", "reason": "audit_persistence_failed"}
        for incident in self._store.histories("incidents"):
            if (
                incident.get("acknowledged") is not True
                and incident.get("reason") == "residual_predict_allowance"
            ):
                try:
                    self._store.acknowledge_incident(
                        str(incident["incident_id"]),
                        {
                            "acknowledged_by": "operator",
                            "reconciliation": "predict_allowance_zero",
                            "at": _timestamp(_utc_now()),
                        },
                    )
                except Exception:
                    pass
        self._cross_breaker_open = False
        return result

    def _run_execution(self, execution_id: str, lock: tuple[threading.Lock, Any]) -> None:
        try:
            row = self.execution(execution_id)
            persisted_intent = self._intent_from_payload(row.get("intent"))
            if persisted_intent is None:
                self._finish_incident(execution_id, "invalid_persisted_intent")
                return
            cross_execution = isinstance(persisted_intent, CrossVenueIntent)
            self._transition(execution_id, "final_validating", {"phase": "final_validating"})
            if cross_execution and self._cross_breaker_open:
                self._finish_cross_rejected(
                    execution_id, "cross_circuit_breaker_open", persisted_intent
                )
                return
            opportunity = self._fresh_opportunity(
                str(row.get("opportunity_id", "")),
                target_quantity=persisted_intent.quantity if cross_execution else None,
            )
            current_intent = self._intent_from_opportunity(opportunity)
            if opportunity is None or current_intent is None:
                if cross_execution:
                    self._finish_cross_rejected(
                        execution_id, "opportunity_unavailable", persisted_intent
                    )
                else:
                    self._finish_rejected(execution_id, "opportunity_unavailable")
                return
            if cross_execution and not isinstance(current_intent, CrossVenueIntent):
                self._finish_cross_rejected(
                    execution_id, "opportunity_changed", persisted_intent
                )
                return
            if cross_execution and not self._cross_preview_matches(
                row, opportunity, current_intent
            ):
                self._finish_cross_rejected(
                    execution_id, "opportunity_changed", persisted_intent
                )
                return
            reason = self._validate_opportunity(
                opportunity,
                current_intent,
                auto_submit=row.get("auto_submit") is True,
            )
            account, volatile_reason = self._volatile_checks(current_intent)
            if reason is not None or account is None:
                if cross_execution:
                    self._finish_cross_rejected(
                        execution_id,
                        reason or volatile_reason or "readiness_unavailable",
                        persisted_intent,
                    )
                    return
                self._finish_rejected(
                    execution_id, reason or volatile_reason or "readiness_unavailable"
                )
                return
            # The intent is rebuilt from current server data; browser and stale
            # preview economics never reach the authenticated client.
            intent = current_intent
            if cross_execution:
                self._run_cross_venue_execution(
                    execution_id,
                    intent,
                    opportunity,
                    account,
                    row,
                )
                return
            if isinstance(intent, ThresholdHedgeIntent):
                self._run_threshold_execution(
                    execution_id,
                    intent=intent,
                    opportunity=opportunity,
                    account=account,
                    preview_payload=row,
                )
                return
            tick_size = self._tick_size(opportunity)
            if tick_size is None:
                self._finish_rejected(execution_id, "tick_size_unavailable")
                return
            self._transition(execution_id, "submitting", {"phase": "submitting"})
            submitted_at = _utc_now()
            preflight = getattr(self._trading, "no_submit_preflight", None)
            if callable(preflight):
                preflight_exc: Exception | None = None
                try:
                    result = _call(preflight, intent, tick_size=tick_size)
                except Exception as exc:
                    result, preflight_exc = None, exc
                if not self._preflight_passed(result):
                    self._finish_rejected(
                        execution_id,
                        (
                            self._preflight_error_code(result)
                            or self._preflight_exception_code(preflight_exc)
                            or "preflight_failed"
                        ),
                        extra_evidence={
                            "preflight": self._preflight_failure_evidence(
                                result, exception=preflight_exc
                            )
                        },
                    )
                    return
            submit_error: dict[str, str] | None = None
            submit = getattr(self._trading, "submit_pair_once", None)
            try:
                submission = _call(submit, intent, tick_size=tick_size)
            except Exception as exc:
                submit_error = _submit_error_detail(exc)
                submission = self._ambiguous_submission()
            yes, no = self._submission_legs(submission)
            self._store.record_leg(execution_id, self._leg_payload(yes))
            self._store.record_leg(execution_id, self._leg_payload(no))
            post_error = self._resolved_post_error(submit_error)
            self._transition(
                execution_id,
                "reconciling",
                {
                    "phase": "reconciling",
                    "post_attempted": True,
                    **post_error,
                },
            )
            if self._both_rejected(yes, no):
                self._transition(
                    execution_id,
                    "both_rejected",
                    {"phase": "both_rejected", "merge": "not_attempted"},
                )
                return

            known = self._reconcile_until(
                intent,
                since=submitted_at,
                yes=yes,
                no=no,
            )
            if known is None:
                self._resolve_submit_unknown(
                    execution_id,
                    account=account,
                    token_ids=(intent.yes_token_id, intent.no_token_id),
                    post_error=post_error,
                )
                return
            yes_quantity, no_quantity, execution_proof = known
            if yes_quantity > 0 and no_quantity <= 0:
                self._handle_one_leg(
                    execution_id,
                    intent,
                    filled_leg="YES",
                    filled_quantity=yes_quantity,
                    yes=yes,
                    no=no,
                    proof=execution_proof,
                    since=submitted_at,
                    tick_size=tick_size,
                    account=account,
                )
                return
            if no_quantity > 0 and yes_quantity <= 0:
                self._handle_one_leg(
                    execution_id,
                    intent,
                    filled_leg="NO",
                    filled_quantity=no_quantity,
                    yes=yes,
                    no=no,
                    proof=execution_proof,
                    since=submitted_at,
                    tick_size=tick_size,
                    account=account,
                )
                return
            if yes_quantity <= 0 or no_quantity <= 0 or yes_quantity != no_quantity:
                self._finish_incident(execution_id, "directional_imbalance")
                return
            if not execution_proof.get("verified") is True:
                self._finish_incident(execution_id, "equal_fill_proof_unverified")
                return
            self._transition(
                execution_id,
                "reconciling",
                {
                    "phase": "reconciled",
                    "yes_quantity": yes_quantity,
                    "no_quantity": no_quantity,
                    "execution_proof": execution_proof,
                },
            )
            self._transition(
                execution_id,
                "merging",
                {
                    "phase": "merging",
                    "quantity": _safe_decimal(yes_quantity),
                    "yes_quantity": yes_quantity,
                    "no_quantity": no_quantity,
                    "execution_proof": execution_proof,
                },
            )
            merge = getattr(self._trading, "merge_once", None)
            try:
                merge_result = _call(
                    merge,
                    condition_id=intent.condition_id,
                    quantity=yes_quantity,
                )
            except Exception:
                merge_result = {
                    "status": "blocked",
                    "confirmed": False,
                    "error_code": "merge_error",
                }
            merge_evidence: dict[str, object] = {"phase": "merge_result"}
            if isinstance(merge_result, Mapping):
                merge_evidence.update(self._safe_mapping(merge_result))
                if not self._merge_confirmed(merge_result):
                    merge_evidence["confirmed"] = False
            else:
                merge_evidence.update(
                    {"status": self._result_status(merge_result), "confirmed": False}
                )
            self._transition(
                execution_id,
                "merging",
                merge_evidence,
            )
            if not self._merge_confirmed(merge_result):
                self._finish_incident(execution_id, "merge_not_confirmed", state="merge_incident")
                return
            # Merge confirmation must be followed by a fresh complete account
            # read; a stale local snapshot cannot unlock the breaker.
            after = self._fresh_account_snapshot()
            before_balance = _decimal(account.get("p_usd_balance"))
            after_balance = _decimal(after.get("p_usd_balance")) if after else None
            if (
                before_balance is None
                or after_balance is None
                or after_balance <= before_balance
            ):
                self._finish_incident(execution_id, "collateral_reconciliation_failed", state="merge_incident")
                return
            if self._real_live_success(execution_proof, merge_result):
                self._first_live_order_verified = True
                self._store.set_first_live_order_validated(
                    _timestamp(_utc_now())
                )
            self._transition(
                execution_id,
                "complete",
                {
                    "phase": "complete",
                    "merge": "confirmed",
                    "p_usd_balance": _safe_decimal(after_balance or Decimal("0")),
                },
            )
        except Exception as exc:
            self._finish_incident(execution_id, f"execution_error:{type(exc).__name__}")
        finally:
            self._threads.pop(execution_id, None)
            self._release_global_lock(lock)

    def _run_cross_venue_execution(
        self,
        execution_id: str,
        intent: CrossVenueIntent,
        opportunity: Mapping[str, object],
        accounts: Mapping[str, object],
        preview_payload: Mapping[str, object],
    ) -> None:
        """Submit the two venue legs once, then trust only REST position proof."""

        by_exchange = {leg.exchange: leg for leg in intent.legs}
        predict_leg = by_exchange.get("predict.fun")
        polymarket_leg = by_exchange.get("polymarket")
        if predict_leg is None or polymarket_leg is None or self._predict_trading is None:
            self._finish_cross_incident(execution_id, "cross_clients_unavailable")
            return
        predict_order = self._cross_predict_entry_order(
            execution_id, intent, predict_leg
        )
        predict_preflight = getattr(self._predict_trading, "no_submit_cross_buy_preflight", None)
        polymarket_preflight = getattr(self._trading, "no_submit_cross_leg_preflight", None)
        if not callable(predict_preflight) or not callable(polymarket_preflight):
            self._finish_cross_rejected(execution_id, "cross_preflight_unavailable", intent)
            return
        try:
            predict_ready = _call(
                predict_preflight,
                predict_order,
            )
            polymarket_ready = _call(polymarket_preflight, polymarket_leg)
        except Exception:
            self._finish_cross_rejected(execution_id, "cross_preflight_failed", intent)
            return
        if not self._preflight_passed(predict_ready) or not self._preflight_passed(polymarket_ready):
            self._finish_cross_rejected(execution_id, "cross_preflight_failed", intent)
            return
        exact_debit_wei = self._predict_debit_base_units(predict_leg.max_cost)
        if exact_debit_wei is None:
            self._finish_cross_rejected(execution_id, "predict_allowance_amount_invalid", intent)
            return
        approval, possible_mutation = self._set_exact_predict_allowance(
            predict_leg.market_id, exact_debit_wei
        )
        if approval is None:
            if possible_mutation:
                self._finish_cross_incident(
                    execution_id,
                    "predict_allowance_approval_unverified",
                    evidence={"market_id": predict_leg.market_id, "submitted": False},
                )
                return
            self._finish_cross_rejected(
                execution_id,
                "predict_allowance_approval_failed",
                intent,
                status_text="未下单",
            )
            return
        refreshed = self._fresh_opportunity(
            str(opportunity.get("opportunity_id", "")),
            target_quantity=intent.quantity,
        )
        refreshed_intent = self._intent_from_opportunity(refreshed)
        reason = (
            "opportunity_changed"
            if refreshed is None
            or not isinstance(refreshed_intent, CrossVenueIntent)
            or not self._cross_preview_matches(preview_payload, refreshed, refreshed_intent)
            else self._validate_opportunity(
                refreshed,
                refreshed_intent,
                auto_submit=preview_payload.get("auto_submit") is True,
            )
        )
        refreshed_account, volatile_reason = (
            (None, None)
            if reason is not None
            else self._volatile_checks(
                refreshed_intent,
                expected_predict_allowance_raw=exact_debit_wei,
            )
        )
        if reason is not None or refreshed_account is None:
            cleanup = self._clear_predict_allowance_zero(predict_leg.market_id)
            if cleanup is None:
                self._finish_cross_incident(
                    execution_id,
                    "predict_allowance_cleanup_failed",
                    evidence={
                        "market_id": predict_leg.market_id,
                        "submitted": False,
                        "status_text": "未下单 · 授权清零失败",
                        "rejection_reason": reason or volatile_reason or "readiness_unavailable",
                    },
                )
                return
            self._finish_cross_rejected(
                execution_id,
                reason or volatile_reason or "readiness_unavailable",
                intent,
                status_text="未下单 · 授权已清零",
                extra={"predict_allowance": cleanup},
            )
            return
        intent = refreshed_intent
        by_exchange = {leg.exchange: leg for leg in intent.legs}
        predict_leg = by_exchange["predict.fun"]
        polymarket_leg = by_exchange["polymarket"]
        predict_order = self._cross_predict_entry_order(
            execution_id, intent, predict_leg
        )
        accounts = refreshed_account
        try:
            refreshed_predict_ready = _call(predict_preflight, predict_order)
        except Exception:
            refreshed_predict_ready = None
        try:
            refreshed_polymarket_ready = _call(polymarket_preflight, polymarket_leg)
        except Exception:
            refreshed_polymarket_ready = None
        if not self._preflight_passed(
            refreshed_predict_ready
        ) or not self._preflight_passed(refreshed_polymarket_ready):
            cleanup = self._clear_predict_allowance_zero(predict_leg.market_id)
            if cleanup is None:
                self._finish_cross_incident(
                    execution_id,
                    "predict_allowance_cleanup_failed",
                    evidence={
                        "market_id": predict_leg.market_id,
                        "submitted": False,
                        "status_text": "未下单 · 授权清零失败",
                        "rejection_reason": "cross_preflight_failed",
                    },
                )
                return
            self._finish_cross_rejected(
                execution_id,
                "cross_preflight_failed",
                intent,
                status_text="未下单 · 授权已清零",
                extra={"predict_allowance": cleanup},
            )
            return
        self._transition(
            execution_id,
            "submitting",
            {
                "phase": "submitting",
                "execution_id": execution_id,
                "accounts": self._safe_mapping(accounts),
                "preview_id": preview_payload.get("preview_id", preview_payload.get("id")),
            },
        )
        submitted_at = _utc_now()
        predict_submit = getattr(self._predict_trading, "submit_cross_buy_once", None)
        polymarket_submit = getattr(self._trading, "submit_cross_leg_once", None)
        if not callable(predict_submit) or not callable(polymarket_submit):
            self._finish_cross_incident(execution_id, "cross_submission_unavailable")
            return
        with ThreadPoolExecutor(max_workers=2) as executor:
            predict_future = executor.submit(
                _call,
                predict_submit,
                predict_order,
            )
            polymarket_future = executor.submit(_call, polymarket_submit, polymarket_leg)
            try:
                predict_result = predict_future.result()
            except Exception:
                predict_result = {"accepted": False, "status": "ambiguous", "error_code": "ambiguous"}
            try:
                polymarket_result = polymarket_future.result()
            except Exception:
                polymarket_result = ThresholdLegResult(
                    "polymarket", polymarket_leg.outcome, polymarket_leg.condition_id,
                    polymarket_leg.token_id, False, "ambiguous", "", Decimal("0"), (), "ambiguous",
                )
        results = {"predict.fun": predict_result, "polymarket": polymarket_result}
        self._store.record_leg(
            execution_id, self._cross_leg_payload(predict_leg, predict_result)
        )
        self._store.record_leg(
            execution_id, self._cross_leg_payload(polymarket_leg, polymarket_result)
        )
        self._transition(
            execution_id,
            "reconciling",
            {"phase": "reconciling", "post_attempted": True, "execution_id": execution_id},
        )
        reconciled = {
            "predict.fun": self._reconcile_predict_cross_leg(predict_leg, predict_result),
            "polymarket": self._reconcile_polymarket_cross_leg(
                polymarket_leg, polymarket_result, submitted_at
            ),
        }
        absent = [
            venue for venue, value in reconciled.items()
            if value.get("conclusively_absent") is True
            and self._cross_result_ambiguous(results[venue])
        ]
        if len(absent) == 1:
            venue = absent[0]
            self._transition(
                execution_id,
                "reconciling",
                {"phase": "bounded_retry", "venue": venue, "execution_id": execution_id},
            )
            retry = self._submit_cross_leg_once(
                venue, execution_id, intent, predict_leg, polymarket_leg
            )
            results[venue] = retry
            retry_leg = predict_leg if venue == "predict.fun" else polymarket_leg
            self._transition(
                execution_id,
                "reconciling",
                {
                    "phase": "bounded_retry_result",
                    "venue": venue,
                    "result": self._cross_leg_payload(retry_leg, retry),
                    "execution_id": execution_id,
                },
            )
            reconciled[venue] = (
                self._reconcile_predict_cross_leg(predict_leg, retry)
                if venue == "predict.fun"
                else self._reconcile_polymarket_cross_leg(
                    polymarket_leg, retry, submitted_at
                )
            )
        if any(self._cross_unknown(value) for value in reconciled.values()):
            self._finish_cross_incident(
                execution_id,
                "cross_reconciliation_unknown",
                evidence={"reconciliation": reconciled},
            )
            return
        positions = {
            venue: _decimal(value.get("position_quantity")) or Decimal("0")
            for venue, value in reconciled.items()
        }
        if all(value.get("conclusively_absent") is True for value in reconciled.values()):
            cleanup = self._clear_predict_allowance_zero(predict_leg.market_id)
            if cleanup is None:
                self._finish_cross_incident(
                    execution_id,
                    "predict_allowance_cleanup_failed",
                    evidence={"reconciliation": reconciled},
                )
                return
            self._transition(
                execution_id,
                "both_rejected",
                {
                    "phase": "both_rejected",
                    "submitted": True,
                    "no_position_observed": True,
                    "positions": {venue: format(quantity, "f") for venue, quantity in positions.items()},
                    "reconciliation": reconciled,
                    "predict_allowance": cleanup,
                },
            )
            try:
                self._store.release_cross_reservation(execution_id, reason="both_rejected")
            except Exception:
                self._finish_cross_incident(
                    execution_id,
                    "cross_reservation_release_failed",
                    evidence={
                        "release_reason": "both_rejected",
                        "reconciliation": reconciled,
                    },
                )
            return
        missing = [
            venue for venue, value in reconciled.items()
            if value.get("conclusively_absent") is True
        ]
        if len(missing) == 1 and all(
            value.get("verified") is True or value.get("conclusively_absent") is True
            for value in reconciled.values()
        ):
            self._remediate_cross_missing_leg(
                execution_id,
                intent,
                predict_leg,
                polymarket_leg,
                missing[0],
                reconciled,
            )
            return
        if not all(value.get("verified") is True for value in reconciled.values()):
            self._finish_cross_incident(
                execution_id,
                "cross_reconciliation_unverified",
                evidence={"reconciliation": reconciled},
            )
            return
        expected = intent.quantity
        if positions["predict.fun"] == expected and positions["polymarket"] == expected:
            self._finish_cross_holding(
                execution_id,
                positions=positions,
                reconciled=reconciled,
                expected_quantity=expected,
                predict_market_id=predict_leg.market_id,
            )
            return
        residual = abs(positions["predict.fun"] - positions["polymarket"])
        minimums = {
            venue: _decimal(value.get("minimum_order_size"))
            for venue, value in reconciled.items()
        }
        if any(value is None or value <= 0 for value in minimums.values()):
            self._finish_cross_incident(
                execution_id,
                "cross_minimum_order_size_unavailable",
                evidence={"reconciliation": reconciled},
            )
            return
        minimum = min(value for value in minimums.values() if value is not None)
        loss = max(
            (
                _decimal(value.get("worst_case_loss"))
                for value in reconciled.values()
                if _decimal(value.get("worst_case_loss")) is not None
            ),
            default=residual,
        )
        if residual > 0 and minimum > residual and loss <= MAX_EMERGENCY_LOSS:
            self._finish_cross_holding(
                execution_id,
                positions=positions,
                reconciled=reconciled,
                unhedged_units=residual,
                worst_case_loss=loss,
                predict_market_id=predict_leg.market_id,
            )
            return
        self._finish_cross_incident(
            execution_id,
            "cross_position_mismatch",
            evidence={
                "positions": {venue: format(quantity, "f") for venue, quantity in positions.items()},
                "unhedged_units": _safe_decimal(residual),
                "worst_case_loss": _safe_decimal(loss),
                "reconciliation": reconciled,
            },
        )

    @staticmethod
    def _cross_result_ambiguous(value: object) -> bool:
        accepted = getattr(
            value, "accepted", value.get("accepted") if isinstance(value, Mapping) else False
        ) is True
        order_id = getattr(
            value, "order_id", value.get("order_id", "") if isinstance(value, Mapping) else ""
        )
        return (
            (accepted and not isinstance(order_id, str))
            or (accepted and not order_id.strip())
            or str(getattr(value, "status", "")).lower()
            in {"ambiguous", "pending", "processing"}
            or str(getattr(value, "error_code", "")).lower() == "ambiguous"
            or (isinstance(value, Mapping) and (
                str(value.get("status", "")).lower() in {"ambiguous", "pending", "processing"}
                or str(value.get("error_code", "")).lower() == "ambiguous"
            ))
        )

    @staticmethod
    def _cross_leg_payload(leg: CrossVenueLeg, result: object) -> dict[str, object]:
        return {
            "label": leg.exchange,
            "exchange": leg.exchange,
            "condition_id": leg.condition_id,
            "outcome": leg.outcome,
            "token_id": leg.token_id,
            "accepted": getattr(result, "accepted", result.get("accepted") if isinstance(result, Mapping) else False) is True,
            "status": PredictionExecutionService._result_status(result),
            "order_id": str(getattr(result, "order_id", result.get("order_id", "") if isinstance(result, Mapping) else "")),
            "trade_ids": list(getattr(result, "trade_ids", result.get("trade_ids", ()) if isinstance(result, Mapping) else ())),
            "filled_quantity": _safe_decimal(getattr(result, "filled_quantity", result.get("filled_quantity") if isinstance(result, Mapping) else Decimal("0"))),
            "error_code": str(getattr(result, "error_code", result.get("error_code", "none") if isinstance(result, Mapping) else "none")),
        }

    def _submit_cross_leg_once(
        self,
        venue: str,
        execution_id: str,
        intent: CrossVenueIntent,
        predict_leg: CrossVenueLeg,
        polymarket_leg: CrossVenueLeg,
    ) -> object:
        if venue == "predict.fun":
            preflight = getattr(self._predict_trading, "no_submit_cross_buy_preflight", None)
            submit = getattr(self._predict_trading, "submit_cross_buy_once", None)
            order = self._cross_predict_entry_order(execution_id, intent, predict_leg)
            if not callable(preflight) or not callable(submit):
                return {"status": "ambiguous", "error_code": "ambiguous"}
            try:
                if not self._preflight_passed(_call(preflight, order)):
                    return {"status": "rejected", "error_code": "rejected"}
                return _call(submit, order)
            except Exception:
                return {"status": "ambiguous", "error_code": "ambiguous"}
        submit = getattr(self._trading, "submit_cross_leg_once", None)
        return _call(submit, polymarket_leg) if callable(submit) else ThresholdLegResult(
            "polymarket", polymarket_leg.outcome, polymarket_leg.condition_id,
            polymarket_leg.token_id, False, "ambiguous", "", Decimal("0"), (), "ambiguous",
        )

    @staticmethod
    def _cross_predict_entry_order(
        execution_id: str, intent: CrossVenueIntent, leg: CrossVenueLeg
    ) -> dict[str, object]:
        return {
            "execution_id": execution_id,
            "idempotency_key": execution_id,
            "venue": "predict.fun",
            "market_id": leg.market_id,
            "condition_id": leg.condition_id,
            "token_id": leg.token_id,
            "outcome": leg.outcome,
            "requested_quantity": leg.requested_quantity,
            "net_quantity": leg.net_quantity,
            "max_price": leg.max_price,
            "max_cost": leg.max_cost,
            "maximum_fee": leg.maximum_fee,
            "calculable_gas": intent.calculable_gas,
        }

    @staticmethod
    def _predict_debit_base_units(amount: Decimal) -> int | None:
        units = amount * Decimal(PREDICT_BASE_UNITS)
        if units != units.to_integral_value():
            return None
        return int(units)

    def _set_exact_predict_allowance(
        self, market_id: str, exact_debit_wei: int
    ) -> tuple[dict[str, object] | None, bool]:
        method = getattr(self._predict_trading, "set_exact_buy_allowance", None)
        if not callable(method):
            return None, False
        try:
            result = _call(method, market_id, exact_debit_wei)
        except Exception:
            return None, False
        if not isinstance(result, Mapping):
            return None, True
        if str(result.get("status", "")).lower() != "confirmed":
            return None, result.get("possible_mutation") is True
        snapshot = self._live_predict_account_snapshot()
        expected = Decimal(exact_debit_wei) / Decimal(PREDICT_BASE_UNITS)
        if (
            snapshot is None
            or _decimal(snapshot.get("allowance")) != expected
            or _decimal(snapshot.get("allowance_raw")) != Decimal(exact_debit_wei)
        ):
            return None, True
        return {
            "market_id": market_id,
            "after": format(expected, "f"),
            "exact_debit_wei": exact_debit_wei,
            "exact_verified": True,
        }, True

    def _clear_predict_allowance_zero(self, market_id: str) -> dict[str, object] | None:
        method = getattr(self._predict_trading, "clear_buy_allowance", None)
        if not callable(method):
            return None
        try:
            result = _call(method, market_id)
        except Exception:
            return None
        if not isinstance(result, Mapping) or str(result.get("status", "")).lower() != "confirmed":
            return None
        snapshot = self._live_predict_account_snapshot()
        if (
            snapshot is None
            or _decimal(snapshot.get("allowance")) != 0
            or _decimal(snapshot.get("allowance_raw")) != 0
        ):
            return None
        return {"market_id": market_id, "after": "0", "zero_verified": True}

    @staticmethod
    def _predict_approval_identity(snapshot: Mapping[str, object]) -> tuple[object, ...]:
        return tuple(
            snapshot.get(name)
            for name in (
                "wallet_address",
                "predict_account",
                "gas_signer",
                "chain",
                "chain_id",
                "spender",
                "allowance_spender",
            )
        )

    def _current_predict_market_id(self) -> str:
        source = self._cross_venue_monitor
        snapshot = getattr(source, "snapshot", None)
        try:
            value = _call(snapshot) if callable(snapshot) else None
        except Exception:
            value = None
        opportunities = value.get("opportunities") if isinstance(value, Mapping) else None
        if not isinstance(opportunities, (list, tuple)):
            return ""
        for opportunity in opportunities:
            intent = self._intent_from_opportunity(opportunity)
            if not isinstance(intent, CrossVenueIntent):
                continue
            for leg in intent.legs:
                if leg.exchange == "predict.fun" and leg.market_id:
                    return leg.market_id
        return ""

    def _reconcile_predict_cross_leg(
        self, leg: CrossVenueLeg, result: object
    ) -> dict[str, object]:
        reconcile = getattr(self._predict_trading, "reconcile_buy", None)
        order_id = getattr(result, "order_id", "")
        if not isinstance(order_id, str) or not order_id:
            if self._cross_result_ambiguous(result):
                return {"status": "unknown", "verified": False, "conclusively_absent": False}
            snapshot = self._live_predict_account_snapshot()
            positions = snapshot.get("positions") if snapshot is not None else None
            if not isinstance(positions, (list, tuple)):
                return {"status": "unknown", "verified": False, "conclusively_absent": False}
            for position in positions:
                if not isinstance(position, Mapping):
                    return {"status": "unknown", "verified": False, "conclusively_absent": False}
                token = position.get("tokenId", position.get("token_id", ""))
                quantity = _decimal(position.get("amount", position.get("quantity", "0")))
                if token == leg.token_id and quantity is not None and quantity > 0:
                    return {"status": "unknown", "verified": False, "conclusively_absent": False}
            return {
                "status": "absent",
                "verified": False,
                "conclusively_absent": True,
                "filled_quantity": Decimal("0"),
                "position_quantity": Decimal("0"),
            }
        if not callable(reconcile):
            return {"status": "unknown", "verified": False, "conclusively_absent": False}
        try:
            value = _call(reconcile, leg.market_id, leg.token_id, order_id)
        except Exception:
            value = None
        return dict(value) if isinstance(value, Mapping) else {"status": "unknown", "verified": False, "conclusively_absent": False}

    def _reconcile_polymarket_cross_leg(
        self, leg: CrossVenueLeg, result: object, since: datetime
    ) -> dict[str, object]:
        reconcile = getattr(self._trading, "reconcile_cross_leg", None)
        if not callable(reconcile) or not isinstance(result, ThresholdLegResult):
            return {"status": "unknown", "verified": False, "conclusively_absent": False}
        try:
            value = _call(reconcile, leg, result, since=since)
        except Exception:
            value = None
        return dict(value) if isinstance(value, Mapping) else {"status": "unknown", "verified": False, "conclusively_absent": False}

    @staticmethod
    def _cross_unknown(value: Mapping[str, object]) -> bool:
        return value.get("verified") is not True and value.get("conclusively_absent") is not True

    def _finish_cross_holding(
        self,
        execution_id: str,
        *,
        positions: Mapping[str, Decimal],
        reconciled: Mapping[str, Mapping[str, object]],
        unhedged_units: Decimal | None = None,
        worst_case_loss: Decimal | None = None,
        remediation_worst_case_loss: Decimal | None = None,
        expected_quantity: Decimal | None = None,
        predict_market_id: str = "",
    ) -> None:
        cleanup = self._clear_predict_allowance_zero(predict_market_id)
        if cleanup is None:
            self._finish_cross_incident(
                execution_id,
                "predict_allowance_cleanup_failed",
                evidence={"positions": {venue: format(quantity, "f") for venue, quantity in positions.items()}},
            )
            return
        evidence: dict[str, object] = {
            "phase": "holding_to_resolution",
            "positions": {venue: format(quantity, "f") for venue, quantity in positions.items()},
            "reconciliation": reconciled,
            "predict_allowance": cleanup,
        }
        established = (
            len(positions) == 2
            and len(set(positions.values())) == 1
            and next(iter(positions.values()), Decimal("0")) > 0
        )
        baseline = self._cross_post_fill_baseline() if established else None
        if baseline is None:
            evidence["settlement_baseline_status"] = (
                "unavailable" if established else "positions_not_equal"
            )
        else:
            evidence["settlement_baseline"] = {
                venue: format(amount, "f") for venue, amount in baseline.items()
            }
        if unhedged_units is not None and worst_case_loss is not None:
            evidence.update(
                {
                    "unhedged_units": _safe_decimal(unhedged_units),
                    "worst_case_loss": _safe_decimal(worst_case_loss),
                    "hedged": False,
                }
            )
        if remediation_worst_case_loss is not None:
            evidence["remediation_worst_case_loss"] = _safe_decimal(
                remediation_worst_case_loss
            )
        if (
            cleanup.get("zero_verified") is True
            and established
            and baseline is not None
            and expected_quantity is not None
            and self._cross_canary_reconciliation_verified(
                reconciled, expected_quantity
            )
        ):
            fingerprint = self._predict_canary_fingerprint()
            if fingerprint is not None:
                evidence["canary_verified"] = True
                evidence["canary_fingerprint"] = fingerprint
        self._transition(execution_id, "holding_to_resolution", evidence)
        if evidence.get("hedged") is False:
            self._notify_cross_auto_residual(execution_id, evidence)
        else:
            self._notify_cross_auto_success(execution_id, evidence)

    @staticmethod
    def _cross_canary_reconciliation_verified(
        reconciled: Mapping[str, Mapping[str, object]], expected_quantity: Decimal
    ) -> bool:
        if (
            set(reconciled) != {"predict.fun", "polymarket"}
            or expected_quantity <= 0
        ):
            return False
        for venue, value in reconciled.items():
            if value.get("verified") is not True:
                return False
            filled = _decimal(value.get("filled_quantity"))
            position = _decimal(value.get("position_quantity"))
            if filled != expected_quantity or position != expected_quantity:
                return False
            fee = _decimal(value.get("actual_fee"))
            proof = value.get("execution_proof")
            if not isinstance(proof, Mapping) or proof.get("verified") is not True:
                return False
            proof_fee = _decimal(proof.get("fee", proof.get("actual_fee")))
            if fee is None or proof_fee is None or fee != proof_fee:
                return False
            if not PredictionExecutionService._proof_has_order_refs(proof, venue):
                return False
        return True

    @staticmethod
    def _proof_has_order_refs(proof: Mapping[str, object], venue: str) -> bool:
        if proof.get("venue") != venue:
            return False
        direct_orders = proof.get("order_ids")
        direct_trades = proof.get("trade_ids")
        if PredictionExecutionService._has_order_trade_refs(direct_orders, direct_trades):
            return True
        matched = proof.get("matched_refs")
        if not isinstance(matched, Mapping):
            return False
        return PredictionExecutionService._has_order_trade_refs(
            matched.get("order_ids"), matched.get("trade_ids")
        )

    @staticmethod
    def _has_order_trade_refs(orders: object, trades: object) -> bool:
        return (
            isinstance(orders, (list, tuple))
            and isinstance(trades, (list, tuple))
            and any(isinstance(item, str) and item.strip() for item in orders)
            and any(isinstance(item, str) and item.strip() for item in trades)
        )

    def _cross_post_fill_baseline(self) -> dict[str, Decimal] | None:
        """Persist balances after the two actual positions are proven.

        The prior submission account snapshot remains audit evidence, but cannot
        prove redemption because both purchases debit it before settlement.
        """

        polymarket = self._fresh_account_snapshot()
        predict = self._live_predict_account_snapshot()
        if polymarket is None or predict is None:
            return None
        values = {
            "polymarket": _decimal(polymarket.get("p_usd_balance")),
            "predict.fun": _decimal(predict.get("available_usdt")),
        }
        if any(value is None or value < 0 for value in values.values()):
            return None
        return {venue: value for venue, value in values.items() if value is not None}

    def _remediate_cross_missing_leg(
        self,
        execution_id: str,
        intent: CrossVenueIntent,
        predict_leg: CrossVenueLeg,
        polymarket_leg: CrossVenueLeg,
        missing_venue: str,
        reconciled: Mapping[str, Mapping[str, object]],
    ) -> None:
        missing = predict_leg if missing_venue == "predict.fun" else polymarket_leg
        filled_venue = "polymarket" if missing_venue == "predict.fun" else "predict.fun"
        filled = polymarket_leg if filled_venue == "polymarket" else predict_leg
        filled_quantity = _decimal(reconciled.get(filled_venue, {}).get("position_quantity"))
        if filled_quantity != intent.quantity:
            self._finish_cross_incident(
                execution_id,
                "cross_remediation_quantity_mismatch",
                evidence={
                    "venue": missing_venue,
                    "filled_quantity": _safe_decimal(filled_quantity or Decimal("0")),
                    "reconciliation": reconciled,
                },
            )
            return
        completion = self._fresh_cross_remediation_option(
            intent, missing, side="BUY", kind="complete"
        )
        unwind = self._fresh_cross_remediation_option(
            intent, filled, side="SELL", kind="unwind"
        )
        chosen = self._choose_cross_remediation_option(completion, unwind)
        if chosen is None:
            cleanup = self._clear_predict_allowance_zero(predict_leg.market_id)
            if cleanup is None:
                self._finish_cross_incident(
                    execution_id,
                    "predict_allowance_cleanup_failed",
                    evidence={"missing_venue": missing_venue},
                )
                return
            self._finish_cross_incident(
                execution_id,
                "cross_remediation_no_safe_option",
                evidence={
                    "missing_venue": missing_venue,
                    "completion": self._safe_mapping(completion or {}),
                    "unwind": self._safe_mapping(unwind or {}),
                    "reconciliation": reconciled,
                    "predict_allowance": cleanup,
                },
            )
            return
        worst_case_loss = chosen["worst_case_loss"]
        self._transition(
            execution_id,
            "remediating",
            {
                "phase": "bounded_cross_remediation",
                "action": chosen["kind"],
                "venue": chosen["venue"],
                "worst_case_loss": _safe_decimal(worst_case_loss),
            },
        )
        result = self._submit_cross_remediation_once(
            chosen, predict_leg=predict_leg, polymarket_leg=polymarket_leg
        )
        self._transition(
            execution_id,
            "remediating",
            {
                "phase": "bounded_cross_remediation_result",
                "action": chosen["kind"],
                "venue": chosen["venue"],
                "result": self._cross_leg_payload(chosen["leg"], result),
            },
        )
        if not self._cross_remediation_accepted(result, chosen["quantity"]):
            self._finish_cross_incident(
                execution_id,
                "cross_remediation_unverified",
                evidence={
                    "action": chosen["kind"],
                    "venue": chosen["venue"],
                    "worst_case_loss": _safe_decimal(worst_case_loss),
                },
            )
            return
        if chosen["kind"] == "unwind":
            if self._cross_unwind_is_proven(predict_leg, polymarket_leg):
                self._finish_cross_neutralized(
                    execution_id,
                    "cross_unwind_confirmed",
                    predict_market_id=predict_leg.market_id,
                    evidence={
                        "action": "unwind",
                        "venue": chosen["venue"],
                        "worst_case_loss": _safe_decimal(worst_case_loss),
                    },
                )
            else:
                self._finish_cross_incident(
                    execution_id,
                    "cross_unwind_unverified",
                    evidence={"venue": chosen["venue"], "worst_case_loss": _safe_decimal(worst_case_loss)},
                )
            return
        repaired = dict(reconciled)
        repaired[missing_venue] = (
            self._reconcile_predict_cross_leg(predict_leg, result)
            if missing_venue == "predict.fun"
            else self._reconcile_polymarket_cross_leg(
                polymarket_leg, result, _utc_now()
            )
        )
        positions = {
            venue: _decimal(value.get("position_quantity")) or Decimal("0")
            for venue, value in repaired.items()
        }
        if (
            all(value.get("verified") is True for value in repaired.values())
            and positions["predict.fun"] == intent.quantity
            and positions["polymarket"] == intent.quantity
        ):
            self._finish_cross_holding(
                execution_id,
                positions=positions,
                reconciled=repaired,
                remediation_worst_case_loss=worst_case_loss,
                predict_market_id=predict_leg.market_id,
            )
            return
        self._finish_cross_incident(
            execution_id,
            "cross_remediation_unverified",
            evidence={
                "venue": missing_venue,
                "worst_case_loss": _safe_decimal(worst_case_loss),
                "reconciliation": repaired,
            },
        )

    def _fresh_cross_remediation_option(
        self,
        intent: CrossVenueIntent,
        leg: CrossVenueLeg,
        *,
        side: str,
        kind: str,
    ) -> dict[str, object] | None:
        collaborator = self._predict_trading if leg.exchange == "predict.fun" else self._trading
        method = getattr(collaborator, "cross_remediation_option", None)
        if not callable(method):
            return None
        try:
            response = _call(
                method,
                venue=leg.exchange,
                market_id=leg.market_id,
                condition_id=leg.condition_id,
                token_id=leg.token_id,
                outcome=leg.outcome,
                side=side,
                quantity=leg.net_quantity,
                maximum_fee=leg.maximum_fee,
            )
        except Exception:
            return None
        if not isinstance(response, Mapping) or response.get("fresh") is not True:
            return None
        age = _age_seconds(response.get("checked_at"))
        raw = response.get("option")
        if age is None or age > float(BOOK_FRESHNESS_SECONDS) or not isinstance(raw, Mapping):
            return None
        return self._normalise_cross_remediation_option(intent, leg, raw, side=side, kind=kind)

    @staticmethod
    def _normalise_cross_remediation_option(
        intent: CrossVenueIntent,
        leg: CrossVenueLeg,
        raw: Mapping[str, object],
        *,
        side: str,
        kind: str,
    ) -> dict[str, object] | None:
        if (
            raw.get("venue") != leg.exchange
            or raw.get("market_id") != leg.market_id
            or raw.get("condition_id") != leg.condition_id
            or raw.get("token_id") != leg.token_id
            or raw.get("outcome") != leg.outcome
            or raw.get("side") != side
        ):
            return None
        quantity = _decimal(raw.get("quantity"))
        price = _decimal(raw.get("executable_price"))
        fee = _decimal(raw.get("fee"))
        slippage = _decimal(raw.get("slippage"))
        dust = _decimal(raw.get("residual_dust"))
        gas = intent.calculable_gas
        if (
            quantity != leg.net_quantity
            or price is None
            or fee is None
            or slippage is None
            or dust is None
            or not all(value.is_finite() and value >= 0 for value in (fee, slippage, dust, gas))
            or price <= 0
            or price > 1
        ):
            return None
        option: dict[str, object] = {
            "kind": kind,
            "venue": leg.exchange,
            "leg": leg,
            "side": side,
            "quantity": quantity,
            "condition_id": leg.condition_id,
            "market_id": leg.market_id,
            "token_id": leg.token_id,
            "outcome": leg.outcome,
            "executable_price": price,
            "fee": fee,
            "slippage": slippage,
            "residual_dust": dust,
        }
        if side == "BUY":
            max_spend = _decimal(raw.get("max_spend"))
            if max_spend is None or max_spend <= 0 or max_spend != quantity * price + fee + slippage:
                return None
            option["max_spend"] = max_spend
            loss = max_spend + gas + dust
        elif side == "SELL":
            shares = _decimal(raw.get("shares"))
            min_price = _decimal(raw.get("min_price"))
            if shares != quantity or min_price != price:
                return None
            option.update({"shares": shares, "min_price": min_price})
            loss = quantity * (Decimal("1") - price) + fee + slippage + gas + dust
        else:
            return None
        if not loss.is_finite() or loss < 0:
            return None
        option["worst_case_loss"] = loss
        return option

    @staticmethod
    def _choose_cross_remediation_option(
        completion: Mapping[str, object] | None,
        unwind: Mapping[str, object] | None,
    ) -> dict[str, object] | None:
        candidates: list[tuple[Decimal, int, dict[str, object]]] = []
        for priority, candidate in enumerate((completion, unwind)):
            if not isinstance(candidate, Mapping):
                continue
            loss = _decimal(candidate.get("worst_case_loss"))
            if loss is None or loss > MAX_EMERGENCY_LOSS:
                continue
            candidates.append((loss, priority, dict(candidate)))
        if not candidates:
            return None
        return min(candidates, key=lambda item: (item[0], item[1]))[2]

    def _submit_cross_remediation_once(
        self,
        chosen: Mapping[str, object],
        *,
        predict_leg: CrossVenueLeg,
        polymarket_leg: CrossVenueLeg,
    ) -> object:
        leg = chosen.get("leg")
        side = chosen.get("side")
        venue = chosen.get("venue")
        if not isinstance(leg, CrossVenueLeg) or side not in {"BUY", "SELL"}:
            return {"status": "blocked", "error_code": "invalid"}
        if side == "SELL":
            if venue != "polymarket":
                return {"status": "blocked", "error_code": "unsupported"}
            submit = getattr(self._trading, "submit_remediation_once", None)
            order = dict(chosen)
            order["leg"] = leg.outcome
            return _call(submit, order) if callable(submit) else {"status": "blocked", "error_code": "unsupported"}
        if venue == "polymarket":
            max_spend = _decimal(chosen.get("max_spend"))
            price = _decimal(chosen.get("executable_price"))
            fee = _decimal(chosen.get("fee"))
            if max_spend is None or price is None or fee is None:
                return {"status": "blocked", "error_code": "invalid"}
            bound = replace(leg, max_price=price, max_cost=max_spend, maximum_fee=fee)
            preflight = getattr(self._trading, "no_submit_cross_leg_preflight", None)
            submit = getattr(self._trading, "submit_cross_leg_once", None)
            if not callable(preflight) or not callable(submit):
                return {"status": "blocked", "error_code": "unsupported"}
            try:
                if not self._preflight_passed(_call(preflight, bound)):
                    return {"status": "blocked", "error_code": "preflight"}
                return _call(submit, bound)
            except Exception:
                return {"status": "ambiguous", "error_code": "ambiguous"}
        if venue == "predict.fun":
            submit = getattr(self._predict_trading, "submit_cross_remediation_once", None)
            return _call(submit, dict(chosen)) if callable(submit) else {"status": "blocked", "error_code": "unsupported"}
        return {"status": "blocked", "error_code": "unsupported"}

    @staticmethod
    def _cross_remediation_accepted(result: object, quantity: object) -> bool:
        if PredictionExecutionService._cross_result_ambiguous(result):
            return False
        accepted = getattr(result, "accepted", result.get("accepted") if isinstance(result, Mapping) else False)
        if accepted is not True:
            return False
        expected = _decimal(quantity)
        filled = _decimal(getattr(result, "filled_quantity", result.get("filled_quantity") if isinstance(result, Mapping) else expected))
        return expected is not None and (filled is None or filled == Decimal("0") or filled == expected)

    def _cross_unwind_is_proven(
        self, predict_leg: CrossVenueLeg, polymarket_leg: CrossVenueLeg
    ) -> bool:
        polymarket = self._fresh_account_snapshot()
        predict = self._live_predict_account_snapshot()
        if polymarket is None or predict is None:
            return False
        return (
            self._cross_position_quantity(polymarket, polymarket_leg) == 0
            and self._cross_position_quantity(predict, predict_leg) == 0
        )

    def _finish_cross_neutralized(
        self,
        execution_id: str,
        reason: str,
        *,
        predict_market_id: str,
        evidence: Mapping[str, object],
    ) -> None:
        cleanup = self._clear_predict_allowance_zero(predict_market_id)
        if cleanup is None:
            self._finish_cross_incident(
                execution_id,
                "predict_allowance_cleanup_failed",
                evidence=evidence,
            )
            return
        self._cross_breaker_open = True
        details = {
            "phase": "cross_incident",
            "reason": reason,
            **self._safe_mapping(evidence),
            "predict_allowance": cleanup,
        }
        incident_id = self._record_incident(
            execution_id, reason, state="neutralized_incident", evidence=details
        )
        self._transition(
            execution_id,
            "neutralized_incident",
            {**details, "breaker": "open", **({"incident_id": incident_id} if incident_id else {})},
        )

    def _finish_cross_incident(
        self,
        execution_id: str,
        reason: str,
        *,
        evidence: Mapping[str, object] | None = None,
    ) -> None:
        self._cross_breaker_open = True
        details = {"phase": "cross_incident", "reason": reason, **self._safe_mapping(evidence or {})}
        incident_id = self._record_incident(
            execution_id, reason, state="directional_incident", evidence=details
        )
        self._transition(
            execution_id,
            "directional_incident",
            {**details, "breaker": "open", **({"incident_id": incident_id} if incident_id else {})},
        )

    def _is_cross_auto_execution(self, execution_id: str) -> bool:
        try:
            row = self.execution(execution_id)
        except KeyError:
            return False
        return (
            row.get("market_type") == "cross_venue_yes_no"
            and row.get("auto_submit") is True
        )

    @staticmethod
    def _feishu_delivery_failed(attempts: object) -> bool:
        if not isinstance(attempts, (list, tuple)):
            return False
        return any(
            isinstance(attempt, Mapping)
            and attempt.get("channel") in {"feishu", "feishu_app"}
            and attempt.get("success") is not True
            for attempt in attempts
        )

    def _notify_cross_auto_success(
        self, execution_id: str, evidence: Mapping[str, object]
    ) -> None:
        if not self._is_cross_auto_execution(execution_id):
            return
        positions = evidence.get("positions")
        message = "\n".join(
            (
                "双边订单已成交并完成 REST 对账。",
                f"执行编号：{execution_id}",
                f"持仓：{positions}",
                "Predict 授权已清零。",
                f"Dashboard：{self._dashboard_url}",
            )
        )
        try:
            sent = self._deliver_feishu_notification("自动下单已完成", message)
        except Exception:
            sent = False
        if not sent:
            self._store.pause_cross_auto("notification_delivery_failed")

    def _notify_cross_auto_residual(
        self, execution_id: str, evidence: Mapping[str, object]
    ) -> None:
        if not self._is_cross_auto_execution(execution_id):
            return
        message = "\n".join(
            (
                "自动下单完成对账，但保留在安全残差状态。",
                f"执行编号：{execution_id}",
                f"未对冲数量：{evidence.get('unhedged_units')}",
                f"最坏损失：{evidence.get('worst_case_loss')}",
                "Predict 授权已清零；未标记为成功套利。",
                f"Dashboard：{self._dashboard_url}",
            )
        )
        try:
            sent = self._deliver_feishu_notification("自动下单残差事件", message)
        except Exception:
            sent = False
        if not sent:
            self._store.pause_cross_auto("notification_delivery_failed")

    def _run_threshold_execution(
        self,
        execution_id: str,
        *,
        intent: ThresholdHedgeIntent,
        opportunity: Mapping[str, object],
        account: Mapping[str, object],
        preview_payload: Mapping[str, object],
    ) -> None:
        """Execute the non-merge branch for two independent conditions."""

        preview_hash_a = preview_payload.get("rules_hash_a")
        preview_hash_b = preview_payload.get("rules_hash_b")
        current_hash_a = opportunity.get("rules_hash_a")
        current_hash_b = opportunity.get("rules_hash_b")
        if (
            preview_hash_a is not None
            and preview_hash_b is not None
            and (preview_hash_a != current_hash_a or preview_hash_b != current_hash_b)
        ):
            self._finish_rejected(execution_id, "rule_hash_changed")
            return
        preview_cache_key = preview_payload.get("cache_key")
        current_cache_key = opportunity.get("cache_key")
        if (
            preview_cache_key
            and current_cache_key
            and preview_cache_key != current_cache_key
        ):
            self._finish_rejected(execution_id, "cache_fingerprint_changed")
            return
        self._transition(execution_id, "submitting", {"phase": "submitting"})
        submitted_at = _utc_now()
        preflight = getattr(self._trading, "no_submit_threshold_preflight", None)
        if not callable(preflight):
            self._finish_rejected(execution_id, "threshold_preflight_unavailable")
            return
        preflight_exc: Exception | None = None
        try:
            result = _call(preflight, intent)
        except Exception as exc:
            result, preflight_exc = None, exc
        if not self._preflight_passed(result):
            self._finish_rejected(
                execution_id,
                (
                    self._preflight_error_code(result)
                    or self._preflight_exception_code(preflight_exc)
                    or "preflight_failed"
                ),
                extra_evidence={
                    "preflight": self._preflight_failure_evidence(
                        result, exception=preflight_exc
                    )
                },
            )
            return
        submit = getattr(self._trading, "submit_threshold_hedge_once", None)
        if not callable(submit):
            self._finish_rejected(execution_id, "threshold_submission_unavailable")
            return
        submit_error: dict[str, str] | None = None
        try:
            submission = _call(submit, intent)
        except Exception as exc:
            submit_error = _submit_error_detail(exc)
            submission = self._ambiguous_threshold_submission(intent)
        leg_a, leg_b = self._threshold_submission_legs(submission, intent)
        self._store.record_leg(execution_id, self._threshold_leg_payload(leg_a))
        self._store.record_leg(execution_id, self._threshold_leg_payload(leg_b))
        for leg, result in ((intent.leg_a, leg_a), (intent.leg_b, leg_b)):
            if result.accepted:
                self._notify_threshold_submitted(leg, result)
            elif not self._threshold_ambiguous(result):
                self._notify_threshold_rejected(leg, result)
        post_error = self._resolved_post_error(submit_error)
        self._transition(
            execution_id,
            "reconciling",
            {
                "phase": "reconciling",
                "post_attempted": True,
                **post_error,
            },
        )
        if self._threshold_both_rejected(leg_a, leg_b):
            self._transition(
                execution_id,
                "both_rejected",
                {"phase": "both_rejected", "merge": "not_applicable"},
            )
            return
        known = self._threshold_reconcile_until(
            intent, since=submitted_at, leg_a=leg_a, leg_b=leg_b
        )
        if known is None:
            self._resolve_submit_unknown(
                execution_id,
                account=account,
                token_ids=(intent.leg_a.token_id, intent.leg_b.token_id),
                post_error=post_error,
            )
            return
        quantity_a, quantity_b, proof = known
        if quantity_a > 0:
            self._notify_threshold_filled(intent.leg_a, leg_a.order_id, quantity_a)
        if quantity_b > 0:
            self._notify_threshold_filled(intent.leg_b, leg_b.order_id, quantity_b)
        if quantity_a > 0 and quantity_b <= 0:
            self._handle_threshold_one_leg(
                execution_id, intent, "A", quantity_a, leg_a, leg_b, proof, submitted_at, account
            )
            return
        if quantity_b > 0 and quantity_a <= 0:
            self._handle_threshold_one_leg(
                execution_id, intent, "B", quantity_b, leg_a, leg_b, proof, submitted_at, account
            )
            return
        if quantity_a <= 0 or quantity_b <= 0 or quantity_a != quantity_b:
            self._finish_incident(execution_id, "directional_imbalance")
            return
        if proof.get("verified") is not True:
            self._finish_incident(execution_id, "equal_fill_proof_unverified")
            return
        self._transition(
            execution_id,
            "reconciling",
            {
                "phase": "reconciled",
                "leg_a_quantity": quantity_a,
                "leg_b_quantity": quantity_b,
                "execution_proof": proof,
            },
        )
        self._transition(
            execution_id,
            "holding_to_resolution",
            {
                "phase": "holding_to_resolution",
                "merge": "not_applicable",
                "relation_id": intent.relation_id,
                "leg_a_condition_id": intent.leg_a.condition_id,
                "leg_b_condition_id": intent.leg_b.condition_id,
                "quantity": quantity_a,
                "execution_proof": proof,
            },
        )
        self._notify_threshold_settlement(
            execution_id, intent, quantity_a, proof
        )

    def _threshold_reconcile_until(
        self,
        intent: ThresholdHedgeIntent,
        *,
        since: datetime,
        leg_a: ThresholdLegResult,
        leg_b: ThresholdLegResult,
    ) -> tuple[Decimal, Decimal, dict[str, object]] | None:
        started = self._clock()
        for attempt in range(MAX_RECONCILIATION_SECONDS + 1):
            reconcile = getattr(self._trading, "reconcile_threshold_hedge", None)
            if not callable(reconcile):
                return None
            try:
                value = _call(
                    reconcile,
                    intent=intent,
                    since=since,
                    leg_a=leg_a,
                    leg_b=leg_b,
                )
            except Exception:
                value = None
            if isinstance(value, Mapping):
                status = str(value.get("status", "")).lower()
                proof = value.get("execution_proof")
                quantity_a = _decimal(value.get("leg_a_quantity")) or Decimal("0")
                quantity_b = _decimal(value.get("leg_b_quantity")) or Decimal("0")
                if status in {"ok", "partial", "confirmed", "filled", "complete"} and isinstance(proof, Mapping):
                    if quantity_a > 0 or quantity_b > 0:
                        return quantity_a, quantity_b, dict(proof)
            if attempt >= MAX_RECONCILIATION_SECONDS or self._clock() - started >= MAX_RECONCILIATION_SECONDS:
                break
            self._sleep(1)
        return None

    @staticmethod
    def _ambiguous_threshold_submission(
        intent: ThresholdHedgeIntent,
    ) -> ThresholdHedgeSubmission:
        return ThresholdHedgeSubmission(
            leg_a=ThresholdLegResult(
                "A", intent.leg_a.outcome, intent.leg_a.condition_id,
                intent.leg_a.token_id, False, "ambiguous", "", Decimal("0"), (), "ambiguous"
            ),
            leg_b=ThresholdLegResult(
                "B", intent.leg_b.outcome, intent.leg_b.condition_id,
                intent.leg_b.token_id, False, "ambiguous", "", Decimal("0"), (), "ambiguous"
            ),
        )

    @staticmethod
    def _threshold_submission_legs(
        value: object, intent: ThresholdHedgeIntent
    ) -> tuple[ThresholdLegResult, ThresholdLegResult]:
        if isinstance(value, ThresholdHedgeSubmission):
            return value.leg_a, value.leg_b
        if isinstance(value, Mapping):
            return (
                PredictionExecutionService._coerce_threshold_leg(value.get("leg_a", value.get("a")), intent.leg_a),
                PredictionExecutionService._coerce_threshold_leg(value.get("leg_b", value.get("b")), intent.leg_b),
            )
        return (
            PredictionExecutionService._ambiguous_threshold_submission(intent).leg_a,
            PredictionExecutionService._ambiguous_threshold_submission(intent).leg_b,
        )

    @staticmethod
    def _coerce_threshold_leg(value: object, leg: ThresholdHedgeLeg) -> ThresholdLegResult:
        if isinstance(value, ThresholdLegResult):
            return value
        if isinstance(value, Mapping):
            filled = _decimal(value.get("filled_quantity", value.get("quantity", 0))) or Decimal("0")
            return ThresholdLegResult(
                leg.label,
                leg.outcome,
                leg.condition_id,
                leg.token_id,
                value.get("accepted") is True,
                str(value.get("status", "rejected")),
                str(value.get("order_id", "")),
                filled,
                tuple(item for item in value.get("trade_ids", ()) if isinstance(item, str)),
                str(value.get("error_code", "none")),
            )
        return ThresholdLegResult(
            leg.label, leg.outcome, leg.condition_id, leg.token_id,
            False, "ambiguous", "", Decimal("0"), (), "ambiguous"
        )

    @staticmethod
    def _threshold_leg_payload(leg: ThresholdLegResult) -> dict[str, object]:
        return {
            "label": leg.label,
            "outcome": leg.outcome,
            "condition_id": leg.condition_id,
            "token_id": leg.token_id,
            "accepted": leg.accepted,
            "status": leg.status,
            "order_id": leg.order_id,
            "filled_quantity": format(leg.filled_quantity, "f"),
            "trade_ids": list(leg.trade_ids),
            "error_code": leg.error_code,
        }

    @staticmethod
    def _threshold_both_rejected(
        leg_a: ThresholdLegResult, leg_b: ThresholdLegResult
    ) -> bool:
        return (
            not leg_a.accepted
            and not leg_b.accepted
            and leg_a.filled_quantity <= 0
            and leg_b.filled_quantity <= 0
            and not PredictionExecutionService._threshold_ambiguous(leg_a)
            and not PredictionExecutionService._threshold_ambiguous(leg_b)
        )

    @staticmethod
    def _threshold_ambiguous(leg: ThresholdLegResult) -> bool:
        return leg.error_code == "ambiguous" or leg.status.lower() in {
            "ambiguous", "pending", "delayed", "processing"
        }

    @staticmethod
    def _threshold_option(options: Mapping[str, object], name: str) -> dict[str, object] | None:
        value = options.get(name)
        return dict(value) if isinstance(value, Mapping) else None

    def _choose_threshold_remediation(
        self,
        options: Mapping[str, object],
        *,
        intent: ThresholdHedgeIntent,
        filled_label: str,
        filled_quantity: Decimal,
    ) -> dict[str, object] | None:
        filled = intent.leg_a if filled_label == "A" else intent.leg_b
        missing = intent.leg_b if filled_label == "A" else intent.leg_a
        candidates: list[tuple[Decimal, int, dict[str, object]]] = []
        complete = self._threshold_option(options, "complete")
        if complete is not None:
            loss = _decimal(complete.get("loss", complete.get("estimated_loss")))
            amount = complete.get("amount", complete.get("max_spend"))
            max_price = complete.get("max_price")
            if (
                loss is not None and 0 <= loss <= Decimal("2")
                and complete.get("leg") == missing.label
                and complete.get("side") == "BUY"
                and complete.get("token_id") == missing.token_id
                and isinstance(amount, Decimal) and isinstance(max_price, Decimal)
                and amount > 0 and amount <= Decimal("2") and 0 < max_price <= 1
                and protected_buy_quantity(spend=amount, price=max_price, tick_size=missing.tick_size) == filled_quantity
            ):
                candidate = dict(complete)
                candidate["quantity"] = filled_quantity
                candidate["condition_id"] = missing.condition_id
                candidates.append((loss, 0, candidate))
        unwind = self._threshold_option(options, "unwind")
        if unwind is not None:
            loss = _decimal(unwind.get("loss", unwind.get("estimated_loss")))
            shares = unwind.get("shares", unwind.get("quantity"))
            min_price = unwind.get("min_price")
            if (
                loss is not None and 0 <= loss <= Decimal("2")
                and unwind.get("leg") == filled.label
                and unwind.get("side") == "SELL"
                and unwind.get("token_id") == filled.token_id
                and shares == filled_quantity
                and isinstance(min_price, Decimal) and 0 < min_price <= 1
            ):
                candidate = dict(unwind)
                candidate["quantity"] = filled_quantity
                candidate["condition_id"] = filled.condition_id
                candidates.append((loss, 1, candidate))
        if not candidates:
            return None
        return min(candidates, key=lambda item: (item[0], item[1]))[2]

    def _handle_threshold_one_leg(
        self,
        execution_id: str,
        intent: ThresholdHedgeIntent,
        filled_label: str,
        filled_quantity: Decimal,
        leg_a: ThresholdLegResult,
        leg_b: ThresholdLegResult,
        proof: Mapping[str, object],
        since: datetime,
        account: Mapping[str, object],
    ) -> None:
        self._breaker_open = True
        filled = intent.leg_a if filled_label == "A" else intent.leg_b
        remediation = getattr(self._trading, "threshold_remediation_options", None)
        if not callable(remediation):
            remediation = getattr(self._trading, "remediation_options", None)
        options: Mapping[str, object] = {}
        if callable(remediation):
            try:
                value = _call(
                    remediation,
                    condition_id=filled.condition_id,
                    yes_token_id=filled.token_id if filled.outcome == "YES" else "",
                    no_token_id=filled.token_id if filled.outcome == "NO" else "",
                    filled_leg=filled_label,
                    filled_quantity=filled_quantity,
                    since=since,
                    intent=intent,
                )
                if isinstance(value, Mapping):
                    options = value
            except Exception:
                options = {}
        chosen = self._choose_threshold_remediation(
            options,
            intent=intent,
            filled_label=filled_label,
            filled_quantity=filled_quantity,
        )
        incident_id = self._record_incident(
            execution_id,
            "one_leg_fill",
            state="remediating" if chosen is not None else "directional_incident",
            evidence={
                "phase": "one_leg",
                "filled_leg": filled_label,
                "filled_quantity": _safe_decimal(filled_quantity),
                "execution_proof": proof,
                "remediation_options": options,
            },
        )
        if chosen is None:
            self._finish_incident(
                execution_id, "no_safe_remediation", incident_id=incident_id, notify=False
            )
            return
        self._transition(execution_id, "remediating", {"phase": "remediation_selected", "order": chosen})
        submit = getattr(self._trading, "submit_threshold_remediation_once", None)
        if not callable(submit):
            submit = getattr(self._trading, "submit_remediation_once", None)
        try:
            result = _call(submit, chosen) if callable(submit) else None
        except Exception:
            result = None
        if isinstance(result, ThresholdLegResult):
            repaired = result
        elif isinstance(result, LegResult):
            repaired = ThresholdLegResult(
                str(chosen.get("leg", filled_label)),
                filled.outcome if str(chosen.get("leg", filled_label)) == filled.label else (intent.leg_a.outcome if filled_label == "B" else intent.leg_b.outcome),
                str(chosen.get("condition_id", filled.condition_id)),
                str(chosen.get("token_id", filled.token_id)),
                result.accepted, result.status, result.order_id, result.filled_quantity,
                result.trade_ids, result.error_code,
            )
        else:
            repaired = ThresholdLegResult(
                str(chosen.get("leg", filled_label)), filled.outcome, filled.condition_id,
                filled.token_id, False, "ambiguous", "", Decimal("0"), (), "ambiguous"
            )
        self._store.record_leg(execution_id, {**self._threshold_leg_payload(repaired), "label": "remediation"})
        self._transition(execution_id, "remediating", {"phase": "remediation_result", **self._threshold_leg_payload(repaired)})
        if not repaired.accepted or repaired.filled_quantity != filled_quantity or self._threshold_ambiguous(repaired):
            self._finish_incident(execution_id, "remediation_unverified", incident_id=incident_id, notify=False)
            return
        if chosen.get("side") == "SELL":
            self._finish_incident(
                execution_id, "one_leg_unwound", state="neutralized_incident", incident_id=incident_id, notify=False
            )
            return
        repaired_a = leg_a if filled_label == "A" else repaired
        repaired_b = leg_b if filled_label == "B" else repaired
        known = self._threshold_reconcile_until(intent, since=since, leg_a=repaired_a, leg_b=repaired_b)
        if known is None or known[0] <= 0 or known[1] <= 0 or known[0] != known[1] or known[2].get("verified") is not True:
            self._finish_incident(execution_id, "remediation_reconciliation_unverified", incident_id=incident_id, notify=False)
            return
        self._transition(
            execution_id,
            "holding_to_resolution",
            {"phase": "holding_to_resolution", "merge": "not_applicable", "quantity": known[0], "execution_proof": known[2]},
        )

    def _fresh_opportunity(
        self,
        opportunity_id: str,
        *,
        target_quantity: Decimal | None = None,
        max_total_cost: Decimal | None = None,
        prefer_smallest: bool = False,
    ) -> dict[str, object] | None:
        cross_venue = opportunity_id.startswith("cross:")
        source = self._cross_venue_monitor if cross_venue else self._monitor
        if source is None:
            return None
        if cross_venue:
            refresh = getattr(source, "refresh_opportunity", None)
            if not callable(refresh):
                return None
            try:
                refreshed = _call(
                    refresh,
                    opportunity_id,
                    target_quantity=target_quantity,
                    max_total_cost=max_total_cost,
                    prefer_smallest=prefer_smallest,
                )
            except Exception:
                return None
            if not isinstance(refreshed, Mapping):
                return None
            if str(refreshed.get("opportunity_id", refreshed.get("id", ""))) == opportunity_id:
                return dict(refreshed)
            return None
        refreshed: object = None
        refresh_attempted = False
        for name in ("refresh_opportunity", "recheck_opportunity", "refresh_once", "refresh"):
            refresh = getattr(source, name, None)
            if not callable(refresh):
                continue
            refresh_attempted = True
            try:
                refreshed = _call(refresh, opportunity_id) if name != "refresh_once" else _call(refresh)
            except Exception:
                return None
            break
        if refresh_attempted and refreshed is None:
            return None
        if refreshed is None:
            snapshot = getattr(source, "snapshot", None)
            if callable(snapshot):
                try:
                    refreshed = _call(snapshot)
                except Exception:
                    refreshed = None
        if isinstance(refreshed, Mapping):
            if str(refreshed.get("opportunity_id", refreshed.get("id", ""))) == opportunity_id:
                return dict(refreshed)
            candidates = refreshed.get("opportunities", ())
            if isinstance(candidates, (list, tuple)):
                for candidate in candidates:
                    if isinstance(candidate, Mapping) and str(
                        candidate.get("opportunity_id", candidate.get("id", ""))
                    ) == opportunity_id:
                        return dict(candidate)
        opportunity = getattr(source, "opportunity", None)
        if callable(opportunity):
            try:
                value = _call(opportunity, opportunity_id)
            except Exception:
                value = None
            if isinstance(value, Mapping):
                return dict(value)
        return None

    def _wait_for_idempotency(self, key: str) -> dict[str, object] | None:
        if not key:
            return None
        for _ in range(200):
            existing = self._execution_for_idempotency(key)
            if existing is not None:
                return existing
            # Yield to the request that currently owns the process lock; this
            # wait is only the duplicate-idempotency race window, not venue
            # reconciliation time.
            time.sleep(0.005)
        return self._execution_for_idempotency(key)

    def _volatile_checks(
        self,
        intent: ExecutionIntent,
        *,
        expected_predict_allowance_raw: int = 0,
    ) -> tuple[dict[str, object] | None, str | None]:
        if isinstance(intent, CrossVenueIntent):
            return self._cross_volatile_checks(
                intent,
                expected_predict_allowance_raw=expected_predict_allowance_raw,
            )
        if not self._notification_channels_ready():
            return None, "notification_config_unavailable"
        geoblock = getattr(self._trading, "geoblock_allowed", None)
        if not callable(geoblock):
            return None, "geoblock_unavailable"
        try:
            if _call(geoblock) is not True:
                return None, "geoblock_blocked"
        except Exception:
            return None, "geoblock_unavailable"
        account = self._fresh_account_snapshot()
        if account is None:
            return None, "account_unavailable"
        wallet = str(account.get("wallet_address", "")).strip()
        if not wallet:
            return None, "wallet_unavailable"
        balance = _decimal(account.get("p_usd_balance"))
        allowance = _decimal(account.get("p_usd_allowance"))
        if (
            balance is None
            or allowance is None
            or balance < intent.total_max_cost
            or allowance < intent.total_max_cost
            or balance < 0
            or allowance < 0
            or balance > MAX_WALLET_BALANCE
        ):
            return None, "account_insufficient"
        if "checked_at" in account:
            checked_age = _age_seconds(account.get("checked_at"))
            if checked_age is None or checked_age > 60:
                return None, "account_stale"
        if not self._relayer_ready(require_merge=not isinstance(intent, ThresholdHedgeIntent)):
            return None, "relayer_unavailable"
        return account, None

    def _cross_volatile_checks(
        self,
        intent: CrossVenueIntent,
        *,
        expected_predict_allowance_raw: int = 0,
    ) -> tuple[dict[str, object] | None, str | None]:
        if not self._notification_channels_ready():
            return None, "notification_config_unavailable"
        geoblock = getattr(self._trading, "geoblock_allowed", None)
        try:
            if not callable(geoblock) or _call(geoblock) is not True:
                return None, "geoblock_blocked"
        except Exception:
            return None, "geoblock_unavailable"
        polymarket = self._fresh_account_snapshot()
        predict = self._live_predict_account_snapshot()
        if polymarket is None or predict is None:
            return None, "account_unavailable"
        by_exchange = {leg.exchange: leg for leg in intent.legs}
        poly_leg = by_exchange.get("polymarket")
        predict_leg = by_exchange.get("predict.fun")
        if poly_leg is None or predict_leg is None:
            return None, "cross_venue_identity"
        poly_balance = _decimal(polymarket.get("p_usd_balance"))
        poly_allowance = _decimal(polymarket.get("p_usd_allowance"))
        predict_balance = _decimal(predict.get("available_usdt"))
        predict_allowance = _decimal(predict.get("allowance"))
        predict_allowance_raw = _decimal(predict.get("allowance_raw"))
        expected_predict_allowance = (
            Decimal(expected_predict_allowance_raw) / Decimal(PREDICT_BASE_UNITS)
        )
        minimum_top_up = _decimal(predict.get("minimum_top_up_bnb")) or Decimal("0")
        if predict.get("gas_ready") is not True or minimum_top_up > 0:
            return None, "insufficient_bnb"
        if (
            predict.get("allowance_breaker")
            != (expected_predict_allowance_raw > 0)
            or (
                predict_allowance is not None
                and predict_allowance != expected_predict_allowance
            )
            or (
                predict_allowance_raw is not None
                and predict_allowance_raw != expected_predict_allowance_raw
            )
        ):
            return None, "residual_predict_allowance"
        if (
            not str(polymarket.get("wallet_address", "")).strip()
            or not str(predict.get("wallet_address", "")).strip()
            or poly_balance is None
            or poly_allowance is None
            or predict_balance is None
            or predict_allowance is None
            or predict_allowance_raw is None
            or poly_balance < poly_leg.max_cost
            or poly_allowance < poly_leg.max_cost
            or predict_balance < predict_leg.max_cost
            or predict.get("scope_ready") is not True
            or predict.get("allowance") in (None, "")
        ):
            return None, "account_insufficient"
        return {
            "predict.fun": {
                "asset": "USDT", "wallet_address": str(predict["wallet_address"]),
                "available_balance": predict_balance,
                "allowance": predict.get("allowance"),
                "allowance_raw": predict.get("allowance_raw"),
                "scope_ready": predict.get("scope_ready"),
                "gas_ready": predict.get("gas_ready"),
                "allowance_breaker": predict.get("allowance_breaker"),
            },
            "polymarket": {
                "asset": "pUSD", "wallet_address": str(polymarket["wallet_address"]),
                "available_balance": poly_balance, "allowance": poly_allowance,
            },
        }, None

    def _live_predict_account_snapshot(self) -> dict[str, object] | None:
        if self._predict_trading is None:
            return None
        method = getattr(self._predict_trading, "account_snapshot", None)
        try:
            value = _call(method)
        except Exception:
            return None
        if not isinstance(value, Mapping):
            return None
        snapshot = dict(value)
        snapshot = _normalize_predict_account_snapshot(snapshot)
        if snapshot is None:
            return None
        age = _age_seconds(snapshot.get("checked_at"))
        if age is None or age > 60 or not self._snapshot_collections_valid({"open_order_ids": snapshot["open_orders"], "positions": snapshot["positions"]}):
            return None
        return snapshot

    def _refresh_predict_account_snapshot(self) -> dict[str, object] | None:
        """Fetch a live predict snapshot and publish it to the read cache (#93)."""
        snapshot = self._live_predict_account_snapshot()
        if snapshot is not None:
            # A failed refresh keeps the previous entry; the <=60s age gate in
            # _fresh_predict_account_snapshot expires it naturally.
            with self._predict_snapshot_lock:
                self._predict_snapshot_cache = snapshot
        return snapshot

    def _fresh_predict_account_snapshot(self) -> dict[str, object] | None:
        # #93: pure cache read for the HTTP read path; the background refresher
        # publishes via _refresh_predict_account_snapshot. No network I/O here.
        with self._predict_snapshot_lock:
            snapshot = self._predict_snapshot_cache
        if snapshot is None:
            return None
        age = _age_seconds(snapshot.get("checked_at"))
        if age is None or age > 60:
            return None
        return snapshot

    def _account_snapshot(self) -> dict[str, object] | None:
        method = getattr(self._trading, "account_snapshot", None)
        try:
            value = _call(method)
        except Exception:
            return None
        if isinstance(value, Mapping):
            return dict(value)
        result: dict[str, object] = {}
        for name in (
            "wallet_address",
            "p_usd_balance",
            "p_usd_allowance",
            "open_order_ids",
            "positions",
            "checked_at",
        ):
            if hasattr(value, name):
                result[name] = getattr(value, name)
        return result or None

    def _fresh_account_snapshot(self) -> dict[str, object] | None:
        snapshot = self._account_snapshot()
        if snapshot is None:
            return None
        if "checked_at" not in snapshot:
            return None
        age = _age_seconds(snapshot.get("checked_at"))
        if age is None or age > 60:
            return None
        if "open_order_ids" not in snapshot or "positions" not in snapshot:
            return None
        if not self._snapshot_collections_valid(snapshot):
            return None
        return snapshot

    def _resolved_post_error(
        self, submit_error: Mapping[str, str] | None
    ) -> dict[str, str]:
        """Prefer the client's redacted submit-error detail, else our own."""

        detail: Mapping[str, object] | None = submit_error
        reader = getattr(self._trading, "last_submit_error", None)
        if callable(reader):
            try:
                client_detail = reader()
            except Exception:
                client_detail = None
            if isinstance(client_detail, Mapping) and any(client_detail.values()):
                detail = client_detail
        if not isinstance(detail, Mapping):
            return {}
        resolved = {
            key: str(detail.get(key, "")) for key in ("error_code", "error_type", "message")
        }
        if not any(resolved.values()):
            return {}
        return {
            "post_error_code": resolved["error_code"],
            "post_error_type": resolved["error_type"],
            "post_error_message": resolved["message"],
        }

    def _zero_landing_check(
        self,
        pre_account: Mapping[str, object],
        token_ids: tuple[str, ...],
    ) -> tuple[bool, dict[str, object]]:
        """One shared account read feeding both self-clear and incident evidence."""

        tokens = {str(token).strip() for token in token_ids if str(token).strip()}
        after = self._fresh_account_snapshot()
        if after is None:
            self._last_zero_landing_summary = {"available": False}
            return False, self._last_zero_landing_summary
        open_orders = self._order_ids(after.get("open_order_ids"))
        positions = after.get("positions")
        leg_hits: list[object] = []
        if isinstance(positions, (list, tuple)):
            for item in positions:
                if not isinstance(item, Mapping):
                    continue
                token = item.get("token_id", item.get("tokenId", item.get("asset_id", "")))
                if isinstance(token, str) and token.strip() in tokens:
                    leg_hits.append(self._safe_mapping(item))
        balance_before = _decimal(pre_account.get("p_usd_balance"))
        balance_after = _decimal(after.get("p_usd_balance"))
        summary: dict[str, object] = {
            "open_orders": "empty" if not open_orders else list(open_orders),
            "leg_positions": "empty" if not leg_hits else leg_hits,
            "balance_before": (
                "" if balance_before is None else format(balance_before, "f")
            ),
            "balance_after": (
                "" if balance_after is None else format(balance_after, "f")
            ),
            "checked_at": after.get("checked_at"),
        }
        self._last_zero_landing_summary = summary
        cleared = (
            not open_orders
            and not leg_hits
            and balance_before is not None
            and balance_after is not None
            and balance_before == balance_after
        )
        return cleared, summary

    def _zero_landing_self_clear(
        self,
        execution_id: str,
        *,
        pre_account: Mapping[str, object],
        token_ids: tuple[str, ...],
        post_error: Mapping[str, str] | None = None,
    ) -> bool:
        cleared, summary = self._zero_landing_check(pre_account, token_ids)
        if not cleared:
            return False
        evidence: dict[str, object] = {
            "phase": "zero_landing_self_clear",
            **dict(post_error or {}),
            "open_orders": "empty",
            "leg_positions": "empty",
            "balance_before": summary.get("balance_before", ""),
            "balance_after": summary.get("balance_after", ""),
            "checked_at": summary.get("checked_at"),
        }
        # Proven zero landing: recover without the breaker or an incident row.
        self._transition(execution_id, "submit_failed_cleared", evidence)
        self._notify_submit_failed_self_cleared(post_error or {}, summary)
        return True

    def _resolve_submit_unknown(
        self,
        execution_id: str,
        *,
        account: Mapping[str, object],
        token_ids: tuple[str, ...],
        post_error: Mapping[str, str],
    ) -> None:
        if self._zero_landing_self_clear(
            execution_id,
            pre_account=account,
            token_ids=token_ids,
            post_error=post_error,
        ):
            return
        self._finish_incident(
            execution_id,
            "reconciliation_timeout",
            zero_landing_check=self._last_zero_landing_summary or {"available": False},
        )

    def _notify_submit_failed_self_cleared(
        self,
        post_error: Mapping[str, str],
        summary: Mapping[str, object],
    ) -> None:
        try:
            reason = ""
            if any(post_error.values()):
                reason = (
                    f"原因：{post_error.get('post_error_code', '')}"
                    f"｜{post_error.get('post_error_type', '')}"
                    f"：{post_error.get('post_error_message', '')}\n"
                )
            message = (
                reason
                + "自证：挂单空 · 两腿无持仓 · 余额未变"
                f"（{summary.get('balance_before', '')} → {summary.get('balance_after', '')}）\n"
                "系统已自动恢复，无需处理。"
            )
            self._deliver_feishu_notification(
                "预测套利单提交失败（已自证零落地）", message
            )
        except Exception:
            return

    def n_leg_account_view(self) -> object | None:
        """Read-only #52 account seam in integer micro-USDC; None fails closed."""
        from open_trader.prediction_market_solution import AccountView

        snapshot = self._fresh_account_snapshot()
        if snapshot is None:
            return None
        try:
            balance = _decimal(snapshot.get("p_usd_balance"))
            allowance = _decimal(snapshot.get("p_usd_allowance"))
        except InvalidOperation:
            return None
        if (
            balance is None
            or allowance is None
            or balance < 0
            or allowance < 0
            or not balance.is_finite()
            or not allowance.is_finite()
        ):
            return None
        return AccountView(
            available_units=int(balance * 1_000_000),
            allowance_units=int(allowance * 1_000_000),
        )

    @staticmethod
    def _snapshot_collections_valid(snapshot: Mapping[str, object]) -> bool:
        open_order_ids = snapshot.get("open_order_ids")
        if isinstance(open_order_ids, Mapping):
            if any(not isinstance(key, str) for key in open_order_ids):
                return False
        elif isinstance(open_order_ids, (list, tuple, set, frozenset)):
            if any(not isinstance(item, str) for item in open_order_ids):
                return False
        else:
            return False
        return isinstance(snapshot.get("positions"), (list, tuple))

    @staticmethod
    def _order_ids(value: object) -> tuple[str, ...]:
        if isinstance(value, Mapping):
            value = value.keys()
        if not isinstance(value, (list, tuple, set, frozenset)):
            return ()
        return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())

    @staticmethod
    def _safe_sequence(value: object) -> list[object]:
        if isinstance(value, (list, tuple, set, frozenset)):
            return [item if isinstance(item, (str, int, bool)) or item is None else str(item) for item in value]
        return []

    @staticmethod
    def _position_totals(
        snapshot: Mapping[str, object],
        intent: ExecutionIntent | None,
        *,
        known_tokens: set[str] | None = None,
    ) -> dict[str, object]:
        positions = snapshot.get("positions", ())
        totals: dict[str, object] = {"yes": Decimal("0"), "no": Decimal("0"), "unknown": []}
        if not isinstance(positions, (list, tuple)):
            return totals
        if isinstance(intent, ThresholdHedgeIntent):
            yes_token = intent.leg_a.token_id
            no_token = intent.leg_b.token_id
        elif isinstance(intent, CrossVenueIntent):
            polymarket_leg = next(
                (leg for leg in intent.legs if leg.exchange == "polymarket"), None
            )
            yes_token = (
                polymarket_leg.token_id
                if polymarket_leg is not None and polymarket_leg.outcome == "YES"
                else None
            )
            no_token = (
                polymarket_leg.token_id
                if polymarket_leg is not None and polymarket_leg.outcome == "NO"
                else None
            )
        else:
            yes_token = intent.yes_token_id if intent else None
            no_token = intent.no_token_id if intent else None
        known = known_tokens or set()
        for position in positions:
            if not isinstance(position, Mapping):
                totals["unknown"].append(str(position))
                continue
            token = position.get("token_id", position.get("tokenId", position.get("asset_id", "")))
            quantity = _decimal(position.get("size", position.get("quantity", position.get("shares"))))
            current_value = _decimal(
                position.get("current_value", position.get("currentValue"))
            )
            redeemable = position.get("redeemable")
            if current_value == 0 and (
                redeemable is True or str(redeemable).casefold() == "true"
            ):
                continue
            if not isinstance(token, str) or quantity is None or quantity < 0:
                totals["unknown"].append(PredictionExecutionService._safe_mapping(position))
                continue
            if yes_token and token == yes_token:
                totals["yes"] = totals["yes"] + quantity
            elif no_token and token == no_token:
                totals["no"] = totals["no"] + quantity
            elif token in known and quantity > 0:
                continue
            elif quantity > 0:
                totals["unknown"].append(PredictionExecutionService._safe_mapping(position))
        return totals

    def _known_holding_tokens(self) -> set[str]:
        tokens: set[str] = set()
        for row in self._store.histories("executions"):
            if row.get("state") != "holding_to_resolution":
                continue
            intent = self._intent_from_payload(row.get("intent"))
            if isinstance(intent, ThresholdHedgeIntent):
                tokens.update((intent.leg_a.token_id, intent.leg_b.token_id))
            elif isinstance(intent, PairIntent):
                tokens.update((intent.yes_token_id, intent.no_token_id))
        return tokens

    def _holding_imbalances(
        self, snapshot: Mapping[str, object]
    ) -> dict[str, dict[str, object]]:
        """Known holdings whose hedge legs diverge without a redeemable winner."""
        imbalances: dict[str, dict[str, object]] = {}
        positions = snapshot.get("positions")
        if not isinstance(positions, (list, tuple)):
            return imbalances
        for row in self._store.histories("executions"):
            if row.get("state") != "holding_to_resolution":
                continue
            intent = self._intent_from_payload(row.get("intent"))
            if isinstance(intent, ThresholdHedgeIntent):
                legs = (
                    ("leg_a", intent.leg_a.token_id),
                    ("leg_b", intent.leg_b.token_id),
                )
            elif isinstance(intent, PairIntent):
                legs = (
                    ("yes", intent.yes_token_id),
                    ("no", intent.no_token_id),
                )
            else:
                continue
            execution_id = str(row.get("execution_id", ""))
            if not execution_id:
                continue
            quantities: dict[str, Decimal] = {}
            redeemable_legs: dict[str, bool] = {}
            for label, token in legs:
                quantity = Decimal("0")
                leg_redeemable = False
                for position in positions:
                    if not isinstance(position, Mapping):
                        continue
                    token_id = position.get(
                        "token_id", position.get("tokenId", position.get("asset_id", ""))
                    )
                    if not isinstance(token_id, str) or token_id != token:
                        continue
                    redeemable = position.get("redeemable")
                    if redeemable is True or str(redeemable).casefold() == "true":
                        leg_redeemable = True
                    current_value = _decimal(
                        position.get("current_value", position.get("currentValue"))
                    )
                    if current_value == 0 and leg_redeemable:
                        continue
                    leg_quantity = _decimal(
                        position.get(
                            "size", position.get("quantity", position.get("shares"))
                        )
                    )
                    if leg_quantity is None or leg_quantity < 0:
                        continue
                    quantity += leg_quantity
                quantities[label] = quantity
                redeemable_legs[label] = leg_redeemable
            first, second = (label for label, _ in legs)
            balanced = quantities[first] == quantities[second]
            settled_winner = (
                (quantities[first] > 0) != (quantities[second] > 0)
                and redeemable_legs[first if quantities[first] > 0 else second]
            )
            if balanced or settled_winner:
                continue
            imbalances[execution_id] = {
                label: format(quantities[label], "f") for label, _ in legs
            }
        return imbalances

    def _startup_incident(
        self,
        execution_id: str,
        reason: str,
        evidence: Mapping[str, object],
        *,
        state: str = "directional_incident",
    ) -> str | None:
        if not execution_id:
            existing = self._store.unacknowledged_incident()
            if (
                isinstance(existing, Mapping)
                and existing.get("reason") == reason
            ):
                incident_id = existing.get("incident_id")
                return str(incident_id) if incident_id else None
            attempts = self._notify_incident(reason)
            recovery = getattr(self._store, "create_recovery_execution", None)
            if not callable(recovery):
                return None
            payload = {
                "recovery": True,
                "reason": reason,
                "state": state,
                "breaker": "open",
                **self._safe_mapping(evidence),
                "notification_attempts": attempts,
            }
            try:
                execution = recovery(
                    payload,
                    idempotency_key=f"startup:{reason}:{_timestamp(_utc_now())[:19]}",
                )
                execution_id = str(execution.get("execution_id", ""))
                return self._record_incident(
                    execution_id,
                    reason,
                    state=state,
                    evidence={**payload, "notification_attempts": attempts},
                    notify=False,
                )
            except Exception:
                return None
        return self._record_incident(
            execution_id,
            reason,
            state=state,
            evidence=evidence,
        )

    def _startup_merge(
        self,
        active: Mapping[str, object] | None,
        intent: PairIntent | None,
        quantity: Decimal,
        *,
        before: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        if active is None or intent is None:
            return {"merge": "blocked", "merge_reason": "unknown_pair"}
        evidence = active.get("evidence", ())
        startup_attempts: list[Mapping[str, object]] = []
        fresh_merge_attempt = False
        if isinstance(evidence, (list, tuple)):
            merge_evidence = [
                item
                for item in evidence
                if isinstance(item, Mapping)
                and item.get("phase") in {"merge_result", "remediation_merge_result"}
            ]
            startup_attempts = [
                item
                for item in evidence
                if isinstance(item, Mapping)
                and item.get("phase") in {
                    "startup_merge_attempt",
                    "remediation_merge_attempt",
                }
            ]
            if merge_evidence:
                if self._merge_confirmed(merge_evidence[-1]):
                    result: object = merge_evidence[-1]
                else:
                    return {
                        "merge": "pending",
                        "reconciled": False,
                        "merge_reason": "merge_attempt_unconfirmed",
                        "merge_result": self._safe_mapping(merge_evidence[-1]),
                    }
            else:
                result = None
        else:
            result = None
        if result is None and (startup_attempts or str(active.get("state", "")) == "merging"):
            return {
                "merge": "pending",
                "reconciled": False,
                "merge_reason": "merge_attempt_in_flight",
                **(
                    {"startup_merge_attempt": self._safe_mapping(startup_attempts[-1])}
                    if startup_attempts
                    else {}
                ),
            }
        if result is None:
            fresh_merge_attempt = True
            execution_id = str(active.get("execution_id", "")).strip()
            if not execution_id:
                return {
                    "merge": "incident",
                    "reconciled": False,
                    "merge_reason": "execution_identity_unavailable",
                }
            idempotency_key = f"startup-merge:{execution_id}:{quantity:f}"
            try:
                self._transition(
                    execution_id,
                    "merging",
                    {
                        "phase": "startup_merge_attempt",
                        "idempotency_key": idempotency_key,
                        "condition_id": intent.condition_id,
                        "quantity": quantity,
                    },
                )
            except Exception:
                return {
                    "merge": "incident",
                    "reconciled": False,
                    "merge_reason": "merge_attempt_persistence_failed",
                }
            merge = getattr(self._trading, "merge_once", None)
            try:
                result = _call(merge, condition_id=intent.condition_id, quantity=quantity)
            except Exception:
                result = {
                    "status": "blocked",
                    "confirmed": False,
                    "error_code": "merge_error",
                }
        merge_result = (
            self._safe_mapping(result)
            if isinstance(result, Mapping)
            else {"status": self._result_status(result)}
        )
        merge_evidence = {"phase": "merge_result", **merge_result}
        if not self._merge_confirmed(result):
            merge_evidence["confirmed"] = False
        if fresh_merge_attempt:
            try:
                self._transition(
                    str(active.get("execution_id", "")),
                    "merging",
                    merge_evidence,
                )
            except Exception:
                return {
                    "merge": "incident",
                    "reconciled": False,
                    "merge_result": merge_result,
                    "merge_reason": "merge_result_persistence_failed",
                }
        if not self._merge_confirmed(result):
            return {
                "merge": "incident",
                "reconciled": False,
                "merge_result": merge_result,
                "merge_reason": "merge_not_confirmed",
            }
        after = self._fresh_account_snapshot()
        if after is None:
            return {
                "merge": "incident",
                "reconciled": False,
                "merge_result": merge_result,
                "merge_reason": "post_merge_account_unavailable",
            }
        after_totals = self._position_totals(after, intent)
        before_balance = _decimal(before.get("p_usd_balance")) if before else None
        after_balance = _decimal(after.get("p_usd_balance"))
        if (
            after_totals["unknown"]
            or after_totals["yes"] != 0
            or after_totals["no"] != 0
            or before_balance is None
            or after_balance is None
            or after_balance <= before_balance
        ):
            return {
                "merge": "incident",
                "reconciled": False,
                "merge_result": merge_result,
                "merge_reason": "post_merge_state_unverified",
                "post_merge_checked_at": after.get("checked_at"),
                "post_merge_positions": after.get("positions", ()),
                "post_merge_p_usd_balance": after_balance,
            }
        return {
            "merge": "confirmed",
            "reconciled": True,
            "merge_result": merge_result,
            "post_merge_checked_at": after.get("checked_at"),
            "post_merge_positions": after.get("positions", ()),
            "post_merge_p_usd_balance": after_balance,
        }

    def _pending_merge(self, active: Mapping[str, object] | None) -> bool:
        if not active:
            return False
        state = str(active.get("state", ""))
        if state in {"merging", "merge_incident"}:
            return True
        evidence = active.get("evidence", ())
        if not isinstance(evidence, (list, tuple)):
            return False
        return any(
            isinstance(item, Mapping)
            and item.get("phase") in {"merge_result", "remediation_merge_result"}
            and item.get("confirmed") is not True
            for item in evidence
        )

    @staticmethod
    def _confirmed_merge_evidence(
        active: Mapping[str, object] | None,
    ) -> dict[str, object] | None:
        if not active:
            return None
        evidence = active.get("evidence", ())
        if not isinstance(evidence, (list, tuple)):
            return None
        for item in reversed(evidence):
            if not isinstance(item, Mapping):
                continue
            phase = item.get("phase")
            if phase in {"startup_merge_attempt", "remediation_merge_attempt"}:
                return None
            # A remediation merge still needs its post-merge account/balance
            # proof; a restart must not treat its transaction marker alone as
            # neutralized.  The startup recovery branch only auto-reconciles
            # the ordinary confirmed merge result.
            if phase != "merge_result":
                continue
            return dict(item) if PredictionExecutionService._merge_confirmed(item) else None
        return None

    def _pending_merge_for_incident(
        self,
        incident: Mapping[str, object],
        execution: Mapping[str, object] | None,
    ) -> bool:
        if str(incident.get("state", "")) == "merge_incident":
            return True
        if execution is None:
            return False
        return self._pending_merge(execution)

    def _fresh_remediation_options(
        self,
        intent: PairIntent,
        *,
        filled_leg: str,
        filled_quantity: Decimal,
        since: datetime,
    ) -> dict[str, object] | None:
        for name in ("remediation_options", "fresh_remediation_options", "incident_snapshot"):
            method = getattr(self._trading, name, None)
            if not callable(method):
                continue
            try:
                value = _call(
                    method,
                    condition_id=intent.condition_id,
                    yes_token_id=intent.yes_token_id,
                    no_token_id=intent.no_token_id,
                    filled_leg=filled_leg,
                    filled_quantity=filled_quantity,
                    since=since,
                )
            except Exception:
                return None
            if not isinstance(value, Mapping):
                return None
            if value.get("fresh") is False:
                return None
            if "fresh" not in value and "checked_at" not in value:
                return None
            if "checked_at" in value:
                age = _age_seconds(value.get("checked_at"))
                if age is None or age > float(BOOK_FRESHNESS_SECONDS):
                    return None
            return dict(value)
        # The authenticated account read is still mandatory when a test/dry-run
        # collaborator does not expose a richer order-book method.
        if self._fresh_account_snapshot() is None:
            return None
        return None

    @staticmethod
    def _option(options: Mapping[str, object], name: str) -> dict[str, object] | None:
        value = options.get(name)
        if isinstance(value, Mapping):
            return dict(value)
        for candidate in options.get("candidates", ()) if isinstance(options.get("candidates"), (list, tuple)) else ():
            if isinstance(candidate, Mapping) and str(candidate.get("kind", candidate.get("action", ""))) == name:
                return dict(candidate)
        return None

    @staticmethod
    def _option_loss(option: Mapping[str, object]) -> Decimal | None:
        value = option.get("loss", option.get("estimated_loss", option.get("expected_loss")))
        parsed = _decimal(value)
        return parsed if parsed is not None and parsed >= 0 else None

    def _choose_remediation(
        self,
        options: Mapping[str, object],
        *,
        filled_leg: str,
        filled_quantity: Decimal,
        intent: PairIntent,
        tick_size: Decimal,
    ) -> dict[str, object] | None:
        complete = self._option(options, "complete")
        unwind = self._option(options, "unwind")
        candidates: list[tuple[Decimal, int, dict[str, object]]] = []
        for priority, option in enumerate((complete, unwind)):
            if option is None:
                continue
            if option.get("executable") is False:
                continue
            loss = self._option_loss(option)
            if loss is None or loss > Decimal("2"):
                continue
            side = option.get("side")
            expected_side = "BUY" if option is complete else "SELL"
            if side != expected_side:
                continue
            expected_leg = "NO" if filled_leg == "YES" else "YES"
            if option.get("leg") != (expected_leg if option is complete else filled_leg):
                continue
            token_id = option.get("token_id")
            expected_token = (
                intent.no_token_id
                if option is complete
                else intent.yes_token_id
                if filled_leg == "YES"
                else intent.no_token_id
            )
            if token_id != expected_token:
                continue
            raw_quantity = option.get("quantity")
            if not isinstance(raw_quantity, Decimal) or not raw_quantity.is_finite():
                continue
            quantity = raw_quantity
            if quantity != filled_quantity:
                continue
            if option is complete:
                amount = option.get("amount")
                max_spend = option.get("max_spend")
                max_price = option.get("max_price")
                if (
                    not isinstance(amount, Decimal)
                    or not amount.is_finite()
                    or not isinstance(max_spend, Decimal)
                    or not max_spend.is_finite()
                    or not isinstance(max_price, Decimal)
                    or not max_price.is_finite()
                    or amount <= 0
                    or amount > Decimal("2")
                    or amount != max_spend
                    or max_price <= 0
                    or max_price > 1
                    or protected_buy_quantity(
                        spend=amount, price=max_price, tick_size=tick_size
                    )
                    != filled_quantity
                ):
                    continue
            else:
                shares = option.get("shares")
                if (
                    not isinstance(shares, Decimal)
                    or not shares.is_finite()
                    or shares <= 0
                    or shares != quantity
                ):
                    continue
                min_price = option.get("min_price")
                if (
                    not isinstance(min_price, Decimal)
                    or not min_price.is_finite()
                    or min_price <= 0
                    or min_price > 1
                ):
                    continue
            candidate = dict(option)
            candidate["loss"] = format(loss, "f")
            candidates.append((loss, priority, candidate))
        if not candidates:
            return None
        _, _, chosen = min(candidates, key=lambda item: (item[0], item[1]))
        return chosen

    def _handle_one_leg(
        self,
        execution_id: str,
        intent: PairIntent,
        *,
        filled_leg: str,
        filled_quantity: Decimal,
        yes: LegResult,
        no: LegResult,
        proof: Mapping[str, object],
        since: datetime,
        tick_size: Decimal,
        account: Mapping[str, object],
    ) -> None:
        # Open the breaker before any remediation-capable collaborator is called.
        self._breaker_open = True
        options = self._fresh_remediation_options(
            intent,
            filled_leg=filled_leg,
            filled_quantity=filled_quantity,
            since=since,
        )
        chosen = self._choose_remediation(
            options or {},
            filled_leg=filled_leg,
            filled_quantity=filled_quantity,
            intent=intent,
            tick_size=tick_size,
        )
        incident_id = self._record_incident(
            execution_id,
            "one_leg_fill",
            state="remediating" if chosen is not None else "directional_incident",
            evidence={
                "phase": "one_leg",
                "filled_leg": filled_leg,
                "filled_quantity": _safe_decimal(filled_quantity),
                "execution_proof": proof,
                "remediation_options": options or {},
            },
        )
        if chosen is None:
            self._finish_incident(
                execution_id,
                "no_safe_remediation",
                state="directional_incident",
                incident_id=incident_id,
                notify=False,
            )
            return
        self._transition(
            execution_id,
            "remediating",
            {"phase": "remediation_selected", "order": chosen},
        )
        submit = getattr(self._trading, "submit_remediation_once", None)
        try:
            result = _call(submit, chosen)
        except Exception:
            result = LegResult(
                str(chosen.get("leg", filled_leg)), False, "ambiguous", "", Decimal("0"), (), "ambiguous"
            )
        self._store.record_leg(execution_id, {**self._leg_payload(result), "label": "remediation"})
        self._transition(execution_id, "remediating", {"phase": "remediation_result", **self._leg_payload(result)})
        expected_quantity = _decimal(
            chosen.get("quantity", chosen.get("shares"))
        )
        if (
            not result.accepted
            or result.filled_quantity <= 0
            or expected_quantity is None
            or result.filled_quantity != expected_quantity
            or self._ambiguous(result)
        ):
            self._finish_incident(
                execution_id,
                "remediation_unverified",
                incident_id=incident_id,
                notify=False,
            )
            return
        if chosen.get("side") == "SELL":
            if self._verify_unwound(intent, filled_leg):
                self._finish_incident(
                    execution_id,
                    "one_leg_unwound",
                    state="neutralized_incident",
                    incident_id=incident_id,
                    notify=False,
                )
            else:
                self._finish_incident(
                    execution_id,
                    "unwind_unverified",
                    incident_id=incident_id,
                    notify=False,
                )
            return
        repaired_yes = yes if filled_leg == "YES" else result
        repaired_no = no if filled_leg == "NO" else result
        known = self._reconcile_until(
            intent,
            since=since,
            yes=repaired_yes,
            no=repaired_no,
        )
        if (
            known is None
            or known[0] <= 0
            or known[1] <= 0
            or known[0] != known[1]
            or not isinstance(known[2], Mapping)
            or known[2].get("verified") is not True
        ):
            self._finish_incident(
                execution_id,
                "remediation_reconciliation_unverified",
                incident_id=incident_id,
                notify=False,
            )
            return
        merge_idempotency_key = f"remediation-merge:{execution_id}:{known[0]:f}"
        try:
            self._transition(
                execution_id,
                "merging",
                {
                    "phase": "remediation_merge_attempt",
                    "idempotency_key": merge_idempotency_key,
                    "condition_id": intent.condition_id,
                    "quantity": known[0],
                },
            )
        except Exception:
            self._finish_incident(
                execution_id,
                "remediation_merge_attempt_persistence_failed",
                state="merge_incident",
                incident_id=incident_id,
                notify=False,
            )
            return
        merge = getattr(self._trading, "merge_once", None)
        try:
            merge_result = _call(
                merge, condition_id=intent.condition_id, quantity=known[0]
            )
        except Exception:
            merge_result = {"status": "blocked", "confirmed": False, "error_code": "merge_error"}
        merge_evidence = {
            "phase": "remediation_merge_result",
            **(
                self._safe_mapping(merge_result)
                if isinstance(merge_result, Mapping)
                else {"status": self._result_status(merge_result)}
            ),
        }
        if not self._merge_confirmed(merge_result):
            merge_evidence["confirmed"] = False
        try:
            self._transition(execution_id, "merging", merge_evidence)
        except Exception:
            self._finish_incident(
                execution_id,
                "remediation_merge_result_persistence_failed",
                state="merge_incident",
                incident_id=incident_id,
                notify=False,
            )
            return
        if not self._merge_confirmed(merge_result):
            self._finish_incident(
                execution_id,
                "remediation_merge_not_confirmed",
                state="merge_incident",
                incident_id=incident_id,
                notify=False,
            )
            return
        after = self._fresh_account_snapshot()
        after_totals = self._position_totals(after, intent) if after is not None else None
        before_balance = _decimal(account.get("p_usd_balance"))
        after_balance = _decimal(after.get("p_usd_balance")) if after else None
        if (
            after is None
            or after_totals is None
            or after_totals["unknown"]
            or after_totals["yes"] != 0
            or after_totals["no"] != 0
            or before_balance is None
            or after_balance is None
            or after_balance <= before_balance
        ):
            self._finish_incident(
                execution_id,
                "remediation_merge_state_unverified",
                state="merge_incident",
                incident_id=incident_id,
                notify=False,
            )
            return
        self._finish_incident(
            execution_id,
            "one_leg_neutralized",
            state="neutralized_incident",
            incident_id=incident_id,
            notify=False,
        )

    def _real_live_success(
        self, proof: Mapping[str, object], merge_result: object
    ) -> bool:
        return (
            isinstance(self._trading, PolymarketTradingClient)
            and proof.get("adapter_verified") is True
            and proof.get("venue") == "polymarket"
            and isinstance(proof.get("matched_refs"), Mapping)
            and isinstance(merge_result, Mapping)
            and merge_result.get("adapter_confirmed") is True
            and PredictionExecutionService._merge_confirmed(merge_result)
        )

    def _verify_unwound(self, intent: PairIntent, filled_leg: str) -> bool:
        method = getattr(self._trading, "reconcile_neutralization", None)
        collaborator_verified = True
        if callable(method):
            try:
                value = _call(
                    method,
                    condition_id=intent.condition_id,
                    token_id=intent.yes_token_id if filled_leg == "YES" else intent.no_token_id,
                    expected_quantity=Decimal("0"),
                )
                if isinstance(value, Mapping):
                    collaborator_verified = value.get("verified") is True or value.get("directional_imbalance") in (False, Decimal("0"), "0")
                else:
                    collaborator_verified = False
            except Exception:
                return False
        snapshot = self._fresh_account_snapshot()
        if snapshot is None:
            return False
        totals = self._position_totals(snapshot, intent)
        return collaborator_verified and totals["yes"] == 0 and totals["no"] == 0 and not totals["unknown"]

    def _relayer_ready(self, *, require_merge: bool = True) -> bool:
        method = getattr(self._trading, "readiness_snapshot", None)
        if callable(method):
            try:
                value = _call(method)
            except Exception:
                return False
            if isinstance(value, Mapping):
                if "checked_at" in value:
                    checked_age = _age_seconds(value.get("checked_at"))
                    if checked_age is None or checked_age > 60:
                        return False
                else:
                    return False
                relayer = next(
                    (
                        value[key]
                        for key in ("relayer_ready", "relayer", "relayer_readiness")
                        if key in value
                    ),
                    None,
                )
                merge = next(
                    (
                        value[key]
                        for key in ("merge_ready", "merge", "merge_capability")
                        if key in value
                    ),
                    None,
                )
                accepted = (True, "ready", "allowed", "pass", "confirmed")
                return relayer in accepted and (not require_merge or merge in accepted)
        for name in ("relayer_ready", "relayer_readiness"):
            value = getattr(self._trading, name, None)
            if callable(value):
                try:
                    value = _call(value)
                except Exception:
                    return False
            if isinstance(value, Mapping):
                checked_age = _age_seconds(value.get("checked_at"))
                if checked_age is None or checked_age > 60:
                    return False
                relayer = value.get("ready", value.get("relayer_ready", value.get("relayer")))
                merge = value.get("merge_ready", value.get("merge"))
                accepted = (True, "ready", "allowed", "pass", "confirmed")
                return relayer in accepted and (not require_merge or merge in accepted)
            return False
        return False

    def _validate_opportunity(
        self,
        opportunity: Mapping[str, object],
        intent: ExecutionIntent,
        *,
        auto_submit: bool = False,
    ) -> str | None:
        if isinstance(intent, CrossVenueIntent):
            return self._validate_cross_venue_opportunity(
                opportunity, intent, auto_submit=auto_submit
            )
        if opportunity.get("actionable") is not True:
            return str(opportunity.get("eligibility_reason", "opportunity_not_actionable"))
        if "confirmed_age_seconds" not in opportunity or "confirmed_at" not in opportunity:
            return "book_freshness_unavailable"
        age = _decimal(opportunity.get("confirmed_age_seconds"))
        timestamp_age = _age_seconds(opportunity.get("confirmed_at"))
        if age is None or timestamp_age is None:
            return "book_freshness_invalid"
        if age > BOOK_FRESHNESS_SECONDS or timestamp_age > float(BOOK_FRESHNESS_SECONDS):
            return "books_stale"
        if isinstance(intent, ThresholdHedgeIntent):
            if opportunity.get("intent_type", "threshold_hedge") != "threshold_hedge":
                return "intent_type_mismatch"
            if opportunity.get("rules_hash_a") in (None, "") or opportunity.get("rules_hash_b") in (None, ""):
                return "rule_hash_unavailable"
            if opportunity.get("cache_key") in (None, ""):
                return "cache_fingerprint_unavailable"
            if intent.leg_a.condition_id == intent.leg_b.condition_id:
                return "condition_identity"
            if intent.leg_a.token_id == intent.leg_b.token_id:
                return "token_identity"
            if intent.leg_a.quantity != intent.quantity or intent.leg_b.quantity != intent.quantity:
                return "order_amount_mismatch"
            if intent.total_max_cost > MAX_NORMAL_COST or intent.minimum_profit <= 0 or intent.net_edge <= 0:
                return "threshold_economics"
            annualized = _decimal(opportunity.get("annualized_yield"))
            if annualized is None:
                return "annualized_yield_unavailable"
            if annualized < MIN_THRESHOLD_ANNUALIZED_YIELD:
                return "annualized_yield_below_minimum"
            return None
        tick_size = self._tick_size(opportunity)
        if tick_size is None:
            return "tick_size_unavailable"
        if any(
            value <= 0
            for value in (
                intent.quantity,
                intent.yes_max_price,
                intent.no_max_price,
                intent.yes_max_cost,
                intent.no_max_cost,
                intent.total_max_cost,
            )
        ):
            return "invalid_intent"
        if intent.yes_max_price > 1 or intent.no_max_price > 1:
            return "price_limit"
        if intent.total_max_cost > Decimal("20"):
            return "normal_cost_limit"
        if intent.minimum_profit < Decimal("1"):
            return "minimum_profit"
        if intent.net_edge < Decimal("0.01"):
            return "minimum_edge"
        if intent.yes_token_id == intent.no_token_id:
            return "token_identity"
        if intent.yes_max_cost + intent.no_max_cost != intent.total_max_cost:
            return "cost_mismatch"
        return None

    def _validate_cross_venue_opportunity(
        self,
        opportunity: Mapping[str, object],
        intent: CrossVenueIntent,
        *,
        auto_submit: bool = False,
    ) -> str | None:
        mode = opportunity.get("execution_mode")
        if auto_submit:
            if opportunity.get("manual_only") is True:
                return "manual_only_requires_approval"
            if mode != "auto_submit":
                return "cross_execution_mode"
        elif mode != "manual_confirm":
            return "cross_execution_mode"
        manual_only = opportunity.get("manual_only") is True
        if manual_only and (
            intent.manual_only is not True
            or not str(opportunity.get("manual_reason", "")).strip()
        ):
            return "manual_only_unavailable"
        if not manual_only and intent.manual_only is True:
            return "manual_only_mismatch"
        if (
            opportunity.get("market_type") != "cross_venue_yes_no"
            or opportunity.get("funnel_stage") != 5
            or opportunity.get("actionable") is not True
            or opportunity.get("clear_signal") is not True
            or intent.actionable is not True
            or intent.quote_available is not True
        ):
            return "opportunity_not_actionable"
        age = _decimal(opportunity.get("confirmed_age_seconds"))
        if age is None or age > BOOK_FRESHNESS_SECONDS or _age_seconds(opportunity.get("confirmed_at")) is None:
            return "books_stale"
        if len(intent.legs) != 2 or tuple(leg.exchange for leg in intent.legs) != ("predict.fun", "polymarket"):
            return "cross_venue_identity"
        if any(
            _age_seconds(leg.book_timestamp) is None
            or _age_seconds(leg.book_timestamp) > float(BOOK_FRESHNESS_SECONDS)
            for leg in intent.legs
        ):
            return "books_stale"
        if intent.quantity <= 0 or any(leg.net_quantity != intent.quantity or leg.net_quantity <= 0 for leg in intent.legs):
            return "order_amount_mismatch"
        if {leg.outcome for leg in intent.legs} != {"YES", "NO"}:
            return "outcome_identity"
        if any(
            not all(value.is_finite() and value > 0 for value in (leg.max_price, leg.max_cost, leg.maximum_fee))
            for leg in intent.legs
        ) or not all(value.is_finite() and value > 0 for value in (intent.total_max_cost, intent.maximum_fee, intent.minimum_payout)):
            return "invalid_intent"
        if not intent.calculable_gas.is_finite() or intent.calculable_gas <= 0:
            return "cross_venue_economics"
        if (
            sum((leg.max_cost for leg in intent.legs), intent.calculable_gas)
            != intent.total_max_cost
            or not intent.minimum_profit.is_finite()
            or intent.minimum_payout - intent.total_max_cost != intent.minimum_profit
        ):
            return "cost_mismatch"
        if intent.total_max_cost > MAX_NORMAL_COST or intent.minimum_profit <= 0:
            return "cross_venue_economics"
        if intent.annualized_yield is None or not intent.annualized_yield.is_finite() or intent.annualized_yield < MIN_THRESHOLD_ANNUALIZED_YIELD:
            return "annualized_yield_below_minimum"
        cutoff = intent.canonical_cutoff
        if not self._cross_canonical_cutoff_matches(opportunity, intent):
            return "canonical_cutoff_invalid"
        if cutoff is None or not canonical_cutoff_is_future(cutoff):
            return "canonical_cutoff_invalid"
        fingerprints = opportunity.get("rules_fingerprints")
        if not isinstance(fingerprints, Mapping) or not all(str(fingerprints.get(exchange, "")).strip() for exchange in ("predict.fun", "polymarket")):
            return "rules_fingerprint_unavailable"
        if not manual_only:
            approval = opportunity.get("codex_approval")
            if not isinstance(approval, Mapping) or approval.get("decision") != "APPROVE" or not str(approval.get("cache_key", "")).strip():
                return "codex_not_approved"
            direct = approval.get("direct_outcome_mapping")
            evidence = approval.get("evidence")
            if direct != {"predict_yes": "YES", "predict_no": "NO", "polymarket_yes": "YES", "polymarket_no": "NO"} or not isinstance(evidence, (list, tuple)) or not evidence:
                return "codex_evidence_unavailable"
        if not self._cross_venue_identity_matches(
            opportunity, intent, fingerprints
        ):
            return "cross_venue_identity"
        return None

    @staticmethod
    def _cross_canonical_cutoff_matches(
        opportunity: Mapping[str, object], intent: CrossVenueIntent
    ) -> bool:
        cutoff = intent.canonical_cutoff
        if cutoff is None or cutoff.tzinfo is not UTC:
            return False
        raw = opportunity.get("canonical_cutoff")
        if isinstance(raw, datetime):
            return raw.tzinfo is UTC and raw == cutoff
        parsed = parse_canonical_cutoff(raw)
        return parsed is not None and parsed == cutoff

    @staticmethod
    def _cross_venue_identity_matches(
        opportunity: Mapping[str, object],
        intent: CrossVenueIntent,
        fingerprints: Mapping[str, object],
    ) -> bool:
        expected_outcomes = {
            "PREDICT_YES_POLYMARKET_NO": {
                "predict.fun": "YES", "polymarket": "NO"
            },
            "POLYMARKET_YES_PREDICT_NO": {
                "predict.fun": "NO", "polymarket": "YES"
            },
        }.get(intent.direction)
        candidates = opportunity.get("approved_candidates")
        if (
            expected_outcomes is None
            or opportunity.get("direction") != intent.direction
            or not isinstance(candidates, Mapping)
        ):
            return False
        legs = {leg.exchange: leg for leg in intent.legs}
        for exchange in ("predict.fun", "polymarket"):
            candidate = candidates.get(exchange)
            leg = legs.get(exchange)
            outcome = expected_outcomes[exchange]
            if not isinstance(candidate, Mapping) or leg is None:
                return False
            token_key = "yes_token_id" if outcome == "YES" else "no_token_id"
            if (
                leg.outcome != outcome
                or leg.market_id != candidate.get("market_id")
                or leg.condition_id != candidate.get("condition_id")
                or leg.token_id != candidate.get(token_key)
                or candidate.get("rules_fingerprint") != fingerprints.get(exchange)
            ):
                return False
        return True

    @staticmethod
    def _tick_size(opportunity: Mapping[str, object]) -> Decimal | None:
        value = _decimal(opportunity.get("tick_size"))
        return value if value in PROTECTED_BUY_SHARE_PRECISION else None

    def _preview_payload(
        self,
        opportunity: Mapping[str, object],
        intent: ExecutionIntent,
        *,
        account: Mapping[str, object],
        expires_at: datetime,
    ) -> dict[str, object]:
        if isinstance(intent, CrossVenueIntent):
            current = self._store.cross_unsettled_principal()
            approval = opportunity.get("codex_approval")
            return {
                "execution_id": uuid.uuid4().hex,
                "opportunity_id": str(opportunity.get("opportunity_id", "")),
                "market_type": "cross_venue_yes_no",
                "signal_episode_id": str(opportunity.get("signal_episode_id", "")),
                "intent_type": "cross_venue",
                "pair_id": intent.pair_id,
                "direction": intent.direction,
                "question": str(opportunity.get("question", "")),
                "intent": self._intent_payload(intent),
                "buy_legs": [self._cross_venue_leg_payload(leg) for leg in intent.legs],
                "net_quantity": format(intent.quantity, "f"),
                "total_max_cost": format(intent.total_max_cost, "f"),
                "maximum_total_cost": format(intent.total_max_cost, "f"),
                "minimum_payout": format(intent.minimum_payout, "f"),
                "minimum_profit": format(intent.minimum_profit, "f"),
                "annualized_yield": format(intent.annualized_yield or Decimal("0"), "f"),
                "canonical_cutoff": _timestamp(intent.canonical_cutoff),
                "codex_approval": self._safe_mapping(approval) if isinstance(approval, Mapping) else {},
                "rules_fingerprints": self._safe_mapping(opportunity.get("rules_fingerprints")) if isinstance(opportunity.get("rules_fingerprints"), Mapping) else {},
                "approved_candidates": self._safe_mapping(opportunity.get("approved_candidates")) if isinstance(opportunity.get("approved_candidates"), Mapping) else {},
                "balances": self._safe_mapping(account),
                "unsettled": {
                    "current": format(current, "f"),
                    "after": format(current + intent.total_max_cost, "f"),
                    "limit": format(MAX_CROSS_UNSETTLED_PRINCIPAL, "f"),
                },
                "policy_limits": {
                    "max_normal_cost": format(
                        _decimal(account.get("canary_cap")) or Decimal("5"), "f"
                    ),
                    "max_emergency_loss": "2",
                },
            }
        payload: dict[str, object] = {
            "opportunity_id": str(
                opportunity.get("opportunity_id", opportunity.get("id", ""))
            ),
            "event_id": intent.event_id,
            "market_id": getattr(intent, "market_id", getattr(intent, "relation_id", "")),
            "condition_id": getattr(intent, "condition_id", ""),
            "question": str(opportunity.get("question", "")),
            "market_type": str(opportunity.get("market_type", "")),
            "intent_type": "threshold_hedge" if isinstance(intent, ThresholdHedgeIntent) else "pair",
            "fee_status": str(opportunity.get("fee_status", "")),
            "volume_24h": _safe_decimal(opportunity.get("volume_24h")) or "0",
            "intent": self._intent_payload(intent),
            "quantity": format(intent.quantity, "f"),
            "total_max_cost": format(intent.total_max_cost, "f"),
            "minimum_profit": format(intent.minimum_profit, "f"),
            "net_edge": format(intent.net_edge, "f"),
            "wallet_address": str(account.get("wallet_address", "")),
            "available_balance": _safe_decimal(account.get("p_usd_balance")),
            "policy_limits": {
                "max_wallet_balance": format(MAX_WALLET_BALANCE, "f"),
                "max_normal_cost": format(MAX_NORMAL_COST, "f"),
                "max_emergency_loss": format(MAX_EMERGENCY_LOSS, "f"),
                "min_estimated_profit": format(MIN_ESTIMATED_PROFIT, "f"),
            },
            "expires_at": _timestamp(expires_at),
        }
        if isinstance(intent, PairIntent):
            payload.update(
                {
                    "condition_id": intent.condition_id,
                    "tick_size": _safe_decimal(self._tick_size(opportunity)) or "",
                    "yes_max_price": format(intent.yes_max_price, "f"),
                    "no_max_price": format(intent.no_max_price, "f"),
                    "yes_max_cost": format(intent.yes_max_cost, "f"),
                    "no_max_cost": format(intent.no_max_cost, "f"),
                    "merge_value": format(intent.quantity, "f"),
                }
            )
        else:
            payload.update(
                {
                    "relation_id": intent.relation_id,
                    "relation": intent.relation,
                    "question_a": str(opportunity.get("question_a", "")),
                    "question_b": str(opportunity.get("question_b", "")),
                    "llm_status": str(opportunity.get("llm_status", "")),
                    "llm_decision": str(opportunity.get("llm_decision", "")),
                    "llm_summary": str(opportunity.get("llm_summary", "")),
                    "rules_hash_a": opportunity.get("rules_hash_a", ""),
                    "rules_hash_b": opportunity.get("rules_hash_b", ""),
                    "cache_key": opportunity.get("cache_key", ""),
                    "condition_id_a": intent.leg_a.condition_id,
                    "condition_id_b": intent.leg_b.condition_id,
                    "buy_legs": [
                        self._threshold_intent_leg_payload(intent.leg_a),
                        self._threshold_intent_leg_payload(intent.leg_b),
                    ],
                    "maximum_fee": format(intent.maximum_fee, "f"),
                    "minimum_payout": format(intent.minimum_payout, "f"),
                    "merge_value": "not_applicable",
                }
            )
        return payload

    @staticmethod
    def _threshold_intent_leg_payload(leg: ThresholdHedgeLeg) -> dict[str, object]:
        return {
            "label": leg.label,
            "condition_id": leg.condition_id,
            "market_id": leg.market_id,
            "outcome": leg.outcome,
            "token_id": leg.token_id,
            "quantity": format(leg.quantity, "f"),
            "max_price": format(leg.max_price, "f"),
            "max_cost": format(leg.max_cost, "f"),
            "tick_size": format(leg.tick_size, "f"),
        }

    @staticmethod
    def _cross_venue_leg_payload(leg: CrossVenueLeg) -> dict[str, object]:
        return {
            "exchange": leg.exchange, "market_id": leg.market_id,
            "condition_id": leg.condition_id, "outcome": leg.outcome,
            "token_id": leg.token_id, "settlement_asset": leg.settlement_asset,
            "requested_quantity": format(leg.requested_quantity, "f"),
            "net_quantity": format(leg.net_quantity, "f"),
            "max_price": format(leg.max_price, "f"), "max_cost": format(leg.max_cost, "f"),
            "maximum_fee": format(leg.maximum_fee, "f"), "fee_asset": leg.fee_asset,
            "minimum_order_size": format(leg.minimum_order_size, "f"),
            "book_timestamp": _timestamp(leg.book_timestamp),
            "settlement_at": _timestamp(leg.settlement_at) if leg.settlement_at else None,
        }

    @staticmethod
    def _intent_payload(intent: ExecutionIntent) -> dict[str, object]:
        if isinstance(intent, CrossVenueIntent):
            return {
                "intent_type": "cross_venue", "pair_id": intent.pair_id,
                "direction": intent.direction,
                "legs": [PredictionExecutionService._cross_venue_leg_payload(leg) for leg in intent.legs],
                "quantity": format(intent.quantity, "f"),
                "calculable_gas": format(intent.calculable_gas, "f"),
                "total_max_cost": format(intent.total_max_cost, "f"),
                "maximum_fee": format(intent.maximum_fee, "f"),
                "minimum_payout": format(intent.minimum_payout, "f"),
                "minimum_profit": format(intent.minimum_profit, "f"),
                "annualized_yield": format(intent.annualized_yield, "f") if intent.annualized_yield is not None else None,
                "canonical_cutoff": _timestamp(intent.canonical_cutoff) if intent.canonical_cutoff else None,
                "resolution_at": _timestamp(intent.resolution_at) if intent.resolution_at else None,
                "actionable": intent.actionable, "quote_available": intent.quote_available,
            }
        if isinstance(intent, ThresholdHedgeIntent):
            return {
                "intent_type": "threshold_hedge",
                "relation_id": intent.relation_id,
                "event_id": intent.event_id,
                "relation": intent.relation,
                "leg_a": PredictionExecutionService._threshold_intent_leg_payload(intent.leg_a),
                "leg_b": PredictionExecutionService._threshold_intent_leg_payload(intent.leg_b),
                "quantity": format(intent.quantity, "f"),
                "maximum_fee": format(intent.maximum_fee, "f"),
                "total_max_cost": format(intent.total_max_cost, "f"),
                "minimum_payout": format(intent.minimum_payout, "f"),
                "minimum_profit": format(intent.minimum_profit, "f"),
                "net_edge": format(intent.net_edge, "f"),
            }
        result: dict[str, object] = {}
        for field in fields(PairIntent):
            value = getattr(intent, field.name)
            result[field.name] = format(value, "f") if isinstance(value, Decimal) else value
        return result

    def _intent_from_opportunity(self, opportunity: Mapping[str, object] | None) -> ExecutionIntent | None:
        if opportunity is None:
            return None
        value = opportunity.get("intent")
        intent = self._intent_from_payload(value)
        if intent is not None:
            return intent
        return self._intent_from_payload(opportunity)

    @staticmethod
    def _intent_from_payload(value: object) -> ExecutionIntent | None:
        if isinstance(value, CrossVenueIntent):
            return value
        if isinstance(value, ThresholdHedgeIntent):
            return value
        if isinstance(value, PairIntent):
            return value
        if not isinstance(value, Mapping):
            return None
        if value.get("intent_type") == "cross_venue":
            raw_legs = value.get("legs")
            if not isinstance(raw_legs, (list, tuple)) or len(raw_legs) != 2:
                return None
            legs = tuple(PredictionExecutionService._cross_venue_leg_from_payload(item) for item in raw_legs)
            if any(leg is None for leg in legs):
                return None
            decimal_names = ("quantity", "calculable_gas", "total_max_cost", "maximum_fee", "minimum_payout", "minimum_profit")
            raw = {name: _decimal(value.get(name)) for name in decimal_names}
            annualized = None if value.get("annualized_yield") is None else _decimal(value.get("annualized_yield"))
            cutoff = parse_canonical_cutoff(value.get("canonical_cutoff"))
            resolution = PredictionExecutionService._datetime_from_payload(value.get("resolution_at"))
            if any(item is None for item in raw.values()) or (value.get("annualized_yield") is not None and annualized is None) or cutoff is None or resolution is None:
                return None
            if not isinstance(value.get("pair_id"), str) or not isinstance(value.get("direction"), str) or value.get("actionable") is not True or value.get("quote_available") is not True:
                return None
            try:
                return CrossVenueIntent(pair_id=value["pair_id"], direction=value["direction"], legs=legs, annualized_yield=annualized, canonical_cutoff=cutoff, resolution_at=resolution, actionable=True, quote_available=True, manual_only=value.get("manual_only") is True, **raw)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return None
        if value.get("intent_type") == "threshold_hedge" or {
            "relation_id", "leg_a", "leg_b", "maximum_fee", "minimum_payout"
        } <= set(value):
            leg_a = PredictionExecutionService._threshold_leg_from_payload(value.get("leg_a"), "A")
            leg_b = PredictionExecutionService._threshold_leg_from_payload(value.get("leg_b"), "B")
            if leg_a is None or leg_b is None:
                return None
            decimal_names = {"quantity", "maximum_fee", "total_max_cost", "minimum_payout", "minimum_profit", "net_edge"}
            raw = {name: _decimal(value.get(name)) for name in decimal_names}
            if any(item is None for item in raw.values()):
                return None
            if not isinstance(value.get("relation_id"), str) or not isinstance(value.get("event_id"), str):
                return None
            try:
                return ThresholdHedgeIntent(
                    relation_id=value["relation_id"], event_id=value["event_id"], relation=value.get("relation"),
                    leg_a=leg_a, leg_b=leg_b, **raw  # type: ignore[arg-type]
                )
            except (TypeError, ValueError):
                return None
        names = {field.name for field in fields(PairIntent)}
        if not names <= set(value):
            return None
        raw = {name: value[name] for name in names}
        for name in names - {"event_id", "market_id", "condition_id", "yes_token_id", "no_token_id"}:
            parsed = _decimal(raw[name])
            if parsed is None:
                return None
            raw[name] = parsed
        if not all(isinstance(raw[name], str) and raw[name].strip() for name in names & {
            "event_id", "market_id", "condition_id", "yes_token_id", "no_token_id"
        }):
            return None
        try:
            return PairIntent(**raw)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _threshold_leg_from_payload(value: object, label: str) -> ThresholdHedgeLeg | None:
        if isinstance(value, ThresholdHedgeLeg):
            return value
        if not isinstance(value, Mapping):
            return None
        if value.get("label", label) != label:
            return None
        names = ("condition_id", "market_id", "outcome", "token_id")
        if not all(isinstance(value.get(name), str) and value.get(name).strip() for name in names):
            return None
        decimal_names = ("quantity", "max_price", "max_cost", "tick_size")
        parsed = {name: _decimal(value.get(name)) for name in decimal_names}
        if any(item is None for item in parsed.values()):
            return None
        try:
            return ThresholdHedgeLeg(label=label, **{name: value[name] for name in names}, **parsed)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _datetime_from_payload(value: object) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else None

    @staticmethod
    def _cross_venue_leg_from_payload(value: object) -> CrossVenueLeg | None:
        if isinstance(value, CrossVenueLeg):
            return value
        if not isinstance(value, Mapping) or value.get("exchange") not in {"predict.fun", "polymarket"} or value.get("outcome") not in {"YES", "NO"}:
            return None
        names = ("market_id", "condition_id", "token_id", "settlement_asset", "fee_asset")
        if not all(isinstance(value.get(name), str) and value[name].strip() for name in names):
            return None
        decimals = {name: _decimal(value.get(name)) for name in ("requested_quantity", "net_quantity", "max_price", "max_cost", "maximum_fee")}
        minimum_order_size = _decimal(value.get("minimum_order_size", "0"))
        book_timestamp = PredictionExecutionService._datetime_from_payload(value.get("book_timestamp"))
        settlement_at = None if value.get("settlement_at") is None else PredictionExecutionService._datetime_from_payload(value.get("settlement_at"))
        if any(item is None for item in decimals.values()) or minimum_order_size is None or minimum_order_size < 0 or book_timestamp is None or (value.get("settlement_at") is not None and settlement_at is None):
            return None
        try:
            return CrossVenueLeg(exchange=value["exchange"], outcome=value["outcome"], book_timestamp=book_timestamp, settlement_at=settlement_at, minimum_order_size=minimum_order_size, **{name: value[name] for name in names}, **decimals)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    def _execution_for_idempotency(self, key: str) -> dict[str, object] | None:
        if not key.strip():
            return None
        for row in self._store.histories("executions"):
            if str(row.get("idempotency_key", "")) == key:
                return self._decorate_execution(row)
        return None

    @staticmethod
    def _decorate_execution(row: Mapping[str, object]) -> dict[str, object]:
        result = dict(row)
        result.setdefault("id", result.get("execution_id", ""))
        return result

    def _transition(
        self, execution_id: str, state: str, evidence: Mapping[str, object]
    ) -> None:
        self._store.transition_execution(
            execution_id, state=state, evidence=self._safe_mapping(evidence)
        )

    @staticmethod
    def _safe_mapping(value: Mapping[str, object]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in value.items():
            if isinstance(item, Decimal):
                result[key] = format(item, "f")
            elif isinstance(item, Mapping):
                result[key] = PredictionExecutionService._safe_mapping(item)
            elif isinstance(item, (list, tuple)):
                result[key] = [
                    PredictionExecutionService._safe_mapping(value)
                    if isinstance(value, Mapping)
                    else format(value, "f")
                    if isinstance(value, Decimal)
                    else value
                    if isinstance(value, (str, int, bool)) or value is None
                    else str(value)
                    for value in item
                ]
            elif isinstance(item, (str, int, bool)) or item is None:
                result[key] = item
            else:
                result[key] = str(item)
        return result

    @staticmethod
    def _preflight_passed(value: object) -> bool:
        if not isinstance(value, Mapping):
            return value is True or getattr(value, "accepted", False) is True
        return value.get("result") in ("PASS", "pass", True)

    @staticmethod
    def _preflight_error_code(value: object) -> str:
        code = (
            value.get("error_code")
            if isinstance(value, Mapping)
            else getattr(value, "error_code", "")
        )
        return str(code).strip() if code else ""

    _PREFLIGHT_EVIDENCE_KEYS = (
        "signer_match",
        "wallet_match",
        "account_reads",
        "geoblock",
        "fok_pair_signed_not_submitted",
        "equal_requested_shares",
        "conditions",
        "merge",
        "error_code",
        "result",
        "posted",
    )

    @classmethod
    def _preflight_failure_evidence(
        cls, result: object, *, exception: BaseException | None = None
    ) -> dict[str, object]:
        if isinstance(result, Mapping):
            evidence: dict[str, object] = {}
            for key in cls._PREFLIGHT_EVIDENCE_KEYS:
                if key in result:
                    evidence[key] = result[key]
            return cls._safe_mapping(evidence)
        code = cls._preflight_exception_code(exception)
        return {
            "error_code": code or "preflight_failed",
            "preflight_exception": True,
        }

    @staticmethod
    def _preflight_exception_code(exception: BaseException | None) -> str:
        if exception is None:
            return ""
        code = getattr(exception, "error_code", "")
        if isinstance(code, str) and code.strip():
            return code.strip()
        return ""

    @staticmethod
    def _result_status(value: object) -> str:
        if isinstance(value, Mapping):
            return str(value.get("status", "blocked")).strip().lower()
        return str(getattr(value, "status", "blocked")).strip().lower()

    @staticmethod
    def _merge_confirmed(value: object) -> bool:
        if not isinstance(value, Mapping):
            return False
        if str(value.get("status", "")).strip().lower() != "confirmed":
            return False
        if value.get("confirmed") is not True:
            return False
        transaction_hash = value.get("transaction_hash", value.get("tx_hash"))
        if not isinstance(transaction_hash, str) or not transaction_hash.strip():
            return False
        transaction_id = value.get("transaction_id")
        if transaction_id is not None and (
            not isinstance(transaction_id, str) or not transaction_id.strip()
        ):
            return False
        return True

    @staticmethod
    def _ambiguous_submission() -> PairSubmission:
        return PairSubmission(
            yes=LegResult("YES", False, "ambiguous", "", Decimal("0"), (), "ambiguous"),
            no=LegResult("NO", False, "ambiguous", "", Decimal("0"), (), "ambiguous"),
        )

    @staticmethod
    def _submission_legs(value: object) -> tuple[LegResult, LegResult]:
        if isinstance(value, PairSubmission):
            return value.yes, value.no
        if isinstance(value, Mapping):
            return (
                PredictionExecutionService._coerce_leg(value.get("yes"), "YES"),
                PredictionExecutionService._coerce_leg(value.get("no"), "NO"),
            )
        return PredictionExecutionService._ambiguous_submission().yes, PredictionExecutionService._ambiguous_submission().no

    @staticmethod
    def _coerce_leg(value: object, label: str) -> LegResult:
        if isinstance(value, LegResult):
            return value
        if isinstance(value, Mapping):
            filled = _decimal(value.get("filled_quantity", value.get("quantity", 0))) or Decimal("0")
            return LegResult(
                label, value.get("accepted") is True, str(value.get("status", "rejected")),
                str(value.get("order_id", "")), filled,
                tuple(item for item in value.get("trade_ids", ()) if isinstance(item, str)),
                str(value.get("error_code", "none")),
            )
        return LegResult(label, False, "ambiguous", "", Decimal("0"), (), "ambiguous")

    @staticmethod
    def _leg_payload(leg: LegResult) -> dict[str, object]:
        return {
            "label": leg.leg,
            "accepted": leg.accepted,
            "status": leg.status,
            "order_id": leg.order_id,
            "filled_quantity": format(leg.filled_quantity, "f"),
            "trade_ids": list(leg.trade_ids),
            "error_code": leg.error_code,
        }

    @staticmethod
    def _both_rejected(yes: LegResult, no: LegResult) -> bool:
        return (
            not yes.accepted
            and not no.accepted
            and yes.filled_quantity <= 0
            and no.filled_quantity <= 0
            and not PredictionExecutionService._ambiguous(yes)
            and not PredictionExecutionService._ambiguous(no)
        )

    @staticmethod
    def _ambiguous(leg: LegResult) -> bool:
        return leg.error_code == "ambiguous" or leg.status.lower() in {
            "ambiguous", "pending", "delayed", "processing"
        }

    def _reconcile_until(
        self,
        intent: PairIntent,
        *,
        since: datetime,
        yes: LegResult,
        no: LegResult,
    ) -> tuple[Decimal, Decimal, dict[str, object]] | None:
        started = self._clock()
        for attempt in range(MAX_RECONCILIATION_SECONDS + 1):
            reconcile = getattr(self._trading, "reconcile", None)
            try:
                value = _call(
                    reconcile,
                    condition_id=intent.condition_id,
                    since=since,
                    yes_token_id=intent.yes_token_id,
                    no_token_id=intent.no_token_id,
                    yes_order_id=yes.order_id,
                    no_order_id=no.order_id,
                    yes_trade_ids=yes.trade_ids,
                    no_trade_ids=no.trade_ids,
                )
            except Exception:
                value = None
            quantities = self._reconciled_quantities(
                value, intent=intent, yes=yes, no=no
            )
            if quantities is not None and (quantities[0] > 0 or quantities[1] > 0):
                return quantities
            if attempt >= MAX_RECONCILIATION_SECONDS:
                break
            if self._clock() - started >= MAX_RECONCILIATION_SECONDS:
                break
            self._sleep(1)
        return None

    @staticmethod
    def _reconciled_quantities(
        value: object,
        *,
        intent: PairIntent,
        yes: LegResult,
        no: LegResult,
    ) -> tuple[Decimal, Decimal, dict[str, object]] | None:
        if not isinstance(value, Mapping):
            return None
        if str(value.get("status", "")).lower() not in {
            "ok", "partial", "confirmed", "filled", "complete"
        }:
            return None
        proof = value.get("execution_proof")
        if not isinstance(proof, Mapping) or not (
            proof.get("verified") is True or proof.get("partial_verified") is True
        ):
            return None
        if proof.get("venue") != "polymarket" or proof.get("positions_verified") is not True:
            return None
        matched_refs = proof.get("matched_refs")
        position_refs = proof.get("position_refs")
        if not isinstance(matched_refs, Mapping) or not isinstance(position_refs, Mapping):
            return None
        yes_quantity = next(
            (
                _decimal(value.get(key))
                for key in ("yes_quantity", "yes_filled_quantity", "actual_yes", "yes_shares")
                if _decimal(value.get(key)) is not None
            ),
            None,
        )
        no_quantity = next(
            (
                _decimal(value.get(key))
                for key in ("no_quantity", "no_filled_quantity", "actual_no", "no_shares")
                if _decimal(value.get(key)) is not None
            ),
            None,
        )
        if yes_quantity is None or no_quantity is None:
            return None
        for label, token_id, leg, quantity in (
            ("YES", intent.yes_token_id, yes, yes_quantity),
            ("NO", intent.no_token_id, no, no_quantity),
        ):
            refs = matched_refs.get(label)
            positions = position_refs.get(label)
            if quantity <= 0:
                continue
            if not isinstance(refs, Mapping) or not isinstance(positions, Mapping):
                return None
            if refs.get("token_id") != token_id or positions.get("token_id") != token_id:
                return None
            order_ids = refs.get("order_ids")
            trade_ids = refs.get("trade_ids")
            if not isinstance(order_ids, (list, tuple)) or not isinstance(trade_ids, (list, tuple)):
                return None
            if not order_ids or not trade_ids:
                return None
            order_refs = tuple(order_ids)
            trade_refs = tuple(trade_ids)
            if any(
                not isinstance(item, str) or not item.strip()
                for item in order_refs + trade_refs
            ):
                return None
            if len(set(order_refs)) != len(order_refs) or len(set(trade_refs)) != len(trade_refs):
                return None
            expected_orders = {leg.order_id} if leg.order_id else set()
            expected_trades = set(leg.trade_ids)
            if quantity <= 0 and not expected_orders and not expected_trades:
                continue
            if (
                not expected_orders
                or not expected_trades
                or not set(order_refs) <= expected_orders
                or not set(trade_refs) <= expected_trades
                or not set(order_refs) & expected_orders
                or not set(trade_refs) & expected_trades
            ):
                return None
            position_quantity = _decimal(positions.get("quantity"))
            if position_quantity is None or position_quantity < quantity:
                return None
        return yes_quantity, no_quantity, dict(proof)

    @staticmethod
    def _proof_claims_success(value: object) -> bool:
        if not isinstance(value, Mapping):
            return False
        proof = value.get("execution_proof")
        return isinstance(proof, Mapping) and proof.get("verified") is True

    def _finish_rejected(
        self,
        execution_id: str,
        reason: str,
        *,
        extra_evidence: Mapping[str, object] | None = None,
    ) -> None:
        self._notify_threshold_order_failed(
            execution_id,
            reason,
            preflight=extra_evidence.get("preflight") if extra_evidence else None,
        )
        self._transition(
            execution_id,
            "both_rejected",
            {
                "phase": "validation_rejected",
                "reason": reason,
                **(extra_evidence or {}),
            },
        )

    def _finish_cross_rejected(
        self,
        execution_id: str,
        reason: str,
        intent: CrossVenueIntent,
        *,
        status_text: str | None = None,
        extra: Mapping[str, object] | None = None,
    ) -> None:
        evidence = self._cross_no_submit_evidence(intent)
        if evidence is None:
            self._transition(
                execution_id,
                "directional_incident",
                {
                    "phase": "validation_rejected",
                    "reason": reason,
                    "submitted": False,
                    "account_proof": "unavailable",
                },
            )
            self._cross_breaker_open = True
            return
        evidence.update(
            {"phase": "validation_rejected", "reason": reason, "submitted": False}
        )
        if status_text is not None:
            evidence["status_text"] = status_text
        if extra is not None:
            evidence.update(self._safe_mapping(extra))
        self._transition(execution_id, "both_rejected", evidence)
        try:
            self._store.release_cross_reservation(execution_id, reason="no_submit")
        except Exception:
            self._finish_cross_incident(
                execution_id,
                "cross_reservation_release_failed",
                evidence={"release_reason": "no_submit"},
            )

    def _cross_no_submit_evidence(
        self, intent: CrossVenueIntent
    ) -> dict[str, object] | None:
        polymarket = self._fresh_account_snapshot()
        predict = self._live_predict_account_snapshot()
        if polymarket is None or predict is None:
            return None
        if (
            self._order_ids(polymarket.get("open_order_ids"))
            or self._order_ids(predict.get("open_orders"))
            or polymarket.get("positions")
            or predict.get("positions")
        ):
            return None
        return {
            "positions": {"predict.fun": "0", "polymarket": "0"},
            "account_proof": {
                "expected_tokens": {
                    leg.exchange: leg.token_id for leg in intent.legs
                },
                "predict.fun": {
                    "checked_at": _timestamp(predict.get("checked_at")),
                    "open_orders": [],
                    "positions": [],
                },
                "polymarket": {
                    "checked_at": _timestamp(polymarket.get("checked_at")),
                    "open_orders": [],
                    "positions": [],
                },
            },
        }

    def _cross_preview_matches(
        self, preview: Mapping[str, object], opportunity: Mapping[str, object], intent: CrossVenueIntent
    ) -> bool:
        stored = self._intent_from_payload(preview.get("intent"))
        if not isinstance(stored, CrossVenueIntent):
            return False
        if (
            not str(preview.get("signal_episode_id", "")).strip()
            or preview.get("signal_episode_id") != opportunity.get("signal_episode_id")
        ):
            return False
        if (stored.pair_id, stored.direction, stored.canonical_cutoff) != (intent.pair_id, intent.direction, intent.canonical_cutoff):
            return False
        if any(
            (old.exchange, old.market_id, old.condition_id, old.token_id, old.outcome) != (new.exchange, new.market_id, new.condition_id, new.token_id, new.outcome)
            or new.requested_quantity != old.requested_quantity
            or new.net_quantity != old.net_quantity
            or new.max_price > old.max_price
            or new.max_cost > old.max_cost
            for old, new in zip(stored.legs, intent.legs, strict=True)
        ):
            return False
        if (
            stored.quantity != intent.quantity
            or intent.total_max_cost > stored.total_max_cost
            or intent.minimum_payout < stored.minimum_payout
            or intent.minimum_profit < stored.minimum_profit
            or stored.annualized_yield is None
            or intent.annualized_yield is None
            or intent.annualized_yield < stored.annualized_yield
        ):
            return False
        old_approval = preview.get("codex_approval")
        new_approval = opportunity.get("codex_approval")
        return (
            isinstance(old_approval, Mapping)
            and isinstance(new_approval, Mapping)
            and self._safe_mapping(old_approval) == self._safe_mapping(new_approval)
            and preview.get("rules_fingerprints") == opportunity.get("rules_fingerprints")
            and preview.get("approved_candidates")
            == opportunity.get("approved_candidates")
        )

    def _finish_incident(
        self,
        execution_id: str,
        reason: str,
        *,
        state: str = "directional_incident",
        incident_id: str | None = None,
        notify: bool = True,
        zero_landing_check: Mapping[str, object] | None = None,
    ) -> None:
        self._breaker_open = True
        if incident_id is None:
            evidence: dict[str, object] = {"phase": "incident", "reason": reason}
            if zero_landing_check is not None:
                evidence["zero_landing_check"] = dict(zero_landing_check)
            incident_id = self._record_incident(
                execution_id,
                reason,
                state=state,
                evidence=evidence,
                notify=notify,
            )
        payload = {
            "phase": "incident_final",
            "reason": reason,
            "breaker": "open",
            "state": state,
            **({"incident_id": incident_id} if incident_id else {}),
        }
        try:
            update_incident = getattr(self._store, "update_incident", None)
            if incident_id and callable(update_incident):
                update_incident(
                    str(incident_id),
                    {"state": state, "reason": reason, "final": True},
                )
            self._transition(execution_id, state, payload)
        except Exception:
            self._breaker_open = True

    def _record_incident(
        self,
        execution_id: str,
        reason: str,
        *,
        state: str,
        evidence: Mapping[str, object],
        notify: bool = True,
    ) -> str | None:
        self._breaker_open = True
        attempts = self._notify_incident(reason) if notify else []
        if self._is_cross_auto_execution(execution_id) and self._feishu_delivery_failed(
            attempts
        ):
            self._store.pause_cross_auto("notification_delivery_failed")
        payload = {
            "reason": reason,
            "breaker": "open",
            "state": state,
            **self._safe_mapping(evidence),
            "notification_attempts": attempts,
        }
        try:
            incident_id = self._store.open_incident(execution_id, payload)
            self._transition(
                execution_id,
                state,
                {
                    "phase": "incident_open",
                    "incident_id": incident_id,
                    "reason": reason,
                    "attempts": attempts,
                },
            )
            return incident_id
        except Exception:
            # A persistence failure must not unlock the process; leave the
            # breaker open even when an incident row cannot be written.
            self._breaker_open = True
            return None

    def _notify_incident(self, reason: str) -> list[dict[str, object]]:
        title = "预测市场执行事故"
        targets = getattr(self._notifier, "_notifiers", None)
        if isinstance(targets, (list, tuple)):
            candidates = list(targets)
        else:
            candidates = [self._notifier]
        selected: list[object] = []
        feishu_selected = False
        for target in candidates:
            channel = self._notification_channel(target)
            if channel == "macos":
                if not any(self._notification_channel(item) == "macos" for item in selected):
                    selected.append(target)
            elif channel in {"feishu", "feishu_app"} and not feishu_selected:
                selected.append(target)
                feishu_selected = True
        attempts: list[dict[str, object]] = []
        for target in selected:
            channel = self._notification_channel(target)
            try:
                raw_attempts = send_notification_with_results(
                    target, title, reason, channels=None
                )
                if raw_attempts:
                    for attempt in raw_attempts:
                        attempts.append(
                            {
                                "channel": channel if channel not in {"unknown", ""} else attempt.channel,
                                "success": attempt.success,
                                "error_type": attempt.error_type,
                                "error": "delivery_failed" if attempt.error else "",
                                "suppressed": attempt.suppressed,
                            }
                        )
                else:
                    attempts.append({"channel": channel, "success": False, "error_type": "not_attempted"})
            except Exception as exc:
                attempts.append(
                    {
                        "channel": channel,
                        "success": False,
                        "error_type": type(exc).__name__,
                        "error": "delivery_failed",
                    }
                )
        selected_channels = {self._notification_channel(target) for target in selected}
        if "macos" not in selected_channels:
            attempts.append(
                {
                    "channel": "macos",
                    "success": False,
                    "error_type": "not_configured",
                }
            )
        if not feishu_selected:
            attempts.append(
                {
                    "channel": "feishu",
                    "success": False,
                    "error_type": "not_configured",
                }
            )
        return attempts

    @staticmethod
    def _notification_channel(target: object) -> str:
        explicit = getattr(target, "channel", None)
        if isinstance(explicit, str) and explicit in {"macos", "feishu", "feishu_app", "xiaoai"}:
            return explicit
        name = target.__class__.__name__.lower()
        if "xiaoai" in name:
            return "xiaoai"
        if "macos" in name or "mac" in name:
            return "macos"
        if "feishuapp" in name or "feishu_app" in name:
            return "feishu_app"
        if "feishu" in name:
            return "feishu"
        return "unknown"

    def _notification_channels_ready(self) -> bool:
        targets = getattr(self._notifier, "_notifiers", None)
        candidates = list(targets) if isinstance(targets, (list, tuple)) else [self._notifier]
        channels = [self._notification_channel(target) for target in candidates]
        return channels.count("macos") == 1 and sum(
            channel in {"feishu", "feishu_app"} for channel in channels
        ) == 1

    def _breaker_is_open(self) -> bool:
        if self._breaker_open:
            return True
        return self._store.unacknowledged_incident() is not None

    def lp_mutation_allowed(self, action: str = "submit") -> bool:
        """Expose the existing execution breaker to LP exchange writes."""

        del action
        return not self._breaker_is_open()

    def _acquire_global_lock(self) -> tuple[threading.Lock, Any] | None:
        if not self._process_lock.acquire(False):
            return None
        try:
            self._lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = self._lock_path.open("a+")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError):
                handle.close()
                self._process_lock.release()
                return None
            return self._process_lock, handle
        except Exception:
            self._process_lock.release()
            return None

    @staticmethod
    def _release_global_lock(lock: tuple[threading.Lock, Any]) -> None:
        process_lock, handle = lock
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            process_lock.release()


__all__ = ["PredictionExecutionService"]
